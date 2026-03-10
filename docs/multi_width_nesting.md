# Multi-Width GPU Nesting

## Problem Statement

Garment factories stock fabric in multiple widths (e.g., 54", 58", 62"). A single order may use the same pattern across different fabric widths. The GPU nesting system needs to evaluate marker ratios at all available widths to find the optimal width-ratio combinations for cutplan generation.

## Key Insight

**Fabric width doesn't change the ranking of marker ratios — it only shifts absolute lengths.**

A ratio that nests well at 60" also nests well at 54" and 62". The efficiency ordering is nearly identical across widths. This means we can:

1. Run the full GPU pipeline at one **base width** (the widest)
2. **Predict** results at other widths using a Ridge regression model trained on a small sample of cross-width GPU evaluations

This avoids re-running the entire GPU pipeline (which may evaluate thousands of ratios) at each additional width.

## Strategy

### Per-Width Processing

For each extra width beyond the base:

| Step | bc=1,2 | bc=3+ |
|------|--------|-------|
| **1. GPU evaluate** | 100% brute-force (hard rule) | Sample diverse anchors |
| **2. Train Ridge** | Include in training data | Include in training data |
| **3. Predict** | N/A (already complete) | Predict remaining ratios |
| **4. Verify** | N/A | GPU-verify top-N predicted |

### Constants

| Constant | Value | Description |
|----------|-------|-------------|
| `CROSS_WIDTH_SAMPLE_RATE` | 0.10 (10%) | Fraction of bc=3+ base results sampled per extra width |
| `CROSS_WIDTH_SAMPLE_MIN` | 15 | Floor: never fewer anchors than this |
| `CROSS_WIDTH_SAMPLE_MAX` | 100 | Cap: never more anchors than this |
| `CROSS_WIDTH_TOP_N_VERIFY` | 25 | GPU-verify top-N predicted ratios per extra width |

### Anchor Selection

Anchors for bc=3+ are chosen using **farthest-first diversity** from the GPU-evaluated base results:

1. Seed with the highest-efficiency ratio
2. Greedily select the ratio maximizing minimum distance to all previously selected, weighted by efficiency
3. Distance is computed on ratio proportions (normalized counts per size)

This ensures broad coverage of the ratio space — both high-efficiency hotspots and diverse composition corners.

## Ridge Feature Vector

The cross-width Ridge model uses the same feature vector as the single-width Ridge predictor, with `fabric_width_mm` prepended as the first feature:

```
[fabric_width_mm, prop_s1, prop_s2, ..., prop_sN, bundle_count,
 total_bundle_area, max_piece_width_ratio,
 count_s1 * area_s1, count_s1 * max_width_s1, ...,
 prop_s1 * prop_s2, prop_s1 * prop_s3, ...]
```

**Target:** `length_yards`

**Training data:**
- All GPU-evaluated base results (at base width)
- bc=1,2 brute-force results at extra width
- bc=3+ anchor results at extra width

The model learns the width→length relationship from this mixed-width training set.

## Benchmark Results (Mar 2026)

Benchmarked across 6 scenarios (2 patterns × 3 width pairs), comparing Ridge-predicted rankings vs full GPU rankings at target widths.

### Ranking Overlap (Top-10)

| Metric | Value |
|--------|-------|
| Mean overlap | 9.5 / 10 |
| Min overlap | 9 / 10 |
| Max overlap | 10 / 10 |

**Conclusion:** Ridge is a near-perfect ranking tool for cross-width prediction.

### Absolute Length Accuracy

| Metric | Value |
|--------|-------|
| R² | 0.93 – 0.96 |
| Max error | 11% – 18% |
| Mean error | 2% – 5% |

**Conclusion:** Absolute lengths are less accurate, which is why we GPU-verify the top-N predictions. The verification step replaces predicted values with actual GPU measurements for the most important ratios.

## Integration with Adaptive Pipeline

The multi-width flow runs **after** the base-width pipeline completes:

```
┌─────────────────────────────────────────┐
│  Base Width Pipeline (full GPU)         │
│  - bc=1,2: brute force                  │
│  - bc=3+: adaptive (LHS+Ridge or BF)   │
│  - Post-process: dual-sort, SVG         │
└────────────────┬────────────────────────┘
                 │
    ┌────────────▼───────────────┐
    │  Per Extra Width:          │
    │  1. bc=1,2 brute force     │
    │  2. bc=3+ anchor sample    │
    │  3. Ridge predict          │
    │  4. Top-N GPU verify       │
    └────────────────────────────┘
```

Progress is reported in the 96-98% range (base pipeline uses 0-95%).

## Frontend Integration

- **Configure page:** "Additional Widths" text input (comma-separated, e.g., "54, 62")
- **Nesting results page:** Width tabs filter results by width; Width column shows per-marker width
- **Cutplan page:** Per-marker width display from nesting result lookup

## Files

| File | Role |
|------|------|
| `backend/backend/services/gpu_nesting_runner.py` | Cross-width sampling, Ridge prediction, verification |
| `frontend/src/app/orders/[id]/configure/page.tsx` | Additional widths input |
| `frontend/src/app/orders/[id]/nesting/page.tsx` | Width tabs, width column |
| `frontend/src/app/orders/[id]/cutplan/page.tsx` | Per-marker width display |
| `frontend/src/lib/api.ts` | `fabric_widths` in NestingJobCreate, `fabric_width_inches` in NestingJobResult |
