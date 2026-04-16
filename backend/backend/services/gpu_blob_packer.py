"""
GPU Blob Packer — Marker Length Predictor.

Packs pieces omnidirectionally into a blob (minimizing bounding box area),
then converts blob density → predicted strip marker length.

Uses progressive container that grows with the blob and a CUDA kernel
for parallel collision detection across all candidate positions.
"""

import logging
import time
from typing import Dict, List, Optional, Tuple

import numpy as np

logger = logging.getLogger(__name__)

# GPU Setup - lazy loading to allow import on systems without GPU
_gpu_available = None
cp = None


def _init_gpu():
    """Initialize GPU libraries if available."""
    global _gpu_available, cp
    if _gpu_available is not None:
        return _gpu_available

    try:
        import cupy as _cp
        cp = _cp
        _gpu_available = True
    except ImportError:
        _gpu_available = False
        logger.warning("CuPy not available - GPU blob packer disabled")

    return _gpu_available


class GPUBlobPacker:
    """GPU blob packer for marker length prediction.

    Packs pieces omnidirectionally into a blob (minimizing bounding box),
    then converts blob density to predicted strip marker length.
    Uses progressive container that grows with the blob.
    """

    # Lazy-init: compiled once on first use, shared across all instances
    _kernel = None

    @classmethod
    def _get_kernel(cls):
        if cls._kernel is None:
            cls._kernel = cp.RawKernel(r'''
extern "C" __global__
void blob_valid_positions(
    const float* container,   /* (canvas_size, canvas_size) row-major */
    const float* piece,       /* (ph, pw) flattened */
    int canvas_size,
    int ph, int pw,
    int search_x0, int search_y0,  /* search region origin in canvas coords */
    int num_x, int num_y,          /* search region dimensions */
    int* out_valid                  /* out_valid[ly * num_x + lx] = 1 if no collision */
) {
    int lx = blockDim.x * blockIdx.x + threadIdx.x;
    int ly = blockDim.y * blockIdx.y + threadIdx.y;
    if (lx >= num_x || ly >= num_y) return;

    int cx = lx + search_x0;
    int cy = ly + search_y0;

    /* Bounds check */
    if (cx < 0 || cy < 0 || cx + pw > canvas_size || cy + ph > canvas_size) return;

    for (int py = 0; py < ph; py++) {
        for (int px = 0; px < pw; px++) {
            if (piece[py * pw + px] > 0.5f) {
                if (container[(cy + py) * canvas_size + (cx + px)] > 0.5f) {
                    return;  /* collision */
                }
            }
        }
    }
    out_valid[ly * num_x + lx] = 1;
}
''', 'blob_valid_positions')
        return cls._kernel

    def __init__(self, initial_size: int = 256):
        if not _init_gpu():
            raise RuntimeError("GPU not available")
        self.size = initial_size
        self.container = cp.zeros((initial_size, initial_size), dtype=cp.float32)
        # Blob bounding box in canvas coordinates
        self.bb_min_x = initial_size
        self.bb_max_x = 0
        self.bb_min_y = initial_size
        self.bb_max_y = 0
        self.placed_area = 0.0

    def reset(self):
        self.container.fill(0)
        self.bb_min_x = self.size
        self.bb_max_x = 0
        self.bb_min_y = self.size
        self.bb_max_y = 0
        self.placed_area = 0.0

    def _grow(self, needed_size: int):
        """Double container size until it fits needed_size."""
        while self.size < needed_size:
            new_size = self.size * 2
            new_container = cp.zeros((new_size, new_size), dtype=cp.float32)
            # Center existing content in new container
            offset = (new_size - self.size) // 2
            new_container[offset:offset + self.size,
                          offset:offset + self.size] = self.container
            # Update BB coordinates
            self.bb_min_x += offset
            self.bb_max_x += offset
            self.bb_min_y += offset
            self.bb_max_y += offset
            self.container = new_container
            self.size = new_size

    def place(self, raster, x: int, y: int):
        """Place piece raster at position using element-wise maximum."""
        ph, pw = raster.shape
        self.container[y:y + ph, x:x + pw] = cp.maximum(
            self.container[y:y + ph, x:x + pw], raster)

    def find_best_position(self, raster_gpu, raster_180_gpu):
        """Find best placement minimizing bounding box area.

        Args:
            raster_gpu: CuPy array (ph, pw) for 0° rotation
            raster_180_gpu: CuPy array (ph, pw) for 180° rotation

        Returns:
            (best_pos_dict, best_raster) or (None, None) if no valid position
        """
        ph, pw = raster_gpu.shape
        ph2, pw2 = raster_180_gpu.shape

        # First piece: place at center
        if self.bb_max_x <= self.bb_min_x:
            cx = self.size // 2 - pw // 2
            cy = self.size // 2 - ph // 2
            return {'x': cx, 'y': cy, 'ph': ph, 'pw': pw}, raster_gpu

        # Search region: ring around current blob BB
        margin_x = max(pw, pw2)
        margin_y = max(ph, ph2)
        sx0 = max(0, self.bb_min_x - margin_x)
        sy0 = max(0, self.bb_min_y - margin_y)

        best_pos = None
        best_bb_area = float('inf')
        best_raster = None

        kernel = self._get_kernel()

        for raster, r_ph, r_pw in [
            (raster_gpu, ph, pw),
            (raster_180_gpu, ph2, pw2),
        ]:
            # Upper bounds for search region
            sx1 = min(self.size - r_pw, self.bb_max_x)
            sy1 = min(self.size - r_ph, self.bb_max_y)
            num_x = max(1, sx1 - sx0 + 1)
            num_y = max(1, sy1 - sy0 + 1)

            if num_x <= 0 or num_y <= 0:
                continue

            # Run kernel
            out_valid = cp.zeros(num_x * num_y, dtype=cp.int32)
            piece_flat = raster.ravel()
            if not piece_flat.flags['C_CONTIGUOUS']:
                piece_flat = cp.ascontiguousarray(piece_flat)

            bx, by = 16, 16
            gx = (num_x + bx - 1) // bx
            gy = (num_y + by - 1) // by

            kernel(
                (gx, gy), (bx, by),
                (self.container, piece_flat,
                 np.int32(self.size),
                 np.int32(r_ph), np.int32(r_pw),
                 np.int32(sx0), np.int32(sy0),
                 np.int32(num_x), np.int32(num_y),
                 out_valid)
            )

            # Vectorized BB area computation on GPU
            valid_2d = out_valid.reshape(num_y, num_x)
            if not cp.any(valid_2d > 0):
                continue

            ys_local, xs_local = cp.where(valid_2d > 0)
            xs = xs_local.astype(cp.int64) + sx0
            ys = ys_local.astype(cp.int64) + sy0

            new_min_x = cp.minimum(cp.int64(self.bb_min_x), xs)
            new_max_x = cp.maximum(cp.int64(self.bb_max_x), xs + r_pw)
            new_min_y = cp.minimum(cp.int64(self.bb_min_y), ys)
            new_max_y = cp.maximum(cp.int64(self.bb_max_y), ys + r_ph)
            bb_areas = (new_max_x - new_min_x) * (new_max_y - new_min_y)

            min_idx = int(cp.argmin(bb_areas))
            min_area = float(bb_areas[min_idx])

            if min_area < best_bb_area:
                best_bb_area = min_area
                best_pos = {
                    'x': int(xs[min_idx]),
                    'y': int(ys[min_idx]),
                    'ph': r_ph,
                    'pw': r_pw,
                }
                best_raster = raster

        return best_pos, best_raster


def predict_blob_density(pieces_list: List[Dict], packer: GPUBlobPacker) -> Tuple[float, float]:
    """Pack pieces into blob, return (density, total_area_px).

    Args:
        pieces_list: List of piece dicts with 'raster_gpu', 'raster_180_gpu', 'area' keys
        packer: GPUBlobPacker instance (will be reset)

    Returns:
        (blob_density, total_area_px)
    """
    packer.reset()
    sorted_pieces = sorted(pieces_list, key=lambda p: -p['area'])
    total_area = sum(p['area'] for p in sorted_pieces)

    if total_area == 0:
        return 0.0, 0.0

    for p in sorted_pieces:
        pos, raster = packer.find_best_position(
            p['raster_gpu'], p['raster_180_gpu'])
        if pos is None:
            continue

        # Grow container if needed
        needed = max(pos['x'] + pos['pw'], pos['y'] + pos['ph'])
        if needed > packer.size:
            packer._grow(needed + 64)

        packer.place(raster, pos['x'], pos['y'])

        # Update BB
        packer.bb_min_x = min(packer.bb_min_x, pos['x'])
        packer.bb_max_x = max(packer.bb_max_x, pos['x'] + pos['pw'])
        packer.bb_min_y = min(packer.bb_min_y, pos['y'])
        packer.bb_max_y = max(packer.bb_max_y, pos['y'] + pos['ph'])
        packer.placed_area += p['area']

    bb_w = packer.bb_max_x - packer.bb_min_x
    bb_h = packer.bb_max_y - packer.bb_min_y
    bb_area = bb_w * bb_h
    density = packer.placed_area / bb_area if bb_area > 0 else 0.0
    return density, total_area


def predict_marker_length(
    pieces_list: List[Dict],
    packer: GPUBlobPacker,
    strip_width_px: int,
    gpu_scale: float,
) -> Tuple[float, float]:
    """Predict marker length from blob density.

    Args:
        pieces_list: Expanded piece list for one marker ratio
        packer: GPUBlobPacker instance
        strip_width_px: Strip width in pixels
        gpu_scale: px/mm scale factor

    Returns:
        (blob_density, length_yards)
    """
    density, total_area_px = predict_blob_density(pieces_list, packer)
    if density <= 0:
        return 0.0, 0.0
    length_px = total_area_px / (strip_width_px * density)
    length_yd = length_px / gpu_scale / 25.4 / 36
    return density, length_yd
