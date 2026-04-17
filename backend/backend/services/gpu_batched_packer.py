"""
GPU Batched BLF Packer — monolithic-kernel bulk evaluator for ratio screening.

Drop-in enhancement to gpu_nesting_runner.py's per-ratio evaluation. Designed for
workloads where many ratios (>= ~64) are evaluated in a single pass, such as LHS /
brute-force ratio screening during cutplan search.

Key characteristics:
  - 3.17x faster than per-ratio Python BLF on 461-ratio sweeps (MK1 benchmark).
  - Bit-identical length output to production Python BLF when `prefer_rot0=True`
    (the default), so it's a risk-free drop-in for the screening path.
  - One CUDA kernel launch processes up to `batch_size` sequences simultaneously
    (one CUDA block per sequence). Saturates 30 SMs at batch >= 128.

When NOT to use:
  - Single-ratio evaluation. At N=1 the monolithic kernel uses 1 of 30 SMs,
    which is SLOWER than the existing `evaluate_ratio` (single-sequence BLF).
    Keep using the existing single-ratio path for per-marker recomputation.
  - Lookup budgets <64 ratios: overhead makes the batch path marginal.

Usage:
    from backend.services.gpu_batched_packer import evaluate_ratios_batched

    results = evaluate_ratios_batched(
        ratios=candidate_ratios,       # List[Dict[str, int]]
        pieces_by_size=pbs,            # from load_pieces_for_material(...)
        fabric_width_mm=1400.0,
        gpu_scale=0.15,
        max_length_mm=15000.0,
        sort_strategy="area_desc",     # or "width_desc"
        dual_sort=False,               # True tries both, keeps shorter (2x cost)
        batch_size=256,
    )
    # results: List[{"length_mm", "length_m", "efficiency",
    #                "efficiency_pct", "sort_used", "sequence",
    #                "placements": [{"piece_idx","name","size","x_mm","y_mm","rot_deg"}]}]
"""
from __future__ import annotations

import time
from typing import Dict, List, Tuple, Optional

import numpy as np

try:
    import cupy as cp
    _CUPY_AVAILABLE = True
except ImportError:
    cp = None
    _CUPY_AVAILABLE = False

# Re-use helpers from the sibling module (polygon area, sort keys).
from backend.services.gpu_nesting_runner import (
    _polygon_area_mm2, _SORT_KEY_FUNCS,
)


# =============================================================================
#  CUDA kernel
# =============================================================================
_KERNEL_SRC = r'''
extern "C" __global__
void batched_blf(
    const float* __restrict__ raster_bank,   // all piece rasters, concatenated
    const int*   __restrict__ piece_offs,    // offset of (piece, rot) in raster_bank
    const int*   __restrict__ piece_shape,   // (ph, pw) per piece (same for both rotations)
    const int*   __restrict__ sequences,     // [N_seq, N_pieces] piece indices
    float*        containers,                 // [N_seq, strip_w, max_len]
    int*          placements,                 // [N_seq, N_pieces, 3] output (x, y, rot)
    int*          lengths_out,                // [N_seq] strip length (px)
    int N_pieces, int strip_w, int max_len,
    int active_w_padding,
    int prefer_rot0
) {
    const int seq = blockIdx.x;
    const int tid = threadIdx.x;
    const int BT  = blockDim.x;

    float* my_cont = containers + (long long)seq * (long long)strip_w * (long long)max_len;
    const int* my_seq = sequences + seq * N_pieces;
    int* my_place = placements + seq * N_pieces * 3;

    __shared__ unsigned long long best_packed;
    __shared__ int cur_len;
    if (tid == 0) cur_len = 0;
    __syncthreads();

    for (int p = 0; p < N_pieces; p++) {
        const int pid = my_seq[p];
        const int ph  = piece_shape[pid * 2 + 0];
        const int pw  = piece_shape[pid * 2 + 1];

        int active_w;
        if (cur_len > 0) {
            active_w = cur_len + pw + active_w_padding;
            if (active_w > max_len) active_w = max_len;
        } else {
            active_w = pw + active_w_padding;
            if (active_w > max_len) active_w = max_len;
        }
        const int num_x = active_w - pw + 1;
        const int num_y = strip_w - ph + 1;

        if (tid == 0) {
            best_packed = 0xFFFFFFFFFFFFFFFFULL;
            if (num_x <= 0 || num_y <= 0) {
                my_place[p * 3 + 0] = -1;
                my_place[p * 3 + 1] = -1;
                my_place[p * 3 + 2] = -1;
            }
        }
        __syncthreads();

        if (num_x <= 0 || num_y <= 0) continue;

        const int total = num_x * num_y * 2;
        unsigned long long local_best = 0xFFFFFFFFFFFFFFFFULL;

        for (int i = tid; i < total; i += BT) {
            const int rot = i / (num_x * num_y);
            const int rem = i - rot * (num_x * num_y);
            const int y   = rem / num_x;
            const int x   = rem - y * num_x;

            const int off = piece_offs[pid * 2 + rot];
            const float* piece = raster_bank + off;

            bool fits = true;
            for (int py = 0; py < ph && fits; py++) {
                const long long row_base = (long long)(y + py) * (long long)max_len + x;
                for (int px = 0; px < pw; px++) {
                    if (piece[py * pw + px] > 0.5f && my_cont[row_base + px] > 0.5f) {
                        fits = false;
                        break;
                    }
                }
            }
            if (!fits) continue;

            const int piece_right = x + pw;
            const int piece_top   = y + ph;
            const bool inside = (cur_len == 0) || (piece_right <= cur_len);
            unsigned int score = inside ? (unsigned int)piece_top
                                         : (0x40000000u + (unsigned int)piece_right);
            // Python BLF strictly prefers rot=0; disjoint score bands enforce:
            //   rot=0 inside  <  rot=1 inside  <  rot=0 outside  <  rot=1 outside
            if (prefer_rot0) {
                score += (unsigned int)rot * 0x20000000u;
            }

            unsigned long long packed =
                ((unsigned long long)score << 32)
                | ((unsigned long long)(x & 0xFFFF) << 16)
                | ((unsigned long long)(y & 0x7FFF) << 1)
                | (unsigned long long)(rot & 0x1);

            if (packed < local_best) local_best = packed;
        }

        atomicMin((unsigned long long*)&best_packed, local_best);
        __syncthreads();

        if (best_packed == 0xFFFFFFFFFFFFFFFFULL) {
            if (tid == 0) {
                my_place[p * 3 + 0] = -1;
                my_place[p * 3 + 1] = -1;
                my_place[p * 3 + 2] = -1;
            }
            __syncthreads();
            continue;
        }

        const unsigned long long bp = best_packed;
        const int best_x   = (int)((bp >> 16) & 0xFFFF);
        const int best_y   = (int)((bp >> 1) & 0x7FFF);
        const int best_rot = (int)(bp & 0x1);

        const int place_off = piece_offs[pid * 2 + best_rot];
        const float* piece = raster_bank + place_off;
        const int total_pix = ph * pw;
        for (int k = tid; k < total_pix; k += BT) {
            const int py = k / pw;
            const int px = k - py * pw;
            if (piece[k] > 0.5f) {
                my_cont[(long long)(best_y + py) * (long long)max_len + (best_x + px)] = 1.0f;
            }
        }

        if (tid == 0) {
            my_place[p * 3 + 0] = best_x;
            my_place[p * 3 + 1] = best_y;
            my_place[p * 3 + 2] = best_rot;
            const int new_right = best_x + pw;
            if (new_right > cur_len) cur_len = new_right;
        }
        __syncthreads();
    }

    if (tid == 0) lengths_out[seq] = cur_len;
}
'''


# =============================================================================
#  Raster bank helpers
# =============================================================================

def build_raster_bank(pieces_list: List[Dict]):
    """
    Concatenate all piece rasters (rot=0 and rot=180) into a flat GPU bank.

    Returns:
        raster_bank: cp.ndarray, float32, 1-D
        piece_offs : cp.ndarray, int32, shape [2*N+1]
        piece_shape: cp.ndarray, int32, shape [2*N]
    """
    if not _CUPY_AVAILABLE:
        raise RuntimeError("CuPy not available — BatchedBLF requires CUDA")

    n = len(pieces_list)
    offs = np.zeros(2 * n + 1, dtype=np.int32)
    shapes = np.zeros(n * 2, dtype=np.int32)

    total_px = 0
    for i, p in enumerate(pieces_list):
        r0 = p['raster_gpu']
        shapes[i * 2 + 0] = r0.shape[0]  # ph
        shapes[i * 2 + 1] = r0.shape[1]  # pw
        total_px += int(r0.size) + int(p['raster_180_gpu'].size)

    bank = cp.empty(total_px, dtype=cp.float32)
    pos = 0
    for i, p in enumerate(pieces_list):
        r0 = p['raster_gpu'].astype(cp.float32, copy=False).ravel()
        r1 = p['raster_180_gpu'].astype(cp.float32, copy=False).ravel()
        offs[2 * i + 0] = pos; bank[pos:pos + r0.size] = r0; pos += int(r0.size)
        offs[2 * i + 1] = pos; bank[pos:pos + r1.size] = r1; pos += int(r1.size)
    offs[-1] = pos

    return bank, cp.asarray(offs), cp.asarray(shapes)


# =============================================================================
#  BatchedBLF
# =============================================================================

class BatchedBLF:
    """GPU-resident batched BLF packer. One CUDA block per sequence."""

    def __init__(
        self,
        pieces_list: List[Dict],
        strip_width_px: int,
        max_length_px: int,
        active_w_padding: int = 50,
    ):
        if not _CUPY_AVAILABLE:
            raise RuntimeError("CuPy not available — BatchedBLF requires CUDA")

        self.pieces_list = pieces_list
        self.n_pieces = len(pieces_list)
        self.strip_w = strip_width_px
        self.max_len = max_length_px
        self.active_w_padding = active_w_padding

        self.raster_bank, self.piece_offs, self.piece_shape = build_raster_bank(pieces_list)
        self.kernel = cp.RawKernel(_KERNEL_SRC, "batched_blf")

        container_bytes_per_seq = self.strip_w * self.max_len * 4
        free_mem, _ = cp.cuda.runtime.memGetInfo()
        self.max_seqs = max(1, int((free_mem - 1_000_000_000) // container_bytes_per_seq))

    def solve(
        self,
        sequences_np: np.ndarray,
        block_size: int = 256,
        prefer_rot0: bool = True,
    ):
        """
        Evaluate a batch of sequences in a single kernel launch.

        Args:
            sequences_np: int32 [N_seq, seq_len], values in [0, n_pieces).
            block_size  : CUDA block size (threads per sequence).
            prefer_rot0 : True = matches production Python BLF bit-identically.
                          False = global-argmin over (x, y, rot) — on average +1-2%
                          shorter but with up to ~12% worst-case regression on some
                          ratios. Default True for drop-in safety.

        Returns:
            (lengths_np, placements_np):
              lengths_np:    int32 [N_seq] — strip length in pixels
              placements_np: int32 [N_seq, seq_len, 3] — (x_px, y_px, rot) per step
        """
        n_seq = sequences_np.shape[0]
        seq_len = sequences_np.shape[1]
        max_idx = int(sequences_np.max()) if sequences_np.size else 0
        if max_idx >= self.n_pieces:
            raise ValueError(f"sequence index {max_idx} out of range for bank size {self.n_pieces}")
        if n_seq > self.max_seqs:
            raise ValueError(f"batch n_seq={n_seq} exceeds memory budget {self.max_seqs}")

        sequences = cp.asarray(sequences_np.astype(np.int32))
        containers = cp.zeros((n_seq, self.strip_w, self.max_len), dtype=cp.float32)
        placements = cp.full((n_seq, seq_len, 3), -1, dtype=cp.int32)
        lengths = cp.zeros(n_seq, dtype=cp.int32)

        cp.cuda.Stream.null.synchronize()
        self.kernel(
            (n_seq,), (block_size,),
            (
                self.raster_bank, self.piece_offs, self.piece_shape,
                sequences, containers, placements, lengths,
                np.int32(seq_len),
                np.int32(self.strip_w),
                np.int32(self.max_len),
                np.int32(self.active_w_padding),
                np.int32(1 if prefer_rot0 else 0),
            ),
        )
        cp.cuda.Stream.null.synchronize()

        return cp.asnumpy(lengths), cp.asnumpy(placements)


# =============================================================================
#  Ratio expansion helpers
# =============================================================================

def build_global_piece_index(
    pieces_by_size: Dict[str, List[Dict]],
) -> Tuple[List[Dict], Dict[Tuple[str, str], int]]:
    """
    Flatten pieces_by_size into a single list with a stable (size, name) -> index map.
    Used to build the shared raster bank and to expand each ratio into an index sequence.
    """
    pieces_list: List[Dict] = []
    name_to_idx: Dict[Tuple[str, str], int] = {}
    for size in sorted(pieces_by_size.keys()):
        for p in pieces_by_size[size]:
            idx = len(pieces_list)
            pieces_list.append(p)
            name_to_idx[(size, p["name"])] = idx
    return pieces_list, name_to_idx


def _piece_lookup(global_idx: int, pieces_list: List[Dict]) -> Dict:
    return pieces_list[global_idx]


def expand_ratio_to_sequence(
    ratio: Dict[str, int],
    pieces_by_size: Dict[str, List[Dict]],
    name_to_idx: Dict[Tuple[str, str], int],
    pieces_list: List[Dict],
    sort_strategy: str = "area_desc",
) -> List[int]:
    """
    Expand a {size: bundles} ratio into a piece-index sequence, then sort per strategy.
    """
    seq: List[int] = []
    for size, bundles in ratio.items():
        if bundles <= 0 or size not in pieces_by_size:
            continue
        for _ in range(bundles):
            for p in pieces_by_size[size]:
                demand = p.get("demand", 1)
                idx = name_to_idx[(size, p["name"])]
                for _ in range(demand):
                    seq.append(idx)

    if sort_strategy not in _SORT_KEY_FUNCS:
        raise ValueError(f"unknown sort_strategy: {sort_strategy!r}")
    sort_fn = _SORT_KEY_FUNCS[sort_strategy]
    return sorted(seq, key=lambda i: sort_fn(pieces_list[i]))


# =============================================================================
#  Public API
# =============================================================================

def evaluate_ratios_batched(
    ratios: List[Dict[str, int]],
    pieces_by_size: Dict[str, List[Dict]],
    fabric_width_mm: float,
    gpu_scale: float = 0.15,
    max_length_mm: float = 15000.0,
    sort_strategy: str = "area_desc",
    dual_sort: bool = False,
    batch_size: int = 256,
    prefer_rot0: bool = True,
    verbose: bool = False,
) -> List[Dict]:
    """
    Evaluate a batch of candidate ratios on the GPU in bulk.

    Args:
        ratios:          List of {size: bundle_count}
        pieces_by_size:  Output of load_pieces_for_material(...) (or equivalent)
        fabric_width_mm: Strip (fabric) width in mm
        gpu_scale:       Rasterization resolution (px/mm). Production default 0.08.
        max_length_mm:   Upper bound on marker length for container allocation
        sort_strategy:   "area_desc" or "width_desc" — within-ratio piece sort
        dual_sort:       If True, run each ratio under both sorts and keep shorter
                         (2x cost). Default False.
        batch_size:      Sequences per kernel launch. Ratios are grouped by piece
                         count and each group is chunked at this size.
        prefer_rot0:     True = matches production Python BLF bit-identically.
        verbose:         Print per-group timing info.

    Returns:
        One dict per input ratio (same order), with:
            length_mm        -- marker length (mm)
            length_m         -- marker length (m)
            efficiency       -- vector_area / (strip_width * length)
            efficiency_pct   -- above x 100
            sort_used        -- "area_desc" or "width_desc"
            sequence         -- int[] (piece indices in the order placed)
            placements       -- [{piece_idx, name, size, x_mm, y_mm, rot_deg}]
    """
    strip_width_px = round(fabric_width_mm * gpu_scale)
    max_length_px = int(max_length_mm * gpu_scale)

    pieces_list, name_to_idx = build_global_piece_index(pieces_by_size)

    # Strategies to try per ratio
    strategies = [sort_strategy]
    if dual_sort:
        alt = "width_desc" if sort_strategy == "area_desc" else "area_desc"
        strategies.append(alt)

    # Expand sequences per ratio per strategy
    per_strategy_seqs: Dict[str, List[List[int]]] = {}
    for strat in strategies:
        per_strategy_seqs[strat] = [
            expand_ratio_to_sequence(r, pieces_by_size, name_to_idx, pieces_list, strat)
            for r in ratios
        ]

    # Group (seq_len, strategy) -> ratio indices
    groups: Dict[Tuple[int, str], List[int]] = {}
    for strat, seqs in per_strategy_seqs.items():
        for ri, seq in enumerate(seqs):
            groups.setdefault((len(seq), strat), []).append(ri)

    # Result slots (keep the shorter across strategies if dual_sort)
    results: List[Dict] = [
        {
            "ratio": r,
            "length_mm": float("inf"),
            "length_m": float("inf"),
            "efficiency": 0.0,
            "efficiency_pct": 0.0,
            "sort_used": None,
            "sequence": None,
            "placements": None,
        }
        for r in ratios
    ]

    blf = BatchedBLF(pieces_list, strip_width_px, max_length_px)

    for (n_pieces, strat), ratio_indices in groups.items():
        if verbose:
            print(f"  group n_pieces={n_pieces} strategy={strat}: {len(ratio_indices)} ratios")

        for chunk_start in range(0, len(ratio_indices), batch_size):
            chunk = ratio_indices[chunk_start: chunk_start + batch_size]
            batch_seqs = np.array(
                [per_strategy_seqs[strat][ri] for ri in chunk], dtype=np.int32,
            )
            t0 = time.time()
            lengths, placements = blf.solve(batch_seqs, prefer_rot0=prefer_rot0)
            dt = time.time() - t0
            if verbose:
                print(
                    f"    chunk {len(chunk):4d} ratios  {dt * 1000:6.0f} ms  "
                    f"{dt * 1000 / len(chunk):5.1f} ms/ratio"
                )

            for i, ri in enumerate(chunk):
                L_px = int(lengths[i])
                length_mm = L_px / gpu_scale
                if length_mm >= results[ri]["length_mm"]:
                    continue  # already have a shorter variant from a different sort

                total_area = sum(
                    _polygon_area_mm2(pieces_list[idx]["vertices_mm"])
                    for idx in per_strategy_seqs[strat][ri]
                )
                eff = total_area / (fabric_width_mm * length_mm) if length_mm else 0.0

                placements_out = []
                for step, global_idx in enumerate(per_strategy_seqs[strat][ri]):
                    x = int(placements[i][step][0])
                    y = int(placements[i][step][1])
                    rot = int(placements[i][step][2])
                    if x < 0:
                        continue
                    p = pieces_list[global_idx]
                    placements_out.append({
                        "piece_idx": global_idx,
                        "name": p.get("name", "?"),
                        "size": p.get("size", "?"),
                        "x_mm": x / gpu_scale,
                        "y_mm": y / gpu_scale,
                        "rot_deg": 180 if rot == 1 else 0,
                    })

                results[ri].update({
                    "length_mm": length_mm,
                    "length_m": length_mm / 1000,
                    "efficiency": eff,
                    "efficiency_pct": eff * 100,
                    "sort_used": strat,
                    "sequence": list(per_strategy_seqs[strat][ri]),
                    "placements": placements_out,
                })

    return results


__all__ = [
    "BatchedBLF",
    "build_raster_bank",
    "build_global_piece_index",
    "expand_ratio_to_sequence",
    "evaluate_ratios_batched",
]
