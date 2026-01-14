#!/usr/bin/env python3
"""
GPU Raster Nesting Experiment - V8

Building on V7 with:
1. BEAM SEARCH - Explore multiple placement paths instead of greedy
2. GEOMETRIC SCORING - Consider piece concavity and interlocking potential
3. All operations remain parallelizable via batched FFT

V7 baseline: 70.5% utilization
Target: 74%+ utilization (closing gap to CPU's ~78%)

Usage:
    PYTHONPATH=. python scripts/gpu_raster_experiment_v8.py
"""

import sys
from pathlib import Path
import numpy as np
import time
import json
import random
from copy import deepcopy

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
    kernel = np.array([[0, 1, 0], [1, 1, 1], [0, 1, 0]], dtype=np.float32)
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
OUTPUT_DIR = Path("experiment_results/gpu_v8_beam")

FABRIC_WIDTH_MM = 1524.0
GLOBAL_SCALE = 0.5  # 2.0mm per pixel
FABRIC_WIDTH_PX = int(FABRIC_WIDTH_MM * GLOBAL_SCALE)

CONTAINER_LENGTH_MULTIPLIER = 3.0
PIECE_BUFFER_PX = 1

# Scoring weights
CONTACT_WEIGHT = 0.5  # Reward for touching existing pieces
CONCAVITY_WEIGHT = 0.3  # Reward for filling concave regions

# Beam search parameters
BEAM_WIDTH = 3  # Number of parallel paths to explore
TOP_K_POSITIONS = 5  # Top positions to consider per piece

# Gravity refinement
GRAVITY_MAX_SHIFT = 2

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
# Rasterization with Boundary and Concavity
# =============================================================================

def rasterize_piece_with_features(vertices, scale, buffer_px=1):
    """
    Rasterize piece and compute:
    - raster: filled polygon
    - boundary: 1-pixel ring around piece
    - concavity_map: identifies concave regions that could interlock
    """
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

    # Boundary: dilation XOR original
    dilated = binary_dilation_numpy(raster > 0, iterations=1)
    boundary = ((dilated > 0) & ~(raster > 0)).astype(np.float32)

    # Concavity map: convex hull - piece = concave regions nearby
    # Simplified: use bounding box - piece to identify "fillable" space
    bbox_fill = np.ones_like(raster)
    concavity_map = ((bbox_fill > 0) & ~(raster > 0)).astype(np.float32)
    # Weight by distance from piece (closer = more valuable)
    for i in range(3):
        concavity_map = binary_dilation_numpy(concavity_map * 0.7, iterations=1)
    concavity_map = np.clip(concavity_map, 0, 1)

    return raster, boundary, concavity_map, w, h


# =============================================================================
# Beam Search State
# =============================================================================

class BeamState:
    """Represents one path in beam search."""

    def __init__(self, container, placements, total_area, max_x, score):
        self.container = container  # Current container state (GPU array)
        self.placements = placements  # List of placed pieces
        self.total_area = total_area  # Total area of placed pieces
        self.max_x = max_x  # Current strip length
        self.score = score  # Cumulative score (lower = better)

    def copy(self, xp):
        """Create a deep copy for branching."""
        return BeamState(
            container=self.container.copy(),
            placements=list(self.placements),
            total_area=self.total_area,
            max_x=self.max_x,
            score=self.score
        )


# =============================================================================
# Position Finding with Geometric Scoring
# =============================================================================

def find_top_k_positions(container, piece_data, current_max_x, xp, fftconvolve, k=TOP_K_POSITIONS):
    """
    Find top-k placement positions for a piece.

    Returns list of (x, y, rotation, score) tuples.
    """
    container_h, container_w = container.shape
    candidates = []

    for rot_deg, rot_data in piece_data['rotations'].items():
        raster = rot_data['raster']
        boundary = rot_data['boundary']
        concavity = rot_data.get('concavity', np.zeros_like(raster))
        ph, pw = rot_data['shape']

        if ph > container_h or pw > container_w:
            continue

        # Pre-flip for FFT correlation
        raster_flipped = raster[::-1, ::-1].copy()
        boundary_flipped = boundary[::-1, ::-1].copy()
        concavity_flipped = concavity[::-1, ::-1].copy()

        piece_gpu = xp.asarray(raster_flipped)
        boundary_gpu = xp.asarray(boundary_flipped)
        concavity_gpu = xp.asarray(concavity_flipped)

        # Collision detection via FFT
        try:
            overlap = fftconvolve(container, piece_gpu, mode='valid')
        except Exception:
            continue

        valid_mask = overlap < 0.5
        if not bool(xp.any(valid_mask)):
            continue

        result_h, result_w = valid_mask.shape

        # Contact scoring via FFT
        try:
            contact_map = fftconvolve(container, boundary_gpu, mode='valid')
            if contact_map.shape != valid_mask.shape:
                contact_map = xp.zeros((result_h, result_w), dtype=xp.float32)
        except Exception:
            contact_map = xp.zeros((result_h, result_w), dtype=xp.float32)

        # Concavity scoring - how well does this piece fill existing gaps
        # Use inverted container (1 where empty) convolved with piece concavity
        empty_space = 1.0 - container
        try:
            concavity_score_map = fftconvolve(empty_space, concavity_gpu, mode='valid')
            if concavity_score_map.shape != valid_mask.shape:
                concavity_score_map = xp.zeros((result_h, result_w), dtype=xp.float32)
        except Exception:
            concavity_score_map = xp.zeros((result_h, result_w), dtype=xp.float32)

        # Strip extension penalty
        x_coords = xp.arange(result_w, dtype=xp.float32)
        strip_extension = xp.maximum(0, x_coords + pw - current_max_x)
        strip_extension_map = xp.broadcast_to(
            strip_extension[None, :], (result_h, result_w)
        ).copy()

        # Combined score (lower = better)
        score_map = (strip_extension_map
                     - contact_map * CONTACT_WEIGHT
                     - concavity_score_map * CONCAVITY_WEIGHT)
        score_map = xp.where(valid_mask, score_map, xp.float32(1e9))

        # Convert to numpy for top-k selection
        if GPU_AVAILABLE:
            score_map_np = cp.asnumpy(score_map)
            valid_mask_np = cp.asnumpy(valid_mask)
        else:
            score_map_np = score_map
            valid_mask_np = valid_mask

        # Find top-k positions for this rotation
        valid_scores = score_map_np[valid_mask_np]
        valid_indices = np.where(valid_mask_np)

        if len(valid_scores) == 0:
            continue

        # Get indices of top-k lowest scores
        top_k_idx = np.argsort(valid_scores)[:k]

        for idx in top_k_idx:
            y = valid_indices[0][idx]
            x = valid_indices[1][idx]
            score = valid_scores[idx]
            candidates.append((int(x), int(y), rot_deg, float(score), raster, boundary))

    # Sort all candidates by score and return top-k overall
    candidates.sort(key=lambda c: c[3])
    return candidates[:k]


# =============================================================================
# Gravity Refinement
# =============================================================================

def gravity_shake(container, raster, x, y, xp, boundary, max_shift=GRAVITY_MAX_SHIFT):
    """Find better position within ±max_shift pixels."""
    ph, pw = raster.shape
    container_h, container_w = container.shape

    piece_gpu = xp.asarray(raster)

    best_pos = None
    best_score = -float('inf')

    for dx in range(-max_shift, max_shift + 1):
        for dy in range(-max_shift, max_shift + 1):
            nx, ny = x + dx, y + dy

            if nx < 0 or ny < 0:
                continue
            if ny + ph > container_h or nx + pw > container_w:
                continue

            # Strict collision check
            region = container[ny:ny+ph, nx:nx+pw]
            overlap = xp.sum((region > 0.5) & (piece_gpu > 0.5))
            if int(overlap) > 0:
                continue

            # Compute contact
            boundary_gpu = xp.asarray(boundary)
            boundary_region = xp.zeros_like(container)
            boundary_region[ny:ny+ph, nx:nx+pw] = boundary_gpu
            contact = float(xp.sum(boundary_region * container))

            # Score: contact bonus, prefer original position for ties
            score = contact - abs(dx) * 0.01 - abs(dy) * 0.01

            if score > best_score:
                best_score = score
                best_pos = (nx, ny)

    return best_pos if best_pos else (x, y)


# =============================================================================
# Beam Search Placement
# =============================================================================

def beam_search_place(piece_order, width_px, length_px, beam_width=BEAM_WIDTH):
    """
    Place pieces using beam search.

    Maintains beam_width parallel placement paths and returns the best.
    """
    xp = cp if GPU_AVAILABLE else np
    fftconvolve = gpu_fftconvolve if GPU_AVAILABLE else cpu_fftconvolve

    # Initialize beams with empty container
    initial_container = xp.zeros((width_px, length_px), dtype=xp.float32)
    beams = [BeamState(
        container=initial_container.copy(),
        placements=[],
        total_area=0.0,
        max_x=0,
        score=0.0
    )]

    # Place each piece
    for piece_idx, piece_data in enumerate(piece_order):
        new_beams = []

        for beam in beams:
            # Find top-k positions for this piece in this beam's container
            candidates = find_top_k_positions(
                beam.container, piece_data, beam.max_x, xp, fftconvolve, k=TOP_K_POSITIONS
            )

            if not candidates:
                # No valid position - this beam dies (or we could keep it with failed piece)
                continue

            # Create new beams for each candidate position
            for x, y, rotation, pos_score, raster, boundary in candidates:
                # Apply gravity refinement
                x, y = gravity_shake(beam.container, raster, x, y, xp, boundary)

                # Create new beam state
                new_beam = beam.copy(xp)

                # Place piece
                ph, pw = raster.shape
                piece_gpu = xp.asarray(raster)
                new_beam.container[y:y+ph, x:x+pw] = xp.maximum(
                    new_beam.container[y:y+ph, x:x+pw], piece_gpu
                )

                new_beam.placements.append({
                    'key': piece_data['key'],
                    'x_px': x, 'y_px': y,
                    'w_px': pw, 'h_px': ph,
                    'rotation': rotation
                })

                new_beam.total_area += piece_data['area']
                new_beam.max_x = max(new_beam.max_x, x + pw)
                new_beam.score += pos_score

                new_beams.append(new_beam)

        if not new_beams:
            # All beams failed - return best so far
            break

        # Keep top beam_width beams by score
        new_beams.sort(key=lambda b: b.score)
        beams = new_beams[:beam_width]

    # Return best beam
    if not beams:
        return None

    best_beam = min(beams, key=lambda b: -b.total_area / max(b.max_x, 1))

    # Convert container to numpy
    if GPU_AVAILABLE:
        container_np = cp.asnumpy(best_beam.container)
    else:
        container_np = best_beam.container

    # Calculate utilization
    if best_beam.max_x == 0:
        utilization = 0.0
        strip_length_mm = 0.0
    else:
        utilization = (best_beam.total_area / (width_px * best_beam.max_x)) * 100
        strip_length_mm = best_beam.max_x / GLOBAL_SCALE

    return {
        'utilization': utilization,
        'strip_length_mm': strip_length_mm,
        'placements': best_beam.placements,
        'failed': [],
        'max_x_px': best_beam.max_x,
        'container': container_np
    }


# =============================================================================
# Multi-Ordering with Beam Search
# =============================================================================

def pack_with_beam_search(piece_data_list, width_px, length_px):
    """Try multiple orderings with beam search."""
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

    # Heuristic orderings
    orderings.append(sorted(piece_data_list, key=lambda p: -p['area']))
    orderings.append(sorted(piece_data_list, key=lambda p: -p['height']))
    orderings.append(sorted(piece_data_list, key=lambda p: -p['width']))
    orderings.append(sorted(piece_data_list, key=lambda p: -max(p['width'], p['height'])))

    # Fewer random orderings since beam search explores more
    for _ in range(4):
        shuffled = piece_data_list.copy()
        random.shuffle(shuffled)
        orderings.append(shuffled)

    results = []
    for ordering in orderings:
        result = beam_search_place(ordering, width_px, length_px, beam_width=BEAM_WIDTH)
        if result:
            results.append(result)

    if not results:
        return {
            'utilization': 0.0,
            'strip_length_mm': 0.0,
            'placements': [],
            'failed': piece_data_list,
            'max_x_px': 0,
            'container': np.zeros((width_px, 100), dtype=np.float32)
        }

    # Return best result
    return max(results, key=lambda r: r['utilization'])


# =============================================================================
# Container Length Calculation
# =============================================================================

def calculate_container_length(piece_data_list, bin_width_px):
    total_area_px = sum(p['area'] for p in piece_data_list)
    theoretical_min = total_area_px / bin_width_px
    return max(int(theoretical_min * CONTAINER_LENGTH_MULTIPLIER), 200)


# =============================================================================
# Visualization
# =============================================================================

def visualize_result(result, combo, combo_id, output_dir):
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
    fig_width = min(max(fig_height * aspect_ratio, 8), 18)

    fig, ax = plt.subplots(figsize=(fig_width, fig_height))
    ax.imshow(img, aspect='equal', origin='lower')

    combo_str = ", ".join(f"{s}:{n}" for s, n in sorted(combo.items()))
    placed = len(result['placements'])

    ax.set_title(
        f"GPU V8 Beam - Combo {combo_id}: {combo_str}\n"
        f"Utilization: {result['utilization']:.1f}% | "
        f"Strip: {result['strip_length_mm']:.0f}mm | "
        f"{placed} pieces",
        fontsize=10
    )

    plt.tight_layout()
    path = output_dir / f"gpu_combo_{combo_id:02d}.png"
    plt.savefig(path, dpi=150, bbox_inches='tight', facecolor='white')
    plt.close()

    return path


# =============================================================================
# Main
# =============================================================================

def main():
    print("=" * 80)
    print("GPU RASTER EXPERIMENT - V8 (BEAM SEARCH + GEOMETRIC SCORING)")
    print("=" * 80)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUTPUT_DIR / "gpu").mkdir(exist_ok=True)

    print(f"\nConfiguration:")
    print(f"  GPU: {GPU_AVAILABLE}")
    print(f"  Scale: {GLOBAL_SCALE} px/mm ({1/GLOBAL_SCALE:.1f}mm per pixel)")
    print(f"  Beam width: {BEAM_WIDTH}")
    print(f"  Top-k positions: {TOP_K_POSITIONS}")
    print(f"  Contact weight: {CONTACT_WEIGHT}")
    print(f"  Concavity weight: {CONCAVITY_WEIGHT}")

    # Load pieces
    print(f"\nLoading pieces from {DXF_PATH}...")
    pieces, _ = load_pieces_from_dxf(str(DXF_PATH), rotations=[0, 180], allow_flip=True)
    grouped = group_pieces_by_type(pieces)
    print(f"  Loaded {len(pieces)} pieces, types: {list(grouped.keys())}")

    piece_type_config = {}
    for ptype in grouped.keys():
        if ptype == "SL":
            piece_type_config[ptype] = {'demand': 2, 'flipped': True}
        else:
            piece_type_config[ptype] = {'demand': 1, 'flipped': False}

    # Rasterize with features
    print(f"\nRasterizing with geometric features...")
    piece_rasters = {}

    for ptype, size_dict in grouped.items():
        for size, piece_list in size_dict.items():
            piece = piece_list[0] if isinstance(piece_list, list) else piece_list
            key = f"{ptype}_{size}"
            vertices = list(piece.polygon.vertices)

            raster_0, boundary_0, concavity_0, w, h = rasterize_piece_with_features(
                vertices, GLOBAL_SCALE, PIECE_BUFFER_PX
            )

            raster_180 = np.rot90(raster_0, 2)
            boundary_180 = np.rot90(boundary_0, 2)
            concavity_180 = np.rot90(concavity_0, 2)

            area = float(np.sum(raster_0 > 0))
            perimeter = float(np.sum(boundary_0 > 0))

            piece_rasters[key] = {
                'key': key,
                'area': area,
                'perimeter': perimeter,
                'width': w,
                'height': h,
                'rotations': {
                    0: {'raster': raster_0, 'boundary': boundary_0, 'concavity': concavity_0, 'shape': raster_0.shape},
                    180: {'raster': raster_180, 'boundary': boundary_180, 'concavity': concavity_180, 'shape': raster_180.shape},
                }
            }

            orig_w = max(v[0] for v in vertices) - min(v[0] for v in vertices)
            orig_h = max(v[1] for v in vertices) - min(v[1] for v in vertices)
            print(f"    {key}: {orig_w:.0f}x{orig_h:.0f}mm -> {w}x{h}px")

    # Run experiments
    print(f"\n{'='*80}")
    print(f"Running V8 experiments ({len(TEST_COMBINATIONS)} combinations)...")
    print("=" * 80)

    results = []
    total_time = 0

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
            for pt in ["BK", "FRT", "SL"]:
                if pt in pname.upper():
                    ptype = pt
                    break
            if ptype is None:
                ptype = pname.split('_')[0] if '_' in pname else pname

            key = f"{ptype}_{size}"
            if key in piece_rasters:
                piece_data_list.append(piece_rasters[key].copy())

        expected = len(piece_data_list)
        container_length_px = calculate_container_length(piece_data_list, FABRIC_WIDTH_PX)

        t0 = time.time()
        result = pack_with_beam_search(piece_data_list, FABRIC_WIDTH_PX, container_length_px)
        elapsed = time.time() - t0
        total_time += elapsed

        placed = len(result['placements'])
        print(f"  [{i}] {combo_str:<30} {result['utilization']:5.1f}% | "
              f"{result['strip_length_mm']:5.0f}mm | {placed}/{expected} | {elapsed*1000:.0f}ms")

        png_path = visualize_result(result, combo, i, OUTPUT_DIR / "gpu")

        results.append({
            'combo_id': i,
            'combo': combo,
            'utilization': result['utilization'],
            'strip_length_mm': result['strip_length_mm'],
            'placed': placed,
            'expected': expected,
            'time_ms': elapsed * 1000,
            'png': str(png_path)
        })

    # Summary
    print(f"\n{'='*80}")
    print("SUMMARY")
    print("=" * 80)

    avg_util = np.mean([r['utilization'] for r in results])
    print(f"  Average utilization: {avg_util:.1f}%")
    print(f"  Range: {min(r['utilization'] for r in results):.1f}% - {max(r['utilization'] for r in results):.1f}%")
    print(f"  Total time: {total_time*1000:.0f}ms ({total_time*1000/len(results):.0f}ms avg)")

    print(f"\n  Comparison:")
    print(f"    V5 baseline:    71.2%")
    print(f"    V7 baseline:    70.5%")
    print(f"    V8 achieved:    {avg_util:.1f}%")
    print(f"    CPU target:     ~78%")
    print(f"    vs V7:          {avg_util - 70.5:+.1f}%")

    with open(OUTPUT_DIR / "v8_results.json", "w") as f:
        json.dump(results, f, indent=2, default=str)

    print(f"\nResults saved to {OUTPUT_DIR}/")


if __name__ == "__main__":
    main()
