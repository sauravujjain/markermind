#!/usr/bin/env python3
"""
GPU Raster Nesting V9: Strip-Constrained Packing

Tests if GPU strip packing efficiency ranking correlates with CPU strip packing ranking.
Uses fixed width (like CPU solver) and minimizes height to get comparable results.

Key insight: Bounding box packing favors smaller markers, but strip packing
(fixed width, variable height) rewards efficient use of the fixed width.

Usage:
    python scripts/gpu_raster_experiment_v9.py
"""

import json
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Tuple, Any, Optional

import numpy as np
from PIL import Image, ImageDraw

# Try GPU acceleration - CuPy preferred (keeps data on GPU), PyTorch as fallback
PYTORCH_AVAILABLE = False
GPU_AVAILABLE = False

# CuPy is better for this use case - keeps container on GPU between operations
try:
    import cupy as cp
    from cupyx.scipy.signal import fftconvolve as fftconvolve_gpu
    GPU_AVAILABLE = True
    gpu_name = cp.cuda.runtime.getDeviceProperties(0)['name'].decode()
    print(f"CuPy GPU: ENABLED ({gpu_name})")
except ImportError:
    cp = np
    # Try PyTorch as fallback
    try:
        import torch
        import torch.nn.functional as F
        if torch.cuda.is_available():
            PYTORCH_AVAILABLE = True
            torch.backends.cudnn.benchmark = True
            print(f"PyTorch GPU: ENABLED ({torch.cuda.get_device_name(0)})")
    except ImportError:
        pass

if not GPU_AVAILABLE and not PYTORCH_AVAILABLE:
    print("GPU acceleration: DISABLED (using NumPy CPU fallback)")


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


# Global cache for PyTorch tensors to avoid repeated transfers
_pytorch_container_cache = {'data': None, 'tensor': None}

def pytorch_conv2d(container, kernel, mode='valid'):
    """
    Fast 2D convolution using PyTorch on GPU.

    Uses caching to minimize CPU-GPU transfers.
    """
    global _pytorch_container_cache

    # Cache container tensor if same data
    if _pytorch_container_cache['data'] is None or \
       _pytorch_container_cache['data'].shape != container.shape or \
       not np.array_equal(_pytorch_container_cache['data'], container):
        _pytorch_container_cache['data'] = container.copy()
        _pytorch_container_cache['tensor'] = torch.from_numpy(
            container.astype(np.float32)
        ).unsqueeze(0).unsqueeze(0).cuda()

    container_t = _pytorch_container_cache['tensor']
    kernel_t = torch.from_numpy(kernel.astype(np.float32)).unsqueeze(0).unsqueeze(0).cuda()

    with torch.no_grad():
        result = F.conv2d(container_t, kernel_t, padding=0)

    return result.squeeze().cpu().numpy()


def pytorch_conv2d_batch(container, kernels):
    """
    Batch convolution - process multiple kernels at once.

    kernels: list of 2D numpy arrays (different rotations)
    Returns: list of result arrays
    """
    container_t = torch.from_numpy(container.astype(np.float32)).unsqueeze(0).unsqueeze(0).cuda()

    results = []
    for kernel in kernels:
        kernel_t = torch.from_numpy(kernel.astype(np.float32)).unsqueeze(0).unsqueeze(0).cuda()
        result = F.conv2d(container_t, kernel_t, padding=0)
        results.append(result.squeeze().cpu().numpy())

    return results


# Set convolution function based on availability
if PYTORCH_AVAILABLE:
    gpu_convolve = pytorch_conv2d
elif GPU_AVAILABLE:
    gpu_convolve = fftconvolve_gpu
else:
    gpu_convolve = cpu_fftconvolve


def binary_dilation(arr, iterations=1):
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

# =============================================================================
# Configuration
# =============================================================================

DXF_PATH = Path("data/24_2506_7_S-AN1000.DXF")
OUTPUT_DIR = Path("experiment_results/gpu_v9_convex_hull")
RESULTS_DIR = Path("experiment_results")

# Resolution: 0.5 scale = 2mm per pixel
GLOBAL_SCALE = 0.15  # pixels per mm (~7mm per pixel - even faster)

# Piece configuration per garment
PIECE_CONFIG = {
    "BK": {"demand": 1, "flip": False},
    "FRT": {"demand": 1, "flip": False},
    "SL": {"demand": 2, "flip": True},
}

# Strip packing parameters
STRIP_WIDTH_INCH = 60.0  # Fixed width in inches (Y axis)
STRIP_WIDTH_MM = STRIP_WIDTH_INCH * 25.4  # Convert to mm
STRIP_WIDTH_PX = int(STRIP_WIDTH_MM * GLOBAL_SCALE)  # Convert to pixels

# Packing parameters
CONTACT_WEIGHT = 1000.0  # Strong bonus for touching existing pieces
PIECE_BUFFER_PX = 1  # Buffer around pieces

# The 10 combinations to test (same as V7/V8 experiments)
# Format: {size: quantity}
TEST_COMBINATIONS = [
    {"M": 1},                            # 0: 1 garment = 4 pieces
    {"S": 2},                            # 1: 2 garments = 8 pieces
    {"XS": 1, "XXL": 1},                 # 2: 2 garments = 8 pieces
    {"M": 2, "L": 1},                    # 3: 3 garments = 12 pieces
    {"XS": 2, "S": 1, "M": 1},           # 4: 4 garments = 16 pieces
    {"S": 1, "M": 1, "L": 1, "XL": 1},   # 5: 4 garments = 16 pieces
    {"M": 1, "S": 2, "XS": 2, "XXL": 1}, # 6: 7 garments = 28 pieces
    {"L": 2, "XL": 2, "XXL": 1},         # 7: 5 garments = 20 pieces
    {"XS": 3, "S": 2, "M": 1},           # 8: 6 garments = 24 pieces
    {"S": 2, "M": 2, "L": 1, "XL": 1},   # 9: 6 garments = 24 pieces
]

# CPU results from previous experiments (for comparison)
# These should be loaded from existing results or re-run
CPU_RESULTS_FILE = RESULTS_DIR / "gpu_raster_results.json"


# =============================================================================
# DXF Parsing using nesting_engine.io
# =============================================================================

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from nesting_engine.io import load_pieces_from_dxf


def load_piece_vertices(dxf_path: Path) -> Dict[str, Dict[str, List[Tuple[float, float]]]]:
    """
    Load piece vertices from DXF file using nesting_engine parser.
    Returns: {size: {piece_type: [(x,y), ...]}}
    """
    pieces_list, _ = load_pieces_from_dxf(str(dxf_path), rotations=[0, 180], allow_flip=True)

    pieces = {}
    sizes = ["XS", "S", "M", "L", "XL", "XXL", "XXXL"]
    piece_types = ["BK", "FRT", "SL"]

    for piece in pieces_list:
        name = piece.identifier.piece_name.upper()
        size = piece.identifier.size.upper()

        # Normalize size variations
        size_map = {
            "XXXL": "XXL",  # Combine XXXL with XXL
        }
        size = size_map.get(size, size)

        if size not in sizes:
            continue

        # Determine piece type from name
        found_type = None
        for ptype in piece_types:
            if ptype in name:
                found_type = ptype
                break

        if found_type is None:
            continue

        if size not in pieces:
            pieces[size] = {}

        # Only keep one polygon per piece type per size
        if found_type not in pieces[size]:
            vertices = list(piece.polygon.vertices)
            pieces[size][found_type] = vertices

    print(f"Loaded pieces from {dxf_path.name}:")
    for size in sizes:
        if size in pieces:
            types = list(pieces[size].keys())
            print(f"  {size}: {types}")

    return pieces


# =============================================================================
# Rasterization
# =============================================================================

def rasterize_polygon(vertices: List[Tuple[float, float]],
                      scale: float = GLOBAL_SCALE,
                      buffer_px: int = PIECE_BUFFER_PX) -> Tuple[np.ndarray, np.ndarray, float]:
    """
    Convert polygon vertices to binary raster and boundary ring.

    Returns:
        raster: Binary image of piece interior
        boundary: Binary image of 1-pixel boundary ring
        area: Area in pixels
    """
    vertices = np.array(vertices)

    # Scale to pixels
    min_xy = vertices.min(axis=0)
    vertices_shifted = vertices - min_xy
    vertices_scaled = vertices_shifted * scale

    # Add buffer
    vertices_scaled += buffer_px

    # Compute size
    max_xy = vertices_scaled.max(axis=0)
    width = int(np.ceil(max_xy[0])) + buffer_px * 2
    height = int(np.ceil(max_xy[1])) + buffer_px * 2

    # Rasterize
    img = Image.new('L', (width, height), 0)
    draw = ImageDraw.Draw(img)
    draw.polygon([tuple(p) for p in vertices_scaled], fill=1)

    raster = np.array(img, dtype=np.float32)
    area = float(np.sum(raster))

    # Compute boundary ring (dilation XOR original)
    dilated = binary_dilation(raster > 0, iterations=1)
    boundary = ((dilated > 0) & ~(raster > 0)).astype(np.float32)

    return raster, boundary, area


def prepare_pieces_for_combination(piece_vertices: Dict,
                                   combination: Dict[str, int]) -> List[Dict]:
    """
    Prepare all piece rasters for a given size combination.

    Returns:
        List of {'raster': np.array, 'boundary': np.array, 'area': float,
                 'size': str, 'type': str, 'id': str}
    """
    pieces = []
    piece_id = 0

    for size, garment_count in combination.items():
        if garment_count <= 0 or size not in piece_vertices:
            continue

        for _ in range(garment_count):
            for piece_type, config in PIECE_CONFIG.items():
                if piece_type not in piece_vertices[size]:
                    continue

                vertices = piece_vertices[size][piece_type]
                raster, boundary, area = rasterize_polygon(vertices)

                # Add required copies (e.g., 2 sleeves per garment)
                for copy in range(config['demand']):
                    pieces.append({
                        'raster': raster,
                        'boundary': boundary,
                        'area': area,
                        'size': size,
                        'type': piece_type,
                        'id': f"{size}_{piece_type}_{piece_id}_{copy}",
                        'width': raster.shape[1],
                        'height': raster.shape[0]
                    })
            piece_id += 1

    return pieces


# =============================================================================
# Bounding Box Packing Algorithm
# =============================================================================

def find_best_position_blf(container: np.ndarray,
                           piece: np.ndarray,
                           xp, fft_func,
                           strip_width: int,
                           current_length: int = 0) -> Optional[Dict]:
    """
    VECTORIZED two-phase position finding for strip packing.

    Supports PyTorch (fastest), CuPy, or NumPy backends.
    """
    best_inside = None
    best_inside_top = float('inf')
    best_inside_x = float('inf')

    best_extend = None
    best_extend_x = float('inf')
    best_extend_y = float('inf')

    # Ensure container is numpy (handle CuPy arrays)
    if hasattr(container, 'get'):
        container_np = container.get()  # CuPy array
    elif isinstance(container, np.ndarray):
        container_np = container
    else:
        container_np = np.asarray(container)

    for rotation in [0, 180]:
        # Apply rotation
        if rotation == 180:
            rot_piece = np.rot90(piece, 2)
        else:
            rot_piece = piece if isinstance(piece, np.ndarray) else np.asarray(piece)

        ph, pw = rot_piece.shape

        if ph > strip_width:
            continue

        # Collision detection - method depends on backend
        if PYTORCH_AVAILABLE:
            # PyTorch conv2d is cross-correlation - NO flip needed
            overlap_map = pytorch_conv2d(container_np, rot_piece, mode='valid')
        elif GPU_AVAILABLE:
            # CuPy FFT needs pre-flip
            piece_flipped = rot_piece[::-1, ::-1].copy()
            piece_gpu = xp.asarray(piece_flipped)
            overlap_map = fft_func(container, piece_gpu, mode='valid')
            if hasattr(overlap_map, 'get'):
                overlap_map = overlap_map.get()
        else:
            # CPU fallback with FFT
            piece_flipped = rot_piece[::-1, ::-1].copy()
            overlap_map = fft_func(container_np, piece_flipped, mode='valid')

        valid_mask_np = overlap_map < 0.5

        result_h, result_w = valid_mask_np.shape
        max_valid_y = strip_width - ph

        if max_valid_y < 0 or result_h <= 0 or result_w <= 0:
            continue

        # Apply Y constraint
        valid_mask_np[max_valid_y + 1:, :] = False

        if not np.any(valid_mask_np):
            continue

        # VECTORIZED: Find lowest valid Y for each X column
        y_indices = np.arange(result_h).reshape(-1, 1)
        y_grid = np.broadcast_to(y_indices, valid_mask_np.shape).copy()
        y_grid = np.where(valid_mask_np, y_grid, result_h + 1)

        drop_y_per_x = np.min(y_grid, axis=0)
        valid_columns = drop_y_per_x <= max_valid_y

        if not np.any(valid_columns):
            continue

        x_indices = np.arange(result_w)
        piece_top_per_x = drop_y_per_x + ph
        piece_right_per_x = x_indices + pw

        # PHASE 1: Positions within current length
        if current_length > 0:
            inside_mask = valid_columns & (piece_right_per_x <= current_length)
        else:
            inside_mask = valid_columns

        if np.any(inside_mask):
            inside_tops = np.where(inside_mask, piece_top_per_x, float('inf'))
            min_top = np.min(inside_tops)
            min_top_mask = inside_mask & (piece_top_per_x == min_top)
            best_x = np.argmax(min_top_mask)
            best_y = int(drop_y_per_x[best_x])

            if min_top < best_inside_top or (min_top == best_inside_top and best_x < best_inside_x):
                best_inside_top = min_top
                best_inside_x = best_x
                best_inside = {
                    'x': int(best_x),
                    'y': best_y,
                    'rotation': rotation,
                    'piece_shape': (ph, pw)
                }

        # PHASE 2: Positions that extend the strip
        if current_length > 0:
            extend_mask = valid_columns & (piece_right_per_x > current_length)

            if np.any(extend_mask):
                extend_x_indices = np.where(extend_mask)[0]
                best_ext_x = extend_x_indices[0]
                best_ext_y = int(drop_y_per_x[best_ext_x])

                if best_ext_x < best_extend_x or (best_ext_x == best_extend_x and best_ext_y < best_extend_y):
                    best_extend_x = best_ext_x
                    best_extend_y = best_ext_y
                    best_extend = {
                        'x': int(best_ext_x),
                        'y': best_ext_y,
                        'rotation': rotation,
                        'piece_shape': (ph, pw)
                    }

    return best_inside if best_inside is not None else best_extend


def compute_bounding_box_area(container: np.ndarray) -> float:
    """
    Compute bounding box area of placed pieces.

    Args:
        container: Binary container with placed pieces

    Returns:
        Bounding box area in pixels
    """
    ys, xs = np.where(container > 0)

    if len(xs) < 1:
        return 0.0

    width = xs.max() - xs.min()
    height = ys.max() - ys.min()

    return float(width * height)


def get_bounding_box_vertices(container: np.ndarray) -> np.ndarray:
    """
    Get bounding box corners for visualization.
    Returns 4 corners: [bottom-left, bottom-right, top-right, top-left]
    """
    ys, xs = np.where(container > 0)

    if len(xs) < 1:
        return np.array([])

    min_x, max_x = xs.min(), xs.max()
    min_y, max_y = ys.min(), ys.max()

    return np.array([
        [min_x, min_y],
        [max_x, min_y],
        [max_x, max_y],
        [min_x, max_y]
    ])


def get_piece_width(piece_raster: np.ndarray) -> int:
    """Get the width (X dimension) of a piece, considering both rotations."""
    h, w = piece_raster.shape
    # Return the smaller dimension as width (piece can be rotated)
    return min(h, w)


def get_piece_dimensions(piece_raster: np.ndarray) -> Tuple[int, int]:
    """Get (width, height) of piece in optimal orientation for strip packing."""
    h, w = piece_raster.shape
    # For strip packing, we want width (X) to be the smaller dimension
    # so pieces stack vertically in the strip
    if w <= h:
        return w, h  # Already good orientation
    else:
        return h, w  # Would need 90° rotation (but we only have 0/180)


def find_best_pair(pieces: List[Dict], target_height: int) -> Tuple[int, int, int]:
    """
    Find the best pair of pieces whose combined HEIGHT (Y dim) fills strip width.

    For strip packing:
    - Strip width = Y axis (fixed at 60")
    - Strip length = X axis (grows)

    So we want pairs that stack vertically to fill the Y dimension.

    Returns: (idx1, idx2, delta) where delta = |combined_height - target|
    If no good pair, returns (idx1, -1, delta) for single piece placement.
    """
    n = len(pieces)
    if n == 0:
        return -1, -1, float('inf')
    if n == 1:
        h = pieces[0]['raster'].shape[0]  # height (Y dimension)
        return 0, -1, abs(h - target_height)

    best_pair = (0, -1, float('inf'))

    # Get heights (Y dimension) for all pieces
    heights = []
    for p in pieces:
        h, w = p['raster'].shape
        heights.append(h)

    # Find best single piece (for cases where one piece fills height well)
    for i in range(n):
        delta = abs(heights[i] - target_height)
        if delta < best_pair[2]:
            best_pair = (i, -1, delta)

    # Find best pair whose combined height ≈ strip width
    for i in range(n):
        for j in range(i + 1, n):
            combined = heights[i] + heights[j]
            delta = abs(combined - target_height)
            if delta < best_pair[2]:
                best_pair = (i, j, delta)

    return best_pair


def order_pieces_by_width_pairing(pieces: List[Dict], strip_width: int) -> List[Dict]:
    """
    Order pieces by pairing them to fill strip width efficiently.

    Strategy:
    1. Find pairs of pieces whose combined width ≈ strip_width
    2. Order: place paired pieces consecutively
    3. This creates efficient "rows" that fill the strip width
    """
    if not pieces:
        return []

    # Work with a copy
    remaining = list(pieces)
    ordered = []

    while remaining:
        # Find best pair (or single) from remaining pieces
        idx1, idx2, delta = find_best_pair(remaining, strip_width)

        if idx1 < 0:
            break

        if idx2 >= 0:
            # Found a pair - add both (larger first for FFD within pair)
            p1, p2 = remaining[idx1], remaining[idx2]
            if p1['area'] >= p2['area']:
                ordered.extend([p1, p2])
            else:
                ordered.extend([p2, p1])
            # Remove both (remove higher index first to preserve lower index)
            remaining.pop(max(idx1, idx2))
            remaining.pop(min(idx1, idx2))
        else:
            # Single piece
            ordered.append(remaining.pop(idx1))

    return ordered


def pack_strip(pieces: List[Dict]) -> Tuple[float, List[Dict], np.ndarray, float, int]:
    """
    Pack pieces in strip mode using width-pairing + two-phase placement.

    Strategy:
    1. Order pieces by width pairing (pairs that fill strip width)
    2. Place using two-phase: fill gaps first, extend only when needed

    Args:
        pieces: List of piece dictionaries with 'raster', 'boundary', 'area'

    Returns:
        efficiency: piece_area / strip_area (0-1, higher is better)
        placements: List of placement info
        container: Final container state
        strip_area: Strip area (width * length)
        strip_length: Actual strip length used
    """
    xp = cp if GPU_AVAILABLE else np
    fft_func = fftconvolve_gpu if GPU_AVAILABLE else cpu_fftconvolve

    if not pieces:
        return 0.0, [], np.zeros((STRIP_WIDTH_PX, 100)), 0.0, 0

    # Order pieces by width pairing strategy
    sorted_pieces = order_pieces_by_width_pairing(pieces, STRIP_WIDTH_PX)

    # Compute initial strip length estimate
    total_area = sum(p['area'] for p in sorted_pieces)
    initial_length = int((total_area * 2) / STRIP_WIDTH_PX) + 200

    # Initialize container: height = strip_width (Y), width = length (X)
    container = xp.zeros((STRIP_WIDTH_PX, initial_length), dtype=xp.float32)
    placements = []
    placed_area = 0.0
    current_length = 0

    for piece_data in sorted_pieces:
        piece = piece_data['raster']

        # Find best position using two-phase search
        result = find_best_position_blf(
            container, piece, xp, fft_func, STRIP_WIDTH_PX, current_length
        )

        if result is None:
            print(f"  Warning: Could not place piece {piece_data['id']}")
            continue

        # Apply rotation
        x, y, rot = result['x'], result['y'], result['rotation']
        if rot == 180:
            piece_to_place = np.rot90(piece, 2)
        else:
            piece_to_place = piece

        ph, pw = piece_to_place.shape

        # Expand container if needed
        required_length = x + pw + 50
        if required_length > container.shape[1]:
            new_length = required_length + 200
            new_container = xp.zeros((STRIP_WIDTH_PX, new_length), dtype=xp.float32)
            new_container[:, :container.shape[1]] = container
            container = new_container

        # Place piece
        piece_gpu = xp.asarray(piece_to_place)
        container[y:y+ph, x:x+pw] = xp.maximum(container[y:y+ph, x:x+pw], piece_gpu)

        placed_area += piece_data['area']
        placements.append({
            'id': piece_data['id'],
            'x': x, 'y': y,
            'width': pw, 'height': ph,
            'rotation': rot,
            'area': piece_data['area']
        })

        # Update current length
        current_length = max(current_length, x + pw)

    # Convert to numpy
    if GPU_AVAILABLE:
        container_np = xp.asnumpy(container)
    else:
        container_np = container

    # Compute strip area (width * actual length used)
    strip_area = float(STRIP_WIDTH_PX * current_length)

    if strip_area > 0:
        efficiency = placed_area / strip_area
    else:
        efficiency = 0.0

    return efficiency, placements, container_np, strip_area, current_length


def compute_predictive_score(pieces: List[Dict], combination: Dict[str, int]) -> float:
    """
    Compute a predictive score for CPU performance based on piece characteristics.

    Key insights from CPU data:
    - Balanced combinations (1 of each size) = best
    - XXL + small pieces = good (complementary)
    - Too many garments OR all-large = bad
    - L/XL dominance without gap-fillers = worst

    Returns a score where HIGHER = better expected CPU performance.
    """
    if not pieces:
        return 0.0

    sizes_used = [s for s, n in combination.items() if n > 0]
    num_sizes = len(sizes_used)
    total_garments = sum(combination.values())

    # Count by category
    l_xl = sum(n for s, n in combination.items() if s in ['L', 'XL'])
    xxl = combination.get('XXL', 0)
    xs_s = sum(n for s, n in combination.items() if s in ['XS', 'S'])

    # Check if balanced (exactly 1 of each size used)
    all_single = all(n == 1 for s, n in combination.items() if n > 0)

    # Base score - start at 80
    score = 80.0

    # BONUS: Balanced combinations (1 of each size) pack well
    # e.g., Combo 5 (L:1, M:1, S:1, XL:1) = 80.4%
    if all_single and num_sizes >= 2:
        score += 5.0 + num_sizes  # More variety when balanced = better

    # BONUS: XXL + small pieces (complementary shapes)
    # e.g., Combo 6 (M:1, S:2, XS:2, XXL:1) = 81.1%
    if xxl > 0 and xs_s > 0:
        score += 5.0

    # BONUS: Diversity (more sizes = more interlocking options)
    if num_sizes >= 4:
        score += 3.0
    elif num_sizes >= 3:
        score += 2.0

    # PENALTY: All-large combinations (no gap fillers)
    # e.g., Combo 7 (L:2, XL:2, XXL:1) = 66.5%
    if all(s in ['L', 'XL', 'XXL'] for s in sizes_used):
        score -= 18.0

    # PENALTY: L/XL heavy without small pieces
    # e.g., Combo 3 (L:1, M:2) = 65.0%
    l_xl_ratio = l_xl / total_garments if total_garments > 0 else 0
    if l_xl_ratio > 0.3 and xs_s == 0:
        score -= 10.0

    # PENALTY: Too many garments (harder to optimize)
    # BUT only if includes L/XL (small-only combos can handle many pieces)
    # e.g., Combo 9 (6 garments with L/XL) = 65.0%
    # vs Combo 8 (6 garments, small only) = 79.6%
    if total_garments > 5 and l_xl > 0:
        score -= (total_garments - 5) * 5.0

    return score


# =============================================================================
# Visualization
# =============================================================================

def visualize_strip_packing(container: np.ndarray,
                            placements: List[Dict],
                            efficiency: float,
                            strip_length: int,
                            combo_idx: int,
                            combination: Dict,
                            output_path: Path):
    """
    Create visualization of the strip packing.

    Shows:
    - Packed pieces (colored by size)
    - Strip boundary (60" width x variable length)
    - Efficiency metrics
    """
    import matplotlib.pyplot as plt
    import matplotlib.patches as mpatches
    from matplotlib.colors import to_rgba

    # Crop container to actual used area
    # Y axis = width (fixed at 60"), X axis = length (variable)
    cropped = container[:STRIP_WIDTH_PX, :strip_length + 20]

    # Create figure - wider than tall for strip layout
    fig_width = max(12, strip_length / 50)
    fig_height = max(4, STRIP_WIDTH_PX / 100)
    fig, ax = plt.subplots(1, 1, figsize=(fig_width, fig_height))

    # Color map for sizes
    size_colors = {
        'XS': '#FF6B6B',  # Red
        'S': '#4ECDC4',   # Teal
        'M': '#45B7D1',   # Blue
        'L': '#96CEB4',   # Green
        'XL': '#FFEAA7',  # Yellow
        'XXL': '#DDA0DD', # Plum
    }

    # Create colored image
    h, w = cropped.shape
    rgb = np.ones((h, w, 3), dtype=np.float32) * 0.95  # Light gray background

    # Color each piece
    for placement in placements:
        x, y = placement['x'], placement['y']
        pw, ph = placement['width'], placement['height']
        size = placement['id'].split('_')[0]
        color = to_rgba(size_colors.get(size, '#888888'))[:3]

        if 0 <= x < w and 0 <= y < h:
            x_end = min(x + pw, w)
            y_end = min(y + ph, h)

            region = cropped[y:y_end, x:x_end]

            for c in range(3):
                rgb[y:y_end, x:x_end, c] = np.where(
                    region > 0,
                    color[c],
                    rgb[y:y_end, x:x_end, c]
                )

    ax.imshow(rgb, origin='lower', aspect='equal')

    # Draw strip boundary
    strip_rect = np.array([
        [0, 0],
        [strip_length, 0],
        [strip_length, STRIP_WIDTH_PX],
        [0, STRIP_WIDTH_PX],
        [0, 0]
    ])
    ax.plot(strip_rect[:, 0], strip_rect[:, 1], 'r-', linewidth=2, label='Strip Boundary')

    # Title and labels
    combo_str = ', '.join(f"{s}:{n}" for s, n in sorted(combination.items()) if n > 0)
    total_pieces = sum(
        n * sum(PIECE_CONFIG[pt]['demand'] for pt in PIECE_CONFIG)
        for n in combination.values()
    )

    # Convert dimensions to real units
    length_mm = strip_length / GLOBAL_SCALE
    length_inch = length_mm / 25.4

    ax.set_title(f"Combo {combo_idx}: {combo_str}\n"
                 f"Strip Efficiency: {efficiency:.1%} | "
                 f"Pieces: {len(placements)}/{total_pieces} | "
                 f"Length: {length_inch:.1f}\"", fontsize=12)

    # Legend
    legend_patches = [mpatches.Patch(color=color, label=size)
                      for size, color in size_colors.items()
                      if any(p['id'].startswith(size) for p in placements)]
    if legend_patches:
        ax.legend(handles=legend_patches, loc='upper right')

    ax.set_xlabel(f'Strip: {STRIP_WIDTH_INCH:.0f}" width x {length_inch:.1f}" length')
    ax.axis('off')

    plt.tight_layout()
    plt.savefig(output_path, dpi=150, bbox_inches='tight')
    plt.close()


# =============================================================================
# Correlation Analysis
# =============================================================================

def spearman_correlation(x: List[float], y: List[float]) -> Tuple[float, float]:
    """Calculate Spearman rank correlation."""
    x = np.array(x)
    y = np.array(y)
    n = len(x)

    def rankdata(arr):
        sorted_idx = np.argsort(arr)
        ranks = np.empty_like(sorted_idx, dtype=float)
        ranks[sorted_idx] = np.arange(1, len(arr) + 1, dtype=float)
        return ranks

    rank_x = rankdata(x)
    rank_y = rankdata(y)

    d = rank_x - rank_y
    d_sq_sum = np.sum(d ** 2)

    rho = 1 - (6 * d_sq_sum) / (n * (n ** 2 - 1))

    # Approximate p-value
    t_stat = rho * np.sqrt((n - 2) / (1 - rho ** 2 + 1e-10))
    p_approx = 2 * (1 - min(0.9999, abs(t_stat) / (abs(t_stat) + n)))

    return float(rho), float(p_approx)


def load_cpu_results() -> Optional[Dict]:
    """Load CPU results from previous experiments."""
    if CPU_RESULTS_FILE.exists():
        with open(CPU_RESULTS_FILE) as f:
            return json.load(f)
    return None


# =============================================================================
# Main Experiment
# =============================================================================

def run_experiment():
    print("=" * 80)
    print("GPU RASTER V9: STRIP PACKING (60\" WIDTH)")
    print("=" * 80)

    # Setup
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUTPUT_DIR / "packings").mkdir(exist_ok=True)

    print(f"\nConfiguration:")
    print(f"  GPU available: {GPU_AVAILABLE}")
    print(f"  Resolution: {GLOBAL_SCALE} px/mm ({1/GLOBAL_SCALE:.1f}mm per pixel)")
    print(f"  Strip width: {STRIP_WIDTH_INCH}\" ({STRIP_WIDTH_PX} px)")
    print(f"  Contact weight: {CONTACT_WEIGHT}")
    print(f"  Combinations to test: {len(TEST_COMBINATIONS)}")

    # Load DXF
    if not DXF_PATH.exists():
        print(f"\nERROR: DXF file not found at {DXF_PATH}")
        return

    print(f"\n" + "-" * 80)
    piece_vertices = load_piece_vertices(DXF_PATH)

    if not piece_vertices:
        print("ERROR: No pieces loaded from DXF")
        return

    # Run GPU strip packing for each combination
    print(f"\n" + "-" * 80)
    print(f"Running Strip Packing ({STRIP_WIDTH_INCH}\" width, variable length)...")
    print("-" * 80)

    gpu_results = []
    total_start = time.time()

    for idx, combination in enumerate(TEST_COMBINATIONS):
        t0 = time.time()

        # Prepare pieces
        pieces = prepare_pieces_for_combination(piece_vertices, combination)

        if not pieces:
            print(f"  [{idx}] No pieces for combination - skipping")
            gpu_results.append({
                'combo_idx': idx,
                'combination': combination,
                'efficiency': 0.0,
                'strip_area': 0.0,
                'strip_length': 0,
                'time_ms': 0,
                'num_pieces': 0
            })
            continue

        # Pack using strip packing
        efficiency, placements, container, strip_area, strip_length = pack_strip(pieces)

        duration_ms = (time.time() - t0) * 1000

        # Convert length to inches
        length_mm = strip_length / GLOBAL_SCALE
        length_inch = length_mm / 25.4

        # Store result
        gpu_results.append({
            'combo_idx': idx,
            'combination': combination,
            'efficiency': efficiency,
            'strip_area': strip_area,
            'strip_length_px': strip_length,
            'strip_length_inch': length_inch,
            'time_ms': duration_ms,
            'num_pieces': len(placements),
            'total_pieces': len(pieces)
        })

        combo_str = ', '.join(f"{s}:{n}" for s, n in sorted(combination.items()) if n > 0)
        print(f"  [{idx}] {combo_str:<30} -> {efficiency:6.1%} ({duration_ms:6.1f}ms) "
              f"[{len(placements)}/{len(pieces)} pieces] L={length_inch:.1f}\"")

        # Save visualization
        visualize_strip_packing(
            container, placements, efficiency, strip_length,
            idx, combination,
            OUTPUT_DIR / "packings" / f"combo_{idx:02d}_strip.png"
        )

    total_time = time.time() - total_start

    print(f"\nGPU packing complete: {len(TEST_COMBINATIONS)} combinations in {total_time:.2f}s")
    print(f"Average time per combination: {total_time/len(TEST_COMBINATIONS)*1000:.1f}ms")

    # Display results
    print(f"\n" + "=" * 80)
    print("RESULTS - STRIP PACKING EFFICIENCY RANKING")
    print("=" * 80)

    gpu_effs = [r['efficiency'] * 100 for r in gpu_results]

    # Sort by efficiency for ranking display
    sorted_results = sorted(enumerate(gpu_results), key=lambda x: -x[1]['efficiency'])

    print(f"\n{'Rank':<6} {'Combo':<30} {'Efficiency %':<14} {'Length (in)':<12}")
    print("-" * 65)
    for rank, (idx, res) in enumerate(sorted_results, 1):
        combo = res['combination']
        combo_str = ', '.join(f"{s}:{n}" for s, n in sorted(combo.items()) if n > 0)
        eff = res['efficiency'] * 100
        length = res.get('strip_length_inch', 0)
        print(f"#{rank:<5} {combo_str:<30} {eff:<14.1f} {length:<12.1f}")

    # Summary
    print(f"\n" + "=" * 80)
    print("SUMMARY")
    print("=" * 80)

    efficiencies = [r['efficiency'] * 100 for r in gpu_results]
    times = [r['time_ms'] for r in gpu_results]
    lengths = [r.get('strip_length_inch', 0) for r in gpu_results]

    print(f"\nGPU Strip Packing Results ({STRIP_WIDTH_INCH}\" width):")
    print(f"  Efficiency range: {min(efficiencies):.1f}% - {max(efficiencies):.1f}%")
    print(f"  Average efficiency: {np.mean(efficiencies):.1f}%")
    print(f"  Length range: {min(lengths):.1f}\" - {max(lengths):.1f}\"")
    print(f"  Time range: {min(times):.1f}ms - {max(times):.1f}ms")
    print(f"  Average time: {np.mean(times):.1f}ms")

    # Save results
    results_output = {
        'metadata': {
            'timestamp': datetime.now().isoformat(),
            'algorithm': 'V9 Strip Packing',
            'gpu_available': GPU_AVAILABLE,
            'resolution_scale': GLOBAL_SCALE,
            'strip_width_inch': STRIP_WIDTH_INCH,
            'strip_width_px': STRIP_WIDTH_PX,
            'contact_weight': CONTACT_WEIGHT,
        },
        'gpu_results': gpu_results,
        'summary': {
            'efficiency_min': min(efficiencies),
            'efficiency_max': max(efficiencies),
            'efficiency_avg': np.mean(efficiencies),
        },
        'timing': {
            'total_seconds': total_time,
            'avg_ms_per_combo': total_time / len(TEST_COMBINATIONS) * 1000
        }
    }

    output_file = OUTPUT_DIR / "v9_results.json"
    with open(output_file, 'w') as f:
        json.dump(results_output, f, indent=2, default=str)

    print(f"\nResults saved to: {output_file}")
    print(f"Visualizations saved to: {OUTPUT_DIR}/packings/")


if __name__ == '__main__':
    run_experiment()
