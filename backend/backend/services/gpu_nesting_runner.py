"""
GPU Nesting Runner - Parameterized GPU nesting algorithm.

Extracted and parameterized from scripts/gpu_20260118_ga_ratio_optimizer.py
for integration into the MarkerMind backend services.
"""

import math
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
DEFAULT_PIECE_BUFFER = 0  # pixels
DEFAULT_EDGE_BUFFER = 0

# Ratio evaluation parameters
BRUTE_FORCE_THRESHOLD = 1000  # Brute-force all ratios below this count
RANDOM_SAMPLE_SIZE = 300      # Sample size for large search spaces (> threshold)


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


def _detect_grain_axis(pieces) -> str:
    """
    Detect the predominant grain line axis across all pieces in a pattern.

    Examines grain lines (Layer 7) from all pieces and determines whether
    grain runs primarily along the DXF X-axis or Y-axis.

    Returns:
        'x' if grain is predominantly along DXF X-axis
        'y' if grain is predominantly along DXF Y-axis
        'y' as fallback if no grain lines found (preserves legacy behavior)
    """
    x_votes = 0
    y_votes = 0

    for piece in pieces:
        if not piece.grain_line:
            continue
        gx = abs(piece.grain_line[1][0] - piece.grain_line[0][0])
        gy = abs(piece.grain_line[1][1] - piece.grain_line[0][1])
        if gx > gy:
            x_votes += 1
        elif gy > gx:
            y_votes += 1
        # Diagonal (gx == gy) doesn't vote

    if x_votes == 0 and y_votes == 0:
        return 'y'  # No grain lines found, default to legacy swap behavior

    return 'x' if x_votes >= y_votes else 'y'


def _orient_vertices_for_grain(
    vertices: List[Tuple[float, float]],
    grain_line: Optional[Tuple[Tuple[float, float], Tuple[float, float]]],
    pattern_grain_axis: str,
    unit_scale: float,
) -> List[Tuple[float, float]]:
    """
    Transform piece vertices so the grain direction maps to the raster's
    column axis (PIL x / fabric length direction).

    The GPU nesting container is laid out as (strip_width, max_length) where
    axis 1 (columns) runs along the fabric length. PIL polygon vertices use
    (x, y) where x maps to columns. So the grain must end up as the first
    coordinate of each vertex tuple.

    Strategy:
        - If grain runs along DXF Y: swap to (y, x) so grain → first coord
        - If grain runs along DXF X: keep as (x, y) so grain → first coord
        - Per-piece grain line overrides the pattern-level default
    """
    # Determine this piece's grain axis
    piece_grain_axis = pattern_grain_axis  # default to pattern-level

    if grain_line:
        gx = abs(grain_line[1][0] - grain_line[0][0])
        gy = abs(grain_line[1][1] - grain_line[0][1])
        if gx > gy:
            piece_grain_axis = 'x'
        elif gy > gx:
            piece_grain_axis = 'y'
        # Diagonal: use pattern default

    if piece_grain_axis == 'y':
        # Grain along DXF Y → swap so Y becomes first coord (PIL x = fabric length)
        return [(y * unit_scale, x * unit_scale) for x, y in vertices]
    else:
        # Grain along DXF X → keep as-is, X is already first coord
        return [(x * unit_scale, y * unit_scale) for x, y in vertices]


def _rasterize_vertices(
    vertices_mm: List[Tuple[float, float]],
    gpu_scale: float,
    piece_buffer: float,
) -> Tuple[np.ndarray, float, List[Tuple[float, float]]]:
    """
    Rasterize a piece polygon and return (raster, area, vertices_mm_norm).

    Shared helper for both AAMA and DXF-only piece loading paths.
    """
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

    vertices_mm_norm = [(v[0] - float(min_xy[0]), v[1] - float(min_xy[1])) for v in vertices_mm]
    return raster, area, vertices_mm_norm


def _compute_perimeter_mm(vertices_mm: List[Tuple[float, float]]) -> float:
    """Sum of edge lengths for a closed polygon. Returns mm."""
    if len(vertices_mm) < 2:
        return 0.0
    perim = 0.0
    for i in range(len(vertices_mm) - 1):
        x1, y1 = vertices_mm[i]
        x2, y2 = vertices_mm[i + 1]
        perim += math.sqrt((x2 - x1) ** 2 + (y2 - y1) ** 2)
    # Close if not already closed
    if vertices_mm[0] != vertices_mm[-1]:
        x1, y1 = vertices_mm[-1]
        x2, y2 = vertices_mm[0]
        perim += math.sqrt((x2 - x1) ** 2 + (y2 - y1) ** 2)
    return perim


def load_pieces_for_material(
    dxf_path: str,
    rul_path: Optional[str],
    material: str,
    sizes: List[str],
    gpu_scale: float = DEFAULT_GPU_SCALE,
    piece_buffer: float = DEFAULT_PIECE_BUFFER,
    file_type: Optional[str] = None,
) -> Dict[str, List[Dict]]:
    """
    Load and rasterize pieces for a specific material.

    Automatically detects grain direction from the pattern's grain lines
    and orients pieces so grain runs parallel to the fabric length.

    Args:
        dxf_path: Path to DXF file
        rul_path: Path to RUL file (None for DXF-only patterns)
        material: Material code to filter (e.g., "SO1", "SHELL")
        sizes: List of sizes to load
        gpu_scale: Rasterization resolution (px/mm)
        piece_buffer: Gap between pieces in pixels
        file_type: Pattern file type ("aama", "dxf_only", "vt_dxf")

    Returns:
        Dictionary mapping size -> list of piece dicts with rasters
    """
    if not _init_gpu():
        raise RuntimeError("GPU not available")

    # VT DXF path: Optitex Graded Nest format
    if file_type == "vt_dxf":
        return _load_pieces_vt_dxf(dxf_path, sizes, gpu_scale, piece_buffer)

    # DXF-only path: no RUL grading, pieces already sized in DXF
    if rul_path is None or not Path(rul_path).exists():
        return _load_pieces_dxf_only(dxf_path, sizes, gpu_scale, piece_buffer)

    pieces, rules = load_aama_pattern(dxf_path, rul_path)
    grader = AAMAGrader(pieces, rules)
    unit_scale = 25.4 if rules.header.units == 'ENGLISH' else 1.0

    # Detect predominant grain axis for this pattern
    pattern_grain_axis = _detect_grain_axis(pieces)

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

            # Orient vertices so grain direction maps to fabric length axis
            vertices_mm = _orient_vertices_for_grain(
                gp.vertices, gp.grain_line, pattern_grain_axis, unit_scale
            )
            if len(vertices_mm) < 3:
                continue
            if vertices_mm[0] != vertices_mm[-1]:
                vertices_mm.append(vertices_mm[0])

            raster, area, vertices_mm_norm = _rasterize_vertices(vertices_mm, gpu_scale, piece_buffer)

            demand = orig_piece.quantity.total
            if orig_piece.quantity.has_left_right:
                demand = orig_piece.quantity.left_qty + orig_piece.quantity.right_qty

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


def _load_pieces_dxf_only(
    dxf_path: str,
    sizes: List[str],
    gpu_scale: float,
    piece_buffer: float,
) -> Dict[str, List[Dict]]:
    """Load and rasterize pieces from a DXF-only pattern (no RUL)."""
    from nesting_engine.io.dxf_parser import load_dxf_pieces_by_size

    nesting_pieces, piece_config, _ = load_dxf_pieces_by_size(
        dxf_path, sizes, size_names=sizes,  # Map SIZE_1..N labels to actual size names
    )

    pieces_by_size: Dict[str, List[Dict]] = {}

    for piece in nesting_pieces:
        size = piece.identifier.size
        if not size:
            continue

        vertices_mm = list(piece.vertices)
        if len(vertices_mm) < 3:
            continue
        if vertices_mm[0] != vertices_mm[-1]:
            vertices_mm.append(vertices_mm[0])

        raster, area, vertices_mm_norm = _rasterize_vertices(vertices_mm, gpu_scale, piece_buffer)

        if size not in pieces_by_size:
            pieces_by_size[size] = []

        pieces_by_size[size].append({
            'name': piece.identifier.piece_name,
            'size': size,
            'raster': raster,
            'raster_gpu': cp.asarray(raster),
            'raster_180': np.rot90(raster, 2),
            'raster_180_gpu': cp.asarray(np.rot90(raster, 2)),
            'area': area,
            'demand': 1,  # Each DXF polyline is a unique piece instance
            'vertices_mm': vertices_mm_norm,
        })

    return pieces_by_size


def _load_pieces_vt_dxf(
    dxf_path: str,
    sizes: List[str],
    gpu_scale: float,
    piece_buffer: float,
) -> Dict[str, List[Dict]]:
    """Load and rasterize pieces from a VT DXF (Optitex Graded Nest) pattern."""
    from nesting_engine.io.vt_dxf_parser import parse_vt_dxf

    pieces, all_sizes, piece_quantities, _material = parse_vt_dxf(dxf_path)

    pieces_by_size: Dict[str, List[Dict]] = {}

    for piece in pieces:
        size = piece.identifier.size
        if not size or size not in sizes:
            continue

        vertices_mm = list(piece.vertices)
        if len(vertices_mm) < 3:
            continue
        if vertices_mm[0] != vertices_mm[-1]:
            vertices_mm.append(vertices_mm[0])

        raster, area, vertices_mm_norm = _rasterize_vertices(vertices_mm, gpu_scale, piece_buffer)

        # Demand from piece_quantities (qty=2 means L/R pair)
        piece_name = piece.identifier.piece_name
        demand = piece_quantities.get(piece_name, 1)

        if size not in pieces_by_size:
            pieces_by_size[size] = []

        pieces_by_size[size].append({
            'name': piece_name,
            'size': size,
            'raster': raster,
            'raster_gpu': cp.asarray(raster),
            'raster_180': np.rot90(raster, 2),
            'raster_180_gpu': cp.asarray(np.rot90(raster, 2)),
            'area': area,
            'demand': demand,
            'vertices_mm': vertices_mm_norm,
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


def _evaluate_single_sort(
    pieces_list: List[Dict],
    packer: GPUPacker,
    strip_width_px: int,
    gpu_scale: float,
    sort_key,
    capture_preview: bool = False,
) -> Tuple[float, float, Optional[str], float]:
    """Evaluate a pre-built pieces_list with a specific sort key. Returns (eff, length_yd, preview, perim_cm)."""
    packer.reset()
    pieces_sorted = sorted(pieces_list, key=sort_key)

    placed_area = 0.0
    current_length = 0
    total_perimeter_mm = 0.0

    for p in pieces_sorted:
        result, raster = packer.find_best_position(p['raster_gpu'], p['raster_180_gpu'], current_length)
        if result is None:
            continue
        packer.place(raster, result['x'], result['y'])
        placed_area += p['area']
        current_length = max(current_length, result['x'] + result['pw'])
        total_perimeter_mm += _compute_perimeter_mm(p['vertices_mm'])

    if current_length == 0:
        return 0.0, 0.0, None, 0.0

    strip_area = strip_width_px * current_length
    efficiency = placed_area / strip_area
    length_yards = current_length / gpu_scale / 25.4 / 36

    preview_base64 = None
    if capture_preview:
        preview_base64 = packer.get_container_base64(current_length)

    perimeter_cm = total_perimeter_mm / 10.0
    return efficiency, length_yards, preview_base64, perimeter_cm


def evaluate_ratio(
    pieces_by_size: Dict,
    ratio: Dict[str, int],
    packer: GPUPacker,
    strip_width_px: int,
    gpu_scale: float,
    capture_preview: bool = False,
    dual_sort: bool = False,
) -> Tuple[float, float, Optional[str], float]:
    """
    Evaluate a single ratio using width_desc sorting (primary strategy).

    When dual_sort=True, tries both width_desc and area_desc, returns the better result.
    Use dual_sort=True for retained results refinement, False for fast screening.

    Returns:
        Tuple of (efficiency, length_yards, preview_base64 or None, perimeter_cm)
    """
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
        return 0.0, 0.0, None, 0.0

    # Primary: width_desc (wins 67% of the time)
    # "width" = piece extent along fabric width (raster height = rows)
    eff_w, len_w, prev_w, perim_w = _evaluate_single_sort(
        pieces_list, packer, strip_width_px, gpu_scale,
        sort_key=lambda p: -p['raster'].shape[0],
        capture_preview=capture_preview,
    )

    if not dual_sort:
        return eff_w, len_w, prev_w, perim_w

    # Secondary: area_desc (wins 28% of the time)
    eff_a, len_a, prev_a, perim_a = _evaluate_single_sort(
        pieces_list, packer, strip_width_px, gpu_scale,
        sort_key=lambda p: -p['area'],
        capture_preview=capture_preview,
    )

    # Return the better result
    if eff_a > eff_w:
        return eff_a, len_a, prev_a, perim_a
    return eff_w, len_w, prev_w, perim_w


def _evaluate_with_svg_single_sort(
    pieces_list: List[Dict],
    packer: GPUPacker,
    strip_width_px: int,
    gpu_scale: float,
    sort_key,
) -> Tuple[float, float, str, float]:
    """Evaluate with SVG using a specific sort key."""
    packer.reset()
    pieces_sorted = sorted(pieces_list, key=sort_key)

    placed_area = 0.0
    current_length = 0
    total_perimeter_mm = 0.0
    placements = []

    for p in pieces_sorted:
        result, raster = packer.find_best_position(p['raster_gpu'], p['raster_180_gpu'], current_length)
        if result is None:
            continue
        packer.place(raster, result['x'], result['y'])
        placed_area += p['area']
        current_length = max(current_length, result['x'] + result['pw'])
        total_perimeter_mm += _compute_perimeter_mm(p['vertices_mm'])
        is_rotated = (raster is not p['raster_gpu'])
        placements.append({
            'piece': p, 'x_px': result['x'], 'y_px': result['y'],
            'rotated_180': is_rotated,
        })

    if current_length == 0:
        return 0.0, 0.0, '', 0.0

    strip_area = strip_width_px * current_length
    efficiency = placed_area / strip_area
    length_yards = current_length / gpu_scale / 25.4 / 36
    fabric_width_mm = strip_width_px / gpu_scale
    strip_length_mm = current_length / gpu_scale
    svg = _generate_placement_svg(placements, fabric_width_mm, strip_length_mm, gpu_scale)
    perimeter_cm = total_perimeter_mm / 10.0
    return efficiency, length_yards, svg, perimeter_cm


def evaluate_ratio_with_svg(
    pieces_by_size: Dict,
    ratio: Dict[str, int],
    packer: GPUPacker,
    strip_width_px: int,
    gpu_scale: float,
) -> Tuple[float, float, str, float]:
    """
    Evaluate a ratio AND produce an SVG preview from original polygon vertices.

    Tries both width_desc and area_desc sorting, returns the better result.
    This is expensive — only call for final best results, not during screening.

    Returns:
        Tuple of (efficiency, length_yards, svg_string, perimeter_cm)
    """
    pieces_list = []
    for size, count in ratio.items():
        if count <= 0 or size not in pieces_by_size:
            continue
        for _ in range(count):
            for p in pieces_by_size[size]:
                for _ in range(p['demand']):
                    pieces_list.append(p)

    if not pieces_list:
        return 0.0, 0.0, '', 0.0

    # Try both sorting strategies
    eff_w, len_w, svg_w, perim_w = _evaluate_with_svg_single_sort(
        pieces_list, packer, strip_width_px, gpu_scale,
        sort_key=lambda p: -p['raster'].shape[0],
    )
    eff_a, len_a, svg_a, perim_a = _evaluate_with_svg_single_sort(
        pieces_list, packer, strip_width_px, gpu_scale,
        sort_key=lambda p: -p['area'],
    )

    if eff_a > eff_w:
        return eff_a, len_a, svg_a, perim_a
    return eff_w, len_w, svg_w, perim_w


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
    file_type: Optional[str] = None,
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
        full_coverage: If True, evaluate ALL ratios (brute force). If False, brute-force up to BRUTE_FORCE_THRESHOLD, random sample above.
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
        dxf_path, rul_path, material, sizes, gpu_scale, file_type=file_type
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
        mode = "full coverage" if full_coverage else f"brute-force (≤{BRUTE_FORCE_THRESHOLD}) + sampling ({RANDOM_SAMPLE_SIZE})"
        progress_callback(2, f"Ratio space: {total_ratios} across {max_bundle_count} bundle counts ({mode})")

    for bundle_count in range(1, max_bundle_count + 1):
        # Check for cancellation at start of each bundle count
        if cancel_check and cancel_check():
            raise NestingCancelled("Job cancelled by user")

        all_ratios = generate_all_ratios(bundle_count, sizes)
        n_combos = len(all_ratios)

        # Decide: brute force (evaluate all) vs random sampling
        use_brute_force = full_coverage or n_combos <= BRUTE_FORCE_THRESHOLD

        if progress_callback:
            progress = int((bundle_count - 1) / max_bundle_count * 80)
            if use_brute_force:
                progress_callback(progress, f"Evaluating: {bundle_count}-bundle ({n_combos} ratios) — {evaluated_count} total")
            else:
                progress_callback(progress, f"Sampling: {bundle_count}-bundle ({RANDOM_SAMPLE_SIZE}/{n_combos} ratios) — {evaluated_count} total")

        if use_brute_force:
            # Brute force: evaluate all ratios
            ratios_to_eval = all_ratios
        else:
            # Random sampling: pick RANDOM_SAMPLE_SIZE ratios uniformly
            ratios_to_eval = random.sample(all_ratios, min(RANDOM_SAMPLE_SIZE, n_combos))

        results = []
        cancel_check_counter = 0
        for ratio in ratios_to_eval:
            cancel_check_counter += 1
            if cancel_check and cancel_check_counter % 20 == 0 and cancel_check():
                raise NestingCancelled("Job cancelled by user")

            current_time = time.time()
            should_capture = preview_callback and (current_time - last_preview_time >= preview_interval_seconds)

            eff, length, preview, perim_cm = evaluate_ratio(
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
                'perimeter_cm': perim_cm,
            })
            evaluated_count += 1

            if progress_callback and cancel_check_counter % 20 == 0:
                n_eval = len(ratios_to_eval)
                progress = int((bundle_count - 1) / max_bundle_count * 80) + int(len(results) / n_eval * (80 / max_bundle_count))
                label = "Evaluating" if use_brute_force else "Sampling"
                progress_callback(min(progress, 95), f"{label}: {bundle_count}-bundle ({len(results)}/{n_eval}) — {evaluated_count} total")

        results.sort(key=lambda x: -x['efficiency'])
        # Retention: ALL for 1-2 bundles, top 25% (floor 25) for 3+ bundles
        if bundle_count <= 2:
            all_results[bundle_count] = results
        else:
            keep_count = max(25, int(len(results) * 0.25))
            all_results[bundle_count] = results[:keep_count]
        # Notify of incremental results
        if result_callback:
            result_callback(bundle_count, all_results[bundle_count])

    # Dual-sort refinement: re-evaluate retained results with area_desc too, keep best
    total_retained = sum(len(v) for v in all_results.values())
    if progress_callback:
        progress_callback(90, f"Refining {total_retained} retained markers with dual-sort...")

    for bc, bc_results in all_results.items():
        for r in bc_results:
            ratio = r.get('ratio')
            if not ratio:
                continue
            eff_dual, len_dual, _, perim_dual = evaluate_ratio(
                pieces_by_size, ratio, packer, strip_width_px, gpu_scale,
                dual_sort=True,
            )
            if eff_dual > r['efficiency']:
                r['efficiency'] = eff_dual
                r['length_yards'] = len_dual
                r['perimeter_cm'] = perim_dual
        # Re-sort after refinement
        bc_results.sort(key=lambda x: -x['efficiency'])

    # Generate SVG preview for the single best result per bundle count
    svg_total = sum(1 for v in all_results.values() if v)
    if progress_callback:
        progress_callback(95, f"Generating vector previews for {svg_total} top markers...")

    for bc, bc_results in all_results.items():
        if bc_results:
            r = bc_results[0]
            ratio = r.get('ratio')
            if ratio:
                _, _, svg, perim_cm = evaluate_ratio_with_svg(
                    pieces_by_size, ratio, packer, strip_width_px, gpu_scale
                )
                r['svg_preview'] = svg
                r['perimeter_cm'] = perim_cm

    total_saved = sum(len(v) for v in all_results.values())
    if progress_callback:
        progress_callback(100, f"Complete — {evaluated_count} ratios evaluated, {total_saved} markers saved")

    return all_results


