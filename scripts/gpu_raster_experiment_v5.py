#!/usr/bin/env python3
"""
GPU Raster Nesting Experiment - V5

Multi-sequence approach with gravity refinement.

Key Components:
1. Multi-sequence selection (14 orderings: 6 heuristic + 8 random)
2. Parallel contact scoring via FFT boundary convolution
3. Gravity refinement - shake pieces ±2px to snap into gaps
4. Container = (total_area * 3) / bin_width

Results show multi-sequence outperforms batch-rank:
- Multi-sequence: 71.1% average utilization
- Batch-rank: 63.4% average utilization

Usage:
    PYTHONPATH=. python scripts/gpu_raster_experiment_v5.py
"""

import sys
from pathlib import Path
import numpy as np
import time
import json
import random

# Add project root and apps directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "apps"))

# Import the SAME functions as working CPU test
from app import (
    group_pieces_by_type,
    build_bundle_pieces,
    STANDARD_SIZES,
)

# Import DXF loading from nesting_engine (same as app.py does)
from nesting_engine.io import load_pieces_from_dxf

from PIL import Image, ImageDraw
import matplotlib.pyplot as plt

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
    print("GPU acceleration: DISABLED (using CPU fallback)")


def cpu_fftconvolve(a, b, mode='full'):
    """FFT-based convolution using pure numpy (scipy fallback)."""
    s1 = np.array(a.shape)
    s2 = np.array(b.shape)
    shape = s1 + s2 - 1

    fft_a = np.fft.fft2(a, shape)
    fft_b = np.fft.fft2(b, shape)
    result = np.fft.ifft2(fft_a * fft_b).real

    if mode == 'valid':
        start_0 = s2[0] - 1
        start_1 = s2[1] - 1
        end_0 = start_0 + s1[0] - s2[0] + 1
        end_1 = start_1 + s1[1] - s2[1] + 1
        return result[start_0:end_0, start_1:end_1]
    return result


def binary_dilation_numpy(arr, iterations=1):
    """Simple binary dilation using numpy (scipy fallback)."""
    from numpy.lib.stride_tricks import sliding_window_view

    kernel = np.array([[0, 1, 0],
                       [1, 1, 1],
                       [0, 1, 0]], dtype=np.float32)

    result = arr.astype(np.float32).copy()
    for _ in range(iterations):
        padded = np.pad(result, 1, mode='constant', constant_values=0)
        windows = sliding_window_view(padded, (3, 3))
        dilated = np.any(windows * kernel > 0, axis=(2, 3)).astype(np.float32)
        result = dilated

    return result


# =============================================================================
# Configuration
# =============================================================================

DXF_PATH = Path("data/24_2506_7_S-AN1000.DXF")
OUTPUT_DIR = Path("experiment_results/gpu_vs_cpu_v5")

FABRIC_WIDTH_MM = 1524.0  # 60 inches (fixed)
GLOBAL_SCALE = 0.3  # pixels per mm (~3.3mm per pixel)
FABRIC_WIDTH_PX = int(FABRIC_WIDTH_MM * GLOBAL_SCALE)

# Container length: (total_area * 3) / bin_width
CONTAINER_LENGTH_MULTIPLIER = 3.0

# Piece buffer in pixels (~3mm buffer)
PIECE_BUFFER_PX = 1

# Contact weight: balance strip minimization vs gap filling
# 0.3 keeps strip minimization primary while rewarding good contact
CONTACT_WEIGHT = 0.3

# Gravity refinement: max shift in pixels
GRAVITY_MAX_SHIFT = 2

# Multi-sequence settings
N_RANDOM_SEQUENCES = 8

# Test combinations
TEST_COMBINATIONS = [
    {"M": 1},
    {"S": 2},
    {"XS": 1, "XXL": 1},
    {"M": 2, "L": 1},
    {"XS": 2, "S": 1, "M": 1},
    {"S": 1, "M": 1, "L": 1, "XL": 1},
    {"M": 1, "S": 2, "XS": 2, "XXL": 1},
    {"L": 2, "XL": 2, "XXL": 1},
    {"XS": 3, "S": 2, "M": 1},
    {"S": 2, "M": 2, "L": 1, "XL": 1},
]


# =============================================================================
# Rasterization with Boundary Computation
# =============================================================================

def rasterize_piece_with_boundary(vertices, scale, buffer_px=1):
    """
    Rasterize a piece polygon and compute its boundary mask.

    Boundary = dilation XOR original (1-pixel ring around piece)
    Used for contact scoring via convolution.
    """
    verts_px = [(x * scale, y * scale) for x, y in vertices]

    min_x = min(v[0] for v in verts_px)
    min_y = min(v[1] for v in verts_px)
    max_x = max(v[0] for v in verts_px)
    max_y = max(v[1] for v in verts_px)

    w = int(max_x - min_x) + 4 + buffer_px * 2
    h = int(max_y - min_y) + 4 + buffer_px * 2

    normalized = [(x - min_x + 2 + buffer_px, y - min_y + 2 + buffer_px) for x, y in verts_px]

    # Rasterize piece
    img = Image.new('L', (w, h), 0)
    ImageDraw.Draw(img).polygon([(x, y) for x, y in normalized], fill=1)
    raster = np.array(img, dtype=np.float32)

    # Add buffer by dilating
    if buffer_px > 0:
        raster = binary_dilation_numpy(raster, iterations=buffer_px)

    # Compute boundary: dilation XOR original
    dilated = binary_dilation_numpy(raster > 0, iterations=1)
    boundary = ((dilated > 0) & ~(raster > 0)).astype(np.float32)

    return raster, boundary, raster.shape[1], raster.shape[0]


# =============================================================================
# Gravity Refinement
# =============================================================================

def gravity_shake(container, raster, x, y, xp, boundary, max_shift=2):
    """
    After initial placement, try shifting ±max_shift pixels.
    Keep the position that maximizes contact with existing pieces.

    This "snaps" pieces into gaps, eliminating pixel-level waste.
    """
    ph, pw = raster.shape
    container_h, container_w = container.shape

    piece_gpu = xp.asarray(raster)
    boundary_gpu = xp.asarray(boundary)

    best_pos = (x, y)
    best_contact = -float('inf')

    for dx in range(-max_shift, max_shift + 1):
        for dy in range(-max_shift, max_shift + 1):
            nx, ny = x + dx, y + dy

            # Bounds check
            if nx < 0 or ny < 0:
                continue
            if ny + ph > container_h or nx + pw > container_w:
                continue

            # Check for collision (must be valid position)
            region = container[ny:ny+ph, nx:nx+pw]
            if bool(xp.any((region > 0) & (piece_gpu > 0))):
                continue

            # Compute contact at this position
            boundary_region = xp.zeros_like(container)
            boundary_region[ny:ny+ph, nx:nx+pw] = boundary_gpu
            contact = float(xp.sum(boundary_region * container))

            # Prefer higher contact, tie-break with smaller x
            if contact > best_contact or (contact == best_contact and nx < best_pos[0]):
                best_contact = contact
                best_pos = (nx, ny)

    return best_pos


# =============================================================================
# Single Piece Placement with Parallel Scoring
# =============================================================================

def find_best_placement_parallel(container, piece_data, current_max_x, xp, fftconvolve):
    """
    Find best placement for a piece using fully parallel GPU operations.
    """
    container_h, container_w = container.shape
    best_placement = None
    best_score = float('inf')

    for rot_deg, rot_data in piece_data['rotations'].items():
        raster = rot_data['raster']
        boundary = rot_data['boundary']
        ph, pw = rot_data['shape']

        if ph > container_h or pw > container_w:
            continue

        piece_gpu = xp.asarray(raster)
        boundary_gpu = xp.asarray(boundary)

        # Find all valid positions via FFT convolution
        try:
            overlap = fftconvolve(container, piece_gpu, mode='valid')
        except Exception:
            continue

        valid_mask = overlap < 0.5

        if not bool(xp.any(valid_mask)):
            continue

        result_h, result_w = valid_mask.shape

        # Compute contact score for ALL positions via FFT
        try:
            contact_map = fftconvolve(container, boundary_gpu, mode='valid')
            if contact_map.shape != valid_mask.shape:
                min_h = min(contact_map.shape[0], result_h)
                min_w = min(contact_map.shape[1], result_w)
                temp = xp.zeros((result_h, result_w), dtype=xp.float32)
                temp[:min_h, :min_w] = contact_map[:min_h, :min_w]
                contact_map = temp
        except Exception:
            contact_map = xp.zeros((result_h, result_w), dtype=xp.float32)

        # Compute strip extension for ALL positions
        x_coords = xp.arange(result_w, dtype=xp.float32)
        strip_extension = xp.maximum(0, x_coords + pw - current_max_x)
        strip_extension_map = xp.broadcast_to(
            strip_extension[None, :], (result_h, result_w)
        ).copy()

        # Combined score (lower = better)
        score_map = strip_extension_map - contact_map * CONTACT_WEIGHT
        score_map = xp.where(valid_mask, score_map, xp.float32(1e9))

        # Find best position
        flat_idx = int(xp.argmin(score_map))
        best_y = flat_idx // result_w
        best_x = flat_idx % result_w
        score = float(score_map[best_y, best_x])

        if score < best_score:
            best_score = score
            best_placement = (int(best_x), int(best_y), pw, ph, rot_deg, raster, boundary)

    return best_placement


# =============================================================================
# Single Sequence Packing with Gravity
# =============================================================================

def pack_single_sequence(piece_order, width_px, length_px):
    """
    Pack pieces in a specific order using parallel contact scoring + gravity.
    """
    xp = cp if GPU_AVAILABLE else np
    fftconvolve = gpu_fftconvolve if GPU_AVAILABLE else cpu_fftconvolve

    container = xp.zeros((width_px, length_px), dtype=xp.float32)

    placements = []
    failed = []
    max_x = 0
    total_area = 0.0

    for piece_data in piece_order:
        placement = find_best_placement_parallel(
            container, piece_data, max_x, xp, fftconvolve
        )

        if placement is None:
            failed.append(piece_data['key'])
            continue

        x, y, pw, ph, rotation, raster, boundary = placement

        # Gravity refinement: try shifting ±2px to maximize contact
        x, y = gravity_shake(
            container, raster, x, y, xp, boundary, max_shift=GRAVITY_MAX_SHIFT
        )

        # Place the piece
        piece_gpu = xp.asarray(raster)
        container[y:y+ph, x:x+pw] = xp.maximum(
            container[y:y+ph, x:x+pw], piece_gpu
        )

        placements.append({
            'key': piece_data['key'],
            'x_px': x, 'y_px': y,
            'w_px': pw, 'h_px': ph,
            'rotation': rotation
        })

        total_area += piece_data['area']
        max_x = max(max_x, x + pw)

    # Calculate utilization
    if max_x == 0:
        utilization = 0.0
        strip_length_mm = 0.0
    else:
        utilization = (total_area / (width_px * max_x)) * 100
        strip_length_mm = max_x / GLOBAL_SCALE

    if GPU_AVAILABLE:
        container = cp.asnumpy(container)

    return {
        'utilization': utilization,
        'strip_length_mm': strip_length_mm,
        'placements': placements,
        'failed': failed,
        'max_x_px': max_x,
        'container': container
    }


# =============================================================================
# Multi-Sequence Packing
# =============================================================================

def gpu_pack_multi_sequence(piece_data_list, width_px, length_px):
    """
    Try multiple piece orderings and return the best result.

    Orderings:
    1-6. Heuristic sorts (area, height, width, perimeter, compactness, max-dim)
    7-N. Random shuffles
    """
    if not piece_data_list:
        return {
            'utilization': 0.0,
            'strip_length_mm': 0.0,
            'placements': [],
            'failed': [],
            'max_x_px': 0,
            'container': np.zeros((width_px, 100), dtype=np.float32)
        }

    orderings = []

    # 1. Sort by area (descending) - classic FFD
    orderings.append(sorted(piece_data_list, key=lambda p: -p['area']))

    # 2. Sort by height (descending)
    orderings.append(sorted(piece_data_list, key=lambda p: -p['height']))

    # 3. Sort by width (descending)
    orderings.append(sorted(piece_data_list, key=lambda p: -p['width']))

    # 4. Sort by perimeter (descending)
    orderings.append(sorted(piece_data_list, key=lambda p: -p['perimeter']))

    # 5. Sort by compactness (area/perimeter ratio)
    orderings.append(sorted(
        piece_data_list,
        key=lambda p: -p['area'] / max(p['perimeter'], 1)
    ))

    # 6. Sort by max dimension
    orderings.append(sorted(
        piece_data_list,
        key=lambda p: -max(p['width'], p['height'])
    ))

    # 7-N. Random shuffles
    for _ in range(N_RANDOM_SEQUENCES):
        shuffled = piece_data_list.copy()
        random.shuffle(shuffled)
        orderings.append(shuffled)

    # Run all orderings
    results = []
    for ordering in orderings:
        result = pack_single_sequence(ordering, width_px, length_px)
        results.append(result)

    # Return best (prefer no failures, then highest utilization)
    successful = [r for r in results if len(r['failed']) == 0]

    if successful:
        best = max(successful, key=lambda r: r['utilization'])
    else:
        best = min(results, key=lambda r: (len(r['failed']), -r['utilization']))

    return best


# =============================================================================
# Container Length Calculation
# =============================================================================

def calculate_container_length(piece_data_list, bin_width_px):
    """
    Container length = (total_area * 3) / bin_width
    """
    total_area_px = sum(p['area'] for p in piece_data_list)
    theoretical_min = total_area_px / bin_width_px
    container_length = int(theoretical_min * CONTAINER_LENGTH_MULTIPLIER)
    return max(container_length, 200)


# =============================================================================
# Visualization
# =============================================================================

def visualize_gpu_result(result, combo, combo_id, output_dir):
    """
    Create PNG with correct aspect ratio, truncated to strip length.
    """
    container = result['container']
    max_x = result['max_x_px']

    margin_px = 30
    truncate_x = min(max_x + margin_px, container.shape[1])
    container_truncated = container[:, :truncate_x]

    h, w = container_truncated.shape

    img = np.zeros((h, w, 3), dtype=np.uint8)
    img[:, :] = [40, 40, 40]
    img[container_truncated > 0] = [100, 150, 200]

    for i in range(0, w, 25):
        img[:, i] = [70, 70, 70]
    for i in range(0, h, 25):
        img[i, :] = [70, 70, 70]

    if 0 < max_x < w:
        img[:, max_x] = [255, 0, 0]

    aspect_ratio = w / h
    fig_height = 6
    fig_width = min(fig_height * aspect_ratio, 18)
    fig_width = max(fig_width, 8)

    fig, ax = plt.subplots(figsize=(fig_width, fig_height))
    ax.imshow(img, aspect='equal', origin='lower')

    combo_str = ", ".join(f"{s}:{n}" for s, n in sorted(combo.items()))
    placed = len(result['placements'])
    failed = len(result['failed'])
    status = "OK" if failed == 0 else f"FAILED: {failed}"

    ax.set_title(
        f"GPU V5 - Combo {combo_id}: {combo_str}\n"
        f"Utilization: {result['utilization']:.1f}% | "
        f"Strip: {result['strip_length_mm']:.0f}mm | "
        f"{placed} pieces | {status}",
        fontsize=11
    )

    ax.set_xlabel(f"Strip Length: {result['strip_length_mm']:.0f}mm")
    ax.set_ylabel(f"Fabric Width: {h/GLOBAL_SCALE:.0f}mm")

    plt.tight_layout()

    path = output_dir / f"gpu_combo_{combo_id:02d}.png"
    plt.savefig(path, dpi=150, bbox_inches='tight', facecolor='white')
    plt.close()

    return path


# =============================================================================
# Main Experiment
# =============================================================================

def main():
    print("=" * 80)
    print("GPU RASTER EXPERIMENT - V5")
    print("Multi-sequence + Boundary Convolution + Gravity Refinement")
    print("=" * 80)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUTPUT_DIR / "gpu").mkdir(exist_ok=True)

    print(f"\nConfiguration:")
    print(f"  GPU available: {GPU_AVAILABLE}")
    print(f"  Scale: {GLOBAL_SCALE} px/mm ({1/GLOBAL_SCALE:.1f}mm per pixel)")
    print(f"  Fabric width: {FABRIC_WIDTH_PX}px ({FABRIC_WIDTH_MM}mm)")
    print(f"  Container multiplier: {CONTAINER_LENGTH_MULTIPLIER}x")
    print(f"  Contact weight: {CONTACT_WEIGHT}")
    print(f"  Gravity max shift: +/-{GRAVITY_MAX_SHIFT}px")
    print(f"  Sequences: {6 + N_RANDOM_SEQUENCES} (6 heuristic + {N_RANDOM_SEQUENCES} random)")

    # Load pieces
    print(f"\nLoading pieces from {DXF_PATH}...")
    pieces, _ = load_pieces_from_dxf(str(DXF_PATH), rotations=[0, 180], allow_flip=True)
    grouped = group_pieces_by_type(pieces)
    print(f"  Loaded {len(pieces)} pieces")
    print(f"  Piece types: {list(grouped.keys())}")

    # Piece config
    piece_type_config = {}
    for ptype in grouped.keys():
        if ptype == "SL":
            piece_type_config[ptype] = {'demand': 2, 'flipped': True}
        else:
            piece_type_config[ptype] = {'demand': 1, 'flipped': False}

    # Pre-rasterize all unique pieces
    print(f"\nRasterizing pieces with boundary computation...")
    piece_rasters = {}

    for ptype, size_dict in grouped.items():
        for size, piece_list in size_dict.items():
            piece = piece_list[0] if isinstance(piece_list, list) else piece_list
            key = f"{ptype}_{size}"
            vertices = list(piece.polygon.vertices)

            raster_0, boundary_0, w, h = rasterize_piece_with_boundary(
                vertices, GLOBAL_SCALE, PIECE_BUFFER_PX
            )

            raster_180 = np.rot90(raster_0, 2)
            boundary_180 = np.rot90(boundary_0, 2)

            area = float(np.sum(raster_0 > 0))
            perimeter = float(np.sum(boundary_0 > 0))

            piece_rasters[key] = {
                'key': key,
                'area': area,
                'perimeter': perimeter,
                'width': w,
                'height': h,
                'rotations': {
                    0: {
                        'raster': raster_0,
                        'boundary': boundary_0,
                        'shape': raster_0.shape,
                    },
                    180: {
                        'raster': raster_180,
                        'boundary': boundary_180,
                        'shape': raster_180.shape,
                    },
                }
            }

            orig_w = max(v[0] for v in vertices) - min(v[0] for v in vertices)
            orig_h = max(v[1] for v in vertices) - min(v[1] for v in vertices)
            print(f"    {key}: {orig_w:.0f}x{orig_h:.0f}mm -> {w}x{h}px, boundary={perimeter:.0f}px")

    # Run experiment
    print(f"\n{'='*80}")
    print(f"Running GPU experiments ({len(TEST_COMBINATIONS)} combinations)...")
    print("=" * 80)

    results = []
    total_gpu_time = 0

    for i, combo in enumerate(TEST_COMBINATIONS):
        combo_str = ", ".join(f"{s}:{n}" for s, n in sorted(combo.items()))

        size_quantities = {s: 0 for s in STANDARD_SIZES}
        for size, count in combo.items():
            size_quantities[size] = count

        bundle_pieces = build_bundle_pieces(grouped, piece_type_config, size_quantities)

        if not bundle_pieces:
            print(f"  [{i}] {combo_str}: No pieces!")
            continue

        piece_data_list = []
        for bp in bundle_pieces:
            pname = bp.piece.identifier.piece_name
            size = bp.piece.identifier.size

            ptype = None
            pname_upper = pname.upper()
            for pt in ["BK", "FRT", "SL"]:
                if pt in pname_upper:
                    ptype = pt
                    break
            if ptype is None:
                ptype = pname.split('_')[0] if '_' in pname else pname

            key = f"{ptype}_{size}"

            if key in piece_rasters:
                piece_data_list.append(piece_rasters[key].copy())

        expected = len(piece_data_list)

        container_length_px = calculate_container_length(
            piece_data_list, FABRIC_WIDTH_PX
        )

        t0 = time.time()
        result = gpu_pack_multi_sequence(
            piece_data_list,
            FABRIC_WIDTH_PX,
            container_length_px
        )
        gpu_time = time.time() - t0
        total_gpu_time += gpu_time

        placed = len(result['placements'])
        failed = len(result['failed'])

        status = "OK" if failed == 0 else f"FAILED: {failed}"
        print(
            f"  [{i}] {combo_str:<30} "
            f"{result['utilization']:5.1f}% | "
            f"{result['strip_length_mm']:5.0f}mm | "
            f"{placed}/{expected} pcs | "
            f"{status} | "
            f"{gpu_time*1000:.0f}ms"
        )

        if failed > 0:
            print(f"       Failed: {result['failed']}")

        png_path = visualize_gpu_result(result, combo, i, OUTPUT_DIR / "gpu")

        results.append({
            'combo_id': i,
            'combo': combo,
            'utilization': result['utilization'],
            'strip_length_mm': result['strip_length_mm'],
            'placed': placed,
            'expected': expected,
            'failed': failed,
            'failed_pieces': result['failed'],
            'time_ms': gpu_time * 1000,
            'png': str(png_path)
        })

    # Summary
    print(f"\n{'='*80}")
    print("SUMMARY")
    print("=" * 80)

    success_count = sum(1 for r in results if r['failed'] == 0)
    avg_util = np.mean([r['utilization'] for r in results])
    avg_time = total_gpu_time * 1000 / len(results)

    print(f"  Success rate: {success_count}/{len(results)}")
    print(f"  Average utilization: {avg_util:.1f}%")
    print(f"  Utilization range: {min(r['utilization'] for r in results):.1f}% - {max(r['utilization'] for r in results):.1f}%")
    print(f"  Total GPU time: {total_gpu_time*1000:.0f}ms ({avg_time:.0f}ms avg)")

    print(f"\n  Performance comparison:")
    print(f"    V4 baseline:          ~68-70% utilization")
    print(f"    V5 achieved:          {avg_util:.1f}% utilization")
    print(f"    CPU Spyrrow target:   ~78% utilization")
    print(f"    Improvement vs V4:    +{avg_util - 69:.1f}%")

    with open(OUTPUT_DIR / "gpu_results.json", "w") as f:
        json.dump(results, f, indent=2, default=str)

    print(f"\nResults saved to {OUTPUT_DIR}/")
    print(f"  - gpu/*.png (visualizations)")
    print(f"  - gpu_results.json")


if __name__ == "__main__":
    main()
