#!/usr/bin/env python3
"""
GPU vs CPU Nesting Comparison - V3 (All Fixes Applied)

Critical fixes from v2:
1. Correct orientation (X = strip length to MINIMIZE, Y = fabric width FIXED)
2. FFT convolution for actual GPU acceleration (O(N log N) vs O(N*M))
3. PNG output for both GPU and CPU results for visual comparison

Usage:
    PYTHONPATH=. python scripts/gpu_cpu_comparison_v3.py
"""

import json
import time
import multiprocessing as mp
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from typing import Dict, List, Tuple, Optional, Any
from collections import defaultdict

import numpy as np
from PIL import Image, ImageDraw
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches

# GPU support
try:
    import cupy as cp
    from cupyx.scipy.signal import fftconvolve as gpu_fftconvolve
    GPU_AVAILABLE = True
    gpu_name = cp.cuda.runtime.getDeviceProperties(0)['name'].decode()
    print(f"GPU acceleration: ENABLED ({gpu_name})")
except ImportError:
    cp = np
    GPU_AVAILABLE = False
    print("GPU acceleration: DISABLED (using NumPy CPU fallback)")

# CPU fallback using numpy FFT
def cpu_fftconvolve(a, b, mode='full'):
    """Simple FFT convolution using numpy (scipy fallback)."""
    # Pad arrays for FFT
    s1 = np.array(a.shape)
    s2 = np.array(b.shape)
    shape = s1 + s2 - 1

    # FFT of both arrays
    fft_a = np.fft.fft2(a, shape)
    fft_b = np.fft.fft2(b, shape)

    # Multiply and inverse FFT
    result = np.fft.ifft2(fft_a * fft_b).real

    if mode == 'valid':
        # Extract valid region
        start = s2 - 1
        end = s1
        return result[start[0]:start[0]+end[0]-s2[0]+1,
                      start[1]:start[1]+end[1]-s2[1]+1]
    return result

# ============================================================================
# Configuration
# ============================================================================

DXF_PATH = Path("data/24_2506_7_S-AN1000.DXF")
OUTPUT_DIR = Path("experiment_results/gpu_vs_cpu_v3")

SIZES = ["XS", "S", "M", "L", "XL", "XXL"]
PIECE_CONFIG = {
    "BK": {"demand": 1, "flip": False},
    "FRT": {"demand": 1, "flip": False},
    "SL": {"demand": 2, "flip": True},
}

# Strip packing convention:
# - WIDTH (Y-axis) = fabric width = FIXED = 60 inches = 1524mm
# - LENGTH (X-axis) = strip length = VARIABLE = what we MINIMIZE
CONTAINER_WIDTH_MM = 1524.0  # 60 inches - FIXED fabric width (Y-axis)
GLOBAL_SCALE = 0.2  # pixels per mm (5mm resolution)

# Derived dimensions in pixels
CONTAINER_WIDTH_PX = int(CONTAINER_WIDTH_MM * GLOBAL_SCALE)  # ~305px (Y-axis)
CONTAINER_LENGTH_PX = 1200  # ~6000mm max strip length (X-axis)

# Nesting parameters
CPU_TIME_LIMIT = 30  # seconds
CPU_WORKERS = 6

# Test combinations (same 10 as before for consistency)
TEST_COMBINATIONS = [
    {"M": 1},                           # 1 garment, 4 pieces
    {"S": 2},                           # 2 garments, 8 pieces
    {"XS": 1, "XXL": 1},                # 2 garments, mixed sizes
    {"M": 2, "L": 1},                   # 3 garments
    {"XS": 2, "S": 1, "M": 1},          # 4 garments
    {"S": 1, "M": 1, "L": 1, "XL": 1},  # 4 garments, size gradient
    {"M": 1, "S": 2, "XS": 2, "XXL": 1},# 6 garments
    {"L": 2, "XL": 2, "XXL": 1},        # 5 garments, larger sizes
    {"XS": 3, "S": 2, "M": 1},          # 6 garments, small-heavy
    {"S": 2, "M": 2, "L": 1, "XL": 1},  # 6 garments, balanced
]


# ============================================================================
# Utility Functions
# ============================================================================

def clean_vertices(vertices: List[Tuple[float, float]]) -> List[Tuple[float, float]]:
    """Remove duplicate vertices that cause Spyrrow to crash."""
    if len(vertices) < 3:
        return vertices
    cleaned = []
    seen = set()
    for x, y in vertices:
        key = (round(x, 3), round(y, 3))
        if key not in seen:
            seen.add(key)
            cleaned.append((x, y))
    return cleaned


def extract_piece_type(piece_name: str) -> Optional[str]:
    """Extract piece type (BK, FRT, SL) from piece name."""
    name_upper = piece_name.upper()
    for pt in PIECE_CONFIG:
        if pt in name_upper:
            return pt
    return None


def load_piece_vertices(dxf_path: Path) -> Dict[str, Dict[str, List[Tuple[float, float]]]]:
    """Load and clean vertices from DXF."""
    from nesting_engine.io.dxf_parser import DXFParser
    from nesting_engine.core.units import LengthUnit

    parser = DXFParser(str(dxf_path))
    result = parser.parse()

    to_mm = 25.4 if result.unit == LengthUnit.INCH else 1.0

    pieces = defaultdict(dict)
    for parsed in result.pieces:
        size = parsed.size
        if size not in SIZES:
            continue
        piece_type = extract_piece_type(parsed.piece_name or "")
        if not piece_type or piece_type in pieces[size]:
            continue

        vertices = [(x * to_mm, y * to_mm) for x, y in parsed.vertices]
        vertices = clean_vertices(vertices)
        if len(vertices) >= 3:
            pieces[size][piece_type] = vertices

    return dict(pieces)


def combo_str(combo: Dict[str, int]) -> str:
    """Convert combo dict to readable string."""
    return ", ".join(f"{s}:{n}" for s, n in sorted(combo.items()) if n > 0)


def expected_pieces(combo: Dict[str, int]) -> int:
    """Calculate expected piece count (4 per garment: 1 BK + 1 FRT + 2 SL)."""
    return sum(combo.values()) * 4


# ============================================================================
# GPU Rasterization with FFT-based Packing
# ============================================================================

def rasterize_piece(vertices: List[Tuple[float, float]], scale: float) -> Tuple[np.ndarray, int, int]:
    """
    Rasterize piece at global scale - PRESERVES ASPECT RATIO.

    IMPORTANT: PIL Image is (width, height) but numpy array is (height, width).
    We return the numpy array with shape (h, w) and the dimensions as (w, h)
    for consistency with PIL conventions when reporting.

    Returns:
        (raster_array, width_px, height_px)
        - raster_array.shape = (height_px, width_px) in numpy convention
    """
    verts_px = [(x * scale, y * scale) for x, y in vertices]

    min_x = min(v[0] for v in verts_px)
    min_y = min(v[1] for v in verts_px)
    max_x = max(v[0] for v in verts_px)
    max_y = max(v[1] for v in verts_px)

    # Add padding for clean edges
    w = int(max_x - min_x) + 4  # Width in pixels (X dimension)
    h = int(max_y - min_y) + 4  # Height in pixels (Y dimension)

    # Normalize to local origin with 2px padding
    normalized = [(x - min_x + 2, y - min_y + 2) for x, y in verts_px]

    # PIL uses (width, height) for Image.new
    img = Image.new('L', (w, h), 0)
    ImageDraw.Draw(img).polygon([tuple(p) for p in normalized], fill=1)

    # numpy array has shape (h, w) = (height, width)
    raster = np.array(img, dtype=np.float32)
    # raster.shape = (h, w)

    return raster, w, h


def gpu_pack_fft(
    piece_rasters: Dict[str, Tuple[np.ndarray, int, int]],
    piece_counts: Dict[str, int],
    width_px: int,    # Y-axis = fabric width (FIXED)
    length_px: int,   # X-axis = strip length (MINIMIZE)
    use_gpu: bool = True
) -> Tuple[float, int, int, List[str], int, np.ndarray]:
    """
    Pack pieces using FFT convolution for overlap detection.

    This is O(N log N) instead of O(N × M) for brute-force approach.
    One FFT kernel call finds ALL valid positions simultaneously.

    Args:
        piece_rasters: {key: (raster, width, height)}
        piece_counts: {key: count}
        width_px: Container width in pixels (Y-axis, fabric width, FIXED)
        length_px: Container length in pixels (X-axis, strip length, MINIMIZE)
        use_gpu: Whether to use GPU acceleration

    Returns:
        (utilization, placed, total, failed_ids, max_x, container_array)
    """
    xp = cp if (use_gpu and GPU_AVAILABLE) else np
    fftconvolve = gpu_fftconvolve if (use_gpu and GPU_AVAILABLE) else cpu_fftconvolve

    # Collect all pieces to place
    all_pieces = []
    for key, (raster, pw, ph) in piece_rasters.items():
        count = piece_counts.get(key, 0)
        for i in range(count):
            area = float(np.sum(raster))
            all_pieces.append((area, raster, pw, ph, f"{key}_{i}"))

    # Sort by area descending (First Fit Decreasing heuristic)
    all_pieces.sort(key=lambda x: -x[0])

    # Container array: shape = (width_px, length_px) = (Y, X)
    # Y = across fabric width (vertical in image)
    # X = along strip length (horizontal in image)
    container = xp.zeros((width_px, length_px), dtype=xp.float32)

    placed = 0
    total_area = 0.0
    max_x = 0
    failed = []

    for area, piece_np, pw, ph, pid in all_pieces:
        piece = xp.asarray(piece_np)

        # FFT convolution to find all overlaps at once
        # Piece shape: (ph, pw) where ph=height, pw=width
        # Container shape: (width_px, length_px)
        # Result shape: (width_px - ph + 1, length_px - pw + 1)
        overlap = fftconvolve(container, piece, mode='valid')

        # Valid positions have zero overlap (threshold for float precision)
        if use_gpu and GPU_AVAILABLE:
            valid = cp.asnumpy(overlap < 0.5)
        else:
            valid = overlap < 0.5

        # Find valid positions
        valid_y, valid_x = np.where(valid)

        if len(valid_x) == 0:
            failed.append(pid)
            continue

        # Bottom-left-fill: minimize X first (strip length), then Y
        # This packs pieces towards the left (minimizing strip length)
        idx = np.lexsort((valid_y, valid_x))[0]
        place_x, place_y = int(valid_x[idx]), int(valid_y[idx])

        # Place piece in container
        container[place_y:place_y+ph, place_x:place_x+pw] += piece

        placed += 1
        total_area += area
        max_x = max(max_x, place_x + pw)

    # Calculate utilization
    if max_x == 0:
        util = 0.0
    else:
        strip_area = width_px * max_x
        util = (total_area / strip_area) * 100

    # Convert container back to numpy for visualization
    if use_gpu and GPU_AVAILABLE:
        container = cp.asnumpy(container)

    return util, placed, len(all_pieces), failed, max_x, container


# ============================================================================
# CPU Spyrrow Nesting
# ============================================================================

def cpu_nest_worker(args: Tuple) -> Tuple:
    """Worker function for parallel CPU nesting."""
    combo_id, combo, time_limit, piece_vertices = args

    t0 = time.time()
    try:
        from nesting_engine.engine.spyrrow_engine import SpyrrowEngine, SpyrrowConfig
        from nesting_engine.core.instance import Container, NestingItem, NestingInstance, FlipMode
        from nesting_engine.core.piece import Piece, PieceIdentifier, OrientationConstraint

        items = []
        for size, count in combo.items():
            if count == 0 or size not in piece_vertices:
                continue
            for ptype, cfg in PIECE_CONFIG.items():
                if ptype not in piece_vertices.get(size, {}):
                    continue
                verts = piece_vertices[size][ptype]

                piece = Piece(
                    vertices=verts,
                    identifier=PieceIdentifier(f"{ptype}_{size}_{combo_id}", size=size),
                    orientation=OrientationConstraint([0, 180], cfg["flip"])
                )

                flip_mode = FlipMode.PAIRED if cfg["flip"] else FlipMode.NONE
                items.append(NestingItem(piece, cfg["demand"] * count, flip_mode))

        if not items:
            return combo_id, combo, 0.0, 0, time.time()-t0, "No items", None

        instance = NestingInstance.create(
            f"CPU_{combo_id}",
            Container(CONTAINER_WIDTH_MM, None),
            items, 2.0, 5.0
        )

        solution = SpyrrowEngine().solve(instance, config=SpyrrowConfig(time_limit))

        return (combo_id, combo, solution.utilization_percent,
                len(solution.placements), time.time()-t0, "", solution)

    except Exception as e:
        import traceback
        return combo_id, combo, 0.0, 0, time.time()-t0, str(e), None


# ============================================================================
# Visualization
# ============================================================================

def save_gpu_png(
    container: np.ndarray,
    max_x: int,
    combo: Dict[str, int],
    combo_id: int,
    util: float,
    placed: int,
    expected: int,
    output_dir: Path
) -> Path:
    """Save GPU raster result as PNG."""
    height, width = container.shape  # (width_px, length_px) = (Y, X)

    # Create RGB image
    img = np.zeros((height, width, 3), dtype=np.uint8)

    # Background = dark gray
    img[:, :] = [40, 40, 40]

    # Placed pieces = light blue
    img[container > 0] = [100, 150, 200]

    # Grid lines every 50 pixels
    for i in range(0, width, 50):
        img[:, i] = [60, 60, 60]
    for i in range(0, height, 50):
        img[i, :] = [60, 60, 60]

    # Strip boundary = red line at max_x
    if 0 < max_x < width:
        img[:, max_x] = [255, 0, 0]

    # Create matplotlib figure
    fig, ax = plt.subplots(figsize=(14, 5))

    # Display image (origin='lower' so Y=0 is at bottom)
    ax.imshow(img, aspect='auto', origin='lower')

    cs = combo_str(combo)
    status = "OK" if placed == expected else f"PARTIAL {placed}/{expected}"
    ax.set_title(f"GPU Raster - Combo {combo_id}: {cs}\n"
                 f"Utilization: {util:.1f}% | {status}", fontsize=12)

    # Axis labels with mm conversion
    length_mm = max_x / GLOBAL_SCALE
    width_mm = height / GLOBAL_SCALE
    ax.set_xlabel(f"Strip Length (X) - {max_x}px = {length_mm:.0f}mm")
    ax.set_ylabel(f"Fabric Width (Y) - {height}px = {width_mm:.0f}mm")

    # Mark key dimensions
    ax.axvline(x=max_x, color='red', linestyle='--', alpha=0.7, label=f'Strip end: {length_mm:.0f}mm')
    ax.legend(loc='upper right')

    path = output_dir / f"gpu_combo_{combo_id:02d}.png"
    plt.savefig(path, dpi=150, bbox_inches='tight', facecolor='white')
    plt.close()
    return path


def save_cpu_png(
    solution: Any,
    piece_vertices: Dict[str, Dict[str, List[Tuple[float, float]]]],
    combo: Dict[str, int],
    combo_id: int,
    output_dir: Path
) -> Optional[Path]:
    """Save CPU Spyrrow result as PNG - FIXED transforms."""
    if solution is None:
        return None

    w_mm = solution.container_width   # Fabric width (Y)
    l_mm = solution.strip_length      # Strip length (X)

    fig, ax = plt.subplots(figsize=(14, 5))

    # Container rectangle
    ax.add_patch(plt.Rectangle((0, 0), l_mm, w_mm, fill=False,
                                edgecolor='red', linewidth=2))

    # Color palette for piece types
    type_colors = {
        'BK': '#4CAF50',   # Green
        'FRT': '#2196F3',  # Blue
        'SL': '#FF9800',   # Orange
    }

    for p in solution.placements:
        # Parse piece_id: format "TYPE_SIZE_COMBOID"
        parts = p.piece_id.split('_')
        ptype = parts[0] if parts else "UNK"
        size = parts[1] if len(parts) > 1 else "M"

        if size not in piece_vertices or ptype not in piece_vertices.get(size, {}):
            continue

        verts = list(piece_vertices[size][ptype])

        # STEP 1: Normalize piece to origin (min corner at 0,0)
        min_x = min(v[0] for v in verts)
        min_y = min(v[1] for v in verts)
        verts = [(x - min_x, y - min_y) for x, y in verts]

        # STEP 2: Apply flip if needed (around piece center X)
        if p.flipped:
            cx = sum(v[0] for v in verts) / len(verts)
            verts = [(2 * cx - x, y) for x, y in verts]

        # STEP 3: Apply rotation around piece centroid
        if p.rotation != 0:
            cx = sum(v[0] for v in verts) / len(verts)
            cy = sum(v[1] for v in verts) / len(verts)
            a = np.radians(p.rotation)
            cos_a, sin_a = np.cos(a), np.sin(a)
            verts = [
                (cx + (x - cx) * cos_a - (y - cy) * sin_a,
                 cy + (x - cx) * sin_a + (y - cy) * cos_a)
                for x, y in verts
            ]

        # STEP 4: Translate to placement position
        # Spyrrow's (x, y) is the position of the piece's reference point
        verts = [(x + p.x, y + p.y) for x, y in verts]

        # Close polygon
        if verts[0] != verts[-1]:
            verts.append(verts[0])

        xs, ys = zip(*verts)
        color = type_colors.get(ptype, '#9E9E9E')
        ax.fill(xs, ys, alpha=0.6, color=color, edgecolor='black', linewidth=0.5)

        # Add label at centroid
        cx, cy = np.mean(xs), np.mean(ys)
        ax.text(cx, cy, f"{ptype}-{size}", fontsize=6, ha='center', va='center')

    ax.set_xlim(-50, l_mm + 50)
    ax.set_ylim(-50, w_mm + 50)
    ax.set_aspect('equal')

    cs = combo_str(combo)
    ax.set_title(f"CPU Spyrrow - Combo {combo_id}: {cs}\n"
                 f"Utilization: {solution.utilization_percent:.1f}% | "
                 f"Strip: {l_mm:.0f}mm | {len(solution.placements)} pieces", fontsize=12)
    ax.set_xlabel("Strip Length X (mm)")
    ax.set_ylabel("Fabric Width Y (mm)")
    ax.grid(True, alpha=0.3, linestyle='--')

    # Legend for piece types
    legend_patches = [mpatches.Patch(color=c, alpha=0.6, label=t)
                      for t, c in type_colors.items()]
    ax.legend(handles=legend_patches, loc='upper right')

    path = output_dir / f"cpu_combo_{combo_id:02d}.png"
    plt.savefig(path, dpi=150, bbox_inches='tight', facecolor='white')
    plt.close()
    return path


# ============================================================================
# Correlation Analysis
# ============================================================================

def spearman_correlation(x: List[float], y: List[float]) -> float:
    """Calculate Spearman rank correlation coefficient."""
    def rankdata(arr):
        sorted_idx = np.argsort(arr)
        ranks = np.empty_like(sorted_idx, dtype=float)
        ranks[sorted_idx] = np.arange(1, len(arr) + 1)
        return ranks

    x, y = np.array(x), np.array(y)
    n = len(x)
    if n < 2:
        return 0.0

    rx, ry = rankdata(x), rankdata(y)
    d = rx - ry
    return float(1 - 6 * np.sum(d**2) / (n * (n**2 - 1)))


# ============================================================================
# Main Experiment
# ============================================================================

def main():
    print("=" * 80)
    print("GPU vs CPU NESTING COMPARISON - V3 (All Fixes Applied)")
    print("=" * 80)

    # Create output directories
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUTPUT_DIR / "gpu").mkdir(exist_ok=True)
    (OUTPUT_DIR / "cpu").mkdir(exist_ok=True)

    print(f"\nConfiguration:")
    print(f"  GPU available: {GPU_AVAILABLE}")
    print(f"  Scale: {GLOBAL_SCALE} px/mm ({1/GLOBAL_SCALE:.0f}mm per pixel)")
    print(f"  Container (Y×X): {CONTAINER_WIDTH_PX}×{CONTAINER_LENGTH_PX} px")
    print(f"  Container (mm): {CONTAINER_WIDTH_MM:.0f}×{CONTAINER_LENGTH_PX/GLOBAL_SCALE:.0f} mm")
    print(f"  Test combinations: {len(TEST_COMBINATIONS)}")

    # Check DXF exists
    if not DXF_PATH.exists():
        print(f"\nERROR: DXF not found at {DXF_PATH}")
        return

    # Load pieces
    print(f"\nLoading pieces from {DXF_PATH}...")
    piece_vertices = load_piece_vertices(DXF_PATH)
    total_pieces = sum(len(t) for t in piece_vertices.values())
    print(f"  Loaded {total_pieces} unique pieces across {len(piece_vertices)} sizes")

    # Rasterize all pieces
    print(f"\nRasterizing pieces (scale={GLOBAL_SCALE} px/mm)...")
    piece_rasters = {}
    for size in SIZES:
        if size not in piece_vertices:
            continue
        for ptype in PIECE_CONFIG:
            if ptype not in piece_vertices[size]:
                continue
            verts = piece_vertices[size][ptype]
            key = f"{size}_{ptype}"
            raster, w, h = rasterize_piece(verts, GLOBAL_SCALE)
            piece_rasters[key] = (raster, w, h)

            # Show piece dimensions
            orig_w = max(v[0] for v in verts) - min(v[0] for v in verts)
            orig_h = max(v[1] for v in verts) - min(v[1] for v in verts)
            print(f"    {key}: {orig_w:.0f}×{orig_h:.0f}mm -> {w}×{h}px")

    # ========== GPU EXPERIMENT ==========
    print(f"\n{'='*80}")
    print("GPU RASTER EXPERIMENT (FFT Convolution)")
    print("="*80)

    gpu_results = []
    gpu_total_time = 0

    for i, combo in enumerate(TEST_COMBINATIONS):
        # Build piece counts
        counts = {}
        for size, n in combo.items():
            if n > 0:
                counts[f"{size}_BK"] = n * PIECE_CONFIG["BK"]["demand"]
                counts[f"{size}_FRT"] = n * PIECE_CONFIG["FRT"]["demand"]
                counts[f"{size}_SL"] = n * PIECE_CONFIG["SL"]["demand"]

        t0 = time.time()
        util, placed, total, failed, max_x, container = gpu_pack_fft(
            piece_rasters, counts, CONTAINER_WIDTH_PX, CONTAINER_LENGTH_PX,
            use_gpu=GPU_AVAILABLE
        )
        dt = time.time() - t0
        gpu_total_time += dt

        exp = expected_pieces(combo)
        status = "OK" if placed == exp else f"PARTIAL ({placed}/{exp})"
        print(f"  [{i+1:2d}] {combo_str(combo):<32} {util:5.1f}% | {status:<15} | {dt:.3f}s")

        if failed:
            print(f"        Failed: {', '.join(failed[:5])}{'...' if len(failed) > 5 else ''}")

        # Save PNG
        png_path = save_gpu_png(container, max_x, combo, i, util, placed, exp, OUTPUT_DIR/"gpu")

        gpu_results.append({
            "combo_id": i,
            "combo": combo,
            "util": util,
            "placed": placed,
            "expected": exp,
            "time": dt,
            "max_x_px": max_x,
            "strip_length_mm": max_x / GLOBAL_SCALE,
            "failed": failed,
            "png": str(png_path)
        })

    print(f"\n  GPU Total: {gpu_total_time:.2f}s ({gpu_total_time/len(TEST_COMBINATIONS):.3f}s avg)")

    # ========== CPU EXPERIMENT ==========
    print(f"\n{'='*80}")
    print(f"CPU SPYRROW EXPERIMENT ({CPU_WORKERS} workers, {CPU_TIME_LIMIT}s each)")
    print("="*80)

    worker_args = [(i, c, CPU_TIME_LIMIT, piece_vertices)
                   for i, c in enumerate(TEST_COMBINATIONS)]

    cpu_results = [None] * len(TEST_COMBINATIONS)
    cpu_solutions = [None] * len(TEST_COMBINATIONS)

    ctx = mp.get_context('spawn')
    cpu_start = time.time()

    with ProcessPoolExecutor(CPU_WORKERS, mp_context=ctx) as executor:
        futures = {executor.submit(cpu_nest_worker, a): a[0] for a in worker_args}

        for f in as_completed(futures):
            cid, combo, util, placed, dt, err, sol = f.result()
            exp = expected_pieces(combo)

            if err:
                print(f"  [{cid+1:2d}] {combo_str(combo):<32} ERROR: {err[:50]}")
            else:
                status = "OK" if placed == exp else f"PARTIAL ({placed}/{exp})"
                print(f"  [{cid+1:2d}] {combo_str(combo):<32} {util:5.1f}% | {status:<15} | {dt:.1f}s")

            cpu_results[cid] = {
                "combo_id": cid,
                "combo": combo,
                "util": util,
                "placed": placed,
                "expected": exp,
                "time": dt,
                "error": err,
                "strip_length_mm": sol.strip_length if sol else 0
            }
            cpu_solutions[cid] = sol

    cpu_total_time = time.time() - cpu_start

    print(f"\n  CPU Total: {cpu_total_time:.1f}s ({cpu_total_time/len(TEST_COMBINATIONS):.1f}s avg)")

    # Save CPU PNGs
    print(f"\nGenerating CPU visualizations...")
    for i, sol in enumerate(cpu_solutions):
        if sol:
            png = save_cpu_png(sol, piece_vertices, TEST_COMBINATIONS[i], i, OUTPUT_DIR/"cpu")
            cpu_results[i]["png"] = str(png) if png else None

    # ========== COMPARISON ==========
    print(f"\n{'='*80}")
    print("COMPARISON")
    print("="*80)

    print(f"\n{'ID':<4} {'Combo':<25} {'GPU%':>7} {'CPU%':>7} {'GPU Len':>10} {'CPU Len':>10} {'GPU Pcs':>9} {'CPU Pcs':>9}")
    print("-" * 95)

    gpu_utils, cpu_utils = [], []
    for g, c in zip(gpu_results, cpu_results):
        gu, cu = g["util"], c["util"]
        gpu_utils.append(gu)
        cpu_utils.append(cu)

        gp = f"{g['placed']}/{g['expected']}"
        cp = f"{c['placed']}/{c['expected']}"
        gpu_len = g.get('strip_length_mm', 0)
        cpu_len = c.get('strip_length_mm', 0)

        print(f"{g['combo_id']:<4} {combo_str(g['combo']):<25} {gu:>6.1f}% {cu:>6.1f}% {gpu_len:>8.0f}mm {cpu_len:>8.0f}mm {gp:>9} {cp:>9}")

    print("-" * 95)

    # Correlation analysis
    rho = spearman_correlation(gpu_utils, cpu_utils)

    print(f"\nSpearman Rank Correlation: rho = {rho:.4f}")

    if rho > 0.85:
        verdict = "STRONG"
        print(f"  -> STRONG correlation - GPU rasterization IS VIABLE for screening")
    elif rho > 0.70:
        verdict = "MODERATE"
        print(f"  -> MODERATE correlation - GPU screening USABLE with caution")
    elif rho > 0.50:
        verdict = "WEAK"
        print(f"  -> WEAK correlation - GPU method needs refinement")
    else:
        verdict = "POOR"
        print(f"  -> POOR correlation - GPU method does NOT predict CPU rankings")

    # Timing summary
    print(f"\nTiming:")
    print(f"  GPU: {gpu_total_time:.2f}s ({gpu_total_time/len(TEST_COMBINATIONS):.3f}s avg)")
    print(f"  CPU: {cpu_total_time:.1f}s ({cpu_total_time/len(TEST_COMBINATIONS):.1f}s avg)")
    speedup = cpu_total_time / gpu_total_time if gpu_total_time > 0 else 0
    print(f"  Speedup: {speedup:.1f}x")

    # Success rates
    gpu_success = sum(1 for r in gpu_results if r["placed"] == r["expected"])
    cpu_success = sum(1 for r in cpu_results if r["placed"] == r["expected"] and not r.get("error"))

    print(f"\nPlacement Success:")
    print(f"  GPU: {gpu_success}/{len(gpu_results)}")
    print(f"  CPU: {cpu_success}/{len(cpu_results)}")

    # Save results
    results = {
        "metadata": {
            "gpu_available": GPU_AVAILABLE,
            "global_scale": GLOBAL_SCALE,
            "container_width_px": CONTAINER_WIDTH_PX,
            "container_length_px": CONTAINER_LENGTH_PX,
            "container_width_mm": CONTAINER_WIDTH_MM,
            "cpu_time_limit": CPU_TIME_LIMIT,
            "cpu_workers": CPU_WORKERS
        },
        "gpu_results": gpu_results,
        "cpu_results": cpu_results,
        "comparison": {
            "spearman_rho": rho,
            "verdict": verdict,
            "gpu_total_time": gpu_total_time,
            "cpu_total_time": cpu_total_time,
            "speedup": speedup,
            "gpu_success_rate": gpu_success / len(gpu_results),
            "cpu_success_rate": cpu_success / len(cpu_results)
        }
    }

    with open(OUTPUT_DIR / "results.json", "w") as f:
        json.dump(results, f, indent=2)

    print(f"\n{'='*80}")
    print("OUTPUT FILES")
    print("="*80)
    print(f"\n  {OUTPUT_DIR}/")
    print(f"    - gpu/*.png  ({len(gpu_results)} GPU raster visualizations)")
    print(f"    - cpu/*.png  ({len(cpu_results)} CPU Spyrrow visualizations)")
    print(f"    - results.json (numerical comparison)")


if __name__ == "__main__":
    main()
