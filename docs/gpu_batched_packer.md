# GPU Batched Packer

A CUDA kernel that evaluates many marker sequences on the GPU simultaneously, with a
cleaner greedy tie-breaker than the existing Python BLF.

## What it is

Two pieces of code, both live under `scripts/gpu_seq_search/` today (pending
integration into `backend/services/gpu_nesting_runner.py`):

- **`batched_kernel.py`** — `BatchedBLF` class + the CUDA kernel. One CUDA
  block per sequence; all block threads collaborate on the per-piece
  `(x, y, rot)` search, reduce via shared-memory `atomicMin` on a packed
  `(score, x, y, rot)` 64-bit word, then parallel-update the block's container.
  All pieces of a sequence are processed in a single kernel launch — no Python
  loop between pieces.

- **`batch_evaluator.py`** — `evaluate_ratios_batched()` API. Groups ratios by
  piece-count, expands each to a sequence of indices into a shared raster bank,
  and dispatches to the kernel in chunks of `batch_size`.

## Why it matters (measured on 126H010C / HD pattern)

- **Speed: 3.17× faster** than production per-ratio Python loop on a 461-ratio
  sweep (bc=1..5, 6 sizes). 63s → 20s. The kernel amortises the per-piece
  kernel launches and eliminates the ~6 scalar-sync roundtrips per piece.
- **Quality: +2.42% mean shorter length** across the same 461 ratios vs Python
  single-sort (same `area_desc` strategy).
- **Regression risk: 26% of ratios are >0.1% longer** than Python. Worst case
  on this pattern: −12.58%. Tie-breaking sensitivity of a greedy algorithm;
  neither rule wins universally. Dual-sort reduces regression rate to 14%.

## Design

### Kernel

```
batched_blf<<<(N_seq,), (256,)>>>(
    const float* raster_bank,      // all pieces packed: [piece][rot][ph*pw] flat
    const int*   piece_offs,       // offset of each (piece, rot) in raster_bank
    const int*   piece_shape,      // (ph, pw) per piece
    const int*   sequences,        // [N_seq, seq_len] piece indices
    float*       containers,       // [N_seq, strip_w, max_len] own container per seq
    int*         placements,       // [N_seq, seq_len, 3] output (x, y, rot)
    int*         lengths_out,      // [N_seq] strip length px
    int seq_len, int strip_w, int max_len, int active_w_padding
)
```

Per block (= per sequence), per piece of that sequence:

1. Compute active-width window and `(num_x, num_y)` search grid.
2. Thread `t` iterates over its slice of `(x, y, rot)` candidates:
   - Early-exit collision check against the block's own container.
   - If fits: compute score `piece_top` (inside) or `0x40000000 + piece_right` (outside).
   - Pack `(score, x, y, rot)` into an unsigned 64-bit word and keep the min locally.
3. Block-wide reduce into `__shared__` via `atomicMin`.
4. Thread 0 unpacks the best; all 256 threads parallel-update the container.
5. Sync, move to next piece.

At the end, `lengths_out[seq] = cur_len`.

### Why block-per-sequence

- A typical piece has ~5K pixels × ~400K candidate positions per placement. A single
  sequence already saturates one SM for a few ms. With 30 SMs on an RTX 3060,
  running ~30–120 sequences concurrently (one per block) fills the machine.
- Shared piece raster bank benefits from L2 cache — all blocks read the same piece data.
- Each block's container is ~1 MB and sits in global memory; cache misses are the
  dominant cost, which is why throughput plateaus around 24 seq/s at N=256 on a 3060 Laptop.

### Score packing

64-bit unsigned word layout:

```
  [ 32 bits score | 16 bits x | 15 bits y | 1 bit rot ]
```

`atomicMin` on unsigned long long gives lexicographic ordering, so the tuple is
compared first by `score`, then by `x`, then `y`, then `rot`. Deterministic.

## API

```python
from batch_evaluator import evaluate_ratios_batched

results = evaluate_ratios_batched(
    ratios=[{"M": 2, "L": 1}, {"M": 1, "L": 2, "XL": 1}, ...],
    pieces_by_size={"M": [...], "L": [...], "XL": [...]},
    fabric_width_mm=1400.0,
    gpu_scale=0.15,
    max_length_mm=10000,
    sort_strategy="area_desc",   # or "width_desc"
    dual_sort=False,              # True tries both, keeps shorter (adds ~2× cost)
    batch_size=256,
)

# Per ratio: length_m, efficiency_pct, placements [{piece_idx, x_mm, y_mm, rot_deg, name, size}]
```

Input constraints:
- `pieces_by_size` values come from `load_pieces_for_material(...)` (or
  `load_pieces_kpr(...)` for KPR-format patterns); each piece must have
  `raster_gpu`, `raster_180_gpu`, `vertices_mm`, `demand`, `name`.
- Ratios within a single kernel launch must share piece count (enforced by the
  grouping logic; cross-bc ratios are dispatched to separate launches).

## When to use it

**Use batch API when**: screening many ratios (≥64) for ranking; sequence search
workloads; anything where throughput > single-call latency.

**Keep the single-ratio Python path for**: individual marker recomputation where
only one answer is needed (the batch kernel's N=1 overhead of ~1–3 s per
sequence is worse than Python's ~200 ms; only 1 of 30 SMs is active).

## Known caveats

1. **Tie-breaking differs from the production Python BLF.** Python prefers
   `rot=0` at ties; the kernel does global argmin over `(x, y, rot)`. Different
   answers on the same input, roughly symmetric in outcome:
   - 71% of ratios: kernel produces shorter length
   - 28% of ratios: Python produces shorter length
   - Mean/median gain: +2.4% (kernel)
   - Worst regression observed: −12.58% (kernel longer)

2. **Coarse raster.** 0.15 px/mm gives ~6.7 mm per pixel. Placement coordinates
   are therefore ~6.7 mm granular. For "production" marker lengths, feed the
   returned `placements` to a vector-space compactor (Sparrow, NFP-based
   engine) — the GPU packer is a *ranking* tool, not a final-length engine.

3. **Grading must come from the correct parser.** The raster bank loaders
   (`load_pieces_for_material` in `gpu_nesting_runner.py`,
   `load_pieces_kpr` in `scripts/gpu_seq_search/kpr_loader.py`) each route to
   a specific AAMA format variant — don't mix across formats.

## Files

```
scripts/gpu_seq_search/
├── batched_kernel.py         # BatchedBLF class + CUDA kernel
├── batch_evaluator.py        # evaluate_ratios_batched() public API
├── kpr_loader.py             # load_pieces_kpr() for KPR-format patterns
├── 01_baseline_mk1.py        # current production Python BLF baseline
├── 04_validate_kernel.py     # correctness check (no overlaps, all placed)
├── 10_bench_ratio_screen.py  # 461-ratio sweep comparison
├── 11_bench_production_default.py  # apples-to-apples vs calibrated single-sort prod default
└── 06_sequence_search_v2.py  # elite-pool sequence search layered on top of BatchedBLF
```

## Integration TODO (future PR)

To ship into production:
1. Move `BatchedBLF` class into `backend/services/gpu_batched_packer.py`.
2. Move `evaluate_ratios_batched` into `gpu_nesting_runner.py` as a companion to
   `evaluate_ratio` / `_evaluate_ratios_batch`.
3. Add `file_type="optitex_kpr"` route in `load_pieces_for_material` (trivial
   4-line addition).
4. Add toggle in cutplan pipeline (`cutplan_service.py`) to use batched path for
   ratio screening while keeping single-ratio path for marker recomputation.
