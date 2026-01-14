#!/usr/bin/env python3
"""
GPU Raster Nesting Experiment - V6 (Energy Minimization)

Building on V5's working raster NFP implementation:
1. Higher resolution (0.5 scale = 2mm/pixel instead of 3.3mm/pixel)
2. Energy minimization via gradient descent after initial placement
3. Distance transform for attraction toward existing pieces
4. Sliding refinement that finds tight nooks

Goal: Improve from V5's 71% to 75-76% (narrowing gap to CPU's ~78%)

Usage:
    PYTHONPATH=. python scripts/gpu_raster_experiment_v6.py
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

from app import (
    group_pieces_by_type,
    build_bundle_pieces,
    STANDARD_SIZES,
)
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


# =============================================================================
# Numpy fallbacks for scipy functions
# =============================================================================

def cpu_fftconvolve(a, b, mode='full'):
    """FFT-based convolution using pure numpy."""
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
    """Simple binary dilation using numpy."""
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


def distance_transform_edt_fast(binary_image, max_distance=30):
    """
    Fast vectorized Chamfer distance transform using GPU if available.
    Computes approximate Euclidean distance from each 1-pixel to nearest 0-pixel.
    Capped at max_distance for performance (we only care about nearby distances).
    """
    xp = cp if GPU_AVAILABLE else np

    h, w = binary_image.shape
    INF = float(max_distance + 10)

    # Convert to GPU array if available
    if GPU_AVAILABLE and not isinstance(binary_image, cp.ndarray):
        binary_gpu = cp.asarray(binary_image)
    else:
        binary_gpu = binary_image

    dist = xp.where(binary_gpu > 0, xp.float32(INF), xp.float32(0))

    # Chamfer weights
    d1 = xp.float32(1.0)    # orthogonal
    d2 = xp.float32(1.414)  # diagonal

    # Limited iterations - max_distance propagation steps is enough
    for iteration in range(max_distance):
        # Create padded version for neighbor access
        padded = xp.pad(dist, 1, mode='constant', constant_values=INF)

        # All 8 neighbors at once
        new_dist = xp.minimum(dist, padded[:-2, 1:-1] + d1)    # top
        new_dist = xp.minimum(new_dist, padded[2:, 1:-1] + d1)   # bottom
        new_dist = xp.minimum(new_dist, padded[1:-1, :-2] + d1)  # left
        new_dist = xp.minimum(new_dist, padded[1:-1, 2:] + d1)   # right
        new_dist = xp.minimum(new_dist, padded[:-2, :-2] + d2)   # top-left
        new_dist = xp.minimum(new_dist, padded[:-2, 2:] + d2)    # top-right
        new_dist = xp.minimum(new_dist, padded[2:, :-2] + d2)    # bottom-left
        new_dist = xp.minimum(new_dist, padded[2:, 2:] + d2)     # bottom-right

        # Check convergence
        if xp.array_equal(new_dist, dist):
            break
        dist = new_dist

    # Convert back to numpy if needed
    if GPU_AVAILABLE:
        return cp.asnumpy(dist)
    return dist


# =============================================================================
# Configuration
# =============================================================================

DXF_PATH = Path("data/24_2506_7_S-AN1000.DXF")
OUTPUT_DIR = Path("experiment_results/gpu_v6_energy")

FABRIC_WIDTH_MM = 1524.0  # 60 inches (fixed)
GLOBAL_SCALE = 0.5  # 2.0mm per pixel (was 0.3 = 3.3mm/pixel)
FABRIC_WIDTH_PX = int(FABRIC_WIDTH_MM * GLOBAL_SCALE)

# Container sizing
CONTAINER_LENGTH_MULTIPLIER = 3.0

# Piece buffer in pixels
PIECE_BUFFER_PX = 1

# FFT-based scoring
CONTACT_WEIGHT = 2.0

# Energy minimization parameters
ENERGY_MAX_ITERATIONS = 20
ENERGY_DISTANCE_WEIGHT = 0.5
ENERGY_CONTACT_WEIGHT = 2.0
ENERGY_NEIGHBOR_MODE = '8-way'  # '4-way' or '8-way'

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
    """Rasterize a piece polygon and compute its boundary mask."""
    verts_px = [(x * scale, y * scale) for x, y in vertices]

    min_x = min(v[0] for v in verts_px)
    min_y = min(v[1] for v in verts_px)
    max_x = max(v[0] for v in verts_px)
    max_y = max(v[1] for v in verts_px)

    w = int(max_x - min_x) + 4 + buffer_px * 2
    h = int(max_y - min_y) + 4 + buffer_px * 2

    normalized = [(x - min_x + 2 + buffer_px, y - min_y + 2 + buffer_px) for x, y in verts_px]

    img = Image.new('L', (w, h), 0)
    ImageDraw.Draw(img).polygon([(x, y) for x, y in normalized], fill=1)
    raster = np.array(img, dtype=np.float32)

    if buffer_px > 0:
        raster = binary_dilation_numpy(raster, iterations=buffer_px)

    dilated = binary_dilation_numpy(raster > 0, iterations=1)
    boundary = ((dilated > 0) & ~(raster > 0)).astype(np.float32)

    return raster, boundary, raster.shape[1], raster.shape[0]


# =============================================================================
# Distance Transform Field
# =============================================================================

def compute_distance_field(container):
    """
    Compute Euclidean distance from each empty pixel to nearest piece.

    Returns distance_field where:
    - 0 where container is occupied
    - distance to nearest occupied pixel elsewhere
    """
    # 1 where empty, 0 where occupied
    # EDT computes distance from 1-pixels to nearest 0-pixel
    empty_mask = (container < 0.5).astype(np.float32)

    distance_field = distance_transform_edt_fast(empty_mask, max_distance=30)

    return distance_field


# =============================================================================
# Energy Function
# =============================================================================

def compute_energy_at_position(x, y, piece_raster, boundary, container, distance_field):
    """
    Compute energy for placing piece at position (x, y).

    Energy = distance_penalty - contact_reward
    Lower energy = better position
    """
    ph, pw = piece_raster.shape
    ch, cw = container.shape

    # Bounds check
    if x < 0 or y < 0 or x + pw > cw or y + ph > ch:
        return float('inf'), False

    # Extract regions
    container_region = container[y:y+ph, x:x+pw]
    distance_region = distance_field[y:y+ph, x:x+pw]

    # Collision check
    overlap = np.sum(container_region * piece_raster)
    if overlap > 0.5:
        return float('inf'), False

    # Distance penalty: average distance of piece pixels to existing pieces
    piece_mask = piece_raster > 0.5
    if np.sum(piece_mask) > 0:
        distance_penalty = np.mean(distance_region[piece_mask])
    else:
        distance_penalty = 0

    # Contact reward: boundary pixels touching existing pieces
    contact_reward = np.sum(container_region * boundary)

    # Combined energy
    energy = (ENERGY_DISTANCE_WEIGHT * distance_penalty
              - ENERGY_CONTACT_WEIGHT * contact_reward)

    return energy, True


# =============================================================================
# Gradient Descent Refinement
# =============================================================================

def gradient_descent_refine(x, y, piece_raster, boundary, container, distance_field,
                            max_iterations=ENERGY_MAX_ITERATIONS):
    """
    Refine placement using gradient descent on energy function.
    """
    # Define neighbors
    if ENERGY_NEIGHBOR_MODE == '8-way':
        neighbors = [(-1, -1), (-1, 0), (-1, 1),
                     (0, -1),           (0, 1),
                     (1, -1),  (1, 0),  (1, 1)]
    else:
        neighbors = [(-1, 0), (1, 0), (0, -1), (0, 1)]

    current_x, current_y = int(x), int(y)
    current_energy, is_valid = compute_energy_at_position(
        current_x, current_y, piece_raster, boundary, container, distance_field
    )

    if not is_valid:
        return x, y, []

    energy_history = [current_energy]

    for _ in range(max_iterations):
        improved = False
        best_neighbor = None
        best_energy = current_energy

        for dx, dy in neighbors:
            nx, ny = current_x + dx, current_y + dy

            neighbor_energy, is_valid = compute_energy_at_position(
                nx, ny, piece_raster, boundary, container, distance_field
            )

            if is_valid and neighbor_energy < best_energy:
                best_energy = neighbor_energy
                best_neighbor = (nx, ny)
                improved = True

        if improved:
            current_x, current_y = best_neighbor
            current_energy = best_energy
            energy_history.append(current_energy)
        else:
            break

    return current_x, current_y, energy_history


# =============================================================================
# Placement with Energy Refinement
# =============================================================================

def find_best_placement_with_energy(container, piece_data, current_max_x, xp, fftconvolve):
    """
    Find best placement using FFT + energy minimization refinement.
    """
    container_h, container_w = container.shape
    best_placement = None
    best_score = float('inf')

    # Convert container to numpy for distance field computation
    if GPU_AVAILABLE:
        container_np = cp.asnumpy(container)
    else:
        container_np = container

    # Compute distance field once per placement
    distance_field = compute_distance_field(container_np)

    for rot_deg, rot_data in piece_data['rotations'].items():
        raster = rot_data['raster']
        boundary = rot_data['boundary']
        ph, pw = rot_data['shape']

        if ph > container_h or pw > container_w:
            continue

        # Flip piece for FFT convolution (convolution flips kernel, so pre-flip for correlation)
        raster_flipped = raster[::-1, ::-1].copy()
        boundary_flipped = boundary[::-1, ::-1].copy()

        piece_gpu = xp.asarray(raster_flipped)
        boundary_gpu = xp.asarray(boundary_flipped)

        # FFT collision detection (now properly correlates due to pre-flip)
        try:
            overlap = fftconvolve(container, piece_gpu, mode='valid')
        except Exception:
            continue

        valid_mask = overlap < 0.5

        if not bool(xp.any(valid_mask)):
            continue

        result_h, result_w = valid_mask.shape

        # FFT contact scoring
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

        # Strip extension map
        x_coords = xp.arange(result_w, dtype=xp.float32)
        strip_extension = xp.maximum(0, x_coords + pw - current_max_x)
        strip_extension_map = xp.broadcast_to(
            strip_extension[None, :], (result_h, result_w)
        ).copy()

        # Combined FFT score
        score_map = strip_extension_map - contact_map * CONTACT_WEIGHT
        score_map = xp.where(valid_mask, score_map, xp.float32(1e9))

        # Find initial candidate from FFT
        if GPU_AVAILABLE:
            score_map_np = cp.asnumpy(score_map)
        else:
            score_map_np = score_map

        flat_idx = int(np.argmin(score_map_np))
        init_y = flat_idx // result_w
        init_x = flat_idx % result_w

        # Energy minimization refinement
        final_x, final_y, energy_history = gradient_descent_refine(
            init_x, init_y, raster, boundary, container_np, distance_field
        )

        # Compute final score
        strip_ext = max(0, final_x + pw - current_max_x)

        # Get contact at final position
        if 0 <= final_y < result_h and 0 <= final_x < result_w:
            if GPU_AVAILABLE:
                contact_np = cp.asnumpy(contact_map)
            else:
                contact_np = contact_map
            final_contact = contact_np[final_y, final_x] if final_y < contact_np.shape[0] and final_x < contact_np.shape[1] else 0
        else:
            final_contact = 0

        final_score = strip_ext - final_contact * CONTACT_WEIGHT

        if final_score < best_score:
            best_score = final_score
            best_placement = (int(final_x), int(final_y), pw, ph, rot_deg, raster, boundary, energy_history)

    return best_placement


# =============================================================================
# Single Sequence Packing
# =============================================================================

def pack_single_sequence_v6(piece_order, width_px, length_px):
    """Pack pieces using V6 algorithm with energy refinement."""
    xp = cp if GPU_AVAILABLE else np
    fftconvolve = gpu_fftconvolve if GPU_AVAILABLE else cpu_fftconvolve

    container = xp.zeros((width_px, length_px), dtype=xp.float32)

    placements = []
    failed = []
    max_x = 0
    total_area = 0.0
    total_descent_steps = 0

    for piece_data in piece_order:
        placement = find_best_placement_with_energy(
            container, piece_data, max_x, xp, fftconvolve
        )

        if placement is None:
            failed.append(piece_data['key'])
            continue

        x, y, pw, ph, rotation, raster, boundary, energy_history = placement
        total_descent_steps += len(energy_history)

        # Place the piece
        piece_gpu = xp.asarray(raster)
        container[y:y+ph, x:x+pw] = xp.maximum(
            container[y:y+ph, x:x+pw], piece_gpu
        )

        placements.append({
            'key': piece_data['key'],
            'x_px': x, 'y_px': y,
            'w_px': pw, 'h_px': ph,
            'rotation': rotation,
            'descent_steps': len(energy_history)
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
        'container': container,
        'total_descent_steps': total_descent_steps
    }


# =============================================================================
# Multi-Sequence Packing
# =============================================================================

def gpu_pack_multi_sequence_v6(piece_data_list, width_px, length_px):
    """Try multiple orderings and return the best result."""
    if not piece_data_list:
        return {
            'utilization': 0.0,
            'strip_length_mm': 0.0,
            'placements': [],
            'failed': [],
            'max_x_px': 0,
            'container': np.zeros((width_px, 100), dtype=np.float32),
            'total_descent_steps': 0
        }

    orderings = []

    # Heuristic orderings
    orderings.append(sorted(piece_data_list, key=lambda p: -p['area']))
    orderings.append(sorted(piece_data_list, key=lambda p: -p['height']))
    orderings.append(sorted(piece_data_list, key=lambda p: -p['width']))
    orderings.append(sorted(piece_data_list, key=lambda p: -p['perimeter']))
    orderings.append(sorted(piece_data_list, key=lambda p: -p['area'] / max(p['perimeter'], 1)))
    orderings.append(sorted(piece_data_list, key=lambda p: -max(p['width'], p['height'])))

    # Random orderings
    for _ in range(N_RANDOM_SEQUENCES):
        shuffled = piece_data_list.copy()
        random.shuffle(shuffled)
        orderings.append(shuffled)

    results = []
    for ordering in orderings:
        result = pack_single_sequence_v6(ordering, width_px, length_px)
        results.append(result)

    # Return best
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
    """Container length = (total_area * 3) / bin_width"""
    total_area_px = sum(p['area'] for p in piece_data_list)
    theoretical_min = total_area_px / bin_width_px
    container_length = int(theoretical_min * CONTAINER_LENGTH_MULTIPLIER)
    return max(container_length, 200)


# =============================================================================
# Visualization
# =============================================================================

def visualize_result(result, combo, combo_id, output_dir):
    """Create PNG visualization."""
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
    descent = result.get('total_descent_steps', 0)

    ax.set_title(
        f"GPU V6 Energy - Combo {combo_id}: {combo_str}\n"
        f"Utilization: {result['utilization']:.1f}% | "
        f"Strip: {result['strip_length_mm']:.0f}mm | "
        f"{placed} pieces | {status} | {descent} descent steps",
        fontsize=10
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
    print("GPU RASTER EXPERIMENT - V6 (ENERGY MINIMIZATION)")
    print("Higher resolution + Distance transform + Gradient descent")
    print("=" * 80)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUTPUT_DIR / "gpu").mkdir(exist_ok=True)

    print(f"\nConfiguration:")
    print(f"  GPU available: {GPU_AVAILABLE}")
    print(f"  Scale: {GLOBAL_SCALE} px/mm ({1/GLOBAL_SCALE:.1f}mm per pixel)")
    print(f"  Fabric width: {FABRIC_WIDTH_PX}px ({FABRIC_WIDTH_MM}mm)")
    print(f"  Container multiplier: {CONTAINER_LENGTH_MULTIPLIER}x")
    print(f"  Contact weight (FFT): {CONTACT_WEIGHT}")
    print(f"  Energy max iterations: {ENERGY_MAX_ITERATIONS}")
    print(f"  Energy distance weight: {ENERGY_DISTANCE_WEIGHT}")
    print(f"  Energy contact weight: {ENERGY_CONTACT_WEIGHT}")
    print(f"  Neighbor mode: {ENERGY_NEIGHBOR_MODE}")
    print(f"  Sequences: {6 + N_RANDOM_SEQUENCES}")

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
    print(f"\nRasterizing pieces at {GLOBAL_SCALE} scale...")
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
            print(f"    {key}: {orig_w:.0f}x{orig_h:.0f}mm -> {w}x{h}px")

    # Run experiment
    print(f"\n{'='*80}")
    print(f"Running GPU V6 experiments ({len(TEST_COMBINATIONS)} combinations)...")
    print("=" * 80)

    results = []
    total_gpu_time = 0
    total_descent_steps = 0

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
        result = gpu_pack_multi_sequence_v6(
            piece_data_list,
            FABRIC_WIDTH_PX,
            container_length_px
        )
        gpu_time = time.time() - t0
        total_gpu_time += gpu_time
        total_descent_steps += result.get('total_descent_steps', 0)

        placed = len(result['placements'])
        failed = len(result['failed'])
        descent = result.get('total_descent_steps', 0)

        status = "OK" if failed == 0 else f"FAILED: {failed}"
        print(
            f"  [{i}] {combo_str:<30} "
            f"{result['utilization']:5.1f}% | "
            f"{result['strip_length_mm']:5.0f}mm | "
            f"{placed}/{expected} pcs | "
            f"{status} | "
            f"{descent} desc | "
            f"{gpu_time*1000:.0f}ms"
        )

        if failed > 0:
            print(f"       Failed: {result['failed']}")

        png_path = visualize_result(result, combo, i, OUTPUT_DIR / "gpu")

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
            'descent_steps': descent,
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
    print(f"  Total descent steps: {total_descent_steps}")

    print(f"\n  Performance comparison:")
    print(f"    V5 baseline:          71.2% utilization")
    print(f"    V6 achieved:          {avg_util:.1f}% utilization")
    print(f"    CPU Spyrrow target:   ~78% utilization")
    print(f"    Improvement vs V5:    {avg_util - 71.2:+.1f}%")

    with open(OUTPUT_DIR / "v6_results.json", "w") as f:
        json.dump(results, f, indent=2, default=str)

    print(f"\nResults saved to {OUTPUT_DIR}/")
    print(f"  - gpu/*.png (visualizations)")
    print(f"  - v6_results.json")


if __name__ == "__main__":
    main()
