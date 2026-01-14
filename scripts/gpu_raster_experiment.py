#!/usr/bin/env python3
"""
GPU Rasterization vs CPU Spyrrow Experiment.

Tests if fast GPU-based rasterization can predict CPU nesting rankings.
Uses SAME 30 combinations (seed=42) for direct comparison.
Generates visual outputs for inspection.

Usage:
    python scripts/gpu_raster_experiment.py
"""

import json
import multiprocessing as mp
import os
import random
import time
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Tuple, Any

import numpy as np
from PIL import Image, ImageDraw


def spearman_correlation(x: List[float], y: List[float]) -> Tuple[float, float]:
    """
    Calculate Spearman rank correlation coefficient.
    Returns (correlation, p-value approximation).
    """
    x = np.array(x)
    y = np.array(y)
    n = len(x)

    # Get ranks (handling ties with average rank)
    def rankdata(arr):
        sorted_idx = np.argsort(arr)
        ranks = np.empty_like(sorted_idx, dtype=float)
        ranks[sorted_idx] = np.arange(1, len(arr) + 1, dtype=float)
        return ranks

    rank_x = rankdata(x)
    rank_y = rankdata(y)

    # Pearson correlation of ranks
    d = rank_x - rank_y
    d_sq_sum = np.sum(d ** 2)

    # Spearman formula
    rho = 1 - (6 * d_sq_sum) / (n * (n ** 2 - 1))

    # Approximate p-value using t-distribution approximation
    t_stat = rho * np.sqrt((n - 2) / (1 - rho ** 2 + 1e-10))
    # Rough p-value approximation (two-tailed)
    # For n=30, t with df=28, |t|>2.05 gives p<0.05
    p_approx = 2 * (1 - min(0.9999, abs(t_stat) / (abs(t_stat) + n)))

    return float(rho), float(p_approx)


def pearson_correlation(x: List[float], y: List[float]) -> Tuple[float, float]:
    """
    Calculate Pearson correlation coefficient.
    Returns (correlation, p-value approximation).
    """
    x = np.array(x)
    y = np.array(y)
    n = len(x)

    # Center the data
    x_centered = x - np.mean(x)
    y_centered = y - np.mean(y)

    # Correlation
    r = np.sum(x_centered * y_centered) / (
        np.sqrt(np.sum(x_centered ** 2)) * np.sqrt(np.sum(y_centered ** 2)) + 1e-10
    )

    # Approximate p-value
    t_stat = r * np.sqrt((n - 2) / (1 - r ** 2 + 1e-10))
    p_approx = 2 * (1 - min(0.9999, abs(t_stat) / (abs(t_stat) + n)))

    return float(r), float(p_approx)

# Try GPU, fallback to CPU
try:
    import cupy as cp
    GPU_AVAILABLE = True
    gpu_name = cp.cuda.runtime.getDeviceProperties(0)['name'].decode()
    print(f"GPU acceleration: ENABLED ({gpu_name})")
except ImportError:
    cp = np
    GPU_AVAILABLE = False
    print("GPU acceleration: DISABLED (using NumPy CPU fallback)")

# Configuration
DXF_PATH = Path("data/24_2506_7_S-AN1000.DXF")
OUTPUT_DIR = Path("experiment_results/gpu_raster_visuals")
RESULTS_DIR = Path("experiment_results")

SIZES = ["XS", "S", "M", "L", "XL", "XXL"]
NUM_COMBINATIONS = 30
RANDOM_SEED = 42
TIME_LIMIT = 30  # seconds per CPU nest
NUM_WORKERS = 6
CONTAINER_WIDTH_MM = 1524.0  # 60 inches

RESOLUTION = 128
VISUAL_SCALE = 4  # Scale up for visualization (128 * 4 = 512px output)

# Piece configuration per garment
PIECE_CONFIG = {
    "BK": {"demand": 1, "flip": False},
    "FRT": {"demand": 1, "flip": False},
    "SL": {"demand": 2, "flip": True},  # left/right pair
}


# =============================================================================
# Rasterization Functions
# =============================================================================

def rasterize_polygon(vertices: List[Tuple[float, float]], resolution: int = RESOLUTION) -> np.ndarray:
    """Convert polygon vertices to binary grid."""
    vertices = np.array(vertices)

    min_xy = vertices.min(axis=0)
    max_xy = vertices.max(axis=0)
    size = max(max_xy - min_xy)

    if size == 0:
        return np.zeros((resolution, resolution), dtype=np.float32)

    scale = (resolution - 4) / size
    normalized = ((vertices - min_xy) * scale + 2).astype(int)

    img = Image.new('L', (resolution, resolution), 0)
    ImageDraw.Draw(img).polygon([tuple(p) for p in normalized], fill=1)

    return np.array(img, dtype=np.float32)


def estimate_packing_raster(
    piece_rasters: Dict[str, np.ndarray],
    piece_counts: Dict[str, int],
    container_width_px: int,
    container_height_px: int,
    return_layout: bool = False
) -> Tuple[float, ...]:
    """
    Fast packing estimation using rasterized pieces.
    Bottom-left-fill simulation with GPU acceleration.

    Args:
        piece_rasters: Dict[piece_key, 2D array]
        piece_counts: Dict[piece_key, int]
        container_width_px, container_height_px: Container dimensions in pixels
        return_layout: If True, return the container grid for visualization

    Returns:
        utilization (float), or (utilization, layout_grid, placements, max_x) if return_layout=True
    """
    xp = cp if GPU_AVAILABLE else np

    # Collect all pieces to place
    all_pieces = []
    for key, count in piece_counts.items():
        if key in piece_rasters and count > 0:
            raster = piece_rasters[key]
            area = float(np.sum(raster))
            for i in range(count):
                all_pieces.append((area, raster, f"{key}_{i}"))

    # Sort by area descending (FFD heuristic)
    all_pieces.sort(key=lambda x: -x[0])

    # Initialize container
    container = xp.zeros((container_height_px, container_width_px), dtype=xp.float32)

    # For visualization: track placed pieces
    placements = []

    total_placed_area = 0
    max_x_used = 0

    step_size = 2  # Pixel step for position search (smaller = more accurate, slower)

    for area, piece_np, piece_id in all_pieces:
        piece = xp.asarray(piece_np)
        ph, pw = piece.shape

        placed = False

        # Search for valid position (bottom-left priority)
        for x in range(0, container_width_px - pw, step_size):
            for y in range(0, container_height_px - ph, step_size):
                # Check overlap
                region = container[y:y+ph, x:x+pw]
                overlap = xp.sum(region * piece)

                if float(overlap) == 0:
                    # Place piece
                    container[y:y+ph, x:x+pw] += piece
                    total_placed_area += area
                    max_x_used = max(max_x_used, x + pw)
                    placements.append((piece_id, x, y, pw, ph))
                    placed = True
                    break

            if placed:
                break

        if not placed:
            # Could not place - extend container or skip
            pass

    # Calculate utilization
    if max_x_used == 0:
        utilization = 0.0
    else:
        strip_area = max_x_used * container_height_px
        utilization = (total_placed_area / strip_area) * 100

    if return_layout:
        # Convert back to numpy for visualization
        if GPU_AVAILABLE:
            container_np = cp.asnumpy(container)
        else:
            container_np = container
        return utilization, container_np, placements, max_x_used

    return utilization


# =============================================================================
# Visualization Functions
# =============================================================================

def save_rasterized_pieces(piece_rasters: Dict[str, np.ndarray], output_dir: Path):
    """Save all rasterized pieces as images for inspection."""
    output_dir.mkdir(parents=True, exist_ok=True)

    for key, raster in piece_rasters.items():
        # Scale up for visibility
        img_array = (raster * 255).astype(np.uint8)
        img = Image.fromarray(img_array, mode='L')
        img_scaled = img.resize((RESOLUTION * VISUAL_SCALE, RESOLUTION * VISUAL_SCALE), Image.NEAREST)
        img_scaled.save(output_dir / f"piece_{key}.png")

    print(f"  Saved {len(piece_rasters)} piece rasters to {output_dir}/")


def save_packing_visualization(
    container_grid: np.ndarray,
    placements: List,
    max_x_used: int,
    combo_id: int,
    combo: Dict[str, int],
    utilization: float,
    output_dir: Path
) -> str:
    """
    Save a visualization of the raster packing result.
    Uses colors to distinguish different pieces.
    """
    output_dir.mkdir(parents=True, exist_ok=True)

    height, width = container_grid.shape

    # Create RGB image
    img_array = np.zeros((height, width, 3), dtype=np.uint8)

    # Background where pieces are placed
    mask = container_grid > 0
    img_array[mask] = [100, 150, 200]  # Light blue for placed area

    # Add grid lines for reference
    for i in range(0, width, 20):
        img_array[:, i, :] = [50, 50, 50]
    for i in range(0, height, 20):
        img_array[i, :, :] = [50, 50, 50]

    # Mark the strip boundary
    if max_x_used > 0 and max_x_used < width:
        img_array[:, int(max_x_used), :] = [255, 0, 0]  # Red line at strip end

    # Scale up
    img = Image.fromarray(img_array, mode='RGB')
    img_scaled = img.resize((width * VISUAL_SCALE, height * VISUAL_SCALE), Image.NEAREST)

    # Add text annotation
    try:
        from PIL import ImageFont
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 20)
    except:
        font = None

    draw = ImageDraw.Draw(img_scaled)
    combo_str = ', '.join(f"{s}:{n}" for s, n in sorted(combo.items()) if n > 0)
    draw.text((10, 10), f"Combo {combo_id}: {combo_str}", fill=(255, 255, 255), font=font)
    draw.text((10, 35), f"Raster Util: {utilization:.1f}%", fill=(255, 255, 0), font=font)

    filename = output_dir / f"raster_combo_{combo_id:02d}.png"
    img_scaled.save(filename)
    return str(filename)


def create_comparison_output(
    raster_results: List[Tuple],
    cpu_results: List[Tuple],
    output_dir: Path
):
    """Create a summary comparison showing top 10 from each method."""
    output_dir.mkdir(parents=True, exist_ok=True)

    # Sort by utilization
    raster_sorted = sorted(raster_results, key=lambda x: -x[2])[:10]
    cpu_sorted = sorted(cpu_results, key=lambda x: -x[2])[:10]

    # Create comparison text file
    with open(output_dir / "ranking_comparison.txt", 'w') as f:
        f.write("=" * 90 + "\n")
        f.write("RANKING COMPARISON: GPU Raster vs CPU Spyrrow\n")
        f.write("=" * 90 + "\n\n")

        f.write(f"{'Rank':<6}{'Raster Combo':<30}{'Raster%':<10}{'CPU Combo':<30}{'CPU%':<10}\n")
        f.write("-" * 90 + "\n")

        for i in range(10):
            r_id, r_combo, r_util = raster_sorted[i][0], raster_sorted[i][1], raster_sorted[i][2]
            c_id, c_combo, c_util = cpu_sorted[i][0], cpu_sorted[i][1], cpu_sorted[i][2]

            r_str = ', '.join(f"{s}:{n}" for s, n in sorted(r_combo.items()) if n > 0)
            c_str = ', '.join(f"{s}:{n}" for s, n in sorted(c_combo.items()) if n > 0)

            f.write(f"{i+1:<6}{r_str:<30}{r_util:<10.1f}{c_str:<30}{c_util:<10.1f}\n")

        f.write("\n" + "=" * 90 + "\n")

    print(f"  Saved ranking comparison to {output_dir}/ranking_comparison.txt")


# =============================================================================
# Piece Loading (reused from parallel scripts)
# =============================================================================

def extract_piece_type(piece_name: str) -> str | None:
    """Extract piece type (BK, FRT, SL) from piece name."""
    name_upper = piece_name.upper()
    for piece_type in PIECE_CONFIG.keys():
        if piece_type in name_upper:
            return piece_type
    return None


def load_piece_vertices(dxf_path: Path) -> Dict[str, Dict[str, List[Tuple[float, float]]]]:
    """
    Load pieces from DXF and extract vertices as primitive data.

    Returns:
        {size: {piece_type: [(x, y), ...]}}
    """
    from nesting_engine.io.dxf_parser import DXFParser

    print(f"Loading pieces from {dxf_path}...")
    parser = DXFParser(str(dxf_path))
    result = parser.parse()

    print(f"  Found {len(result.pieces)} raw pieces")

    pieces_by_size: Dict[str, Dict[str, List[Tuple[float, float]]]] = defaultdict(dict)

    for parsed in result.pieces:
        size = parsed.size
        if size not in SIZES:
            continue

        piece_name = parsed.piece_name or ""
        piece_type = extract_piece_type(piece_name)

        if piece_type is None:
            continue

        if piece_type in pieces_by_size[size]:
            continue

        # Convert to mm
        to_mm = 25.4
        vertices_mm = [(x * to_mm, y * to_mm) for x, y in parsed.vertices]

        # Clean duplicate vertices
        cleaned = []
        seen = set()
        for x, y in vertices_mm:
            key = (round(x, 3), round(y, 3))
            if key not in seen:
                seen.add(key)
                cleaned.append((x, y))

        if len(cleaned) < 3:
            continue

        pieces_by_size[size][piece_type] = cleaned

    total_unique = sum(len(types) for types in pieces_by_size.values())
    print(f"  Organized into {total_unique} unique pieces across {len(pieces_by_size)} sizes")

    return dict(pieces_by_size)


# =============================================================================
# Combination Generation
# =============================================================================

def generate_combinations(n: int, seed: int = 42) -> List[Dict[str, int]]:
    """Generate n random size combinations (1-6 garments total)."""
    random.seed(seed)
    combinations = []

    for _ in range(n):
        total = random.randint(1, 6)
        size_counts = {size: 0 for size in SIZES}

        for _ in range(total):
            size = random.choice(SIZES)
            size_counts[size] += 1

        # Remove zeros for cleaner representation
        size_counts = {k: v for k, v in size_counts.items() if v > 0}
        combinations.append(size_counts)

    return combinations


# =============================================================================
# CPU Parallel Nesting (reused from ratio_parallel_test.py)
# =============================================================================

def nest_worker(args: Tuple) -> Tuple[int, Dict[str, int], float, float, float, str]:
    """
    Worker function - imports happen inside each process.
    """
    combo_id, size_mix, time_limit, piece_vertices_by_size = args

    start_time = time.time()

    try:
        # Import inside worker to avoid serialization issues
        from nesting_engine.engine.spyrrow_engine import SpyrrowEngine, SpyrrowConfig
        from nesting_engine.core.instance import Container, NestingItem, NestingInstance, FlipMode
        from nesting_engine.core.piece import Piece, PieceIdentifier, OrientationConstraint

        # Build pieces from primitive vertex data
        items = []

        for size, count in size_mix.items():
            if count == 0 or size not in piece_vertices_by_size:
                continue

            for piece_type, config in PIECE_CONFIG.items():
                if piece_type not in piece_vertices_by_size[size]:
                    continue

                vertices = piece_vertices_by_size[size][piece_type]

                identifier = PieceIdentifier(
                    piece_name=f"{piece_type}_{size}_{combo_id}",
                    size=size
                )

                orientation = OrientationConstraint(
                    allowed_rotations=[0, 180],
                    allow_flip=config["flip"]
                )

                piece = Piece(
                    vertices=vertices,
                    identifier=identifier,
                    orientation=orientation
                )

                total_demand = config["demand"] * count
                flip_mode = FlipMode.PAIRED if config["flip"] else FlipMode.NONE

                item = NestingItem(
                    piece=piece,
                    demand=total_demand,
                    flip_mode=flip_mode
                )
                items.append(item)

        if not items:
            return combo_id, size_mix, 0.0, 0.0, time.time() - start_time, "No items created"

        # Create container (60 inches = 1524mm)
        container = Container(width=CONTAINER_WIDTH_MM, height=None)

        instance = NestingInstance.create(
            name=f"GPUTest_{combo_id}",
            container=container,
            items=items,
            piece_buffer=2.0,
            edge_buffer=5.0
        )

        # Solve
        engine = SpyrrowEngine()
        config = SpyrrowConfig(time_limit=time_limit)
        solution = engine.solve(instance, config=config)

        duration = time.time() - start_time
        return combo_id, size_mix, solution.utilization_percent, solution.strip_length, duration, ""

    except Exception as e:
        duration = time.time() - start_time
        return combo_id, size_mix, 0.0, 0.0, duration, str(e)


def run_parallel_cpu_nests(
    combinations: List[Dict[str, int]],
    piece_vertices: Dict,
    time_limit: int = 30,
    workers: int = 6
) -> List[Tuple]:
    """Run CPU nests using ProcessPoolExecutor."""

    worker_args = [
        (i, combo, time_limit, piece_vertices)
        for i, combo in enumerate(combinations)
    ]

    ctx = mp.get_context('spawn')
    results = [None] * len(combinations)

    with ProcessPoolExecutor(max_workers=workers, mp_context=ctx) as executor:
        futures = {executor.submit(nest_worker, args): args[0] for args in worker_args}

        completed = 0
        for future in as_completed(futures):
            combo_id, size_mix, utilization, strip_length, duration, error = future.result()
            results[combo_id] = (combo_id, size_mix, utilization, duration)
            completed += 1

            if completed % 5 == 0 or completed == len(combinations):
                print(f"  CPU progress: {completed}/{len(combinations)}")

    return results


# =============================================================================
# Main Experiment
# =============================================================================

def run_experiment():
    print("=" * 80)
    print("GPU RASTERIZATION vs CPU SPYRROW EXPERIMENT")
    print("=" * 80)

    # Setup info
    print(f"\nConfiguration:")
    print(f"  GPU available: {GPU_AVAILABLE}")
    print(f"  Raster resolution: {RESOLUTION}x{RESOLUTION}")
    print(f"  Combinations: {NUM_COMBINATIONS} (seed={RANDOM_SEED})")
    print(f"  CPU time limit: {TIME_LIMIT}s")
    print(f"  Workers: {NUM_WORKERS}")

    # Check DXF exists
    if not DXF_PATH.exists():
        print(f"\nERROR: DXF file not found at {DXF_PATH}")
        return

    # 1. Load pieces
    print(f"\n" + "-" * 80)
    piece_vertices = load_piece_vertices(DXF_PATH)

    if not piece_vertices:
        print("ERROR: No valid pieces loaded from DXF")
        return

    # 2. Rasterize all unique pieces
    print(f"\nRasterizing pieces...")
    piece_rasters = {}
    for size, pieces in piece_vertices.items():
        for piece_type, vertices in pieces.items():
            key = f"{size}_{piece_type}"
            piece_rasters[key] = rasterize_polygon(vertices)
    print(f"  Rasterized {len(piece_rasters)} unique pieces")

    # Save rasterized pieces for inspection
    save_rasterized_pieces(piece_rasters, OUTPUT_DIR / "pieces")

    # 3. Generate combinations
    combinations = generate_combinations(NUM_COMBINATIONS, seed=RANDOM_SEED)
    print(f"\nGenerated {len(combinations)} combinations")

    # 4. Container dimensions in pixels
    # Scale: ~5mm per pixel -> 1524mm / 5 = ~300px width
    # Height estimate for strip packing: allow room for pieces
    container_width_px = 300
    container_height_px = 400

    # 5. Run raster estimates with visualization
    print(f"\n" + "-" * 80)
    print(f"Running raster estimates...")
    print("-" * 80)

    raster_results = []
    raster_start = time.time()

    for i, combo in enumerate(combinations):
        t0 = time.time()

        # Build piece counts
        piece_counts = {}
        for size, garments in combo.items():
            if garments > 0:
                piece_counts[f"{size}_BK"] = garments * PIECE_CONFIG["BK"]["demand"]
                piece_counts[f"{size}_FRT"] = garments * PIECE_CONFIG["FRT"]["demand"]
                piece_counts[f"{size}_SL"] = garments * PIECE_CONFIG["SL"]["demand"]

        # Estimate with layout for visualization
        result = estimate_packing_raster(
            piece_rasters,
            piece_counts,
            container_width_px,
            container_height_px,
            return_layout=True
        )

        utilization, layout, placements, max_x = result
        duration = time.time() - t0

        raster_results.append((i, combo, utilization, duration))

        combo_str = ', '.join(f"{s}:{n}" for s, n in sorted(combo.items()) if n > 0)
        print(f"  [{i+1:2d}/{NUM_COMBINATIONS}] {combo_str:<35} -> {utilization:5.1f}% ({duration:.3f}s)")

        # Save visualization for first 10 and any high performers
        if i < 10 or utilization > 70:
            save_packing_visualization(
                layout, placements, max_x, i, combo, utilization,
                OUTPUT_DIR / "packings"
            )

    raster_total = time.time() - raster_start
    print(f"\nRaster phase complete: {len(combinations)} combos in {raster_total:.1f}s")

    # 6. Run CPU nests (parallel)
    print(f"\n" + "-" * 80)
    print(f"Running CPU nests ({NUM_WORKERS} workers, {TIME_LIMIT}s each)...")
    print("-" * 80)

    cpu_start = time.time()
    cpu_results = run_parallel_cpu_nests(
        combinations, piece_vertices,
        time_limit=TIME_LIMIT, workers=NUM_WORKERS
    )
    cpu_total = time.time() - cpu_start
    print(f"\nCPU phase complete: {len(combinations)} combos in {cpu_total:.1f}s")

    # Print CPU results
    print(f"\nCPU Results:")
    for i, (combo_id, combo, util, duration) in enumerate(cpu_results):
        combo_str = ', '.join(f"{s}:{n}" for s, n in sorted(combo.items()) if n > 0)
        print(f"  [{i+1:2d}/{NUM_COMBINATIONS}] {combo_str:<35} -> {util:5.1f}%")

    # 7. Correlation analysis
    print("\n" + "=" * 80)
    print("CORRELATION ANALYSIS")
    print("=" * 80)

    raster_utils = [r[2] for r in raster_results]
    cpu_utils = [r[2] for r in cpu_results]

    # Spearman rank correlation (using numpy-based implementation)
    corr, pval = spearman_correlation(raster_utils, cpu_utils)

    # Pearson correlation
    pearson_corr, pearson_pval = pearson_correlation(raster_utils, cpu_utils)

    print(f"\nRaster scores range: {min(raster_utils):.1f}% - {max(raster_utils):.1f}%")
    print(f"CPU scores range:    {min(cpu_utils):.1f}% - {max(cpu_utils):.1f}%")
    print(f"\nSpearman Rank Correlation: ρ = {corr:.4f} (p = {pval:.2e})")
    print(f"Pearson Correlation:       r = {pearson_corr:.4f} (p = {pearson_pval:.2e})")

    # Top 10 overlap
    raster_top10 = set(r[0] for r in sorted(raster_results, key=lambda x: -x[2])[:10])
    cpu_top10 = set(r[0] for r in sorted(cpu_results, key=lambda x: -x[2])[:10])
    overlap = len(raster_top10 & cpu_top10)

    print(f"\nTop 10 Overlap: {overlap}/10 combinations appear in both top-10 lists")
    print(f"  Raster top 10 IDs: {sorted(raster_top10)}")
    print(f"  CPU top 10 IDs:    {sorted(cpu_top10)}")

    # Create comparison outputs
    create_comparison_output(raster_results, cpu_results, OUTPUT_DIR)

    # 8. Conclusion
    print("\n" + "=" * 80)
    print("CONCLUSION")
    print("=" * 80)

    if corr > 0.85:
        verdict = f"STRONG correlation (ρ = {corr:.3f}) - GPU rasterization IS VIABLE for screening"
        symbol = "✓"
    elif corr > 0.70:
        verdict = f"MODERATE correlation (ρ = {corr:.3f}) - GPU screening USABLE with caution"
        symbol = "~"
    else:
        verdict = f"WEAK correlation (ρ = {corr:.3f}) - GPU method needs refinement"
        symbol = "✗"

    print(f"\n{symbol} {verdict}")

    print(f"\nTiming Summary:")
    print(f"  Raster ({NUM_COMBINATIONS} combos): {raster_total:.1f}s ({raster_total/NUM_COMBINATIONS:.3f}s avg)")
    print(f"  CPU ({NUM_COMBINATIONS} combos):    {cpu_total:.1f}s ({cpu_total/NUM_COMBINATIONS:.1f}s avg)")
    print(f"  Potential speedup:  {cpu_total/raster_total:.0f}x (if correlation holds)")

    # 9. Save results
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)

    results = {
        "metadata": {
            "timestamp": datetime.now().isoformat(),
            "dxf_file": str(DXF_PATH),
            "gpu_available": GPU_AVAILABLE,
            "resolution": RESOLUTION,
            "container_width_px": container_width_px,
            "container_height_px": container_height_px,
            "cpu_time_limit": TIME_LIMIT,
            "num_combinations": NUM_COMBINATIONS,
            "num_workers": NUM_WORKERS,
            "random_seed": RANDOM_SEED
        },
        "results": [
            {
                "combo_id": i,
                "size_mix": combinations[i],
                "raster_util": raster_results[i][2],
                "raster_time": raster_results[i][3],
                "cpu_util": cpu_results[i][2],
                "cpu_time": cpu_results[i][3]
            }
            for i in range(len(combinations))
        ],
        "correlation": {
            "spearman_rho": corr,
            "spearman_p_value": pval,
            "pearson_r": pearson_corr,
            "pearson_p_value": pearson_pval,
            "top10_overlap": overlap
        },
        "timing": {
            "raster_total_seconds": raster_total,
            "cpu_total_seconds": cpu_total,
            "speedup_factor": cpu_total / raster_total
        }
    }

    with open(RESULTS_DIR / "gpu_raster_results.json", 'w') as f:
        json.dump(results, f, indent=2)

    print(f"\nResults saved to: {RESULTS_DIR}/gpu_raster_results.json")
    print(f"Visuals saved to: {OUTPUT_DIR}/")
    print(f"  - pieces/: Rasterized piece images")
    print(f"  - packings/: Sample packing visualizations")
    print(f"  - ranking_comparison.txt: Side-by-side ranking")


if __name__ == '__main__':
    run_experiment()
