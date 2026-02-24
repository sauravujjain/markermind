"""
GPU Nesting Runner - Parameterized GPU nesting algorithm.

Extracted and parameterized from scripts/gpu_20260118_ga_ratio_optimizer.py
for integration into the MarkerMind backend services.
"""

import sys
import time
import random
import io
import base64
from pathlib import Path
from typing import Dict, List, Tuple, Callable, Optional
from itertools import combinations_with_replacement

import numpy as np
from PIL import Image, ImageDraw

# Add MarkerMind project root to path so nesting_engine is importable
PROJECT_ROOT = Path(__file__).parent.parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from nesting_engine.io.aama_parser import load_aama_pattern, AAMAGrader


# GPU Setup - lazy loading to allow import on systems without GPU
_gpu_available = None
cp = None
fftconvolve_gpu = None


def _init_gpu():
    """Initialize GPU libraries if available."""
    global _gpu_available, cp, fftconvolve_gpu
    if _gpu_available is not None:
        return _gpu_available

    try:
        import cupy as _cp
        from cupyx.scipy.signal import fftconvolve as _fftconvolve_gpu
        cp = _cp
        fftconvolve_gpu = _fftconvolve_gpu
        _gpu_available = True
        gpu_name = cp.cuda.runtime.getDeviceProperties(0)['name'].decode()
        print(f"CuPy GPU: ENABLED ({gpu_name})")
    except ImportError:
        _gpu_available = False
        print("CuPy not available - GPU nesting disabled")

    return _gpu_available


# Configuration defaults
DEFAULT_GPU_SCALE = 0.15  # px/mm
DEFAULT_PIECE_BUFFER = 0.1  # pixels
DEFAULT_EDGE_BUFFER = 0

# GA Parameters
GA_GENERATIONS = 3
MIN_ISLAND_SIZE = 10  # Use GA for almost all cases (lowered from 50)
MAX_ISLANDS = 5
MIN_ISLANDS = 3


class GPUPacker:
    """GPU-accelerated strip packer using FFT convolution."""

    def __init__(self, strip_width: int, max_length: int):
        if not _init_gpu():
            raise RuntimeError("GPU not available")

        self.strip_width = strip_width
        self.max_length = max_length
        self.container = cp.zeros((strip_width, max_length), dtype=cp.float32)

    def reset(self):
        self.container.fill(0)

    def find_best_position(self, raster_gpu, raster_180_gpu, current_length):
        """Find best placement using FFT convolution for collision detection.

        Optimized to minimize GPU-CPU sync points.
        """
        best = None
        best_raster = None

        for raster in [raster_gpu, raster_180_gpu]:
            ph, pw = raster.shape
            if ph > self.strip_width:
                continue

            kernel = raster[::-1, ::-1].copy()  # Copy needed for contiguous memory
            overlap = fftconvolve_gpu(self.container, kernel, mode='valid')

            if overlap.size == 0:
                continue

            valid = overlap < 0.5
            result_h, result_w = valid.shape
            max_valid_y = self.strip_width - ph

            if max_valid_y < 0:
                continue
            if max_valid_y + 1 < result_h:
                valid[max_valid_y + 1:, :] = False

            # Single sync point: check if any valid and get coordinates in one go
            valid_count = int(cp.sum(valid))
            if valid_count == 0:
                continue

            y_idx = cp.arange(result_h, dtype=cp.int32).reshape(-1, 1)
            y_grid = cp.where(valid, y_idx, result_h + 1)
            drop_y = cp.min(y_grid, axis=0)
            valid_cols = drop_y <= max_valid_y

            valid_col_count = int(cp.sum(valid_cols))
            if valid_col_count == 0:
                continue

            x_idx = cp.arange(result_w, dtype=cp.int32)
            piece_right = x_idx + pw
            piece_top = drop_y + ph

            if current_length > 0:
                inside = valid_cols & (piece_right <= current_length)
            else:
                inside = valid_cols

            inside_count = int(cp.sum(inside))
            if inside_count > 0:
                tops = cp.where(inside, piece_top, 999999)
                min_idx = int(cp.argmin(tops))
                bx = min_idx
                by = int(drop_y[bx])

                if best is None or (bx + pw <= current_length and (best['x'] + best['pw'] > current_length)):
                    best = {'x': bx, 'y': by, 'ph': ph, 'pw': pw}
                    best_raster = raster
            elif current_length > 0:
                extend = valid_cols & (piece_right > current_length)
                extend_count = int(cp.sum(extend))
                if extend_count > 0:
                    # Find first extending position
                    ext_idx = int(cp.argmax(extend))
                    ext_x = ext_idx
                    ext_y = int(drop_y[ext_x])
                    if best is None:
                        best = {'x': ext_x, 'y': ext_y, 'ph': ph, 'pw': pw}
                        best_raster = raster

        return best, best_raster

    def place(self, raster, x, y):
        """Place piece raster at position using element-wise maximum."""
        ph, pw = raster.shape
        self.container[y:y+ph, x:x+pw] = cp.maximum(self.container[y:y+ph, x:x+pw], raster)

    def get_container_png(self, current_length: int = 0) -> bytes:
        """Export current container state as PNG bytes."""
        # Get container as numpy array
        container_np = cp.asnumpy(self.container)

        # Determine visible length (actual content)
        if current_length > 0:
            visible_length = min(current_length + 50, container_np.shape[1])
        else:
            # Find rightmost non-zero column
            non_zero_cols = np.where(container_np.max(axis=0) > 0)[0]
            if len(non_zero_cols) > 0:
                visible_length = min(non_zero_cols.max() + 50, container_np.shape[1])
            else:
                visible_length = 100  # Minimum visible length

        # Crop to visible area
        visible = container_np[:, :visible_length]

        # Container is (strip_width, visible_length) = (height, width) for PIL
        # Normalize to 0-255, invert (pieces = dark, background = light)
        img_data = (1 - visible) * 255  # No transpose - already (height, width)
        img_data = img_data.astype(np.uint8)

        # Create PIL image - shape is (strip_width, visible_length) = (height, width)
        img = Image.fromarray(img_data, mode='L')

        # Send at native resolution if container height <= 300px (covers 0.15 and 0.3 px/mm).
        # Only downscale with LANCZOS for very high gpu_scale (0.5+) to keep PNGs reasonable.
        if self.strip_width > 300:
            target_height = 300
            scale = target_height / self.strip_width
            display_width = int(visible_length * scale)
            if display_width > 0:
                img = img.resize((display_width, target_height), Image.Resampling.LANCZOS)

        # Convert to PNG bytes
        buffer = io.BytesIO()
        img.save(buffer, format='PNG')
        buffer.seek(0)
        return buffer.read()

    def get_container_base64(self, current_length: int = 0) -> str:
        """Export current container state as base64-encoded PNG."""
        png_bytes = self.get_container_png(current_length)
        return base64.b64encode(png_bytes).decode('utf-8')


def load_pieces_for_material(
    dxf_path: str,
    rul_path: str,
    material: str,
    sizes: List[str],
    gpu_scale: float = DEFAULT_GPU_SCALE,
    piece_buffer: float = DEFAULT_PIECE_BUFFER,
) -> Dict[str, List[Dict]]:
    """
    Load and rasterize pieces for a specific material.

    Args:
        dxf_path: Path to DXF file
        rul_path: Path to RUL file
        material: Material code to filter (e.g., "SO1", "SHELL")
        sizes: List of sizes to load
        gpu_scale: Rasterization resolution (px/mm)
        piece_buffer: Gap between pieces in pixels

    Returns:
        Dictionary mapping size -> list of piece dicts with rasters
    """
    if not _init_gpu():
        raise RuntimeError("GPU not available")

    pieces, rules = load_aama_pattern(dxf_path, rul_path)
    grader = AAMAGrader(pieces, rules)
    unit_scale = 25.4 if rules.header.units == 'ENGLISH' else 1.0

    pieces_by_size = {}

    for target_size in sizes:
        if target_size not in rules.header.size_list:
            continue

        graded = grader.grade(target_size)
        pieces_by_size[target_size] = []

        for gp in graded:
            orig_piece = next((p for p in pieces if p.name == gp.source_piece), None)
            if orig_piece is None or orig_piece.material != material:
                continue

            vertices_mm = [(x * unit_scale, y * unit_scale) for x, y in gp.vertices]
            if len(vertices_mm) < 3:
                continue
            if vertices_mm[0] != vertices_mm[-1]:
                vertices_mm.append(vertices_mm[0])

            # Rasterize
            verts = np.array(vertices_mm)
            min_xy = verts.min(axis=0)
            verts_scaled = (verts - min_xy) * gpu_scale + piece_buffer
            max_xy = verts_scaled.max(axis=0)
            width = int(np.ceil(max_xy[0])) + int(np.ceil(piece_buffer * 2))
            height = int(np.ceil(max_xy[1])) + int(np.ceil(piece_buffer * 2))

            img = Image.new('L', (width, height), 0)
            ImageDraw.Draw(img).polygon([tuple(p) for p in verts_scaled], fill=1)
            raster = np.array(img, dtype=np.float32)
            area = float(np.sum(raster))

            demand = orig_piece.quantity.total
            if orig_piece.quantity.has_left_right:
                demand = orig_piece.quantity.left_qty + orig_piece.quantity.right_qty

            # Store vertices in mm (for SVG preview) and pixel offset
            vertices_mm_norm = [(v[0] - float(min_xy[0]), v[1] - float(min_xy[1])) for v in vertices_mm]

            pieces_by_size[target_size].append({
                'name': gp.name,
                'size': target_size,
                'raster': raster,
                'raster_gpu': cp.asarray(raster),
                'raster_180': np.rot90(raster, 2),
                'raster_180_gpu': cp.asarray(np.rot90(raster, 2)),
                'area': area,
                'demand': demand,
                'vertices_mm': vertices_mm_norm,  # normalized to origin, in mm
            })

    return pieces_by_size


def ratio_to_key(ratio: Dict[str, int], sizes: List[str]) -> tuple:
    """Convert ratio to hashable key."""
    return tuple(ratio.get(s, 0) for s in sizes)


def ratio_to_str(ratio: Dict[str, int], sizes: List[str]) -> str:
    """Convert ratio to display string."""
    return '-'.join(str(ratio.get(s, 0)) for s in sizes)


def generate_all_ratios(bundle_count: int, sizes: List[str]) -> List[Dict[str, int]]:
    """Generate all possible ratios for a bundle count."""
    all_ratios = []
    for combo in combinations_with_replacement(sizes, bundle_count):
        ratio = {s: 0 for s in sizes}
        for size in combo:
            ratio[size] += 1
        all_ratios.append(ratio)
    return all_ratios


def evaluate_ratio(
    pieces_by_size: Dict,
    ratio: Dict[str, int],
    packer: GPUPacker,
    strip_width_px: int,
    gpu_scale: float,
    capture_preview: bool = False,
) -> Tuple[float, float, Optional[str]]:
    """
    Evaluate a single ratio and return (efficiency, length_yards, preview_base64).

    Args:
        capture_preview: If True, capture and return the container as base64 PNG

    Returns:
        Tuple of (efficiency, length_yards, preview_base64 or None)
    """
    packer.reset()

    # Build piece list
    pieces_list = []
    for size, count in ratio.items():
        if count <= 0 or size not in pieces_by_size:
            continue
        for _ in range(count):
            for p in pieces_by_size[size]:
                for _ in range(p['demand']):
                    pieces_list.append(p)

    if not pieces_list:
        return 0.0, 0.0, None

    # Sort by area descending
    pieces_list.sort(key=lambda p: -p['area'])

    placed_area = 0.0
    current_length = 0

    for p in pieces_list:
        result, raster = packer.find_best_position(p['raster_gpu'], p['raster_180_gpu'], current_length)
        if result is None:
            continue

        packer.place(raster, result['x'], result['y'])
        placed_area += p['area']
        current_length = max(current_length, result['x'] + result['pw'])

    if current_length == 0:
        return 0.0, 0.0, None

    strip_area = strip_width_px * current_length
    efficiency = placed_area / strip_area

    # Convert length from pixels to yards
    length_yards = current_length / gpu_scale / 25.4 / 36

    # Capture raster preview if requested (fast — no extra GPU/CPU cost)
    preview_base64 = None
    if capture_preview:
        preview_base64 = packer.get_container_base64(current_length)

    return efficiency, length_yards, preview_base64


def evaluate_ratio_with_svg(
    pieces_by_size: Dict,
    ratio: Dict[str, int],
    packer: GPUPacker,
    strip_width_px: int,
    gpu_scale: float,
) -> Tuple[float, float, str]:
    """
    Evaluate a ratio AND produce an SVG preview from original polygon vertices.

    This is expensive — only call for final best results, not during screening.

    Returns:
        Tuple of (efficiency, length_yards, svg_string)
    """
    packer.reset()

    pieces_list = []
    for size, count in ratio.items():
        if count <= 0 or size not in pieces_by_size:
            continue
        for _ in range(count):
            for p in pieces_by_size[size]:
                for _ in range(p['demand']):
                    pieces_list.append(p)

    if not pieces_list:
        return 0.0, 0.0, ''

    pieces_list.sort(key=lambda p: -p['area'])

    placed_area = 0.0
    current_length = 0
    placements = []

    for p in pieces_list:
        result, raster = packer.find_best_position(p['raster_gpu'], p['raster_180_gpu'], current_length)
        if result is None:
            continue

        packer.place(raster, result['x'], result['y'])
        placed_area += p['area']
        current_length = max(current_length, result['x'] + result['pw'])

        is_rotated = (raster is not p['raster_gpu'])
        placements.append({
            'piece': p,
            'x_px': result['x'],
            'y_px': result['y'],
            'rotated_180': is_rotated,
        })

    if current_length == 0:
        return 0.0, 0.0, ''

    strip_area = strip_width_px * current_length
    efficiency = placed_area / strip_area
    length_yards = current_length / gpu_scale / 25.4 / 36

    fabric_width_mm = strip_width_px / gpu_scale
    strip_length_mm = current_length / gpu_scale
    svg = _generate_placement_svg(placements, fabric_width_mm, strip_length_mm, gpu_scale)

    return efficiency, length_yards, svg


def _generate_placement_svg(
    placements: List[Dict],
    fabric_width_mm: float,
    strip_length_mm: float,
    gpu_scale: float,
    max_width_px: int = 1200,
) -> str:
    """Generate SVG preview from GPU placements using original polygon vertices."""
    import math

    # Color palette — one color per size
    colors = [
        '#4CAF50', '#2196F3', '#FF9800', '#9C27B0', '#F44336',
        '#00BCD4', '#795548', '#607D8B', '#E91E63', '#3F51B5',
    ]
    size_color_map: Dict[str, str] = {}
    color_idx = 0

    scale = max_width_px / strip_length_mm
    svg_width = max_width_px
    svg_height = int(fabric_width_mm * scale)

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{svg_width}" height="{svg_height + 2}" '
        f'viewBox="0 0 {strip_length_mm:.1f} {fabric_width_mm:.1f}" '
        f'style="background:#f5f5f5;border:1px solid #ddd;border-radius:4px">',
    ]

    # Container outline
    sw = max(strip_length_mm, fabric_width_mm) * 0.002
    parts.append(
        f'<rect x="0" y="0" width="{strip_length_mm:.1f}" height="{fabric_width_mm:.1f}" '
        f'fill="none" stroke="#999" stroke-width="{sw:.2f}"/>'
    )

    for pl in placements:
        piece = pl['piece']
        size = piece.get('size', '')
        verts_mm = piece.get('vertices_mm', [])
        if not verts_mm:
            continue

        # Convert pixel position to mm
        x_mm = pl['x_px'] / gpu_scale
        y_mm = pl['y_px'] / gpu_scale

        # Apply rotation if 180°
        if pl['rotated_180']:
            # Rotate around piece center
            xs = [v[0] for v in verts_mm]
            ys = [v[1] for v in verts_mm]
            cx = (min(xs) + max(xs)) / 2
            cy = (min(ys) + max(ys)) / 2
            transformed = [(2 * cx - vx + x_mm, 2 * cy - vy + y_mm) for vx, vy in verts_mm]
        else:
            transformed = [(vx + x_mm, vy + y_mm) for vx, vy in verts_mm]

        # Flip Y for SVG (origin top-left)
        transformed = [(x, fabric_width_mm - y) for x, y in transformed]

        # Color by size
        if size not in size_color_map:
            size_color_map[size] = colors[color_idx % len(colors)]
            color_idx += 1
        color = size_color_map[size]

        points_str = ' '.join(f'{x:.1f},{y:.1f}' for x, y in transformed)
        stroke_w = max(strip_length_mm, fabric_width_mm) * 0.001
        parts.append(
            f'<polygon points="{points_str}" fill="{color}" fill-opacity="0.5" '
            f'stroke="{color}" stroke-width="{stroke_w:.2f}"/>'
        )

    parts.append('</svg>')
    return '\n'.join(parts)


class NestingCancelled(Exception):
    """Raised when a nesting job is cancelled by the user."""
    pass


def run_nesting_for_material(
    dxf_path: str,
    rul_path: str,
    material: str,
    sizes: List[str],
    fabric_width_inches: float,
    max_bundle_count: int = 6,
    top_n: int = 10,
    gpu_scale: float = DEFAULT_GPU_SCALE,
    progress_callback: Optional[Callable[[int, str], None]] = None,
    preview_callback: Optional[Callable[[str, str, float], None]] = None,
    preview_interval_seconds: float = 5.0,
    full_coverage: bool = False,
    result_callback: Optional[Callable[[int, List[Dict]], None]] = None,
    cancel_check: Optional[Callable[[], bool]] = None,
) -> Dict[int, List[Dict]]:
    """
    Run GPU nesting for a specific material.

    Args:
        dxf_path: Path to DXF pattern file
        rul_path: Path to RUL grading file
        material: Material code to filter
        sizes: List of sizes to process
        fabric_width_inches: Fabric width in inches
        max_bundle_count: Maximum bundles per marker (1-6)
        top_n: Number of top results per bundle count
        gpu_scale: Rasterization resolution (px/mm)
        progress_callback: Optional callback for progress updates
        preview_callback: Optional callback for marker previews (ratio_str, preview_base64, efficiency)
        preview_interval_seconds: How often to capture previews
        full_coverage: If True, evaluate ALL ratios (brute force). If False, use Island GA for larger search spaces.
        result_callback: Optional callback called after each bundle_count completes with (bundle_count, results_list)

    Returns:
        Dictionary mapping bundle_count -> list of result dicts
        Each result: {ratio_str, efficiency, length_yards, bundle_count}
    """
    if not _init_gpu():
        raise RuntimeError("GPU not available for nesting")

    fabric_width_mm = fabric_width_inches * 25.4
    strip_width_px = int(fabric_width_mm * gpu_scale)

    # Load and rasterize pieces
    if progress_callback:
        progress_callback(0, f"Loading pieces for material {material}...")

    pieces_by_size = load_pieces_for_material(
        dxf_path, rul_path, material, sizes, gpu_scale
    )

    if not pieces_by_size:
        raise ValueError(f"No pieces found for material {material}")

    # Estimate max container length
    max_area = max(
        sum(p['area'] * p['demand'] for p in pieces_by_size.get(s, []))
        for s in sizes if s in pieces_by_size
    )
    max_length = int((max_bundle_count * max_area * 2) / strip_width_px) + 500

    packer = GPUPacker(strip_width_px, max_length)

    all_results = {}
    last_preview_time = 0.0
    evaluated_count = 0

    # Pre-compute total ratio counts per bundle for progress display
    ratios_per_bundle = {}
    total_ratios = 0
    for bc in range(1, max_bundle_count + 1):
        n = len(generate_all_ratios(bc, sizes))
        ratios_per_bundle[bc] = n
        total_ratios += n

    if progress_callback:
        mode = "brute-force (100% coverage)" if full_coverage else "GA + brute-force"
        progress_callback(2, f"Total ratios to evaluate: {total_ratios} across {max_bundle_count} bundle counts ({mode})")

    for bundle_count in range(1, max_bundle_count + 1):
        # Check for cancellation at start of each bundle count
        if cancel_check and cancel_check():
            raise NestingCancelled("Job cancelled by user")

        all_ratios = generate_all_ratios(bundle_count, sizes)
        n_combos = len(all_ratios)

        if progress_callback:
            progress = int((bundle_count - 1) / max_bundle_count * 80)
            if full_coverage or n_combos < MIN_ISLAND_SIZE:
                progress_callback(progress, f"Brute-force: {bundle_count}-bundle ({n_combos} ratios) — {evaluated_count}/{total_ratios} total")
            else:
                progress_callback(progress, f"GA search: {bundle_count}-bundle ({n_combos} possible) — {evaluated_count}/{total_ratios} total")

        if full_coverage or n_combos < MIN_ISLAND_SIZE:
            # Brute force: evaluate all ratios (full_coverage=True or small search space)
            results = []
            cancel_check_counter = 0
            for ratio in all_ratios:
                # Check for cancellation every 20 ratios (avoid DB overhead)
                cancel_check_counter += 1
                if cancel_check and cancel_check_counter % 20 == 0 and cancel_check():
                    raise NestingCancelled("Job cancelled by user")

                # Check if we should capture a preview
                current_time = time.time()
                should_capture = preview_callback and (current_time - last_preview_time >= preview_interval_seconds)

                eff, length, preview = evaluate_ratio(
                    pieces_by_size, ratio, packer, strip_width_px, gpu_scale,
                    capture_preview=should_capture
                )

                if should_capture and preview:
                    ratio_str = ratio_to_str(ratio, sizes)
                    preview_callback(ratio_str, preview, eff)
                    last_preview_time = current_time

                results.append({
                    'ratio': ratio,
                    'ratio_str': ratio_to_str(ratio, sizes),
                    'efficiency': eff,
                    'length_yards': length,
                    'bundle_count': bundle_count,
                })
                evaluated_count += 1

                # Update progress within bundle (every 20 ratios)
                if progress_callback and cancel_check_counter % 20 == 0:
                    progress = int((bundle_count - 1) / max_bundle_count * 80) + int(len(results) / n_combos * (80 / max_bundle_count))
                    progress_callback(min(progress, 95), f"Brute-force: {bundle_count}-bundle ({len(results)}/{n_combos}) — {evaluated_count}/{total_ratios} total")

            results.sort(key=lambda x: -x['efficiency'])
            # Retention: ALL for 1-2 bundles, top 25% (floor 25) for 3+ bundles
            if bundle_count <= 2:
                all_results[bundle_count] = results  # Keep all
            else:
                keep_count = max(25, int(len(results) * 0.25))  # Top 25%, min 25
                all_results[bundle_count] = results[:keep_count]
            # Notify of incremental results
            if result_callback:
                result_callback(bundle_count, all_results[bundle_count])
        else:
            # Island GA for larger search spaces
            results = _run_island_ga(
                pieces_by_size, all_ratios, bundle_count,
                packer, strip_width_px, gpu_scale, sizes, top_n,
                preview_callback=preview_callback,
                preview_interval_seconds=preview_interval_seconds,
                cancel_check=cancel_check,
            )
            # GA evaluates a subset — count the results it returned
            evaluated_count += len(results)
            # Retention: ALL for 1-2 bundles, top 25% (floor 25) for 3+ bundles
            if bundle_count <= 2:
                all_results[bundle_count] = results  # Keep all
            else:
                keep_count = max(25, int(len(results) * 0.25))  # Top 25%, min 25
                all_results[bundle_count] = results[:keep_count]
            # Notify of incremental results
            if result_callback:
                result_callback(bundle_count, all_results[bundle_count])

    # Generate SVG preview for the single best result per bundle count
    svg_total = sum(1 for v in all_results.values() if v)
    if progress_callback:
        progress_callback(95, f"Generating vector previews for {svg_total} top markers...")

    for bc, bc_results in all_results.items():
        if bc_results:
            r = bc_results[0]
            ratio = r.get('ratio')
            if ratio:
                _, _, svg = evaluate_ratio_with_svg(
                    pieces_by_size, ratio, packer, strip_width_px, gpu_scale
                )
                r['svg_preview'] = svg

    total_saved = sum(len(v) for v in all_results.values())
    if progress_callback:
        progress_callback(100, f"Complete — {evaluated_count} ratios evaluated, {total_saved} markers saved")

    return all_results


def _run_island_ga(
    pieces_by_size: Dict,
    all_ratios: List[Dict],
    bundle_count: int,
    packer: GPUPacker,
    strip_width_px: int,
    gpu_scale: float,
    sizes: List[str],
    top_n: int,
    preview_callback: Optional[Callable[[str, str, float], None]] = None,
    preview_interval_seconds: float = 5.0,
    cancel_check: Optional[Callable[[], bool]] = None,
) -> List[Dict]:
    """Run island-based GA for larger search spaces."""
    total_combos = len(all_ratios)

    # Determine number of islands
    if total_combos <= 150:
        num_islands = MIN_ISLANDS
    elif total_combos <= 250:
        num_islands = min(MAX_ISLANDS, max(MIN_ISLANDS, total_combos // MIN_ISLAND_SIZE))
    else:
        num_islands = MAX_ISLANDS

    # Linear partition into islands
    island_size = total_combos // num_islands
    islands = []
    for i in range(num_islands):
        start = i * island_size
        end = start + island_size if i < num_islands - 1 else total_combos
        islands.append(all_ratios[start:end])

    global_cache = {}
    island_bests = []
    last_preview_time = [0.0]  # Use list for mutability in closure

    for island_idx, island_pool in enumerate(islands):
        if cancel_check and cancel_check():
            raise NestingCancelled("Job cancelled by user")
        best, _ = _run_island_ga_single(
            pieces_by_size, island_pool, bundle_count,
            packer, strip_width_px, gpu_scale, sizes, global_cache,
            preview_callback=preview_callback,
            preview_interval_seconds=preview_interval_seconds,
            last_preview_time=last_preview_time,
        )
        best['bundle_count'] = bundle_count
        best['ratio_str'] = ratio_to_str(best['ratio'], sizes)
        island_bests.append(best)

    # Sort by efficiency
    island_bests.sort(key=lambda x: -x['efficiency'])
    final_results = list(island_bests)

    # Fill remaining slots from global cache
    included_keys = set(ratio_to_key(r['ratio'], sizes) for r in final_results)
    remaining_slots = top_n - len(final_results)

    if remaining_slots > 0:
        all_evaluated = list(global_cache.values())
        all_evaluated.sort(key=lambda x: -x['efficiency'])

        for r in all_evaluated:
            if remaining_slots <= 0:
                break
            key = ratio_to_key(r['ratio'], sizes)
            if key not in included_keys:
                result = {
                    'ratio': r['ratio'],
                    'ratio_str': ratio_to_str(r['ratio'], sizes),
                    'efficiency': r['efficiency'],
                    'length_yards': r.get('length_yards', 0.0),
                    'bundle_count': bundle_count,
                }
                final_results.append(result)
                included_keys.add(key)
                remaining_slots -= 1

    return final_results[:top_n]


def _run_island_ga_single(
    pieces_by_size: Dict,
    island_pool: List[Dict],
    bundle_count: int,
    packer: GPUPacker,
    strip_width_px: int,
    gpu_scale: float,
    sizes: List[str],
    global_cache: Dict,
    preview_callback: Optional[Callable[[str, str, float], None]] = None,
    preview_interval_seconds: float = 5.0,
    last_preview_time: Optional[List[float]] = None,
) -> Tuple[Dict, int]:
    """Run GA on a single island."""
    island_size = len(island_pool)
    pop_size = max(5, min(10, island_size // 10))
    elite_count = max(1, pop_size // 5)
    mutation_rate = 0.4
    eval_count = 0

    if last_preview_time is None:
        last_preview_time = [0.0]

    def evaluate_and_cache(ratio: Dict) -> Dict:
        nonlocal eval_count
        key = ratio_to_key(ratio, sizes)
        if key not in global_cache:
            # Check if we should capture a preview
            current_time = time.time()
            should_capture = preview_callback and (current_time - last_preview_time[0] >= preview_interval_seconds)

            eff, length, preview = evaluate_ratio(
                pieces_by_size, ratio, packer, strip_width_px, gpu_scale,
                capture_preview=should_capture
            )
            eval_count += 1
            global_cache[key] = {'ratio': dict(ratio), 'efficiency': eff, 'length_yards': length}

            if should_capture and preview:
                ratio_str = ratio_to_str(ratio, sizes)
                preview_callback(ratio_str, preview, eff)
                last_preview_time[0] = current_time
        return global_cache[key]

    # Initialize population
    sample_size = min(pop_size, island_size)
    population = []
    sampled = random.sample(island_pool, sample_size)
    for ratio in sampled:
        result = evaluate_and_cache(ratio)
        population.append(result)

    # Evolution
    for gen in range(GA_GENERATIONS):
        population.sort(key=lambda x: -x['efficiency'])
        new_pop = population[:elite_count]

        while len(new_pop) < pop_size:
            sample_k = min(3, len(population))
            p1 = max(random.sample(population, sample_k), key=lambda x: x['efficiency'])
            p2 = max(random.sample(population, sample_k), key=lambda x: x['efficiency'])

            # Crossover
            child = {}
            for size in sizes:
                avg = (p1['ratio'].get(size, 0) + p2['ratio'].get(size, 0)) / 2
                child[size] = int(avg + random.uniform(-0.5, 0.5))
                child[size] = max(0, child[size])

            # Normalize to bundle_count
            total = sum(child.values())
            while total != bundle_count:
                if total < bundle_count:
                    size = random.choice(sizes)
                    child[size] += 1
                    total += 1
                elif total > bundle_count:
                    sizes_with_count = [s for s in sizes if child[s] > 0]
                    if sizes_with_count:
                        size = random.choice(sizes_with_count)
                        child[size] -= 1
                        total -= 1
                    else:
                        break

            # Check if child is in island pool
            child_key = ratio_to_key(child, sizes)
            found_in_pool = any(ratio_to_key(r, sizes) == child_key for r in island_pool)

            if not found_in_pool:
                child = random.choice(island_pool)

            # Mutation
            if random.random() < mutation_rate:
                child = random.choice(island_pool)

            result = evaluate_and_cache(child)

            # Avoid duplicates
            child_key = ratio_to_key(child, sizes)
            if not any(ratio_to_key(p['ratio'], sizes) == child_key for p in new_pop):
                new_pop.append(result)
            else:
                random_ratio = random.choice(island_pool)
                random_result = evaluate_and_cache(random_ratio)
                random_key = ratio_to_key(random_ratio, sizes)
                if not any(ratio_to_key(p['ratio'], sizes) == random_key for p in new_pop):
                    new_pop.append(random_result)

        population = new_pop

    population.sort(key=lambda x: -x['efficiency'])
    return population[0], eval_count
