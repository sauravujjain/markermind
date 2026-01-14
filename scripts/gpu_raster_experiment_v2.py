#!/usr/bin/env python3
"""
GPU Rasterization vs CPU Spyrrow Experiment - V2 (Fixed)

This version fixes critical bugs from v1:
1. Uses GLOBAL scale factor for piece rasterization (preserves relative sizes)
2. Properly sized container matching real dimensions
3. Tracks and reports placement failures
4. Better bottom-left-fill algorithm with diagnostics

Usage:
    PYTHONPATH=. python scripts/gpu_raster_experiment_v2.py
"""

import json
import time
from pathlib import Path
from typing import Dict, List, Tuple

import numpy as np
from PIL import Image, ImageDraw

# Import shared utilities
import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from scripts.experiment_utils import (
    load_piece_vertices, TEST_COMBINATIONS, count_expected_pieces,
    combo_to_string, combo_to_filename, get_piece_dimensions,
    get_piece_area, PIECE_CONFIG, SIZES
)

# Try GPU acceleration
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
OUTPUT_DIR = Path("experiment_results/gpu_vs_cpu_v2")
CONTAINER_WIDTH_MM = 1524.0  # 60 inches

# Rasterization settings - GLOBAL scale preserves relative sizes
GLOBAL_SCALE = 0.2  # pixels per mm (5mm per pixel resolution)
CONTAINER_WIDTH_PX = int(CONTAINER_WIDTH_MM * GLOBAL_SCALE)  # ~305 px
CONTAINER_HEIGHT_PX = 1200  # Enough for long markers (~6000mm)

# Placement algorithm settings
STEP_SIZE = 4  # pixels - smaller = more accurate but slower


def rasterize_piece_global_scale(
    vertices: List[Tuple[float, float]],
    scale: float
) -> Tuple[np.ndarray, Tuple[int, int], float]:
    """
    Rasterize piece using global scale factor.

    This preserves relative sizes between pieces - an XXL piece will be
    larger than an XS piece in pixel space.

    Args:
        vertices: Piece vertices in mm
        scale: Pixels per mm (global scale factor)

    Returns:
        (raster_array, (width_px, height_px), area_px)
    """
    # Scale vertices to pixels
    vertices_px = [(x * scale, y * scale) for x, y in vertices]

    # Get bounds in pixel space
    min_x = min(v[0] for v in vertices_px)
    min_y = min(v[1] for v in vertices_px)
    max_x = max(v[0] for v in vertices_px)
    max_y = max(v[1] for v in vertices_px)

    # Calculate dimensions with padding
    width = int(max_x - min_x) + 4
    height = int(max_y - min_y) + 4

    # Normalize to origin for this piece's raster
    normalized = [(x - min_x + 2, y - min_y + 2) for x, y in vertices_px]

    # Create appropriately-sized raster
    img = Image.new('L', (width, height), 0)
    ImageDraw.Draw(img).polygon([tuple(p) for p in normalized], fill=1)

    raster = np.array(img, dtype=np.float32)
    area_px = float(np.sum(raster))

    return raster, (width, height), area_px


def estimate_packing_raster(
    piece_rasters: Dict[str, Tuple[np.ndarray, Tuple[int, int], float]],
    piece_counts: Dict[str, int],
    container_width_px: int,
    container_height_px: int,
    verbose: bool = False
) -> Tuple[float, int, int, List[str], float]:
    """
    Bottom-left-fill packing simulation with GPU acceleration.

    Args:
        piece_rasters: {piece_key: (raster, (w, h), area)}
        piece_counts: {piece_key: count}
        container_width_px: Container width in pixels
        container_height_px: Container height in pixels
        verbose: Print detailed progress

    Returns:
        (utilization%, placed_count, total_count, failed_piece_ids, max_x_used)
    """
    xp = cp if GPU_AVAILABLE else np

    # Collect all pieces to place
    all_pieces = []
    for key, count in piece_counts.items():
        if key in piece_rasters and count > 0:
            raster, (pw, ph), area = piece_rasters[key]
            for i in range(count):
                all_pieces.append({
                    'area': area,
                    'raster': raster,
                    'width': pw,
                    'height': ph,
                    'id': f"{key}_{i}"
                })

    total_count = len(all_pieces)

    if verbose:
        print(f"    Pieces to place: {total_count}")

    # Sort by area descending (First Fit Decreasing heuristic)
    all_pieces.sort(key=lambda x: -x['area'])

    # Initialize container
    container = xp.zeros((container_height_px, container_width_px), dtype=xp.float32)

    placed_count = 0
    total_placed_area = 0.0
    max_x_used = 0
    failed_pieces = []

    for piece in all_pieces:
        piece_raster = xp.asarray(piece['raster'])
        pw, ph = piece['width'], piece['height']
        piece_id = piece['id']

        placed = False

        # Search for valid position (bottom-left priority)
        # X is the strip direction, Y is container width
        for x in range(0, container_width_px - pw + 1, STEP_SIZE):
            for y in range(0, container_height_px - ph + 1, STEP_SIZE):
                # Check overlap using element-wise multiplication
                region = container[y:y+ph, x:x+pw]

                # Ensure shapes match
                if region.shape != piece_raster.shape:
                    continue

                overlap = xp.sum(region * piece_raster)

                if float(overlap) == 0:
                    # No collision - place piece
                    container[y:y+ph, x:x+pw] += piece_raster
                    total_placed_area += piece['area']
                    max_x_used = max(max_x_used, x + pw)
                    placed_count += 1
                    placed = True
                    break

            if placed:
                break

        if not placed:
            failed_pieces.append(piece_id)
            if verbose:
                print(f"      Failed to place: {piece_id} ({pw}x{ph}px)")

    # Calculate utilization
    if max_x_used == 0:
        utilization = 0.0
    else:
        strip_area = max_x_used * container_height_px
        utilization = (total_placed_area / strip_area) * 100

    if verbose and failed_pieces:
        print(f"    WARNING: Failed to place {len(failed_pieces)}/{total_count} pieces")

    return utilization, placed_count, total_count, failed_pieces, float(max_x_used)


def save_packing_visualization(
    piece_rasters: Dict[str, Tuple[np.ndarray, Tuple[int, int], float]],
    piece_counts: Dict[str, int],
    container_width_px: int,
    container_height_px: int,
    combo_id: int,
    combo: Dict[str, int],
    utilization: float,
    output_dir: Path
) -> str:
    """
    Create and save a visualization of the GPU raster packing.
    """
    xp = cp if GPU_AVAILABLE else np

    # Collect pieces
    all_pieces = []
    for key, count in piece_counts.items():
        if key in piece_rasters and count > 0:
            raster, (pw, ph), area = piece_rasters[key]
            for i in range(count):
                all_pieces.append({
                    'area': area,
                    'raster': raster,
                    'width': pw,
                    'height': ph,
                    'id': f"{key}_{i}",
                    'key': key
                })

    all_pieces.sort(key=lambda x: -x['area'])

    # Initialize container
    container = xp.zeros((container_height_px, container_width_px), dtype=xp.float32)

    placements = []
    max_x_used = 0

    for piece in all_pieces:
        piece_raster = xp.asarray(piece['raster'])
        pw, ph = piece['width'], piece['height']

        placed = False
        for x in range(0, container_width_px - pw + 1, STEP_SIZE):
            for y in range(0, container_height_px - ph + 1, STEP_SIZE):
                region = container[y:y+ph, x:x+pw]
                if region.shape != piece_raster.shape:
                    continue

                overlap = xp.sum(region * piece_raster)
                if float(overlap) == 0:
                    container[y:y+ph, x:x+pw] += piece_raster
                    max_x_used = max(max_x_used, x + pw)
                    placements.append((piece['key'], x, y, pw, ph))
                    placed = True
                    break
            if placed:
                break

    # Convert container to numpy for visualization
    if GPU_AVAILABLE:
        container_np = cp.asnumpy(container)
    else:
        container_np = container

    # Crop to used area
    if max_x_used > 0:
        container_np = container_np[:, :max_x_used + 10]

    # Create RGB visualization
    height, width = container_np.shape
    img_array = np.zeros((height, width, 3), dtype=np.uint8)

    # Background
    img_array[:, :] = [40, 40, 40]

    # Placed area in blue
    mask = container_np > 0
    img_array[mask] = [100, 150, 200]

    # Grid lines
    for i in range(0, width, 20):
        img_array[:, i, :] = [60, 60, 60]
    for i in range(0, height, 20):
        img_array[i, :, :] = [60, 60, 60]

    # Mark strip boundary
    if max_x_used > 0 and max_x_used < width:
        img_array[:, int(max_x_used), :] = [255, 0, 0]

    # Scale up for visibility
    scale = 2
    img = Image.fromarray(img_array, mode='RGB')
    img_scaled = img.resize((width * scale, height * scale), Image.NEAREST)

    # Add text
    draw = ImageDraw.Draw(img_scaled)
    combo_str = combo_to_string(combo)
    draw.text((10, 10), f"Combo {combo_id}: {combo_str}", fill=(255, 255, 255))
    draw.text((10, 30), f"GPU Raster Util: {utilization:.1f}%", fill=(255, 255, 0))

    # Save
    output_dir.mkdir(parents=True, exist_ok=True)
    filename = output_dir / f"gpu_combo_{combo_id:02d}.png"
    img_scaled.save(filename)

    return str(filename)


def run_gpu_experiment():
    """Main GPU rasterization experiment."""
    print("=" * 80)
    print("GPU RASTERIZATION EXPERIMENT - V2 (Fixed)")
    print("=" * 80)

    print(f"\nConfiguration:")
    print(f"  GPU available: {GPU_AVAILABLE}")
    print(f"  Global scale: {GLOBAL_SCALE} px/mm ({1/GLOBAL_SCALE:.1f} mm/px)")
    print(f"  Container: {CONTAINER_WIDTH_PX} x {CONTAINER_HEIGHT_PX} px")
    print(f"  Container (mm): {CONTAINER_WIDTH_MM:.0f} x {CONTAINER_HEIGHT_PX/GLOBAL_SCALE:.0f} mm")
    print(f"  Step size: {STEP_SIZE} px")
    print(f"  Test combinations: {len(TEST_COMBINATIONS)}")

    # Check DXF exists
    if not DXF_PATH.exists():
        print(f"\nERROR: DXF file not found at {DXF_PATH}")
        return None

    # Load pieces with vertex cleaning
    print(f"\nLoading pieces from {DXF_PATH}...")
    piece_vertices = load_piece_vertices(DXF_PATH)

    if not piece_vertices:
        print("ERROR: No valid pieces loaded from DXF")
        return None

    total_pieces = sum(len(types) for types in piece_vertices.values())
    print(f"  Loaded {total_pieces} unique pieces across {len(piece_vertices)} sizes")

    # Rasterize all pieces with GLOBAL scale
    print(f"\nRasterizing pieces (global scale = {GLOBAL_SCALE} px/mm)...")
    piece_rasters = {}

    for size in SIZES:
        if size not in piece_vertices:
            continue
        for piece_type in PIECE_CONFIG.keys():
            if piece_type not in piece_vertices[size]:
                continue

            vertices = piece_vertices[size][piece_type]
            key = f"{size}_{piece_type}"

            raster, dims, area = rasterize_piece_global_scale(vertices, GLOBAL_SCALE)
            piece_rasters[key] = (raster, dims, area)

            # Diagnostic: show piece sizes
            orig_w, orig_h = get_piece_dimensions(vertices)
            orig_area = get_piece_area(vertices)
            print(f"    {key}: {orig_w:.0f}x{orig_h:.0f}mm ({orig_area:.0f}mm^2) -> {dims[0]}x{dims[1]}px ({area:.0f}px^2)")

    print(f"\n  Total rasterized: {len(piece_rasters)} pieces")

    # Run experiment for each combination
    print(f"\n" + "-" * 80)
    print("Running GPU raster estimates...")
    print("-" * 80)

    results = []
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    visuals_dir = OUTPUT_DIR / "visuals"

    for i, combo in enumerate(TEST_COMBINATIONS):
        expected_pieces = count_expected_pieces(combo)
        combo_str = combo_to_string(combo)

        print(f"\n  [{i+1:2d}/{len(TEST_COMBINATIONS)}] {combo_str} (expecting {expected_pieces} pieces)")

        # Build piece counts for this combo
        piece_counts = {}
        for size, garments in combo.items():
            if garments > 0:
                piece_counts[f"{size}_BK"] = garments * PIECE_CONFIG["BK"]["demand"]
                piece_counts[f"{size}_FRT"] = garments * PIECE_CONFIG["FRT"]["demand"]
                piece_counts[f"{size}_SL"] = garments * PIECE_CONFIG["SL"]["demand"]

        t0 = time.time()
        util, placed, total, failed, max_x = estimate_packing_raster(
            piece_rasters, piece_counts,
            CONTAINER_WIDTH_PX, CONTAINER_HEIGHT_PX,
            verbose=True
        )
        duration = time.time() - t0

        # Status indicator
        if placed == expected_pieces:
            status = "OK"
        else:
            status = f"PARTIAL ({placed}/{expected_pieces})"

        print(f"    Result: {util:.1f}% utilization | {status} | {duration:.3f}s")

        # Save visualization
        vis_path = save_packing_visualization(
            piece_rasters, piece_counts,
            CONTAINER_WIDTH_PX, CONTAINER_HEIGHT_PX,
            i, combo, util, visuals_dir
        )

        results.append({
            "combo_id": i,
            "combo": combo,
            "expected_pieces": expected_pieces,
            "placed_pieces": placed,
            "utilization": util,
            "time": duration,
            "failed_pieces": failed,
            "max_x_px": max_x,
            "strip_length_mm": max_x / GLOBAL_SCALE,
            "visualization": vis_path
        })

    # Summary
    print(f"\n" + "=" * 80)
    print("GPU EXPERIMENT SUMMARY")
    print("=" * 80)

    successful = [r for r in results if r["placed_pieces"] == r["expected_pieces"]]
    partial = [r for r in results if 0 < r["placed_pieces"] < r["expected_pieces"]]
    failed = [r for r in results if r["placed_pieces"] == 0]

    print(f"\n  Fully placed:  {len(successful)}/{len(results)}")
    print(f"  Partial:       {len(partial)}/{len(results)}")
    print(f"  Failed:        {len(failed)}/{len(results)}")

    if successful:
        utils = [r["utilization"] for r in successful]
        print(f"\n  Utilization (successful): {min(utils):.1f}% - {max(utils):.1f}% (avg: {sum(utils)/len(utils):.1f}%)")

    total_time = sum(r["time"] for r in results)
    print(f"\n  Total time: {total_time:.2f}s ({total_time/len(results):.3f}s avg)")

    # Save results
    results_file = OUTPUT_DIR / "gpu_results.json"
    with open(results_file, "w") as f:
        json.dump({
            "metadata": {
                "gpu_available": GPU_AVAILABLE,
                "global_scale": GLOBAL_SCALE,
                "container_width_px": CONTAINER_WIDTH_PX,
                "container_height_px": CONTAINER_HEIGHT_PX,
                "container_width_mm": CONTAINER_WIDTH_MM,
                "step_size": STEP_SIZE,
                "num_combinations": len(TEST_COMBINATIONS)
            },
            "results": results
        }, f, indent=2)

    print(f"\nResults saved to: {results_file}")
    print(f"Visualizations saved to: {visuals_dir}/")

    return results


if __name__ == "__main__":
    run_gpu_experiment()
