"""
GPU Nesting Runner - Parameterized GPU nesting algorithm.

Extracted and parameterized from scripts/gpu_20260118_ga_ratio_optimizer.py
for integration into the MarkerMind backend services.
"""

import math
import sys
import time
import io
import base64
import logging
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Dict, List, Tuple, Callable, Optional
from itertools import combinations_with_replacement

import numpy as np
from PIL import Image, ImageDraw

logger = logging.getLogger(__name__)

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
DEFAULT_GPU_SCALE = 0.08  # px/mm — validated: 0.9993 Spearman rho vs 0.15, 2.5x faster
DEFAULT_PIECE_BUFFER = 0  # pixels
DEFAULT_EDGE_BUFFER = 0

# Adaptive nesting parameters
WHOLE_ORDER_THRESHOLD = 1500   # Brute-force ALL BCs when total ratios ≤ this
LHS_SAMPLE_RATE = 0.12         # 12% LHS sampling rate per BC in adaptive mode
LHS_SAMPLE_MIN = 50            # Floor: never sample fewer than this
LHS_SAMPLE_MAX = 500           # Cap: never sample more than this per BC
BASE_MAX_BC = 12               # Explore up to this BC in Phase 1 (adaptive sampling)
SEED_COUNT = 30                # Top diverse seeds per productive BC for multiplier expansion
PLATEAU_DELTA = 0.003          # Efficiency improvement threshold (0.3%) for plateau detection
NEIGHBORHOOD_TOP_N = 100       # Top multiplied candidates to perturb in Phase 4
CROSS_WIDTH_SAMPLE_RATE = 0.10   # 10% of bc=3+ base results sampled per extra width
CROSS_WIDTH_SAMPLE_MIN = 15      # Floor: never fewer than this
CROSS_WIDTH_SAMPLE_MAX = 100     # Cap: never more than this
CROSS_WIDTH_TOP_N_VERIFY = 25    # GPU-verify top-N predicted ratios per extra width


class GPUPacker:
    """GPU-accelerated strip packer using CUDA RawKernel direct collision detection.

    Replaces FFT convolution with a custom CUDA 3D kernel (X, Y, Rotation)
    that does direct per-pixel overlap checking with atomicMin gravity-drop.
    Achieves ~1.7-2.5x speedup over FFT with bit-exact identical results.

    See docs/gpu_nesting.md "CUDA RawKernel Packer" section for algorithm details.
    """

    ACTIVE_WIDTH_PADDING = 50

    # Lazy-init: compiled once on first use, shared across all instances
    _kernel_3d = None

    @classmethod
    def _get_kernel(cls):
        if cls._kernel_3d is None:
            cls._kernel_3d = cp.RawKernel(r'''
extern "C" __global__
void blf_overlap_3d(
    const float* container,   /* (strip_width, container_len) row-major */
    const float* piece0,      /* rotation 0: (ph, pw) flattened */
    const float* piece1,      /* rotation 1: (ph, pw) flattened */
    int container_len,
    int ph,
    int pw,
    int num_x,
    int num_y,
    int* out_y                /* out_y[rot * num_x + x] = lowest valid y */
) {
    int x   = blockDim.x * blockIdx.x + threadIdx.x;
    int y   = blockDim.y * blockIdx.y + threadIdx.y;
    int rot = blockIdx.z;   /* 0 or 1 */

    if (x >= num_x || y >= num_y) return;

    const float* piece = (rot == 0) ? piece0 : piece1;

    for (int py = 0; py < ph; py++) {
        for (int px = 0; px < pw; px++) {
            float pval = piece[py * pw + px];
            if (pval > 0.5f) {
                float cval = container[(y + py) * container_len + (x + px)];
                if (cval > 0.5f) {
                    return;
                }
            }
        }
    }

    atomicMin(&out_y[rot * num_x + x], y);
}
''', 'blf_overlap_3d')
        return cls._kernel_3d

    def __init__(self, strip_width: int, max_length: int):
        import numpy as _np
        self._np = _np
        if not _init_gpu():
            raise RuntimeError("GPU not available")

        self.strip_width = strip_width
        self.max_length = max_length
        self.container = cp.zeros((strip_width, max_length), dtype=cp.float32)
        self._out_y = cp.empty((2 * max_length,), dtype=cp.int32)

    def reset(self):
        self.container.fill(0)

    def find_best_position(self, raster_gpu, raster_180_gpu, current_length):
        """Find best placement using 3D CUDA kernel (X, Y, Rotation in one launch)."""
        ph, pw = raster_gpu.shape
        if ph > self.strip_width:
            return None, None

        max_valid_y = self.strip_width - ph
        if max_valid_y < 0:
            return None, None
        num_y = max_valid_y + 1

        # Scope active width
        if current_length > 0:
            active_width = min(current_length + pw + self.ACTIVE_WIDTH_PADDING,
                               self.max_length)
        else:
            active_width = min(pw + self.ACTIVE_WIDTH_PADDING, self.max_length)

        num_x = active_width - pw + 1
        if num_x <= 0:
            return None, None

        # Flatten both rotations
        p0 = raster_gpu.ravel()
        p1 = raster_180_gpu.ravel()
        if not p0.flags['C_CONTIGUOUS']:
            p0 = cp.ascontiguousarray(p0)
        if not p1.flags['C_CONTIGUOUS']:
            p1 = cp.ascontiguousarray(p1)

        # Init output for both rotations
        self._out_y[:2 * num_x] = 0x7FFFFFFF

        # 3D grid: (X-blocks, Y-blocks, 2-rotations)
        bx, by = 16, 16
        gx = (num_x + bx - 1) // bx
        gy = (num_y + by - 1) // by

        kernel = self._get_kernel()
        kernel(
            (gx, gy, 2), (bx, by, 1),
            (self.container, p0, p1,
             self._np.int32(self.max_length),
             self._np.int32(ph), self._np.int32(pw),
             self._np.int32(num_x), self._np.int32(num_y),
             self._out_y)
        )

        # Position selection across both rotations
        out_y_both = self._out_y[:2 * num_x].reshape(2, num_x)
        x_idx = cp.arange(num_x, dtype=cp.int32)
        piece_right = x_idx + pw
        sentinel = cp.int32(0x7FFFFFFF)

        best = None
        best_raster = None
        rasters = [raster_gpu, raster_180_gpu]

        for rot in range(2):
            drop_y = out_y_both[rot]
            valid_mask = drop_y < sentinel
            valid_count = int(cp.sum(valid_mask))
            if valid_count == 0:
                continue

            piece_top = drop_y + ph

            if current_length > 0:
                inside = valid_mask & (piece_right <= current_length)
            else:
                inside = valid_mask

            inside_count = int(cp.sum(inside))
            if inside_count > 0:
                tops = cp.where(inside, piece_top, cp.int32(999999))
                min_idx = int(cp.argmin(tops))
                bx_pos = min_idx
                by_pos = int(drop_y[bx_pos])

                if best is None or (bx_pos + pw <= current_length and
                                    (best['x'] + best['pw'] > current_length)):
                    best = {'x': bx_pos, 'y': by_pos, 'ph': ph, 'pw': pw}
                    best_raster = rasters[rot]

            elif current_length > 0:
                extend = valid_mask & (piece_right > current_length)
                extend_count = int(cp.sum(extend))
                if extend_count > 0:
                    ext_idx = int(cp.argmax(extend))
                    ext_x = ext_idx
                    ext_y = int(drop_y[ext_x])
                    if best is None:
                        best = {'x': ext_x, 'y': ext_y, 'ph': ph, 'pw': pw}
                        best_raster = rasters[rot]

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

    # ------------------------------------------------------------------
    # Module 3: Gap-Fill Position Scoring
    # ------------------------------------------------------------------

    def find_gap_fill_position(self, raster_gpu, raster_180_gpu, current_length):
        """Find best gap-filling position for a small piece.

        Reuses EXACT same CUDA kernel as find_best_position() — no new CUDA code.
        Different scoring: maximize x (fill rightmost gap first), then minimize y.
        Standard BLF: minimize y, then x.  Gap-fill: inverts priority for SMALL pieces.

        Falls back to extend-strip if no inside positions.
        """
        ph, pw = raster_gpu.shape
        if ph > self.strip_width:
            return None, None

        max_valid_y = self.strip_width - ph
        if max_valid_y < 0:
            return None, None
        num_y = max_valid_y + 1

        # Scope active width
        if current_length > 0:
            active_width = min(current_length + pw + self.ACTIVE_WIDTH_PADDING,
                               self.max_length)
        else:
            active_width = min(pw + self.ACTIVE_WIDTH_PADDING, self.max_length)

        num_x = active_width - pw + 1
        if num_x <= 0:
            return None, None

        # Flatten both rotations
        p0 = raster_gpu.ravel()
        p1 = raster_180_gpu.ravel()
        if not p0.flags['C_CONTIGUOUS']:
            p0 = cp.ascontiguousarray(p0)
        if not p1.flags['C_CONTIGUOUS']:
            p1 = cp.ascontiguousarray(p1)

        # Init output for both rotations
        self._out_y[:2 * num_x] = 0x7FFFFFFF

        # 3D grid: (X-blocks, Y-blocks, 2-rotations)
        bx, by = 16, 16
        gx = (num_x + bx - 1) // bx
        gy = (num_y + by - 1) // by

        kernel = self._get_kernel()
        kernel(
            (gx, gy, 2), (bx, by, 1),
            (self.container, p0, p1,
             self._np.int32(self.max_length),
             self._np.int32(ph), self._np.int32(pw),
             self._np.int32(num_x), self._np.int32(num_y),
             self._out_y)
        )

        # Gap-fill scoring: maximize x (fill rightmost gap), then minimize y
        out_y_both = self._out_y[:2 * num_x].reshape(2, num_x)
        x_idx = cp.arange(num_x, dtype=cp.int32)
        piece_right = x_idx + pw
        sentinel = cp.int32(0x7FFFFFFF)

        best = None
        best_raster = None
        rasters = [raster_gpu, raster_180_gpu]

        for rot in range(2):
            drop_y = out_y_both[rot]
            valid_mask = drop_y < sentinel
            valid_count = int(cp.sum(valid_mask))
            if valid_count == 0:
                continue

            if current_length > 0:
                inside = valid_mask & (piece_right <= current_length)
            else:
                inside = valid_mask

            inside_count = int(cp.sum(inside))
            if inside_count > 0:
                # Gap-fill: score = x * (strip_width + 1) - y
                # This maximizes x first, then minimizes y as tiebreaker
                scores = cp.where(
                    inside,
                    x_idx * (self.strip_width + 1) - drop_y,
                    cp.int32(-0x7FFFFFFF),
                )
                best_idx = int(cp.argmax(scores))
                bx_pos = best_idx
                by_pos = int(drop_y[bx_pos])

                if best is None or (bx_pos + pw <= current_length and
                                    (best['x'] + best['pw'] > current_length)):
                    best = {'x': bx_pos, 'y': by_pos, 'ph': ph, 'pw': pw}
                    best_raster = rasters[rot]

            elif current_length > 0:
                extend = valid_mask & (piece_right > current_length)
                extend_count = int(cp.sum(extend))
                if extend_count > 0:
                    ext_idx = int(cp.argmax(extend))
                    ext_x = ext_idx
                    ext_y = int(drop_y[ext_x])
                    if best is None:
                        best = {'x': ext_x, 'y': ext_y, 'ph': ph, 'pw': pw}
                        best_raster = rasters[rot]

        return best, best_raster

    # ------------------------------------------------------------------
    # Module 4: Gravity Compaction
    # ------------------------------------------------------------------

    def remove_piece(self, raster, x, y):
        """Remove a piece from the container (subtract + clamp to 0)."""
        ph, pw = raster.shape
        self.container[y:y+ph, x:x+pw] = cp.maximum(
            self.container[y:y+ph, x:x+pw] - raster, 0
        )


def _gravity_compact(packer, placements, strip_width_px, current_length, n_passes=1):
    """Post-placement compaction. For each piece (y ascending):
    1. Remove from container (subtract + clamp to 0)
    2. find_best_position() → new lowest valid position
    3. Re-place
    Early termination if no piece moved. ~150ms/pass for 48 pieces.

    Args:
        packer: GPUPacker instance with pieces already placed
        placements: list of dicts with keys: raster_gpu, x, y, piece (original dict)
        strip_width_px: container strip width in px
        current_length: current rightmost extent in px
        n_passes: number of compaction passes

    Returns:
        (new_current_length, updated placements list)
    """
    for _pass in range(n_passes):
        moved = False
        # Sort by y descending (top pieces first — they might drop into lower gaps)
        placements.sort(key=lambda p: -p['y'])

        for pl in placements:
            old_x, old_y = pl['x'], pl['y']
            raster_placed = pl['raster_placed']  # the raster that was actually placed
            piece = pl['piece']

            # Remove piece from container
            packer.remove_piece(raster_placed, old_x, old_y)

            # Recompute current_length after removal
            # (use stored extent — full recompute is too expensive)
            temp_length = current_length

            # Find new best position via standard BLF
            result, raster = packer.find_best_position(
                piece['raster_gpu'], piece['raster_180_gpu'], temp_length,
            )

            if result is not None:
                packer.place(raster, result['x'], result['y'])
                pl['x'] = result['x']
                pl['y'] = result['y']
                pl['raster_placed'] = raster
                if result['x'] != old_x or result['y'] != old_y:
                    moved = True
            else:
                # Could not re-place — restore original position
                packer.place(raster_placed, old_x, old_y)

        # Recompute current_length from all placements
        current_length = 0
        for pl in placements:
            right_edge = pl['x'] + pl['raster_placed'].shape[1]
            current_length = max(current_length, right_edge)

        if not moved:
            break

    return current_length, placements


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


def _polygon_area_mm2(vertices_mm: List[Tuple[float, float]]) -> float:
    """Shoelace formula for polygon area. Returns mm². Always positive."""
    n = len(vertices_mm)
    if n < 3:
        return 0.0
    area = 0.0
    for i in range(n):
        x1, y1 = vertices_mm[i]
        x2, y2 = vertices_mm[(i + 1) % n]
        area += x1 * y2 - x2 * y1
    return abs(area) / 2.0


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
    material_sources: Optional[List[str]] = None,
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
        material_sources: For merged materials, the original material names
            from the DXF (e.g., ["S10", "S11"]). If None, filters by `material`.

    Returns:
        Dictionary mapping size -> list of piece dicts with rasters
    """
    if not _init_gpu():
        raise RuntimeError("GPU not available")

    # VT DXF path: Optitex Graded Nest format
    if file_type == "vt_dxf":
        return _load_pieces_vt_dxf(dxf_path, sizes, gpu_scale, piece_buffer)

    # Gerber AccuMark path: pre-graded DXF with rich metadata
    if file_type == "gerber_accumark":
        return _load_pieces_gerber_accumark(dxf_path, material, sizes, gpu_scale, piece_buffer)

    # Gerber AAMA path: DXF+RUL with grading (same as optitex_aama but different parser)
    if file_type == "gerber_aama":
        return _load_pieces_gerber_aama(dxf_path, rul_path, material, sizes, gpu_scale, piece_buffer, material_sources)

    # DXF-only path: no RUL grading, pieces already sized in DXF
    if rul_path is None or not Path(rul_path).exists():
        return _load_pieces_dxf_only(dxf_path, sizes, gpu_scale, piece_buffer)

    # Route to correct parser based on file_type
    if file_type == "optitex_aama":
        from nesting_engine.io.optitex_kpr_parser import load_aama_pattern as load_optitex, AAMAGrader as OptitexGrader
        pieces, rules = load_optitex(dxf_path, rul_path)
        grader = OptitexGrader(pieces, rules)
    else:
        pieces, rules = load_aama_pattern(dxf_path, rul_path)
        grader = AAMAGrader(pieces, rules)
    unit_scale = 25.4 if rules.header.units == 'ENGLISH' else 1.0

    # Detect predominant grain axis for this pattern
    pattern_grain_axis = _detect_grain_axis(pieces)

    # For merged materials, accept any of the source material names
    accept_materials = set(material_sources) if material_sources else {material}

    pieces_by_size = {}

    for target_size in sizes:
        if target_size not in rules.header.size_list:
            continue

        graded = grader.grade(target_size)
        pieces_by_size[target_size] = []

        for gp in graded:
            orig_piece = next((p for p in pieces if p.name == gp.source_piece), None)
            if orig_piece is None or orig_piece.material not in accept_materials:
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


def _load_pieces_gerber_accumark(
    dxf_path: str,
    material: str,
    sizes: List[str],
    gpu_scale: float,
    piece_buffer: float,
) -> Dict[str, List[Dict]]:
    """Load and rasterize pieces from a Gerber AccuMark DXF, filtered by material."""
    from nesting_engine.io.gerber_accumark_parser import parse_gerber_accumark_dxf

    all_pieces, all_sizes, mat_map, piece_config = parse_gerber_accumark_dxf(
        dxf_path, size_names=sizes,
    )

    pieces_by_size: Dict[str, List[Dict]] = {}

    for piece in all_pieces:
        size = piece.identifier.size
        pname = piece.identifier.piece_name
        if not size or size not in sizes:
            continue
        # Filter by material
        if mat_map.get(pname) != material:
            continue

        vertices_mm = list(piece.vertices)
        if len(vertices_mm) < 3:
            continue
        if vertices_mm[0] != vertices_mm[-1]:
            vertices_mm.append(vertices_mm[0])

        raster, area, vertices_mm_norm = _rasterize_vertices(vertices_mm, gpu_scale, piece_buffer)

        cfg = piece_config.get(pname, {'demand': 1, 'flipped': False})
        demand = cfg.get('demand', 1)
        if cfg.get('flipped', False):
            demand = demand * 2  # L+R total for GPU

        if size not in pieces_by_size:
            pieces_by_size[size] = []

        pieces_by_size[size].append({
            'name': pname,
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


def _load_pieces_gerber_aama(
    dxf_path: str,
    rul_path: Optional[str],
    material: str,
    sizes: List[str],
    gpu_scale: float,
    piece_buffer: float,
    material_sources: Optional[List[str]] = None,
) -> Dict[str, List[Dict]]:
    """Load and rasterize pieces from a Gerber AAMA DXF+RUL, filtered by material."""
    from nesting_engine.io.gerber_aama_parser import parse_gerber_aama, GerberAAMAGrader

    pieces, rules = parse_gerber_aama(dxf_path, rul_path)
    grader = GerberAAMAGrader(pieces, rules)
    unit_scale = 25.4 if rules.header.units == 'ENGLISH' else 1.0

    # Detect predominant grain axis for this pattern
    pattern_grain_axis = _detect_grain_axis(pieces)

    # For merged materials, accept any of the source material names
    accept_materials = set(material_sources) if material_sources else {material}

    pieces_by_size = {}

    for target_size in sizes:
        if target_size not in rules.header.size_list:
            continue

        graded = grader.grade(target_size)
        pieces_by_size[target_size] = []

        for gp in graded:
            orig_piece = next((p for p in pieces if p.name == gp.source_piece), None)
            if orig_piece is None:
                continue
            orig_mat = (orig_piece.material or "").upper()
            if orig_mat not in {m.upper() for m in accept_materials}:
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

            # GerberAAMAPiece uses quantity as plain int (no L/R distinction at parser level)
            demand = orig_piece.quantity

            pieces_by_size[target_size].append({
                'name': gp.name,
                'size': target_size,
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


# ---------------------------------------------------------------------------
# Module 1: Geometric Piece Classifier
# ---------------------------------------------------------------------------

class PieceRole(str, Enum):
    """Piece role classification based on raster dimensions vs strip width."""
    LARGE = 'large'
    MEDIUM = 'medium'
    SMALL = 'small'


def classify_pieces(
    pieces_list: List[Dict],
    strip_width_px: int,
    large_thresh: float = 0.35,
    small_thresh: float = 0.25,
) -> None:
    """Tag each piece dict with 'role' based on raster dims vs strip width.

    LARGE: max(ph, pw) > large_thresh * strip_width  (body panels)
    SMALL: min(ph, pw) < small_thresh * strip_width   (thin/narrow pieces: sleeves, pockets)
           Uses min dimension — a piece that's thin in any direction can fill gaps.
    MEDIUM: everything else

    Mutates pieces in-place. O(n), no piece name matching.
    """
    large_px = large_thresh * strip_width_px
    small_px = small_thresh * strip_width_px

    for p in pieces_list:
        ph, pw = p['raster'].shape
        max_dim = max(ph, pw)
        min_dim = min(ph, pw)
        if max_dim > large_px:
            p['role'] = PieceRole.LARGE
        elif min_dim < small_px:
            p['role'] = PieceRole.SMALL
        else:
            p['role'] = PieceRole.MEDIUM


# Sort key constants
SORT_WIDTH_DESC = 'width_desc'
SORT_AREA_DESC = 'area_desc'
SORT_HEIGHT_DESC = 'height_desc'
SORT_HEIGHT_WIDTH_DESC = 'height_width_desc'
SORT_AREA_HEIGHT_DESC = 'area_height_desc'
SORT_PERIMETER_DESC = 'perimeter_desc'

_SORT_KEY_FUNCS = {
    SORT_WIDTH_DESC: lambda p: -p['raster'].shape[0],
    SORT_AREA_DESC: lambda p: -p['area'],
    SORT_HEIGHT_DESC: lambda p: -p['raster'].shape[1],
    SORT_HEIGHT_WIDTH_DESC: lambda p: (-p['raster'].shape[1], -p['raster'].shape[0]),
    SORT_AREA_HEIGHT_DESC: lambda p: (-p['area'], -p['raster'].shape[1]),
    SORT_PERIMETER_DESC: lambda p: -(p['raster'].shape[0] + p['raster'].shape[1]) * 2,
}


def _calibrate_sort_strategy(
    pieces_by_size: Dict,
    packer: GPUPacker,
    strip_width_px: int,
    gpu_scale: float,
    sizes: List[str],
    n_sample_per_bc: int = 3,
    progress_callback: Optional[Callable] = None,
) -> str:
    """Quick LHS-on-LHS calibration to pick the best sort strategy for this pattern.

    Tests a handful of ratios from 2-3 bundle counts with both area_desc and
    width_desc, and returns whichever wins more often.  Typically evaluates
    ~9-18 markers total (< 10 seconds).
    """
    area_wins = 0
    width_wins = 0

    # Test on bc=1, 3, 5 (spread across low/mid/high) — or whatever exists
    test_bcs = [bc for bc in [1, 3, 5] if bc <= 10]

    for bc in test_bcs:
        all_ratios = generate_all_ratios(bc, sizes)
        n_sample = min(n_sample_per_bc, len(all_ratios))
        if len(all_ratios) <= n_sample:
            sample = all_ratios
        else:
            sample = _lhs_sample(all_ratios, sizes, n_sample)

        for ratio in sample:
            pieces_list = _build_pieces_list(ratio, pieces_by_size)
            if not pieces_list:
                continue

            eff_w, _, _, _ = _evaluate_single_sort(
                pieces_list, packer, strip_width_px, gpu_scale,
                sort_key=_SORT_KEY_FUNCS[SORT_WIDTH_DESC],
                capture_preview=False,
            )
            eff_a, _, _, _ = _evaluate_single_sort(
                pieces_list, packer, strip_width_px, gpu_scale,
                sort_key=_SORT_KEY_FUNCS[SORT_AREA_DESC],
                capture_preview=False,
            )

            if eff_a > eff_w + 0.001:
                area_wins += 1
            elif eff_w > eff_a + 0.001:
                width_wins += 1

    winner = SORT_AREA_DESC if area_wins >= width_wins else SORT_WIDTH_DESC
    logger.info(
        "Sort calibration: area=%d width=%d → using %s",
        area_wins, width_wins, winner,
    )
    if progress_callback:
        progress_callback(
            3,
            f"Sort calibration: {winner} selected "
            f"(area={area_wins}, width={width_wins})",
        )
    return winner


def _build_pieces_list(ratio: Dict[str, int], pieces_by_size: Dict) -> List[Dict]:
    """Expand a ratio dict into a flat list of piece dicts."""
    pieces_list = []
    for size, count in ratio.items():
        if count <= 0 or size not in pieces_by_size:
            continue
        for _ in range(count):
            for p in pieces_by_size[size]:
                for _ in range(p['demand']):
                    pieces_list.append(p)
    return pieces_list


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

    total_vector_area_mm2 = 0.0
    current_length = 0
    total_perimeter_mm = 0.0

    for p in pieces_sorted:
        result, raster = packer.find_best_position(p['raster_gpu'], p['raster_180_gpu'], current_length)
        if result is None:
            continue
        packer.place(raster, result['x'], result['y'])
        total_vector_area_mm2 += _polygon_area_mm2(p['vertices_mm'])
        current_length = max(current_length, result['x'] + result['pw'])
        total_perimeter_mm += _compute_perimeter_mm(p['vertices_mm'])

    if current_length == 0:
        return 0.0, 0.0, None, 0.0

    # Vector-area efficiency: piece_area_mm2 / (fabric_width_mm × marker_length_mm)
    fabric_width_mm = strip_width_px / gpu_scale
    length_mm = current_length / gpu_scale
    efficiency = total_vector_area_mm2 / (fabric_width_mm * length_mm)
    length_yards = length_mm / 25.4 / 36

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
    sort_strategy: Optional[str] = None,
) -> Tuple[float, float, Optional[str], float]:
    """
    Evaluate a single ratio.

    Sort strategy selection (in priority order):
      1. sort_strategy='width_desc'|'area_desc' — use the specified single sort
         (from _calibrate_sort_strategy)
      2. dual_sort=True — try both, return the better result (2x cost)
      3. Default (dual_sort=False, no sort_strategy) — width_desc only

    Returns:
        Tuple of (efficiency, length_yards, preview_base64 or None, perimeter_cm)
    """
    pieces_list = _build_pieces_list(ratio, pieces_by_size)
    if not pieces_list:
        return 0.0, 0.0, None, 0.0

    # If a calibrated sort strategy was provided, use it directly (single eval)
    if sort_strategy and sort_strategy in _SORT_KEY_FUNCS:
        return _evaluate_single_sort(
            pieces_list, packer, strip_width_px, gpu_scale,
            sort_key=_SORT_KEY_FUNCS[sort_strategy],
            capture_preview=capture_preview,
        )

    # Primary: width_desc
    eff_w, len_w, prev_w, perim_w = _evaluate_single_sort(
        pieces_list, packer, strip_width_px, gpu_scale,
        sort_key=_SORT_KEY_FUNCS[SORT_WIDTH_DESC],
        capture_preview=capture_preview,
    )

    if not dual_sort:
        return eff_w, len_w, prev_w, perim_w

    # Secondary: area_desc
    eff_a, len_a, prev_a, perim_a = _evaluate_single_sort(
        pieces_list, packer, strip_width_px, gpu_scale,
        sort_key=_SORT_KEY_FUNCS[SORT_AREA_DESC],
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

    total_vector_area_mm2 = 0.0
    current_length = 0
    total_perimeter_mm = 0.0
    placements = []

    for p in pieces_sorted:
        result, raster = packer.find_best_position(p['raster_gpu'], p['raster_180_gpu'], current_length)
        if result is None:
            continue
        packer.place(raster, result['x'], result['y'])
        total_vector_area_mm2 += _polygon_area_mm2(p['vertices_mm'])
        current_length = max(current_length, result['x'] + result['pw'])
        total_perimeter_mm += _compute_perimeter_mm(p['vertices_mm'])
        is_rotated = (raster is not p['raster_gpu'])
        placements.append({
            'piece': p, 'x_px': result['x'], 'y_px': result['y'],
            'rotated_180': is_rotated,
        })

    if current_length == 0:
        return 0.0, 0.0, '', 0.0

    fabric_width_mm = strip_width_px / gpu_scale
    strip_length_mm = current_length / gpu_scale
    efficiency = total_vector_area_mm2 / (fabric_width_mm * strip_length_mm)
    length_yards = strip_length_mm / 25.4 / 36
    svg = _generate_placement_svg(placements, fabric_width_mm, strip_length_mm, gpu_scale)
    perimeter_cm = total_perimeter_mm / 10.0
    return efficiency, length_yards, svg, perimeter_cm


def evaluate_ratio_with_svg(
    pieces_by_size: Dict,
    ratio: Dict[str, int],
    packer: GPUPacker,
    strip_width_px: int,
    gpu_scale: float,
    sort_strategy: Optional[str] = None,
) -> Tuple[float, float, str, float]:
    """
    Evaluate a ratio AND produce an SVG preview from original polygon vertices.

    If sort_strategy is provided, uses that single sort.  Otherwise tries both
    width_desc and area_desc, returning the better result (2x cost).

    Returns:
        Tuple of (efficiency, length_yards, svg_string, perimeter_cm)
    """
    pieces_list = _build_pieces_list(ratio, pieces_by_size)
    if not pieces_list:
        return 0.0, 0.0, '', 0.0

    # If a calibrated sort strategy was provided, use it directly
    if sort_strategy and sort_strategy in _SORT_KEY_FUNCS:
        return _evaluate_with_svg_single_sort(
            pieces_list, packer, strip_width_px, gpu_scale,
            sort_key=_SORT_KEY_FUNCS[sort_strategy],
        )

    # Try both sorting strategies
    eff_w, len_w, svg_w, perim_w = _evaluate_with_svg_single_sort(
        pieces_list, packer, strip_width_px, gpu_scale,
        sort_key=_SORT_KEY_FUNCS[SORT_WIDTH_DESC],
    )
    eff_a, len_a, svg_a, perim_a = _evaluate_with_svg_single_sort(
        pieces_list, packer, strip_width_px, gpu_scale,
        sort_key=_SORT_KEY_FUNCS[SORT_AREA_DESC],
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


# ---------------------------------------------------------------------------
# Module 2: Type-Aware Evaluation Pipeline
# ---------------------------------------------------------------------------

def _evaluate_typed_sort(
    pieces_list: List[Dict],
    packer: GPUPacker,
    strip_width_px: int,
    gpu_scale: float,
    large_sort_key=None,
    small_sort_key=None,
    gap_fill_mode: bool = True,
    compaction_passes: int = 0,
    capture_preview: bool = False,
) -> Tuple[float, float, Optional[str], float]:
    """Type-aware BLF evaluation with optional gap-fill and compaction.

    Same return signature as _evaluate_single_sort:
        (efficiency, length_yards, preview_base64 or None, perimeter_cm)

    1. classify_pieces() → tag all pieces
    2. Partition: LARGE+MEDIUM first, SMALL second
    3. Place LARGE+MEDIUM with standard BLF (find_best_position)
    4. Place SMALL with gap-fill scoring if enabled, else standard BLF
    5. Compact if compaction_passes > 0
    6. Return metrics
    """
    if large_sort_key is None:
        large_sort_key = _SORT_KEY_FUNCS[SORT_WIDTH_DESC]
    if small_sort_key is None:
        small_sort_key = _SORT_KEY_FUNCS[SORT_AREA_DESC]

    packer.reset()

    # Classify all pieces
    classify_pieces(pieces_list, strip_width_px)

    # Partition
    large_medium = [p for p in pieces_list if p.get('role') != PieceRole.SMALL]
    smalls = [p for p in pieces_list if p.get('role') == PieceRole.SMALL]

    # Sort each partition
    large_medium.sort(key=large_sort_key)
    smalls.sort(key=small_sort_key)

    total_vector_area_mm2 = 0.0
    current_length = 0
    total_perimeter_mm = 0.0
    all_placements = []  # for compaction tracking

    # Phase 1: Place LARGE + MEDIUM with standard BLF
    for p in large_medium:
        result, raster = packer.find_best_position(
            p['raster_gpu'], p['raster_180_gpu'], current_length,
        )
        if result is None:
            continue
        packer.place(raster, result['x'], result['y'])
        total_vector_area_mm2 += _polygon_area_mm2(p['vertices_mm'])
        current_length = max(current_length, result['x'] + result['pw'])
        total_perimeter_mm += _compute_perimeter_mm(p['vertices_mm'])
        all_placements.append({
            'x': result['x'], 'y': result['y'],
            'raster_placed': raster, 'piece': p,
        })

    # Phase 2: Place SMALL pieces
    for p in smalls:
        if gap_fill_mode:
            result, raster = packer.find_gap_fill_position(
                p['raster_gpu'], p['raster_180_gpu'], current_length,
            )
        else:
            result, raster = packer.find_best_position(
                p['raster_gpu'], p['raster_180_gpu'], current_length,
            )
        if result is None:
            continue
        packer.place(raster, result['x'], result['y'])
        total_vector_area_mm2 += _polygon_area_mm2(p['vertices_mm'])
        current_length = max(current_length, result['x'] + result['pw'])
        total_perimeter_mm += _compute_perimeter_mm(p['vertices_mm'])
        all_placements.append({
            'x': result['x'], 'y': result['y'],
            'raster_placed': raster, 'piece': p,
        })

    if current_length == 0:
        return 0.0, 0.0, None, 0.0

    # Phase 3: Gravity compaction
    if compaction_passes > 0 and all_placements:
        current_length, all_placements = _gravity_compact(
            packer, all_placements, strip_width_px, current_length,
            n_passes=compaction_passes,
        )

    # Vector-area efficiency: piece_area_mm2 / (fabric_width_mm × marker_length_mm)
    fabric_width_mm = strip_width_px / gpu_scale
    length_mm = current_length / gpu_scale
    efficiency = total_vector_area_mm2 / (fabric_width_mm * length_mm)
    length_yards = length_mm / 25.4 / 36

    preview_base64 = None
    if capture_preview:
        preview_base64 = packer.get_container_base64(current_length)

    perimeter_cm = total_perimeter_mm / 10.0
    return efficiency, length_yards, preview_base64, perimeter_cm


def evaluate_ratio_typed(
    pieces_by_size: Dict,
    ratio: Dict[str, int],
    packer: GPUPacker,
    strip_width_px: int,
    gpu_scale: float,
    capture_preview: bool = False,
    gap_fill_mode: bool = True,
    compaction_passes: int = 0,
) -> Tuple[float, float, Optional[str], float]:
    """Typed quad: tries 4 sort combos (2 sort orders × gap-fill on/off).
    Returns best result. Drop-in alongside evaluate_ratio().

    Returns:
        Tuple of (efficiency, length_yards, preview_base64 or None, perimeter_cm)
    """
    pieces_list = _build_pieces_list(ratio, pieces_by_size)
    if not pieces_list:
        return 0.0, 0.0, None, 0.0

    best = (0.0, 0.0, None, 0.0)

    sort_combos = [
        (_SORT_KEY_FUNCS[SORT_WIDTH_DESC], _SORT_KEY_FUNCS[SORT_AREA_DESC]),
        (_SORT_KEY_FUNCS[SORT_AREA_DESC], _SORT_KEY_FUNCS[SORT_WIDTH_DESC]),
    ]
    gf_modes = [True, False] if gap_fill_mode else [False]

    for large_sk, small_sk in sort_combos:
        for gf in gf_modes:
            eff, ln, prev, perim = _evaluate_typed_sort(
                pieces_list, packer, strip_width_px, gpu_scale,
                large_sort_key=large_sk,
                small_sort_key=small_sk,
                gap_fill_mode=gf,
                compaction_passes=compaction_passes,
                capture_preview=capture_preview,
            )
            if eff > best[0]:
                best = (eff, ln, prev, perim)

    return best


# ---------------------------------------------------------------------------
# Module 6: Width-Packing Heuristic (Geometric Ratio Ranking)
# ---------------------------------------------------------------------------

def _extract_piece_geometry(
    pieces_by_size: Dict[str, List[Dict]],
    sizes: List[str],
    gpu_scale: float,
) -> Dict[str, List[Dict]]:
    """Extract piece dimensions in mm from rasterized pieces.

    Returns: {size: [{'name', 'ph_mm', 'pw_mm', 'area_mm2', 'demand', 'perimeter_mm'}, ...]}
    ph_mm = across-width dimension (raster shape[0] / gpu_scale)
    pw_mm = along-length dimension (raster shape[1] / gpu_scale)
    """
    geom = {}
    for size in sizes:
        pieces = pieces_by_size.get(size, [])
        if not pieces:
            continue
        geom[size] = []
        for p in pieces:
            ph, pw = p['raster'].shape
            geom[size].append({
                'name': p['name'],
                'ph_mm': ph / gpu_scale,
                'pw_mm': pw / gpu_scale,
                'area_mm2': p['area'] / (gpu_scale ** 2),
                'demand': p['demand'],
                'perimeter_mm': _compute_perimeter_mm(p['vertices_mm']),
            })
    return geom


def score_ratio_width_packing(
    ratio: Dict[str, int],
    piece_geom: Dict[str, List[Dict]],
    strip_width_mm: float,
    sizes: List[str],
) -> Optional[Dict]:
    """Score a ratio by how well its pieces tile the fabric width.

    Algorithm (FFD bin-packing across strip width):
    1. Expand ratio → flat list of (ph_mm, pw_mm, area_mm2) for ALL piece instances
    2. Sort by ph_mm descending (First Fit Decreasing)
    3. Assign each piece to first row where it fits (row capacity = strip_width_mm)
    4. Each row's length contribution = max(pw_mm) of pieces in that row
    5. Predicted length = sum of row lengths
    6. Width utilization = mean(row_fill / strip_width)
    7. Predicted efficiency = total_area / (strip_width × predicted_length)

    Returns result dict compatible with ILP solver, or None if ratio has no pieces.
    """
    # 1. Expand ratio to flat piece list
    items = []  # (ph_mm, pw_mm, area_mm2)
    total_area = 0.0
    total_perimeter = 0.0
    bc = 0

    for size in sizes:
        count = ratio.get(size, 0)
        if count <= 0 or size not in piece_geom:
            continue
        bc += count
        for _ in range(count):
            for pg in piece_geom[size]:
                for _ in range(pg['demand']):
                    items.append((pg['ph_mm'], pg['pw_mm'], pg['area_mm2']))
                    total_area += pg['area_mm2']
                    total_perimeter += pg['perimeter_mm']

    if not items or bc == 0:
        return None

    # 2. Sort by ph descending (FFD)
    items.sort(key=lambda x: -x[0])

    # 3. Bin-pack into rows
    rows = []  # each row: {'fill': float, 'max_pw': float}
    for ph, pw, area in items:
        placed = False
        for row in rows:
            if row['fill'] + ph <= strip_width_mm:
                row['fill'] += ph
                row['max_pw'] = max(row['max_pw'], pw)
                placed = True
                break
        if not placed:
            rows.append({'fill': ph, 'max_pw': pw})

    # 4. Compute metrics
    predicted_length_mm = sum(r['max_pw'] for r in rows)
    width_fills = [r['fill'] / strip_width_mm for r in rows]
    width_utilization = sum(width_fills) / len(width_fills) if width_fills else 0

    if predicted_length_mm <= 0:
        return None

    predicted_efficiency = total_area / (strip_width_mm * predicted_length_mm)
    predicted_length_yards = predicted_length_mm / 914.4

    return {
        'ratio': ratio,
        'ratio_str': ratio_to_str(ratio, sizes),
        'efficiency': predicted_efficiency,
        'length_yards': predicted_length_yards,
        'bundle_count': bc,
        'perimeter_cm': total_perimeter / 10.0,
        'width_utilization': width_utilization,
        'num_rows': len(rows),
        'total_area_mm2': total_area,
        'source': 'width_heuristic',
    }


def rank_ratios_by_geometry(
    pieces_by_size: Dict[str, List[Dict]],
    sizes: List[str],
    strip_width_mm: float,
    gpu_scale: float,
    max_bundle_count: int = 6,
    top_n_per_bc: int = 25,
) -> Dict[int, List[Dict]]:
    """Rank all ratios for bc=1..max using width-packing heuristic.

    For each bundle count:
      1. generate_all_ratios(bc, sizes)
      2. score_ratio_width_packing() for each
      3. Sort by predicted_efficiency descending
      4. Return top_n_per_bc

    Returns: {bc: [scored_ratio_dicts sorted by efficiency desc]}
    Compatible with ILP solver input format.
    """
    piece_geom = _extract_piece_geometry(pieces_by_size, sizes, gpu_scale)
    results = {}

    for bc in range(1, max_bundle_count + 1):
        all_ratios = generate_all_ratios(bc, sizes)
        scored = []
        for ratio in all_ratios:
            result = score_ratio_width_packing(ratio, piece_geom, strip_width_mm, sizes)
            if result is not None:
                scored.append(result)

        scored.sort(key=lambda x: -x['efficiency'])
        results[bc] = scored[:top_n_per_bc]

    return results


# ---------------------------------------------------------------------------
# Module 5: Column-Fill (GEAR) Placement
# ---------------------------------------------------------------------------

def _probe_column_fill(
    pieces_by_size: Dict[str, List[Dict]],
    strip_width_px: int,
    sizes: List[str],
) -> Optional[Dict]:
    """Probe whether column-fill (3-across interlocking) is viable for this pattern.

    Detection criteria:
      1. Garment has 3+ distinct piece types per size
      2. For the smallest size, sum(piece_heights) / strip_width < 1.05

    If viable, builds a cross-size pairing feasibility table by actually
    testing raster collision for each (FRT_size, BK_size) pair.

    Returns:
        None if column-fill not viable.
        Dict with keys:
          'piece_names': list of piece names sorted by height desc (first 2 are "body", rest are "fill")
          'feasibility': dict of (frt_size, bk_size) -> {'bk_y': int, 'gap': int, 'max_fill_size': str or None}
    """
    # Find smallest size with pieces
    smallest_size = None
    for size in sizes:
        if size in pieces_by_size and pieces_by_size[size]:
            smallest_size = size
            break
    if smallest_size is None:
        return None

    pieces = pieces_by_size[smallest_size]

    # Criterion 1: need 3+ distinct piece types
    if len(pieces) < 3:
        return None

    # Criterion 2: sum of piece heights (across-width dim) must fit the strip
    # Use ONE instance of each piece type — demand is about quantity, not stacking
    sum_ph = sum(p['raster'].shape[0] for p in pieces)
    if sum_ph / strip_width_px > 1.05:
        return None

    # Identify body pieces (2 tallest) and fill pieces (the rest)
    # Sort by height descending — the 2 tallest per garment are "body panels"
    unique_pieces = sorted(pieces, key=lambda p: -p['raster'].shape[0])
    body_names = [unique_pieces[0]['name'], unique_pieces[1]['name']]
    fill_names = [p['name'] for p in unique_pieces[2:]]

    if not fill_names:
        return None  # Need at least one fill piece

    # Build cross-size feasibility table using actual collision detection
    feasibility = {}

    for frt_sz in sizes:
        frt_list = [p for p in pieces_by_size.get(frt_sz, []) if p['name'] == body_names[0]]
        if not frt_list:
            continue
        frt = frt_list[0]
        frt_h = frt['raster'].shape[0]

        for bk_sz in sizes:
            bk_list = [p for p in pieces_by_size.get(bk_sz, []) if p['name'] == body_names[1]]
            if not bk_list:
                continue
            bk = bk_list[0]

            # Place frt(0°) at y=0, find lowest y where bk(180°) fits
            packer = GPUPacker(strip_width_px, max(frt['raster'].shape[1], bk['raster'].shape[1]) + 10)
            packer.place(frt['raster_gpu'], 0, 0)

            bk_180 = bk['raster_180_gpu']
            bk_ph, bk_pw = bk_180.shape

            best_y = None
            for y in range(strip_width_px - bk_ph + 1):
                max_w = min(bk_pw, packer.max_length)
                region = packer.container[y:y + bk_ph, 0:max_w]
                if region.shape[1] < bk_pw:
                    continue
                overlap = float(cp.sum(region * bk_180[:, :region.shape[1]]))
                if overlap < 0.5:
                    best_y = y
                    break

            if best_y is None:
                continue

            bk_bottom = best_y + bk_ph
            gap = strip_width_px - bk_bottom

            # Find largest fill piece that fits in the gap
            max_fill_size = None
            for fill_sz in reversed(sizes):
                fill_list = [p for p in pieces_by_size.get(fill_sz, []) if p['name'] == fill_names[0]]
                if not fill_list:
                    continue
                if fill_list[0]['raster'].shape[0] <= gap:
                    max_fill_size = fill_sz
                    break

            feasibility[(frt_sz, bk_sz)] = {
                'bk_y': best_y,
                'gap': gap,
                'max_fill_size': max_fill_size,
            }

    # Check if any 3-across combo exists
    has_3across = any(v['max_fill_size'] is not None for v in feasibility.values())
    if not has_3across:
        return None

    logger.info(
        "Column-fill probe: viable! body=%s fill=%s, %d/%d combos fit 3-across",
        body_names, fill_names,
        sum(1 for v in feasibility.values() if v['max_fill_size'] is not None),
        len(feasibility),
    )

    return {
        'body_names': body_names,
        'fill_names': fill_names,
        'feasibility': feasibility,
    }


def _evaluate_column_fill(
    pieces_list: List[Dict],
    packer: GPUPacker,
    strip_width_px: int,
    gpu_scale: float,
    probe: Dict,
    capture_preview: bool = False,
) -> Tuple[float, float, Optional[str], float]:
    """Column-fill placement: pack body pairs + fill pieces in vertical columns.

    Strategy:
      1. Group pieces into triplets: body_A(0°) + body_B(180°) + fill, stacked across width
      2. Use cross-size pairing: big body + small body → more gap for fill piece
      3. Place each column at next available x position
      4. Leftover pieces (infeasible sizes, orphan fills) placed with standard BLF

    Same return signature as _evaluate_single_sort.
    """
    packer.reset()

    body_names = probe['body_names']
    fill_names = probe['fill_names']
    feasibility = probe['feasibility']

    # Partition pieces
    body_a_by_size: Dict[str, List[Dict]] = {}  # e.g., FRT SD
    body_b_by_size: Dict[str, List[Dict]] = {}  # e.g., BK SD
    fill_by_size: Dict[str, List[Dict]] = {}    # e.g., SLV
    for p in pieces_list:
        size = p['size']
        if p['name'] == body_names[0]:
            body_a_by_size.setdefault(size, []).append(p)
        elif p['name'] == body_names[1]:
            body_b_by_size.setdefault(size, []).append(p)
        elif p['name'] in fill_names:
            fill_by_size.setdefault(size, []).append(p)

    # Build pairing plan: greedily pair body_a with body_b cross-size
    # Prefer combos where 3-across is feasible, with smallest gap (tightest fit)
    pairs = []  # list of (body_a_piece, body_b_piece, fill_piece_or_None)

    # Sort feasible combos by gap ascending (tightest fit first = least waste)
    feasible_combos = [
        (k, v) for k, v in feasibility.items()
        if v['max_fill_size'] is not None
    ]
    feasible_combos.sort(key=lambda x: x[1]['gap'])

    # Also add 2-across combos as fallback (sorted by tightest)
    two_across_combos = [
        (k, v) for k, v in feasibility.items()
        if v['max_fill_size'] is None
    ]
    two_across_combos.sort(key=lambda x: x[1]['gap'])

    all_combos = feasible_combos + two_across_combos

    used_a: Dict[str, int] = {}  # count of body_a used per size
    used_b: Dict[str, int] = {}
    used_fill: Dict[str, int] = {}

    for (a_sz, b_sz), info in all_combos:
        while True:
            # Check if we have unused body_a and body_b
            a_avail = body_a_by_size.get(a_sz, [])
            a_used = used_a.get(a_sz, 0)
            if a_used >= len(a_avail):
                break

            b_avail = body_b_by_size.get(b_sz, [])
            b_used = used_b.get(b_sz, 0)
            if b_used >= len(b_avail):
                break

            body_a = a_avail[a_used]
            body_b = b_avail[b_used]

            # Find a fill piece
            fill_piece = None
            if info['max_fill_size'] is not None:
                # Try fill pieces from largest that fits down to smallest
                fill_gap = info['gap']
                for fill_name in fill_names:
                    # Try the max_fill_size first, then smaller
                    for f_sz in reversed(list(fill_by_size.keys())):
                        f_list = fill_by_size.get(f_sz, [])
                        f_used = used_fill.get((fill_name, f_sz), 0)
                        if f_used >= len([p for p in f_list if p['name'] == fill_name]):
                            continue
                        candidates = [p for p in f_list if p['name'] == fill_name]
                        if f_used >= len(candidates):
                            continue
                        if candidates[f_used]['raster'].shape[0] <= fill_gap:
                            fill_piece = candidates[f_used]
                            used_fill[(fill_name, f_sz)] = f_used + 1
                            break
                    if fill_piece is not None:
                        break

            pairs.append((body_a, body_b, fill_piece, info))
            used_a[a_sz] = a_used + 1
            used_b[b_sz] = b_used + 1

    # Place columns
    total_vector_area_mm2 = 0.0
    current_length = 0
    total_perimeter_mm = 0.0
    leftover = []

    for body_a, body_b, fill_piece, info in pairs:
        bk_y = info['bk_y']
        bk_bottom = bk_y + body_b['raster_180_gpu'].shape[0]

        # Place body_a(0°) at (current_length, 0)
        a_raster = body_a['raster_gpu']
        a_ph, a_pw = a_raster.shape

        # Place body_b(180°) at (current_length, bk_y)
        b_raster = body_b['raster_180_gpu']
        b_ph, b_pw = b_raster.shape

        # Column width = max of the two body piece widths
        col_x = current_length

        # Check if column fits in container
        col_w = max(a_pw, b_pw)
        if col_x + col_w > packer.max_length:
            leftover.extend([body_a, body_b])
            if fill_piece:
                leftover.append(fill_piece)
            continue

        packer.place(a_raster, col_x, 0)
        total_vector_area_mm2 += _polygon_area_mm2(body_a['vertices_mm'])
        total_perimeter_mm += _compute_perimeter_mm(body_a['vertices_mm'])

        packer.place(b_raster, col_x, bk_y)
        total_vector_area_mm2 += _polygon_area_mm2(body_b['vertices_mm'])
        total_perimeter_mm += _compute_perimeter_mm(body_b['vertices_mm'])

        if fill_piece is not None:
            f_raster = fill_piece['raster_gpu']
            f_ph, f_pw = f_raster.shape
            fill_y = bk_bottom
            if fill_y + f_ph <= strip_width_px:
                packer.place(f_raster, col_x, fill_y)
                total_vector_area_mm2 += _polygon_area_mm2(fill_piece['vertices_mm'])
                total_perimeter_mm += _compute_perimeter_mm(fill_piece['vertices_mm'])
            else:
                leftover.append(fill_piece)

        current_length = max(current_length, col_x + col_w)

    # Collect unused pieces
    for sz, pieces in body_a_by_size.items():
        for i, p in enumerate(pieces):
            if i >= used_a.get(sz, 0):
                leftover.append(p)
    for sz, pieces in body_b_by_size.items():
        for i, p in enumerate(pieces):
            if i >= used_b.get(sz, 0):
                leftover.append(p)
    for sz, pieces in fill_by_size.items():
        for p in pieces:
            key = (p['name'], sz)
            if used_fill.get(key, 0) > 0:
                used_fill[key] -= 1
            else:
                leftover.append(p)

    # Place leftovers with standard BLF (width_desc sort)
    leftover.sort(key=_SORT_KEY_FUNCS[SORT_WIDTH_DESC])
    for p in leftover:
        result, raster = packer.find_best_position(
            p['raster_gpu'], p['raster_180_gpu'], current_length,
        )
        if result is None:
            continue
        packer.place(raster, result['x'], result['y'])
        total_vector_area_mm2 += _polygon_area_mm2(p['vertices_mm'])
        current_length = max(current_length, result['x'] + result['pw'])
        total_perimeter_mm += _compute_perimeter_mm(p['vertices_mm'])

    if current_length == 0:
        return 0.0, 0.0, None, 0.0

    # Vector-area efficiency: piece_area_mm2 / (fabric_width_mm × marker_length_mm)
    fabric_width_mm = strip_width_px / gpu_scale
    length_mm = current_length / gpu_scale
    efficiency = total_vector_area_mm2 / (fabric_width_mm * length_mm)
    length_yards = length_mm / 25.4 / 36

    preview_base64 = None
    if capture_preview:
        preview_base64 = packer.get_container_base64(current_length)

    perimeter_cm = total_perimeter_mm / 10.0
    return efficiency, length_yards, preview_base64, perimeter_cm


def evaluate_ratio_column_fill(
    pieces_by_size: Dict,
    ratio: Dict[str, int],
    packer: GPUPacker,
    strip_width_px: int,
    gpu_scale: float,
    probe: Dict,
    capture_preview: bool = False,
) -> Tuple[float, float, Optional[str], float]:
    """Column-fill evaluation with BLF fallback.

    Runs column-fill placement, then standard BLF dual-sort.
    Returns the better result.
    """
    pieces_list = _build_pieces_list(ratio, pieces_by_size)
    if not pieces_list:
        return 0.0, 0.0, None, 0.0

    # Column-fill
    eff_cf, len_cf, prev_cf, perim_cf = _evaluate_column_fill(
        pieces_list, packer, strip_width_px, gpu_scale, probe,
        capture_preview=capture_preview,
    )

    # BLF dual-sort baseline
    eff_w, len_w, prev_w, perim_w = _evaluate_single_sort(
        pieces_list, packer, strip_width_px, gpu_scale,
        sort_key=_SORT_KEY_FUNCS[SORT_WIDTH_DESC],
        capture_preview=capture_preview,
    )
    eff_a, len_a, prev_a, perim_a = _evaluate_single_sort(
        pieces_list, packer, strip_width_px, gpu_scale,
        sort_key=_SORT_KEY_FUNCS[SORT_AREA_DESC],
        capture_preview=capture_preview,
    )

    # Pick best of 3
    best = (eff_cf, len_cf, prev_cf, perim_cf)
    if eff_w > best[0]:
        best = (eff_w, len_w, prev_w, perim_w)
    if eff_a > best[0]:
        best = (eff_a, len_a, prev_a, perim_a)

    return best


class NestingCancelled(Exception):
    """Raised when a nesting job is cancelled by the user."""
    pass


# ---------------------------------------------------------------------------
# LHS + Ridge Prediction Pipeline
# ---------------------------------------------------------------------------

def _extract_geometry(
    pieces_by_size: Dict[str, List[Dict]],
    sizes: List[str],
    fabric_width_mm: float,
    gpu_scale: float,
) -> Dict:
    """
    Extract per-piece per-size geometry features from already-loaded pieces.

    Returns a dict with:
      piece_names: list of unique piece names (consistent order)
      demands: {piece_name: demand}
      features: {(piece_name, size): {area_mm2, perimeter_mm, bbox_width_mm, bbox_height_mm, bbox_width_ratio}}
      bundle_area: {size: total area in mm² for one bundle}
      max_piece_width: {size: widest piece in mm}
    """
    # Collect unique piece names (from first size that has pieces)
    piece_names = []
    seen = set()
    for size in sizes:
        for p in pieces_by_size.get(size, []):
            if p['name'] not in seen:
                piece_names.append(p['name'])
                seen.add(p['name'])

    demands = {}
    features = {}
    bundle_area = {}
    max_piece_width = {}

    for size in sizes:
        size_pieces = pieces_by_size.get(size, [])
        total_area = 0.0
        max_w = 0.0

        for p in size_pieces:
            name = p['name']
            demands[name] = p['demand']

            verts = p['vertices_mm']
            xs = [v[0] for v in verts]
            ys = [v[1] for v in verts]
            bbox_w = max(xs) - min(xs) if xs else 0
            bbox_h = max(ys) - min(ys) if ys else 0
            area_mm2 = p['area'] / (gpu_scale ** 2)  # convert pixel area back to mm²
            perimeter_mm = _compute_perimeter_mm(verts)

            features[(name, size)] = {
                'area_mm2': area_mm2,
                'perimeter_mm': perimeter_mm,
                'bbox_width_mm': bbox_w,
                'bbox_height_mm': bbox_h,
                'bbox_width_ratio': bbox_w / fabric_width_mm if fabric_width_mm > 0 else 0,
            }

            total_area += area_mm2 * p['demand']
            max_w = max(max_w, bbox_w)

        bundle_area[size] = total_area
        max_piece_width[size] = max_w

    return {
        'piece_names': piece_names,
        'demands': demands,
        'features': features,
        'bundle_area': bundle_area,
        'max_piece_width': max_piece_width,
    }


def _lhs_sample(
    all_ratios: List[Dict[str, int]],
    sizes: List[str],
    sample_size: int,
) -> List[Dict[str, int]]:
    """
    Latin Hypercube Sampling on the discrete ratio space.

    1. Normalize each ratio to proportions (sum-to-1 vector)
    2. Generate LHS points in [0,1]^n_sizes
    3. Map each LHS point to nearest actual ratio (greedy, no duplicates)
    4. Always include boundary ratios (single-size ratios)
    """
    from scipy.stats.qmc import LatinHypercube

    n_sizes = len(sizes)
    n_ratios = len(all_ratios)

    if sample_size >= n_ratios:
        return list(all_ratios)

    # Convert ratios to proportion vectors for distance calculation
    ratio_props = np.zeros((n_ratios, n_sizes))
    for i, ratio in enumerate(all_ratios):
        vals = [ratio.get(s, 0) for s in sizes]
        total = sum(vals)
        if total > 0:
            ratio_props[i] = [v / total for v in vals]

    # Identify boundary ratios (single-size: all bundles in one size)
    boundary_indices = set()
    for i, ratio in enumerate(all_ratios):
        nonzero = [s for s in sizes if ratio.get(s, 0) > 0]
        if len(nonzero) == 1:
            boundary_indices.add(i)

    # Generate LHS points
    n_lhs = min(sample_size - len(boundary_indices), n_ratios - len(boundary_indices))
    if n_lhs <= 0:
        # Boundaries already fill the sample
        return [all_ratios[i] for i in list(boundary_indices)[:sample_size]]

    sampler = LatinHypercube(d=n_sizes, seed=42)
    lhs_points = sampler.random(n=n_lhs)

    # Map each LHS point to nearest unused ratio (greedy)
    used = set(boundary_indices)
    selected = list(boundary_indices)

    for pt in lhs_points:
        if len(selected) >= sample_size:
            break
        # Find nearest unused ratio
        best_idx = -1
        best_dist = float('inf')
        for j in range(n_ratios):
            if j in used:
                continue
            dist = np.sum((ratio_props[j] - pt) ** 2)
            if dist < best_dist:
                best_dist = dist
                best_idx = j
        if best_idx >= 0:
            selected.append(best_idx)
            used.add(best_idx)

    return [all_ratios[i] for i in selected]


def _lhs_predict_ratios(
    all_ratios: List[Dict[str, int]],
    sample_results: List[Dict],
    sizes: List[str],
    geom: Dict,
    fabric_width_mm: float,
    gpu_scale: float,
    strip_width_px: int,
    pieces_by_size: Dict[str, List[Dict]],
) -> List[Dict]:
    """
    Train Ridge on GPU-evaluated sample, predict length for all remaining ratios.

    Returns list of result dicts for predicted ratios (same format as evaluate_ratio output),
    tagged with source="predicted".
    """
    from sklearn.linear_model import Ridge

    piece_names = geom['piece_names']
    demands = geom['demands']

    def _build_features(ratio: Dict[str, int]) -> List[float]:
        """Build feature vector for a single ratio."""
        feats = []
        bc = sum(ratio.get(s, 0) for s in sizes)
        if bc == 0:
            bc = 1

        # Group 1: Ratio features
        props = [ratio.get(s, 0) / bc for s in sizes]
        feats.extend(props)
        feats.append(bc)

        # Total bundle area (mm²) for this ratio
        total_area = sum(
            ratio.get(s, 0) * geom['bundle_area'].get(s, 0)
            for s in sizes
        )
        feats.append(total_area)

        # Max piece width ratio
        max_wr = max(
            (geom['max_piece_width'].get(s, 0) / fabric_width_mm
             if ratio.get(s, 0) > 0 and fabric_width_mm > 0 else 0)
            for s in sizes
        )
        feats.append(max_wr)

        # Group 2: Ratio × geometry cross features
        for s in sizes:
            s_count = ratio.get(s, 0)
            if s_count > 0:
                feats.append(s_count * geom['bundle_area'].get(s, 0))
                feats.append(s_count * geom['max_piece_width'].get(s, 0))
            else:
                feats.append(0)
                feats.append(0)

        # Group 3: Pairwise interactions (proportion products)
        for i in range(len(sizes)):
            for j in range(i + 1, len(sizes)):
                feats.append(props[i] * props[j])

        return feats

    # Build feature matrices
    # Sample features + targets (strip length in px for numerical stability)
    sample_ratio_strs = {r['ratio_str'] for r in sample_results}
    sample_map = {r['ratio_str']: r for r in sample_results}

    X_train = []
    y_train = []
    for r in sample_results:
        ratio = r['ratio']
        X_train.append(_build_features(ratio))
        # Target: length in yards (directly useful)
        y_train.append(r['length_yards'])

    X_train = np.array(X_train)
    y_train = np.array(y_train)

    if len(X_train) < 5:
        logger.warning("LHS predict: too few samples (%d), skipping prediction", len(X_train))
        return []

    # Train Ridge
    model = Ridge(alpha=1.0)
    model.fit(X_train, y_train)

    # Training R² for logging
    y_pred_train = model.predict(X_train)
    ss_res = np.sum((y_train - y_pred_train) ** 2)
    ss_tot = np.sum((y_train - np.mean(y_train)) ** 2)
    r2 = 1 - ss_res / ss_tot if ss_tot > 0 else 0
    logger.info("LHS Ridge R²=%.4f on %d training samples", r2, len(X_train))

    # Predict remaining ratios
    predicted = []
    for ratio in all_ratios:
        rstr = ratio_to_str(ratio, sizes)
        if rstr in sample_ratio_strs:
            continue  # Already GPU-evaluated

        feats = _build_features(ratio)
        pred_length_yards = float(model.predict([feats])[0])
        if pred_length_yards <= 0:
            pred_length_yards = 0.001  # Avoid division by zero

        # Compute efficiency from predicted length
        bc = sum(ratio.get(s, 0) for s in sizes)
        total_vector_area_mm2 = 0.0
        for size, count in ratio.items():
            if count <= 0 or size not in pieces_by_size:
                continue
            for _ in range(count):
                for p in pieces_by_size[size]:
                    total_vector_area_mm2 += _polygon_area_mm2(p['vertices_mm']) * p['demand']

        fabric_width_mm = strip_width_px / gpu_scale
        pred_length_mm = pred_length_yards * 25.4 * 36
        eff = total_vector_area_mm2 / (fabric_width_mm * pred_length_mm) if pred_length_mm > 0 else 0
        eff = max(0, min(1, eff))  # clamp

        # Estimate perimeter (sum of all piece perimeters for this ratio)
        total_perimeter_mm = 0
        for size, count in ratio.items():
            if count <= 0 or size not in pieces_by_size:
                continue
            for _ in range(count):
                for p in pieces_by_size[size]:
                    for _ in range(p['demand']):
                        total_perimeter_mm += _compute_perimeter_mm(p['vertices_mm'])

        predicted.append({
            'ratio': ratio,
            'ratio_str': rstr,
            'efficiency': eff,
            'length_yards': pred_length_yards,
            'bundle_count': bc,
            'perimeter_cm': total_perimeter_mm / 10.0,
            'source': 'predicted',
        })

    return predicted


# ---------------------------------------------------------------------------
# Adaptive Nesting Helpers
# ---------------------------------------------------------------------------

def _compute_total_ratios(n_sizes: int, max_bc: int) -> int:
    """Sum of C(n_sizes + bc - 1, bc) for bc=1..max_bc."""
    total = 0
    for bc in range(1, max_bc + 1):
        total += math.comb(n_sizes + bc - 1, bc)
    return total


def _detect_plateau(results_by_bc: Dict[int, List[Dict]]) -> Tuple[int, List[int]]:
    """
    Detect efficiency plateau and return productive BCs.

    Returns:
        (plateau_bc, productive_bcs) where productive_bcs are BCs whose best
        efficiency is within 3% of plateau.
    """
    explored = sorted(results_by_bc.keys())
    if not explored:
        return 3, []

    best_eff = {}
    for bc in explored:
        if results_by_bc[bc]:
            best_eff[bc] = max(r['efficiency'] for r in results_by_bc[bc])
        else:
            best_eff[bc] = 0.0

    # Find plateau: Δeff < 0.3% for 2 consecutive BCs
    plateau_bc = explored[-1]
    for i, bc in enumerate(explored):
        if bc < 4:
            continue
        prev_bc = explored[i - 1] if i > 0 else None
        if prev_bc is None:
            continue
        delta = best_eff.get(bc, 0) - best_eff.get(prev_bc, 0)
        if delta < PLATEAU_DELTA:
            # Check next BC too
            next_bc = explored[i + 1] if i + 1 < len(explored) else None
            if next_bc and best_eff.get(next_bc, 0) - best_eff.get(bc, 0) < PLATEAU_DELTA:
                plateau_bc = prev_bc
                break

    # Productive BCs: those within 3% of plateau best efficiency
    plateau_eff = best_eff.get(plateau_bc, 0)
    productive_bcs = [
        bc for bc in explored
        if bc >= 3 and best_eff.get(bc, 0) >= plateau_eff - 0.03
    ]

    return plateau_bc, productive_bcs


def _select_seeds(
    results_by_bc: Dict[int, List[Dict]],
    productive_bcs: List[int],
    sizes: List[str],
    n: int = SEED_COUNT,
) -> Dict[int, List[Dict[str, int]]]:
    """
    Select top-n diverse seed ratios from each productive BC.

    Diversity: greedy farthest-first selection in proportion space after
    picking the top result by efficiency.
    """
    seeds = {}
    for bc in productive_bcs:
        bc_results = results_by_bc.get(bc, [])
        if not bc_results:
            continue

        # Sort by efficiency descending
        sorted_results = sorted(bc_results, key=lambda r: -r['efficiency'])

        if len(sorted_results) <= n:
            seeds[bc] = [r['ratio'] for r in sorted_results]
            continue

        # Greedy farthest-first for diversity
        n_sizes = len(sizes)
        selected = [sorted_results[0]]  # Start with best
        selected_props = []

        def _to_props(ratio):
            vals = [ratio.get(s, 0) for s in sizes]
            total = sum(vals)
            return [v / total for v in vals] if total > 0 else [0] * n_sizes

        selected_props.append(_to_props(selected[0]['ratio']))

        remaining = sorted_results[1:]
        while len(selected) < n and remaining:
            best_idx = -1
            best_min_dist = -1
            for i, r in enumerate(remaining):
                props = _to_props(r['ratio'])
                min_dist = min(
                    sum((a - b) ** 2 for a, b in zip(props, sp))
                    for sp in selected_props
                )
                # Weight by efficiency (top results preferred even if close)
                weighted = min_dist * (0.5 + 0.5 * r['efficiency'] / sorted_results[0]['efficiency'])
                if weighted > best_min_dist:
                    best_min_dist = weighted
                    best_idx = i
            if best_idx >= 0:
                selected.append(remaining[best_idx])
                selected_props.append(_to_props(remaining[best_idx]['ratio']))
                remaining.pop(best_idx)
            else:
                break

        seeds[bc] = [r['ratio'] for r in selected]

    return seeds


def _generate_multiplied(
    seeds: Dict[int, List[Dict[str, int]]],
    max_bc: int,
    sizes: List[str],
) -> List[Dict[str, int]]:
    """
    Generate multiplied candidates from seed ratios (Phase 3).

    For each seed at base BC b, scale by multiplier 2,3,... while target_bc ≤ max_bc.
    Dedup across all seeds.
    """
    seen = set()
    candidates = []

    for base_bc, seed_list in seeds.items():
        for seed in seed_list:
            for multiplier in range(2, max_bc // base_bc + 1):
                target_bc = base_bc * multiplier
                if target_bc > max_bc:
                    break
                scaled = {s: seed.get(s, 0) * multiplier for s in sizes}
                key = ratio_to_key(scaled, sizes)
                if key not in seen:
                    seen.add(key)
                    candidates.append(scaled)

    return candidates


def _generate_neighborhoods(
    top_ratios: List[Dict[str, int]],
    sizes: List[str],
    already_evaluated: set,
) -> List[Dict[str, int]]:
    """
    Generate ±1 perturbation neighbors for top ratios (Phase 4).

    For each ratio, swap one bundle between each pair of sizes
    (maintaining total BC). Skip already-evaluated ratios.
    """
    neighbors = []
    seen = set(already_evaluated)

    for ratio in top_ratios:
        for i in range(len(sizes)):
            si = sizes[i]
            if ratio.get(si, 0) == 0:
                continue
            for j in range(len(sizes)):
                if i == j:
                    continue
                sj = sizes[j]
                neighbor = dict(ratio)
                neighbor[si] = ratio.get(si, 0) - 1
                neighbor[sj] = ratio.get(sj, 0) + 1
                key = ratio_to_key(neighbor, sizes)
                if key not in seen:
                    seen.add(key)
                    neighbors.append(neighbor)

    return neighbors


def _evaluate_ratios_batch(
    ratios: List[Dict[str, int]],
    bundle_count_label: str,
    pieces_by_size: Dict,
    packer: GPUPacker,
    strip_width_px: int,
    gpu_scale: float,
    sizes: List[str],
    source_tag: str = 'gpu',
    cancel_check: Optional[Callable[[], bool]] = None,
    progress_callback: Optional[Callable[[int, str], None]] = None,
    preview_callback: Optional[Callable[[str, str, float], None]] = None,
    preview_interval_seconds: float = 5.0,
    base_progress: int = 0,
    progress_span: int = 10,
    evaluated_count_ref: Optional[List[int]] = None,
    sort_strategy: Optional[str] = None,
    use_batch_kernel: bool = True,
    batch_kernel_min_ratios: int = 32,
) -> List[Dict]:
    """
    GPU-evaluate a batch of ratios with cancellation, progress, and preview support.

    Args:
        ratios: List of ratio dicts to evaluate
        bundle_count_label: Display label for progress messages
        source_tag: Tag for result source ('gpu', 'multiplied', 'neighborhood')
        evaluated_count_ref: Mutable [int] for tracking total evals across batches
        base_progress: Starting progress percentage for this batch
        progress_span: Progress percentage range allocated to this batch
        sort_strategy: Calibrated sort strategy ('width_desc' or 'area_desc').
                       If None, falls back to dual_sort for backward compat.
        use_batch_kernel: Use the GPU batched BLF kernel (gpu_batched_packer) when
                       enough ratios are passed. Bit-identical length vs legacy Python
                       BLF (prefer_rot0=True) with ~3x speedup on large batches.
                       If the batch path fails for any reason, falls back transparently.
        batch_kernel_min_ratios: Minimum number of ratios before the batch kernel is
                       worth it (below this, per-ratio Python path is faster at N=1
                       because the monolithic kernel uses only 1 of 30 SMs).

    Returns:
        List of result dicts
    """
    results = []
    last_preview_time = 0.0
    n_total = len(ratios)
    if n_total == 0:
        return results

    # --- Fast path: GPU batched kernel ---
    if use_batch_kernel and n_total >= batch_kernel_min_ratios:
        try:
            results = _evaluate_ratios_batch_gpu_kernel(
                ratios, bundle_count_label, pieces_by_size, packer,
                strip_width_px, gpu_scale, sizes, source_tag,
                cancel_check, progress_callback, preview_callback,
                preview_interval_seconds, base_progress, progress_span,
                evaluated_count_ref, sort_strategy,
            )
            return results
        except NestingCancelled:
            raise
        except Exception as e:
            logger.warning(
                f"_evaluate_ratios_batch: GPU batched kernel failed ({e!r}); "
                f"falling back to per-ratio Python BLF loop"
            )
            # Fall through to legacy path — ensures zero functional regression

    # --- Legacy path: per-ratio Python BLF loop ---
    for idx, ratio in enumerate(ratios):
        if cancel_check and idx % 20 == 0 and idx > 0 and cancel_check():
            raise NestingCancelled("Job cancelled by user")

        current_time = time.time()
        should_capture = preview_callback and (current_time - last_preview_time >= preview_interval_seconds)

        eff, length, preview, perim_cm = evaluate_ratio(
            pieces_by_size, ratio, packer, strip_width_px, gpu_scale,
            capture_preview=should_capture,
            dual_sort=sort_strategy is None,  # only dual_sort if no calibrated strategy
            sort_strategy=sort_strategy,
        )

        if should_capture and preview:
            r_str = ratio_to_str(ratio, sizes)
            preview_callback(r_str, preview, eff)
            last_preview_time = current_time

        bc = sum(ratio.get(s, 0) for s in sizes)
        results.append({
            'ratio': ratio,
            'ratio_str': ratio_to_str(ratio, sizes),
            'efficiency': eff,
            'length_yards': length,
            'bundle_count': bc,
            'perimeter_cm': perim_cm,
            'source': source_tag,
        })

        if evaluated_count_ref is not None:
            evaluated_count_ref[0] += 1

        if progress_callback and idx % 20 == 0:
            pct = base_progress + int((idx + 1) / n_total * progress_span)
            total_str = f" — {evaluated_count_ref[0]} total" if evaluated_count_ref else ""
            progress_callback(
                min(pct, 95),
                f"{bundle_count_label} ({idx + 1}/{n_total}){total_str}",
            )

    return results


def _evaluate_ratios_batch_gpu_kernel(
    ratios: List[Dict[str, int]],
    bundle_count_label: str,
    pieces_by_size: Dict,
    packer: GPUPacker,
    strip_width_px: int,
    gpu_scale: float,
    sizes: List[str],
    source_tag: str,
    cancel_check: Optional[Callable[[], bool]],
    progress_callback: Optional[Callable[[int, str], None]],
    preview_callback: Optional[Callable[[str, str, float], None]],
    preview_interval_seconds: float,
    base_progress: int,
    progress_span: int,
    evaluated_count_ref: Optional[List[int]],
    sort_strategy: Optional[str],
) -> List[Dict]:
    """GPU batched BLF kernel variant of _evaluate_ratios_batch.

    Produces the same result schema as the legacy path but does the BLF work
    in ~3x less wall time for large batches. Preserves cancellation, progress,
    preview, and evaluated_count semantics.
    """
    # Lazy import to avoid circular dependency
    from backend.services.gpu_batched_packer import evaluate_ratios_batched

    n_total = len(ratios)
    fabric_width_mm = strip_width_px / gpu_scale
    # Use packer's max_length for allocation if available; else a safe default.
    max_length_mm = getattr(packer, 'max_length', int(15000 * gpu_scale)) / gpu_scale

    # Map sort_strategy -> batch API args
    if sort_strategy and sort_strategy in ('width_desc', 'area_desc'):
        strat_primary = sort_strategy
        dual = False
    else:
        strat_primary = 'area_desc'  # batch API default
        dual = True  # match legacy: sort_strategy=None -> dual_sort

    # Run the batch kernel — dual_rot=True tries both rot0/free and keeps shorter.
    batch_results = evaluate_ratios_batched(
        ratios=ratios,
        pieces_by_size=pieces_by_size,
        fabric_width_mm=fabric_width_mm,
        gpu_scale=gpu_scale,
        max_length_mm=max_length_mm,
        sort_strategy=strat_primary,
        dual_sort=dual,
        batch_size=256,
        prefer_rot0=False,
        dual_rot=True,
        verbose=False,
    )

    # --- Translate batch output to legacy result schema ---
    # Compute perimeter per-ratio (cheap CPU op on piece vertices)
    results: List[Dict] = []
    preview_fire_interval = max(1, n_total // max(1, int(n_total / 50) or 1))

    last_preview_time = 0.0
    for idx, (ratio, br) in enumerate(zip(ratios, batch_results)):
        if cancel_check and idx % 20 == 0 and idx > 0 and cancel_check():
            raise NestingCancelled("Job cancelled by user")

        # Perimeter: sum over expanded pieces_list
        pieces_list = _build_pieces_list(ratio, pieces_by_size)
        perim_mm = sum(_compute_perimeter_mm(p['vertices_mm']) for p in pieces_list)

        length_mm = br['length_mm']
        length_yards = length_mm / 25.4 / 36.0
        eff = br['efficiency']
        bc = sum(ratio.get(s, 0) for s in sizes)

        results.append({
            'ratio': ratio,
            'ratio_str': ratio_to_str(ratio, sizes),
            'efficiency': eff,
            'length_yards': length_yards,
            'bundle_count': bc,
            'perimeter_cm': perim_mm / 10.0,
            'source': source_tag,
        })

        # Preview: since batch processes ratios up-front, throttle by wall-clock.
        # We regenerate a single ratio's preview via legacy evaluate_ratio when due.
        if preview_callback:
            now = time.time()
            if now - last_preview_time >= preview_interval_seconds:
                try:
                    _, _, preview_b64, _ = evaluate_ratio(
                        pieces_by_size, ratio, packer, strip_width_px, gpu_scale,
                        capture_preview=True,
                        dual_sort=(sort_strategy is None),
                        sort_strategy=sort_strategy,
                    )
                    if preview_b64:
                        preview_callback(ratio_to_str(ratio, sizes), preview_b64, eff)
                        last_preview_time = now
                except Exception:
                    pass  # preview is best-effort; never fail the main eval

        if evaluated_count_ref is not None:
            evaluated_count_ref[0] += 1

        if progress_callback and idx % 20 == 0:
            pct = base_progress + int((idx + 1) / n_total * progress_span)
            total_str = f" — {evaluated_count_ref[0]} total" if evaluated_count_ref else ""
            progress_callback(
                min(pct, 95),
                f"{bundle_count_label} ({idx + 1}/{n_total}){total_str}",
            )

    return results


# ---------------------------------------------------------------------------
# Cross-Width Ridge Prediction
# ---------------------------------------------------------------------------

def _select_cross_width_anchors(
    base_results: Dict[int, List[Dict]],
    sizes: List[str],
) -> List[Dict[str, int]]:
    """
    Select diverse anchor ratios from bc=3+ base results for cross-width sampling.

    bc=1,2 are handled separately (always brute-forced), so this function
    only selects from bc=3+ GPU-evaluated results using farthest-first diversity.

    Count is determined by CROSS_WIDTH_SAMPLE_RATE/MIN/MAX applied to bc=3+ pool size.

    Returns list of ratio dicts (bc=3+ only).
    """
    # Collect all bc=3+ GPU-evaluated results
    remaining_pool = []
    for bc, bc_results in base_results.items():
        if bc <= 2:
            continue
        for r in bc_results:
            if r.get('source') == 'predicted':
                continue
            ratio = r.get('ratio')
            if not ratio:
                continue
            remaining_pool.append(r)

    if not remaining_pool:
        return []

    # Determine anchor count from rate, clamped to min/max
    n = int(len(remaining_pool) * CROSS_WIDTH_SAMPLE_RATE)
    n = max(CROSS_WIDTH_SAMPLE_MIN, min(CROSS_WIDTH_SAMPLE_MAX, n))
    n = min(n, len(remaining_pool))

    # Greedy farthest-first diversity selection
    n_sizes = len(sizes)

    def _to_props(ratio):
        vals = [ratio.get(s, 0) for s in sizes]
        total = sum(vals)
        return [v / total for v in vals] if total > 0 else [0] * n_sizes

    # Sort by efficiency descending, seed with best
    remaining_pool.sort(key=lambda r: -r['efficiency'])

    selected = [remaining_pool[0]]
    selected_props = [_to_props(remaining_pool[0]['ratio'])]
    used = {ratio_to_key(remaining_pool[0]['ratio'], sizes)}

    candidates = remaining_pool[1:]
    while len(selected) < n and candidates:
        best_idx = -1
        best_min_dist = -1
        for i, r in enumerate(candidates):
            key = ratio_to_key(r['ratio'], sizes)
            if key in used:
                continue
            props = _to_props(r['ratio'])
            min_dist = min(
                sum((a - b) ** 2 for a, b in zip(props, sp))
                for sp in selected_props
            )
            weighted = min_dist * (0.5 + 0.5 * r['efficiency'] / remaining_pool[0]['efficiency'])
            if weighted > best_min_dist:
                best_min_dist = weighted
                best_idx = i
        if best_idx >= 0:
            r = candidates[best_idx]
            selected.append(r)
            selected_props.append(_to_props(r['ratio']))
            used.add(ratio_to_key(r['ratio'], sizes))
            candidates.pop(best_idx)
        else:
            break

    return [r['ratio'] for r in selected]


def _build_features_with_width(
    ratio: Dict[str, int],
    sizes: List[str],
    geom: Dict,
    fabric_width_mm: float,
    shrinkage_x: float = 0.0,
    shrinkage_y: float = 0.0,
) -> List[float]:
    """
    Build Ridge feature vector for cross-width and cross-lot prediction.

    Extends the single-width feature vector with:
    - fabric_width_mm: absolute fabric width (captures width effect)
    - shrinkage_x, shrinkage_y: shrinkage percentages (captures lot effect)

    The geometry features (bundle_area, max_piece_width) should be computed
    from the lot-specific pattern, so they naturally reflect shrinkage-scaled
    piece sizes. The explicit shrinkage features let Ridge learn any residual
    non-linear effects beyond what geometry captures.

    Backwards compatible: defaults shrinkage to (0,0) for single-shrinkage mode.
    """
    feats = []

    # Feature 0-2: fabric width + shrinkage
    feats.append(fabric_width_mm)
    feats.append(shrinkage_x)
    feats.append(shrinkage_y)

    bc = sum(ratio.get(s, 0) for s in sizes)
    if bc == 0:
        bc = 1

    # Ratio proportions
    props = [ratio.get(s, 0) / bc for s in sizes]
    feats.extend(props)
    feats.append(bc)

    # Total bundle area (from lot-specific geometry)
    total_area = sum(
        ratio.get(s, 0) * geom['bundle_area'].get(s, 0)
        for s in sizes
    )
    feats.append(total_area)

    # Max piece width ratio (relative to this width)
    max_wr = max(
        (geom['max_piece_width'].get(s, 0) / fabric_width_mm
         if ratio.get(s, 0) > 0 and fabric_width_mm > 0 else 0)
        for s in sizes
    )
    feats.append(max_wr)

    # Ratio × geometry cross features (lot-specific)
    for s in sizes:
        s_count = ratio.get(s, 0)
        if s_count > 0:
            feats.append(s_count * geom['bundle_area'].get(s, 0))
            feats.append(s_count * geom['max_piece_width'].get(s, 0))
        else:
            feats.append(0)
            feats.append(0)

    # Pairwise interactions
    for i in range(len(sizes)):
        for j in range(i + 1, len(sizes)):
            feats.append(props[i] * props[j])

    return feats


def _cross_width_predict(
    base_results: Dict[int, List[Dict]],
    anchor_results: List[Dict],
    base_width_inches: float,
    target_width_inches: float,
    sizes: List[str],
    geom: Dict,
    pieces_by_size: Dict[str, List[Dict]],
    gpu_scale: float,
    base_shrinkage: Tuple[float, float] = (0.0, 0.0),
    target_shrinkage: Tuple[float, float] = (0.0, 0.0),
    target_geom: Optional[Dict] = None,
) -> Dict[int, List[Dict]]:
    """
    Predict nesting results at target_width using Ridge trained on
    base_width GPU data + anchor GPU data at target_width.

    Returns: {bc: [result dicts]} for predicted ratios at target_width.
    """
    from sklearn.linear_model import Ridge

    base_width_mm = base_width_inches * 25.4
    target_width_mm = target_width_inches * 25.4
    target_strip_px = int(target_width_mm * gpu_scale)

    # Use target-specific geometry if provided (for cross-lot with different pieces)
    tgt_geom = target_geom if target_geom is not None else geom

    # Build training data from base results (all GPU-evaluated)
    X_train = []
    y_train = []
    train_ratio_strs = set()

    for bc, bc_results in base_results.items():
        for r in bc_results:
            if r.get('source') == 'predicted':
                continue
            ratio = r.get('ratio')
            if not ratio:
                continue
            feats = _build_features_with_width(
                ratio, sizes, geom, base_width_mm,
                shrinkage_x=base_shrinkage[0], shrinkage_y=base_shrinkage[1],
            )
            X_train.append(feats)
            y_train.append(r['length_yards'])
            train_ratio_strs.add(r['ratio_str'])

    # Add anchor results at target width (using target-lot geometry)
    anchor_ratio_strs = set()
    for r in anchor_results:
        ratio = r.get('ratio')
        if not ratio:
            continue
        feats = _build_features_with_width(
            ratio, sizes, tgt_geom, target_width_mm,
            shrinkage_x=target_shrinkage[0], shrinkage_y=target_shrinkage[1],
        )
        X_train.append(feats)
        y_train.append(r['length_yards'])
        anchor_ratio_strs.add(r['ratio_str'])

    X_train = np.array(X_train)
    y_train = np.array(y_train)

    if len(X_train) < 5:
        logger.warning("Cross-width predict: too few samples (%d)", len(X_train))
        return {}

    model = Ridge(alpha=1.0)
    model.fit(X_train, y_train)

    # Log training R²
    y_pred_train = model.predict(X_train)
    ss_res = np.sum((y_train - y_pred_train) ** 2)
    ss_tot = np.sum((y_train - np.mean(y_train)) ** 2)
    r2 = 1 - ss_res / ss_tot if ss_tot > 0 else 0
    logger.info(
        "Cross-width Ridge: base=%.0f\" → target=%.0f\", R²=%.4f, %d train samples",
        base_width_inches, target_width_inches, r2, len(X_train),
    )

    # Predict all ratios from base results that weren't anchored
    predicted_by_bc: Dict[int, List[Dict]] = {}

    for bc, bc_results in base_results.items():
        predicted_by_bc[bc] = []
        for r in bc_results:
            ratio = r.get('ratio')
            if not ratio:
                continue
            rstr = r['ratio_str']

            # If this ratio was anchored, use its actual GPU result
            if rstr in anchor_ratio_strs:
                # Find the anchor result
                for ar in anchor_results:
                    if ar['ratio_str'] == rstr:
                        predicted_by_bc[bc].append(ar)
                        break
                continue

            # Predict at target width/lot
            feats = _build_features_with_width(
                ratio, sizes, tgt_geom, target_width_mm,
                shrinkage_x=target_shrinkage[0], shrinkage_y=target_shrinkage[1],
            )
            pred_length = float(model.predict([feats])[0])
            if pred_length <= 0:
                pred_length = 0.001

            # Compute efficiency from predicted length (vector area)
            total_vector_area_mm2 = 0.0
            for size, count in ratio.items():
                if count <= 0 or size not in pieces_by_size:
                    continue
                for _ in range(count):
                    for p in pieces_by_size[size]:
                        total_vector_area_mm2 += _polygon_area_mm2(p['vertices_mm']) * p['demand']

            fabric_width_mm = target_strip_px / gpu_scale
            pred_length_mm = pred_length * 25.4 * 36
            eff = total_vector_area_mm2 / (fabric_width_mm * pred_length_mm) if pred_length_mm > 0 else 0
            eff = max(0, min(1, eff))

            predicted_by_bc[bc].append({
                'ratio': ratio,
                'ratio_str': rstr,
                'efficiency': eff,
                'length_yards': pred_length,
                'bundle_count': r['bundle_count'],
                'perimeter_cm': r.get('perimeter_cm', 0),
                'source': 'cross_width_predicted',
            })

    return predicted_by_bc


@dataclass
class FabricLot:
    """A fabric lot defined by shrinkage group + width + available yardage."""
    shrinkage: str          # e.g., "3.5X3.5"
    shrinkage_x: float      # X shrinkage % (e.g., 3.5)
    shrinkage_y: float      # Y shrinkage % (e.g., 3.5)
    width_inches: float     # Cuttable width in inches (e.g., 71.0)
    available_yards: float  # Fabric available in this lot (after waste deduction)
    dxf_path: str           # Path to shrinkage-specific DXF pattern
    rul_path: Optional[str] # Path to shrinkage-specific RUL grading file

    @property
    def key(self) -> str:
        return f"{self.shrinkage}_{self.width_inches}"

    @staticmethod
    def parse_shrinkage(shrinkage_str: str) -> Tuple[float, float]:
        """Parse '3.5X3.5' into (3.5, 3.5)."""
        parts = shrinkage_str.upper().split('X')
        return float(parts[0]), float(parts[1]) if len(parts) > 1 else float(parts[0])


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
    nesting_strategy: str = "auto",
    fabric_widths: Optional[List[float]] = None,
    material_sources: Optional[List[str]] = None,
    fabric_lots: Optional[List[FabricLot]] = None,
) -> Dict[float, Dict[int, List[Dict]]]:
    """
    Run GPU nesting for a specific material at one or more fabric widths.

    Adaptive architecture that scales to any order size:
    - total_ratios ≤ 1,500: whole-order brute force (all BCs, all ratios)
    - total_ratios > 1,500: adaptive pipeline
      - bc=1,2: always brute force (hard rule)
      - bc=3 to base_max_bc: Phase 1 adaptive sampling (+Ridge where ≤10K ratios)
      - bc > base_max_bc: Phase 2-4 multiplier expansion + neighborhood refinement

    Multi-width mode (fabric_widths has >1 entry):
    - Runs full pipeline at the widest (base) width
    - Uses cross-width Ridge prediction for other widths with anchor sampling

    Args:
        dxf_path: Path to DXF pattern file
        rul_path: Path to RUL grading file
        material: Material code to filter
        sizes: List of sizes to process
        fabric_width_inches: Fabric width in inches (used when fabric_widths is None)
        max_bundle_count: Maximum bundles per marker
        top_n: Number of top results per bundle count
        gpu_scale: Rasterization resolution (px/mm)
        progress_callback: Optional callback for progress updates
        preview_callback: Optional callback for marker previews
        preview_interval_seconds: How often to capture previews
        full_coverage: If True, force brute force at all BCs
        result_callback: Optional callback called after each bundle_count completes
        cancel_check: Optional callable returning True to cancel
        file_type: Pattern file type ("aama", "dxf_only", "vt_dxf")
        nesting_strategy: "auto" (adaptive), "brute_force", or "lhs_predict" (legacy)
        fabric_widths: Optional list of fabric widths in inches for multi-width mode

    Returns:
        Dictionary mapping width_inches -> {bundle_count -> list of result dicts}
    """
    if not _init_gpu():
        raise RuntimeError("GPU not available for nesting")

    # Determine widths list
    if fabric_widths and len(fabric_widths) > 1:
        widths = sorted(fabric_widths)
        base_width = max(widths)  # Widest as base
        extra_widths = [w for w in widths if w != base_width]
        multi_width = True
    else:
        base_width = fabric_widths[0] if fabric_widths and len(fabric_widths) == 1 else fabric_width_inches
        widths = [base_width]
        extra_widths = []
        multi_width = False

    fabric_width_mm = base_width * 25.4
    strip_width_px = int(fabric_width_mm * gpu_scale)

    # Load and rasterize pieces (once — rasters are width-independent)
    if progress_callback:
        progress_callback(0, f"Loading pieces for material {material}...")

    pieces_by_size = load_pieces_for_material(
        dxf_path, rul_path, material, sizes, gpu_scale, file_type=file_type,
        material_sources=material_sources,
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

    n_sizes = len(sizes)
    total_ratios = _compute_total_ratios(n_sizes, max_bundle_count)
    evaluated_count_ref = [0]  # Mutable for batch helper

    # ── Sort strategy calibration ──
    # Quick LHS-on-LHS test (~9 markers) to pick the best sort for this pattern.
    # Saves ~50% of GPU time by avoiding dual_sort on every ratio.
    sort_strategy = _calibrate_sort_strategy(
        pieces_by_size, packer, strip_width_px, gpu_scale, sizes,
        progress_callback=progress_callback,
    )

    # Common kwargs for _evaluate_ratios_batch
    batch_kwargs = dict(
        pieces_by_size=pieces_by_size,
        packer=packer,
        strip_width_px=strip_width_px,
        gpu_scale=gpu_scale,
        sizes=sizes,
        cancel_check=cancel_check,
        progress_callback=progress_callback,
        preview_callback=preview_callback,
        preview_interval_seconds=preview_interval_seconds,
        evaluated_count_ref=evaluated_count_ref,
        sort_strategy=sort_strategy,
    )

    # -------------------------------------------------------------------
    # Strategy dispatch
    # -------------------------------------------------------------------
    if nesting_strategy == "brute_force" or full_coverage:
        strategy = "brute_force"
    elif nesting_strategy == "lhs_predict":
        strategy = "lhs_predict"
    else:
        strategy = "auto"

    width_label = f"{base_width}\"" if not multi_width else f"{base_width}\" (base of {len(widths)} widths)"
    if progress_callback:
        if strategy == "brute_force":
            mode = "full coverage (brute force)"
        elif strategy == "lhs_predict":
            mode = "LHS + Ridge predict (legacy)"
        elif total_ratios <= WHOLE_ORDER_THRESHOLD:
            mode = f"whole-order brute force ({total_ratios} ≤ {WHOLE_ORDER_THRESHOLD})"
        else:
            mode = f"adaptive ({total_ratios:,} ratios, {int(LHS_SAMPLE_RATE*100)}% LHS + Ridge)"
        progress_callback(2, f"Ratio space: {total_ratios:,} across {max_bundle_count} BCs @ {width_label} ({mode})")

    # -------------------------------------------------------------------
    # STRATEGY 1: Forced brute force
    # -------------------------------------------------------------------
    if strategy == "brute_force":
        base_results = _run_brute_force_all(
            max_bundle_count, result_callback, **batch_kwargs
        )

    # -------------------------------------------------------------------
    # STRATEGY 2: Legacy LHS+predict (backward compat)
    # -------------------------------------------------------------------
    elif strategy == "lhs_predict":
        base_results = _run_lhs_predict_all(
            max_bundle_count, fabric_width_mm,
            result_callback, **batch_kwargs
        )

    # -------------------------------------------------------------------
    # STRATEGY 3: Auto — adaptive architecture
    # -------------------------------------------------------------------
    else:
        if total_ratios <= WHOLE_ORDER_THRESHOLD:
            # Small order: evaluate everything
            base_results = _run_brute_force_all(
                max_bundle_count, result_callback, **batch_kwargs
            )
        else:
            # Large order: adaptive pipeline
            base_results = _run_adaptive_pipeline(
                max_bundle_count, fabric_width_mm,
                total_ratios, result_callback, **batch_kwargs
            )

    # -------------------------------------------------------------------
    # Post-processing: sort retained results, SVG previews (base width)
    # -------------------------------------------------------------------
    # Dual-sort is already applied during screening — just sort by efficiency
    for bc, bc_results in base_results.items():
        bc_results.sort(key=lambda x: -x['efficiency'])

    # SVG preview for best result per BC
    svg_total = sum(1 for v in base_results.values() if v)
    if progress_callback:
        progress_callback(95, f"Generating vector previews for {svg_total} top markers...")

    for bc, bc_results in base_results.items():
        if bc_results:
            r = bc_results[0]
            ratio = r.get('ratio')
            if ratio:
                _, _, svg, perim_cm = evaluate_ratio_with_svg(
                    pieces_by_size, ratio, packer, strip_width_px, gpu_scale,
                    sort_strategy=sort_strategy,
                )
                r['svg_preview'] = svg
                r['perimeter_cm'] = perim_cm

    total_saved = sum(len(v) for v in base_results.values())
    evaluated_count = evaluated_count_ref[0]

    # -------------------------------------------------------------------
    # Multi-width: cross-width Ridge prediction for extra widths
    # -------------------------------------------------------------------
    all_width_results: Dict[float, Dict[int, List[Dict]]] = {base_width: base_results}

    if multi_width and extra_widths:
        geom = _extract_geometry(pieces_by_size, sizes, fabric_width_mm, gpu_scale)

        for extra_w in extra_widths:
            if cancel_check and cancel_check():
                raise NestingCancelled("Job cancelled by user")

            extra_w_mm = extra_w * 25.4
            extra_strip_px = int(extra_w_mm * gpu_scale)
            extra_max_length = int((max_bundle_count * max_area * 2) / extra_strip_px) + 500
            extra_packer = GPUPacker(extra_strip_px, extra_max_length)

            # ----------------------------------------------------------
            # Step 1: Brute-force bc=1,2 at extra width (hard rule)
            # ----------------------------------------------------------
            if progress_callback:
                progress_callback(96, f"Cross-width {extra_w}\": brute-forcing bc=1,2...")

            bf_results: List[Dict] = []
            for bc in [1, 2]:
                if bc > max_bundle_count:
                    continue
                all_ratios = generate_all_ratios(bc, sizes)
                for ratio in all_ratios:
                    eff, length, _, perim = evaluate_ratio(
                        pieces_by_size, ratio, extra_packer, extra_strip_px, gpu_scale,
                        sort_strategy=sort_strategy,
                    )
                    bf_results.append({
                        'ratio': ratio,
                        'ratio_str': ratio_to_str(ratio, sizes),
                        'efficiency': eff,
                        'length_yards': length,
                        'bundle_count': bc,
                        'perimeter_cm': perim,
                        'source': 'gpu',
                    })
            evaluated_count_ref[0] += len(bf_results)

            # ----------------------------------------------------------
            # Step 2: Sample diverse bc=3+ anchors and GPU-eval at extra width
            # ----------------------------------------------------------
            anchor_ratios = _select_cross_width_anchors(base_results, sizes)

            if progress_callback:
                progress_callback(
                    96,
                    f"Cross-width {extra_w}\": {len(bf_results)} bc=1,2 done, "
                    f"evaluating {len(anchor_ratios)} bc=3+ anchors...",
                )

            anchor_results: List[Dict] = []
            for ratio in anchor_ratios:
                eff, length, _, perim = evaluate_ratio(
                    pieces_by_size, ratio, extra_packer, extra_strip_px, gpu_scale,
                    sort_strategy=sort_strategy,
                )
                bc = sum(ratio.get(s, 0) for s in sizes)
                anchor_results.append({
                    'ratio': ratio,
                    'ratio_str': ratio_to_str(ratio, sizes),
                    'efficiency': eff,
                    'length_yards': length,
                    'bundle_count': bc,
                    'perimeter_cm': perim,
                    'source': 'gpu',
                })
            evaluated_count_ref[0] += len(anchor_ratios)

            logger.info(
                "Cross-width %s\": %d bc=1,2 brute-forced, %d bc=3+ anchors GPU-evaluated",
                extra_w, len(bf_results), len(anchor_ratios),
            )

            # ----------------------------------------------------------
            # Step 3: Train Ridge on all data and predict remaining bc=3+
            # ----------------------------------------------------------
            if progress_callback:
                progress_callback(97, f"Cross-width {extra_w}\": Ridge predicting bc=3+...")

            # Combine bf + anchor as the anchor set for Ridge training
            all_anchor_results = bf_results + anchor_results

            extra_results = _cross_width_predict(
                base_results, all_anchor_results,
                base_width, extra_w,
                sizes, geom, pieces_by_size, gpu_scale,
            )

            # ----------------------------------------------------------
            # Step 4: GPU-verify top-N predicted ratios at extra width
            # ----------------------------------------------------------
            if progress_callback:
                progress_callback(
                    98,
                    f"Cross-width {extra_w}\": verifying top {CROSS_WIDTH_TOP_N_VERIFY} predictions...",
                )

            verified_count = 0
            for bc in sorted(extra_results.keys()):
                if bc <= 2:
                    continue  # bc=1,2 already fully GPU-evaluated
                bc_preds = extra_results[bc]
                # Sort by predicted efficiency to find top candidates
                bc_preds.sort(key=lambda x: -x['efficiency'])
                # Verify top-N that were predicted (not already GPU-evaluated)
                to_verify = [
                    r for r in bc_preds
                    if r.get('source') == 'cross_width_predicted'
                ][:CROSS_WIDTH_TOP_N_VERIFY]

                for r in to_verify:
                    ratio = r.get('ratio')
                    if not ratio:
                        continue
                    eff, length, _, perim = evaluate_ratio(
                        pieces_by_size, ratio, extra_packer, extra_strip_px, gpu_scale,
                        sort_strategy=sort_strategy,
                    )
                    # Replace predicted values with actuals
                    r['efficiency'] = eff
                    r['length_yards'] = length
                    r['perimeter_cm'] = perim
                    r['source'] = 'gpu_verified'
                    verified_count += 1

            evaluated_count_ref[0] += verified_count

            logger.info(
                "Cross-width %s\": %d top predictions GPU-verified",
                extra_w, verified_count,
            )

            # Sort and retain per BC
            for bc in sorted(extra_results.keys()):
                bc_results = extra_results[bc]
                bc_results.sort(key=lambda x: -x['efficiency'])
                if bc > 2:
                    keep_count = max(25, int(len(bc_results) * 0.25))
                    extra_results[bc] = bc_results[:keep_count]

            all_width_results[extra_w] = extra_results

        total_saved += sum(
            len(v) for w in extra_widths for v in all_width_results.get(w, {}).values()
        )

    # -------------------------------------------------------------------
    # Multi-lot: cross-lot prediction for fabric lots with different
    # shrinkage variants (different DXF patterns per lot).
    #
    # Mirrors the multi-width pipeline:
    #   1. Brute-force bc=1,2 at each lot (with lot-specific pieces)
    #   2. Diversity-sample bc=3+ anchors via _select_cross_width_anchors
    #   3. GPU-eval anchors at lot
    #   4. Ridge train (base + lot anchors) → predict remaining bc=3+
    #   5. GPU-verify top-N predictions
    # -------------------------------------------------------------------
    if fabric_lots and len(fabric_lots) > 1:
        import time as _time

        base_lot = max(fabric_lots, key=lambda l: l.available_yards)
        extra_lots = [l for l in fabric_lots if l.key != base_lot.key]

        # Base lot geometry (for Ridge feature building)
        base_geom = _extract_geometry(pieces_by_size, sizes, fabric_width_mm, gpu_scale)

        # Select diverse bc=3+ anchors from base results (farthest-first)
        anchor_ratios = _select_cross_width_anchors(base_results, sizes)

        if progress_callback:
            progress_callback(
                92,
                f"Multi-lot: {len(anchor_ratios)} diverse anchors, "
                f"bc=1,2 brute-force × {len(extra_lots)} lots",
            )

        lot_results: Dict[str, Dict[int, List[Dict]]] = {base_lot.key: base_results}
        lot_timings: Dict[str, float] = {}

        for lot_idx, lot in enumerate(extra_lots):
            if cancel_check and cancel_check():
                raise NestingCancelled("Job cancelled by user")

            lot_t0 = _time.time()

            # Load lot-specific pieces (different DXF for different shrinkage)
            lot_pieces = load_pieces_for_material(
                lot.dxf_path, lot.rul_path, material, sizes, gpu_scale,
                file_type=file_type, material_sources=material_sources,
            )
            lot_strip_px = int(lot.width_inches * 25.4 * gpu_scale)
            lot_max_length = int((max_bundle_count * max_area * 2) / lot_strip_px) + 500
            lot_packer = GPUPacker(lot_strip_px, lot_max_length)
            lot_geom = _extract_geometry(lot_pieces, sizes, lot.width_inches * 25.4, gpu_scale)

            # ----------------------------------------------------------
            # Step 1: Brute-force bc=1,2 at this lot (hard rule)
            # ----------------------------------------------------------
            bf_results: List[Dict] = []
            for bc in [1, 2]:
                if bc > max_bundle_count:
                    continue
                all_ratios = generate_all_ratios(bc, sizes)
                for ratio in all_ratios:
                    eff, length, _, perim = evaluate_ratio(
                        lot_pieces, ratio, lot_packer, lot_strip_px, gpu_scale,
                        sort_strategy=sort_strategy,
                    )
                    bf_results.append({
                        'ratio': ratio,
                        'ratio_str': ratio_to_str(ratio, sizes),
                        'efficiency': eff,
                        'length_yards': length,
                        'bundle_count': bc,
                        'perimeter_cm': perim,
                        'source': 'gpu',
                    })
            evaluated_count_ref[0] += len(bf_results)

            # ----------------------------------------------------------
            # Step 2: GPU-eval diverse bc=3+ anchors at this lot
            # ----------------------------------------------------------
            anchor_results: List[Dict] = []
            for ratio in anchor_ratios:
                eff, length, _, perim = evaluate_ratio(
                    lot_pieces, ratio, lot_packer, lot_strip_px, gpu_scale,
                    sort_strategy=sort_strategy,
                )
                bc = sum(ratio.get(s, 0) for s in sizes)
                anchor_results.append({
                    'ratio': ratio,
                    'ratio_str': ratio_to_str(ratio, sizes),
                    'efficiency': eff,
                    'length_yards': length,
                    'bundle_count': bc,
                    'perimeter_cm': perim,
                    'source': 'gpu',
                })
            evaluated_count_ref[0] += len(anchor_results)

            logger.info(
                "Multi-lot %s: %d bc=1,2 brute-forced, %d bc=3+ anchors GPU-evaluated",
                lot.key, len(bf_results), len(anchor_results),
            )

            # ----------------------------------------------------------
            # Step 3: Ridge train + predict remaining bc=3+
            # ----------------------------------------------------------
            all_lot_anchors = bf_results + anchor_results

            lot_predicted = _cross_width_predict(
                base_results, all_lot_anchors,
                base_lot.width_inches, lot.width_inches,
                sizes, base_geom, pieces_by_size, gpu_scale,
                base_shrinkage=(base_lot.shrinkage_x, base_lot.shrinkage_y),
                target_shrinkage=(lot.shrinkage_x, lot.shrinkage_y),
                target_geom=lot_geom,
            )

            # ----------------------------------------------------------
            # Step 4: GPU-verify top-N predicted ratios at this lot
            # ----------------------------------------------------------
            verified_count = 0
            for bc in sorted(lot_predicted.keys()):
                if bc <= 2:
                    continue  # bc=1,2 already fully GPU-evaluated
                bc_preds = lot_predicted[bc]
                bc_preds.sort(key=lambda x: -x['efficiency'])
                to_verify = [
                    r for r in bc_preds
                    if r.get('source') == 'cross_width_predicted'
                ][:CROSS_WIDTH_TOP_N_VERIFY]

                for r in to_verify:
                    ratio = r.get('ratio')
                    if not ratio:
                        continue
                    eff, length, _, perim = evaluate_ratio(
                        lot_pieces, ratio, lot_packer, lot_strip_px, gpu_scale,
                        sort_strategy=sort_strategy,
                    )
                    r['efficiency'] = eff
                    r['length_yards'] = length
                    r['perimeter_cm'] = perim
                    r['source'] = 'gpu_verified'
                    verified_count += 1

            evaluated_count_ref[0] += verified_count

            # Sort and retain per BC
            for bc in sorted(lot_predicted.keys()):
                bc_results_lot = lot_predicted[bc]
                bc_results_lot.sort(key=lambda x: -x['efficiency'])
                if bc > 2:
                    keep_count = max(25, int(len(bc_results_lot) * 0.25))
                    lot_predicted[bc] = bc_results_lot[:keep_count]

            lot_results[lot.key] = lot_predicted

            lot_elapsed = _time.time() - lot_t0
            lot_timings[lot.key] = lot_elapsed

            if progress_callback:
                progress_callback(
                    92 + int(6 * (lot_idx + 1) / len(extra_lots)),
                    f"Multi-lot {lot.key}: {len(bf_results)} bf + "
                    f"{len(anchor_results)} anchors + {verified_count} verified "
                    f"({lot_elapsed:.1f}s)",
                )

            logger.info(
                "Multi-lot %s: %d bf, %d anchors, %d predicted, %d verified (%.1fs)",
                lot.key, len(bf_results), len(anchor_results),
                sum(len(v) for v in lot_predicted.values()),
                verified_count, lot_elapsed,
            )

        # Store lot results + timings
        all_width_results['_lot_results'] = lot_results
        all_width_results['_lot_timings'] = lot_timings

    if progress_callback:
        width_str = f" across {len(widths)} widths" if multi_width else ""
        lot_str = f", {len(fabric_lots)} lots" if fabric_lots and len(fabric_lots) > 1 else ""
        progress_callback(
            100,
            f"Complete — {evaluated_count_ref[0]} ratios evaluated, {total_saved} markers saved{width_str}{lot_str}",
        )

    return all_width_results


# ---------------------------------------------------------------------------
# Strategy Implementations
# ---------------------------------------------------------------------------

def _run_brute_force_all(
    max_bundle_count: int,
    result_callback: Optional[Callable],
    **batch_kwargs,
) -> Dict[int, List[Dict]]:
    """Brute-force all ratios at all BCs."""
    sizes = batch_kwargs['sizes']
    all_results = {}

    for bc in range(1, max_bundle_count + 1):
        if batch_kwargs.get('cancel_check') and batch_kwargs['cancel_check']():
            raise NestingCancelled("Job cancelled by user")

        all_ratios = generate_all_ratios(bc, sizes)
        progress_span = max(1, int(80 / max_bundle_count))
        base_progress = int((bc - 1) / max_bundle_count * 80)

        results = _evaluate_ratios_batch(
            ratios=all_ratios,
            bundle_count_label=f"Brute force: {bc}-bundle",
            source_tag='gpu',
            base_progress=base_progress,
            progress_span=progress_span,
            **batch_kwargs,
        )

        results.sort(key=lambda x: -x['efficiency'])
        if bc <= 2:
            all_results[bc] = results
        else:
            keep_count = max(25, int(len(results) * 0.25))
            all_results[bc] = results[:keep_count]

        if result_callback:
            result_callback(bc, all_results[bc])

    return all_results


def _run_lhs_predict_all(
    max_bundle_count: int,
    fabric_width_mm: float,
    result_callback: Optional[Callable],
    **batch_kwargs,
) -> Dict[int, List[Dict]]:
    """Legacy LHS+Ridge predict path for all BCs."""
    sizes = batch_kwargs['sizes']
    pieces_by_size = batch_kwargs['pieces_by_size']
    all_results = {}
    gpu_scale = batch_kwargs['gpu_scale']
    strip_width_px = batch_kwargs['strip_width_px']
    progress_callback = batch_kwargs.get('progress_callback')
    geom = None

    for bc in range(1, max_bundle_count + 1):
        if batch_kwargs.get('cancel_check') and batch_kwargs['cancel_check']():
            raise NestingCancelled("Job cancelled by user")

        all_ratios = generate_all_ratios(bc, sizes)
        n_combos = len(all_ratios)
        progress_span = max(1, int(80 / max_bundle_count))
        base_progress = int((bc - 1) / max_bundle_count * 80)

        # bc=1,2 always brute force (hard rule)
        if bc <= 2:
            results = _evaluate_ratios_batch(
                ratios=all_ratios,
                bundle_count_label=f"Brute force: {bc}-bundle",
                source_tag='gpu',
                base_progress=base_progress,
                progress_span=progress_span,
                **batch_kwargs,
            )
        else:
            # LHS sample + Ridge predict
            if geom is None:
                geom = _extract_geometry(pieces_by_size, sizes, fabric_width_mm, gpu_scale)

            sample_size = max(LHS_SAMPLE_MIN, min(LHS_SAMPLE_MAX, int(n_combos * LHS_SAMPLE_RATE)))
            sample_ratios = _lhs_sample(all_ratios, sizes, sample_size)

            sample_results = _evaluate_ratios_batch(
                ratios=sample_ratios,
                bundle_count_label=f"LHS sample: {bc}-bundle",
                source_tag='gpu',
                base_progress=base_progress,
                progress_span=progress_span // 2,
                **batch_kwargs,
            )

            if progress_callback:
                n_remaining = n_combos - len(sample_results)
                progress_callback(
                    min(base_progress + progress_span // 2, 95),
                    f"Ridge predicting: {bc}-bundle ({n_remaining} remaining)...",
                )

            predicted = _lhs_predict_ratios(
                all_ratios, sample_results, sizes, geom,
                fabric_width_mm, gpu_scale, strip_width_px, pieces_by_size,
            )
            results = sample_results + predicted

        results.sort(key=lambda x: -x['efficiency'])
        if bc <= 2:
            all_results[bc] = results
        else:
            keep_count = max(25, int(len(results) * 0.25))
            all_results[bc] = results[:keep_count]

        if result_callback:
            result_callback(bc, all_results[bc])

    return all_results


def _run_adaptive_pipeline(
    max_bundle_count: int,
    fabric_width_mm: float,
    total_ratios: int,
    result_callback: Optional[Callable],
    **batch_kwargs,
) -> Dict[int, List[Dict]]:
    """
    Adaptive nesting pipeline for large ratio spaces.

    Phase 1: bc=1,2 brute force; bc=3..base_max adaptive sampling (+Ridge where ≤10K)
    Phase 2: Plateau detection + seed selection from Phase 1 results
    Phase 3: Multiplier expansion of seeds to higher BCs
    Phase 4: Neighborhood perturbation of top multiplied candidates
    """
    sizes = batch_kwargs['sizes']
    pieces_by_size = batch_kwargs['pieces_by_size']
    all_results = {}
    gpu_scale = batch_kwargs['gpu_scale']
    strip_width_px = batch_kwargs['strip_width_px']
    progress_callback = batch_kwargs.get('progress_callback')

    base_max_bc = min(max_bundle_count, BASE_MAX_BC)
    needs_expansion = max_bundle_count > base_max_bc

    # Allocate progress budget:
    #   Phase 1: 0-60%  (bc=1..base_max exploration)
    #   Phase 3: 60-75% (multiplier expansion)
    #   Phase 4: 75-85% (neighborhood refinement)
    #   Post:    85-100% (dual-sort, SVG — handled by caller)
    phase1_budget = 60
    phase3_budget = 15 if needs_expansion else 0
    phase4_budget = 10 if needs_expansion else 0

    geom = None  # Lazy-init for Ridge

    # ---------------------------------------------------------------
    # Phase 1: Explore bc=1 through base_max_bc
    # ---------------------------------------------------------------
    phase1_results = {}  # {bc: [results]} — full results for plateau detection

    for bc in range(1, base_max_bc + 1):
        if batch_kwargs.get('cancel_check') and batch_kwargs['cancel_check']():
            raise NestingCancelled("Job cancelled by user")

        all_ratios = generate_all_ratios(bc, sizes)
        n_combos = len(all_ratios)
        per_bc_span = max(1, phase1_budget // base_max_bc)
        base_progress = int((bc - 1) / base_max_bc * phase1_budget)

        # bc=1,2: always brute force (hard rule)
        if bc <= 2:
            results = _evaluate_ratios_batch(
                ratios=all_ratios,
                bundle_count_label=f"Brute force: {bc}-bundle",
                source_tag='gpu',
                base_progress=base_progress,
                progress_span=per_bc_span,
                **batch_kwargs,
            )
            phase1_results[bc] = results

        else:
            # Adaptive: percentage-based budget with floor/cap
            budget = max(LHS_SAMPLE_MIN, min(LHS_SAMPLE_MAX, int(n_combos * LHS_SAMPLE_RATE)))
            budget = min(budget, n_combos)  # Can't sample more than exist

            if budget >= n_combos:
                # 100% coverage — evaluate all
                sample_ratios = all_ratios
            else:
                # LHS sample
                sample_ratios = _lhs_sample(all_ratios, sizes, budget)

            gpu_results = _evaluate_ratios_batch(
                ratios=sample_ratios,
                bundle_count_label=f"Phase 1: {bc}-bundle ({budget}/{n_combos})",
                source_tag='gpu',
                base_progress=base_progress,
                progress_span=per_bc_span,
                **batch_kwargs,
            )

            # Always Ridge-predict remaining ratios (model learns cross-BC patterns)
            if budget < n_combos:
                if geom is None:
                    geom = _extract_geometry(pieces_by_size, sizes, fabric_width_mm, gpu_scale)

                predicted = _lhs_predict_ratios(
                    all_ratios, gpu_results, sizes, geom,
                    fabric_width_mm, gpu_scale, strip_width_px, pieces_by_size,
                )
                results = gpu_results + predicted
                logger.info(
                    "BC %d: %d GPU + %d Ridge = %d total (of %d)",
                    bc, len(gpu_results), len(predicted), len(results), n_combos,
                )
            else:
                results = gpu_results

            phase1_results[bc] = results

        # Store retained results
        results = phase1_results[bc]
        results.sort(key=lambda x: -x['efficiency'])
        if bc <= 2:
            all_results[bc] = results
        else:
            keep_count = max(25, int(len(results) * 0.25))
            all_results[bc] = results[:keep_count]

        if result_callback:
            result_callback(bc, all_results[bc])

    # ---------------------------------------------------------------
    # Phase 2: Plateau detection + seed selection (if expansion needed)
    # ---------------------------------------------------------------
    if not needs_expansion:
        # All BCs covered in Phase 1, no multiplier needed
        return all_results

    if progress_callback:
        progress_callback(phase1_budget, "Phase 2: Detecting plateau and selecting seeds...")

    plateau_bc, productive_bcs = _detect_plateau(phase1_results)
    if not productive_bcs:
        # Fallback: use all explored BCs ≥ 3
        productive_bcs = [bc for bc in range(3, base_max_bc + 1) if bc in phase1_results]

    seeds = _select_seeds(phase1_results, productive_bcs, sizes, n=SEED_COUNT)
    total_seeds = sum(len(s) for s in seeds.values())

    logger.info(
        "Plateau at bc=%d, productive BCs: %s, seeds: %d total",
        plateau_bc, productive_bcs, total_seeds,
    )
    if progress_callback:
        progress_callback(
            phase1_budget + 1,
            f"Phase 2: Plateau at bc={plateau_bc}, {total_seeds} seeds from {len(productive_bcs)} productive BCs",
        )

    # ---------------------------------------------------------------
    # Phase 3: Multiplier expansion (bc > base_max_bc)
    # ---------------------------------------------------------------
    multiplied_ratios = _generate_multiplied(seeds, max_bundle_count, sizes)

    if progress_callback:
        progress_callback(
            phase1_budget + 2,
            f"Phase 3: Evaluating {len(multiplied_ratios)} multiplied candidates...",
        )

    if multiplied_ratios:
        multiplied_results = _evaluate_ratios_batch(
            ratios=multiplied_ratios,
            bundle_count_label=f"Phase 3: Multiplied candidates",
            source_tag='multiplied',
            base_progress=phase1_budget + 2,
            progress_span=phase3_budget - 2,
            **batch_kwargs,
        )
    else:
        multiplied_results = []

    # ---------------------------------------------------------------
    # Phase 4: Neighborhood perturbation of top multiplied candidates
    # ---------------------------------------------------------------
    # Collect all evaluated keys so far (Phase 1 + Phase 3)
    all_evaluated_keys = set()
    for bc_results_list in phase1_results.values():
        for r in bc_results_list:
            if r.get('ratio'):
                all_evaluated_keys.add(ratio_to_key(r['ratio'], sizes))
    for r in multiplied_results:
        if r.get('ratio'):
            all_evaluated_keys.add(ratio_to_key(r['ratio'], sizes))

    # Select top-N from multiplied results for neighborhood search
    if multiplied_results:
        multiplied_sorted = sorted(multiplied_results, key=lambda x: -x['efficiency'])
        top_multiplied_ratios = [
            r['ratio'] for r in multiplied_sorted[:NEIGHBORHOOD_TOP_N]
            if r.get('ratio')
        ]

        neighbor_ratios = _generate_neighborhoods(top_multiplied_ratios, sizes, all_evaluated_keys)

        if progress_callback:
            progress_callback(
                phase1_budget + phase3_budget,
                f"Phase 4: Evaluating {len(neighbor_ratios)} neighborhood candidates...",
            )

        if neighbor_ratios:
            neighbor_results = _evaluate_ratios_batch(
                ratios=neighbor_ratios,
                bundle_count_label="Phase 4: Neighborhoods",
                source_tag='neighborhood',
                base_progress=phase1_budget + phase3_budget,
                progress_span=phase4_budget,
                **batch_kwargs,
            )
        else:
            neighbor_results = []
    else:
        neighbor_results = []

    # ---------------------------------------------------------------
    # Merge Phase 3 + Phase 4 results into all_results by BC
    # ---------------------------------------------------------------
    for r in multiplied_results + neighbor_results:
        bc = r['bundle_count']
        if bc not in all_results:
            all_results[bc] = []
        all_results[bc].append(r)

    # Sort and retain for expanded BCs
    for bc in sorted(all_results.keys()):
        if bc <= base_max_bc:
            continue  # Already retained in Phase 1
        bc_results = all_results[bc]
        bc_results.sort(key=lambda x: -x['efficiency'])
        keep_count = max(25, int(len(bc_results) * 0.25))
        all_results[bc] = bc_results[:keep_count]

        if result_callback:
            result_callback(bc, all_results[bc])

    return all_results


