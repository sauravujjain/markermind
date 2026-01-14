#!/usr/bin/env python3
"""
GPU Raster Nesting Experiment - V4

Key fixes:
1. Uses SAME piece loading as working CPU/Streamlit app (imports from app.py)
2. Dynamic container length: (total_piece_area * 5) / bin_width
3. Rotation support (0° and 180°)
4. Proper piece buffer via dilation

Usage:
    PYTHONPATH=. python scripts/gpu_raster_experiment_v4.py
"""

import sys
from pathlib import Path
import numpy as np
import time
import json

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

from PIL import Image, ImageDraw, ImageFilter
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

    # FFT of both arrays
    fft_a = np.fft.fft2(a, shape)
    fft_b = np.fft.fft2(b, shape)

    # Multiply and inverse FFT
    result = np.fft.ifft2(fft_a * fft_b).real

    if mode == 'valid':
        # Extract valid region
        start_0 = s2[0] - 1
        start_1 = s2[1] - 1
        end_0 = start_0 + s1[0] - s2[0] + 1
        end_1 = start_1 + s1[1] - s2[1] + 1
        return result[start_0:end_0, start_1:end_1]
    return result

# =============================================================================
# Configuration
# =============================================================================

DXF_PATH = Path("data/24_2506_7_S-AN1000.DXF")
OUTPUT_DIR = Path("experiment_results/gpu_vs_cpu_v4")

FABRIC_WIDTH_MM = 1524.0  # 60 inches (fixed)
GLOBAL_SCALE = 0.3  # pixels per mm (~3.3mm per pixel)
FABRIC_WIDTH_PX = int(FABRIC_WIDTH_MM * GLOBAL_SCALE)

# Container length safety factor (5x for development, can tighten to 3x later)
CONTAINER_LENGTH_SAFETY_FACTOR = 5.0

# Piece buffer in pixels (~3mm buffer between pieces)
PIECE_BUFFER_PX = 1

# Test combinations (same as CPU test)
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
# Rasterization
# =============================================================================

def binary_dilation_numpy(arr, iterations=1):
    """Simple binary dilation using numpy convolution."""
    # 3x3 structuring element (cross pattern)
    kernel = np.array([[0, 1, 0],
                       [1, 1, 1],
                       [0, 1, 0]], dtype=np.float32)

    result = arr.copy()
    for _ in range(iterations):
        # Pad array
        padded = np.pad(result, 1, mode='constant', constant_values=0)
        # Convolve
        from numpy.lib.stride_tricks import sliding_window_view
        windows = sliding_window_view(padded, (3, 3))
        # Any overlap with kernel means dilation
        dilated = np.any(windows * kernel > 0, axis=(2, 3)).astype(np.float32)
        result = dilated

    return result


def rasterize_piece(vertices, scale, buffer_px=1):
    """
    Rasterize a piece polygon to a binary image.

    Args:
        vertices: List of (x, y) coordinates in mm
        scale: Pixels per mm
        buffer_px: Buffer to add around piece (simulates piece_buffer)

    Returns:
        (raster, width_px, height_px)
    """
    # Scale vertices to pixels
    verts_px = [(x * scale, y * scale) for x, y in vertices]

    # Get bounds
    min_x = min(v[0] for v in verts_px)
    min_y = min(v[1] for v in verts_px)
    max_x = max(v[0] for v in verts_px)
    max_y = max(v[1] for v in verts_px)

    # Piece dimensions with padding (extra for dilation)
    w = int(max_x - min_x) + 4 + buffer_px * 2
    h = int(max_y - min_y) + 4 + buffer_px * 2

    # Normalize vertices to local origin with buffer space
    normalized = [(x - min_x + 2 + buffer_px, y - min_y + 2 + buffer_px) for x, y in verts_px]

    # Rasterize using PIL
    img = Image.new('L', (w, h), 0)
    ImageDraw.Draw(img).polygon([(x, y) for x, y in normalized], fill=1)
    raster = np.array(img, dtype=np.float32)

    # Add buffer by dilating the shape
    if buffer_px > 0:
        raster = binary_dilation_numpy(raster, iterations=buffer_px)

    # Return raster and dimensions AFTER dilation
    h_final, w_final = raster.shape
    return raster, w_final, h_final


# =============================================================================
# GPU Packing with FFT
# =============================================================================

def calculate_container_length(pieces_to_place, piece_rasters, bin_width_px):
    """
    Calculate container length based on total piece area.
    Uses 5x safety factor (covers utilization down to 20%).
    """
    total_area_px = 0
    for k in pieces_to_place:
        if k in piece_rasters:
            total_area_px += np.sum(piece_rasters[k][0] > 0)

    if total_area_px == 0:
        return 300  # Minimum floor

    theoretical_min = total_area_px / bin_width_px
    container_length = int(theoretical_min * CONTAINER_LENGTH_SAFETY_FACTOR)

    # Minimum floor
    return max(container_length, 300)


def gpu_pack_fft(piece_rasters, pieces_to_place, width_px, length_px):
    """
    Pack pieces using FFT convolution with rotation support.

    Args:
        piece_rasters: Dict of {piece_key: (raster, w, h)}
        pieces_to_place: List of piece keys to place
        width_px: Container width in pixels (fabric width - FIXED)
        length_px: Container length in pixels (strip length - what we minimize)

    Returns:
        Dict with utilization, strip_length_mm, placements, failed, container
    """
    xp = cp if GPU_AVAILABLE else np
    fftconvolve = gpu_fftconvolve if GPU_AVAILABLE else cpu_fftconvolve

    # Build pieces list with both rotations pre-computed
    all_pieces = []
    for piece_key in pieces_to_place:
        if piece_key not in piece_rasters:
            continue

        raster_0, pw, ph = piece_rasters[piece_key]
        raster_180 = np.rot90(raster_0, 2)  # 180° rotation

        area = float(np.sum(raster_0 > 0))

        all_pieces.append({
            'key': piece_key,
            'area': area,
            'rotations': [
                (raster_0, pw, ph, 0),
                (raster_180, raster_180.shape[1], raster_180.shape[0], 180),
            ]
        })

    # Sort by area descending (First Fit Decreasing)
    all_pieces.sort(key=lambda x: -x['area'])

    # Initialize container: shape = (height, width) = (fabric_width, strip_length)
    container = xp.zeros((width_px, length_px), dtype=xp.float32)

    placements = []
    failed = []
    max_x = 0
    total_area = 0.0

    for piece_info in all_pieces:
        placed = False

        # Try each rotation
        for raster, pw, ph, rotation in piece_info['rotations']:
            # Check if piece fits in container dimensions
            if ph > width_px or pw > length_px:
                continue

            piece = xp.asarray(raster)

            # FFT convolution to find all valid (non-overlapping) positions
            try:
                overlap = fftconvolve(container, piece, mode='valid')
            except Exception as e:
                continue

            # Valid positions have zero overlap
            if GPU_AVAILABLE:
                valid = cp.asnumpy(overlap < 0.5)
            else:
                valid = overlap < 0.5

            valid_y, valid_x = np.where(valid)

            if len(valid_x) == 0:
                continue  # Try next rotation

            # Bottom-left fill: minimize X first (strip length), then Y
            idx = np.lexsort((valid_y, valid_x))[0]
            px, py = int(valid_x[idx]), int(valid_y[idx])

            # Place piece in container
            container[py:py+ph, px:px+pw] = xp.maximum(
                container[py:py+ph, px:px+pw],
                piece
            )

            placements.append({
                'key': piece_info['key'],
                'x_px': px,
                'y_px': py,
                'w_px': pw,
                'h_px': ph,
                'rotation': rotation
            })

            total_area += piece_info['area']
            max_x = max(max_x, px + pw)
            placed = True
            break  # Successfully placed, move to next piece

        if not placed:
            failed.append(piece_info['key'])

    # Calculate results
    if max_x == 0:
        utilization = 0.0
        strip_length_mm = 0.0
    else:
        utilization = (total_area / (width_px * max_x)) * 100
        strip_length_mm = max_x / GLOBAL_SCALE

    # Convert container to numpy for visualization
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
# Visualization
# =============================================================================

def visualize_gpu_result(result, combo, combo_id, output_dir):
    """Create PNG visualization of GPU packing result."""
    container = result['container']
    h, w = container.shape

    fig, ax = plt.subplots(figsize=(14, 5))

    # Create RGB image
    img = np.zeros((h, w, 3), dtype=np.uint8)
    img[:, :] = [40, 40, 40]  # Dark gray background
    img[container > 0] = [100, 150, 200]  # Light blue for pieces

    # Add grid
    for i in range(0, w, 25):
        img[:, i] = [70, 70, 70]
    for i in range(0, h, 25):
        img[i, :] = [70, 70, 70]

    # Mark strip end (red line)
    max_x = result['max_x_px']
    if 0 < max_x < w:
        img[:, min(max_x, w-1)] = [255, 0, 0]

    ax.imshow(img, aspect='auto', origin='lower')

    # Title with stats
    combo_str = ", ".join(f"{s}:{n}" for s, n in sorted(combo.items()))
    placed = len(result['placements'])
    failed = len(result['failed'])
    status = "OK" if failed == 0 else f"FAILED: {failed} pieces"

    ax.set_title(
        f"GPU Raster - Combo {combo_id}: {combo_str}\n"
        f"Utilization: {result['utilization']:.1f}% | "
        f"Strip: {result['strip_length_mm']:.0f}mm | "
        f"{placed} pieces | {status}"
    )
    ax.set_xlabel(f"Strip Length (X) - {max_x}px = {result['strip_length_mm']:.0f}mm")
    ax.set_ylabel(f"Fabric Width (Y) - {h}px = {FABRIC_WIDTH_MM:.0f}mm")

    # Save
    path = output_dir / f"gpu_combo_{combo_id:02d}.png"
    plt.savefig(path, dpi=150, bbox_inches='tight', facecolor='white')
    plt.close()

    return path


# =============================================================================
# Main Experiment
# =============================================================================

def main():
    print("=" * 80)
    print("GPU RASTER EXPERIMENT - V4")
    print("=" * 80)

    # Create output directories
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUTPUT_DIR / "gpu").mkdir(exist_ok=True)

    print(f"\nConfiguration:")
    print(f"  GPU available: {GPU_AVAILABLE}")
    print(f"  Scale: {GLOBAL_SCALE} px/mm ({1/GLOBAL_SCALE:.1f}mm per pixel)")
    print(f"  Fabric width: {FABRIC_WIDTH_PX}px ({FABRIC_WIDTH_MM}mm)")
    print(f"  Container length safety factor: {CONTAINER_LENGTH_SAFETY_FACTOR}x")
    print(f"  Piece buffer: {PIECE_BUFFER_PX}px (~{PIECE_BUFFER_PX/GLOBAL_SCALE:.1f}mm)")

    # Load pieces using SAME function as CPU/Streamlit
    print(f"\nLoading pieces from {DXF_PATH}...")
    pieces, _ = load_pieces_from_dxf(str(DXF_PATH), rotations=[0, 180], allow_flip=True)
    grouped = group_pieces_by_type(pieces)
    print(f"  Loaded {len(pieces)} pieces")
    print(f"  Piece types: {list(grouped.keys())}")

    # Piece config (same as CPU/Streamlit)
    piece_type_config = {}
    for ptype in grouped.keys():
        if ptype == "SL":
            piece_type_config[ptype] = {'demand': 2, 'flipped': True}
        else:
            piece_type_config[ptype] = {'demand': 1, 'flipped': False}

    # Rasterize all unique pieces
    print(f"\nRasterizing pieces...")
    piece_rasters = {}

    for ptype, size_dict in grouped.items():
        for size, piece_list in size_dict.items():
            # Get first piece from the list
            piece = piece_list[0] if isinstance(piece_list, list) else piece_list
            key = f"{ptype}_{size}"
            vertices = list(piece.polygon.vertices)
            raster, w, h = rasterize_piece(vertices, GLOBAL_SCALE, PIECE_BUFFER_PX)
            piece_rasters[key] = (raster, w, h)

            # Report dimensions
            orig_w = max(v[0] for v in vertices) - min(v[0] for v in vertices)
            orig_h = max(v[1] for v in vertices) - min(v[1] for v in vertices)
            print(f"    {key}: {orig_w:.0f}x{orig_h:.0f}mm -> {w}x{h}px")

    # Run experiment
    print(f"\n{'='*80}")
    print(f"Running GPU experiments ({len(TEST_COMBINATIONS)} combinations)...")
    print("=" * 80)

    results = []

    for i, combo in enumerate(TEST_COMBINATIONS):
        combo_str = ", ".join(f"{s}:{n}" for s, n in sorted(combo.items()))

        # Build size quantities (same format as CPU/Streamlit)
        size_quantities = {s: 0 for s in STANDARD_SIZES}
        for size, count in combo.items():
            size_quantities[size] = count

        # Build bundle pieces using SAME function as CPU
        bundle_pieces = build_bundle_pieces(grouped, piece_type_config, size_quantities)

        if not bundle_pieces:
            print(f"  [{i}] {combo_str}: No pieces to place!")
            continue

        # Build list of piece keys to place
        pieces_to_place = []
        for bp in bundle_pieces:
            pname = bp.piece.identifier.piece_name
            size = bp.piece.identifier.size

            # Extract piece type from name - look for BK, FRT, SL in the name
            ptype = None
            pname_upper = pname.upper()
            for pt in ["BK", "FRT", "SL"]:
                if pt in pname_upper:
                    ptype = pt
                    break

            if ptype is None:
                ptype = pname.split('_')[0] if '_' in pname else pname

            key = f"{ptype}_{size}"
            pieces_to_place.append(key)

        expected = len(pieces_to_place)

        # Calculate dynamic container length
        container_length_px = calculate_container_length(
            pieces_to_place, piece_rasters, FABRIC_WIDTH_PX
        )

        # Run GPU packing
        t0 = time.time()
        result = gpu_pack_fft(
            piece_rasters,
            pieces_to_place,
            FABRIC_WIDTH_PX,
            container_length_px
        )
        gpu_time = time.time() - t0

        # Report
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
            print(f"       Failed: {result['failed'][:5]}{'...' if len(result['failed']) > 5 else ''}")

        # Visualize
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
    total_time_ms = sum(r['time_ms'] for r in results)

    print(f"  Success rate: {success_count}/{len(results)}")
    print(f"  Total GPU time: {total_time_ms:.0f}ms ({total_time_ms/len(results):.0f}ms avg)")

    if success_count < len(results):
        print(f"\n  Failed combinations:")
        for r in results:
            if r['failed'] > 0:
                combo_str = ", ".join(f"{s}:{n}" for s, n in sorted(r['combo'].items()))
                print(f"    Combo {r['combo_id']} ({combo_str}): {r['failed_pieces'][:3]}...")

    # Save results
    with open(OUTPUT_DIR / "gpu_results.json", "w") as f:
        json.dump(results, f, indent=2, default=str)

    print(f"\nResults saved to {OUTPUT_DIR}/")
    print(f"  - gpu/*.png (visualizations)")
    print(f"  - gpu_results.json (numerical results)")


if __name__ == "__main__":
    main()
