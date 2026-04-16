# Multi-Lot Cutplan Solver (Greedy + GA Polish)

**Status**: Pre-final production version
**Validated on**: HD FGL 2 order (AEO-5907-IN-SU26) — both colors
**Scripts**: `scripts/linear_lot_ilp.py` (210BONEWHI), `scripts/multilot_solver_100bwnw.py` (100BWNW)

## Problem Statement

Given:
- An **order demand** per size (e.g., XS:433, S:790, M:1556, L:1214, XL:663, XXL:171, XXXL:109)
- **Multiple fabric lots** with different shrinkage rates, widths, and yardage supplies
- **DXF patterns** for each shrinkage group (different grading = different piece geometries)

Find: A cutplan (list of marker assignments) that minimizes total fabric usage while:
1. Meeting demand per size (within ~3% overshoot)
2. Respecting each lot's supply limit
3. Using as few unique markers as possible

## Pipeline Overview

```
Raw roll inventory
       │
       ▼
┌──────────────┐
│ Lot Grouping │  Phase 1: Cumulative waste heuristic
│ (18 → 9 lots)│         reduces raw (shrinkage, width) tiers
└──────┬───────┘
       │
       ▼
┌──────────────┐
│ Area Model   │  Phase 2: Load DXF → compute piece areas → instant length prediction
│ (16K lengths)│         predict_length(ratio, shrink, width, eff, areas)
└──────┬───────┘
       │
       ▼
┌──────────────┐
│ Greedy Solve │  Phase 3: Fill lots biggest-first, score by demand×fill×bc
│ (~0.03s)     │         + tail fill for remaining sizes with BC 1-2
└──────┬───────┘
       │
       ▼
┌──────────────┐
│ GA Polish    │  Phase 4: Evolutionary optimization on ply counts/ratios/lots
│ (~2-3s)      │         200 generations, pop 50, tournament selection
└──────┬───────┘
       │
       ▼
Cutplan with predicted lengths → CPU nest for actual lengths
```

## Phase 1: Lot Grouping

### Why
Raw fabric inventory has many (shrinkage, width) combinations (e.g., 18 distinct tiers). Many adjacent widths can be merged — cutting at 71" uses 71.5" fabric with only 0.7% waste. But merging across too large a width gap wastes fabric, and merging across shrinkage groups is geometrically invalid (different piece sizes).

### Algorithm: Cumulative Waste Heuristic

```
for each shrinkage group:
    sort width tiers: widest → narrowest
    carry = []  # (original_width, supply) tuples being carried forward

    for each tier (widest to narrowest):
        carry.append(this tier)

        if this is the last tier:
            emit lot at this width with all carried supply
            continue

        # Calculate waste if we merge into the NEXT (narrower) tier
        cum_waste = Σ (source_original_width - next_width) / next_width × source_supply
                    for each source in carry

        if cum_waste > MAX_WASTE_YD (default: 5 yd):
            emit lot at THIS width with all carried supply
            carry = []
```

**Key design decisions:**
- Waste is cumulative from each source's **original** width, not incremental
- `MAX_WASTE_YD = 5` is configurable per order
- Cross-shrinkage merging is **never** allowed (different DXF patterns)
- The heuristic runs in microseconds

### Example: 3.5X3.5 group (100BWNW)

```
Raw tiers: 72.0"(217yd) → 71.5"(271yd) → 71.0"(1454yd) → 70.75"(46yd) → 70.5"(221yd)

Walk:
  72→71.5: waste = (72-71.5)/71.5 × 217 = 1.5 yd → COLLAPSE
  carry=[72"(217), 71.5"(271)] → 71.0:
    waste = (72-71)/71 × 217 + (71.5-71)/71 × 271 = 3.1 + 1.9 = 5.0 yd → AT LIMIT, COLLAPSE
  carry=[72"(217), 71.5"(271), 71.0"(1454)] → 70.75:
    waste = (72-70.75)/70.75 × 217 + ... = 3.8 + 2.9 + 0.5 + 0.2 = 7.4 yd → KEEP SEPARATE

Result:
  3.5X3.5_71.0:  1942 yd (merged from 72" + 71.5" + 71.0")
  3.5X3.5_70.5:   267 yd (merged from 70.75" + 70.5")
```

### Optional: GPU Cliff Detection

For production, Phase 1 can include a GPU probe step before the waste heuristic:
- GPU nest `1-1-1-1-1-1-1` at every raw (shrinkage, width) with 6 sort strategies
- Compare efficiency between adjacent tiers
- Mark "no-go zones" where efficiency drops sharply (structural incompatibility)
- No-go zones block merging regardless of waste calculation

See `memory/multilot_grouping.md` for full details.

## Phase 2: Area-Based Length Prediction

### Formula

```python
def predict_length(ratio, shrink, width, eff, areas):
    width_mm = width × 25.4
    total_mm = Σ ratio[s] × areas[shrink][s] / (eff × width_mm)  for each size s
    return total_mm / 914.4  # mm → yards
```

Where:
- `areas[shrink][s]` = total mm² of all pieces for one garment of size `s` at shrinkage `shrink`
- `eff` = nesting efficiency (0.82–0.85), calibrated from 1-1-1-1-1-1-1 reference nests
- The formula models: **marker length ≈ total piece area ÷ (efficiency × fabric width)**

### Accuracy
- 2-3% error vs 2-minute CPU nesting (Spyrrow)
- Validated across 7 lots, 50+ ratio combinations
- The model's accuracy is sufficient for ratio **ranking** — the top ratios are CPU-nested for actual lengths

### Pre-computation
- ~2,400 ratios × 7-9 lots = ~16-22K lengths
- Completes in 0.02-0.05 seconds
- Stored in `lengths[(ratio_idx, lot_idx)]` lookup dict

## Phase 3: Greedy Construction

### Ratio Generation

```
BC 1-6:  full brute force via combinations_with_replacement(7 sizes, bc)
         1,715 total ratios

BC 7-12: demand-proportional base + ±1/±2 perturbations
         ~100 per BC, ~680 total

Total: ~2,400 ratios
```

### Algorithm

```python
remaining_demand = copy(DEMAND)
remaining_supply = {lot: supply for each lot}

for lot in sorted(lots, by supply descending):
    while remaining_supply[lot] > 0.5 yd:

        for each of ~2,400 ratios:
            length = lengths[ratio, lot]      # instant lookup
            max_plies = min(
                floor(remaining_supply / length),   # lot capacity
                min over sizes: (remaining_demand[s] + 3%) / ratio[s]  # demand cap
            )
            if max_plies < 10: skip

            # SCORING
            demand_reduction = Σ min(ratio[s] × plies, remaining_demand[s])
            demand_pct = demand_reduction / total_remaining
            lot_fill = (length × plies) / remaining_supply
            bc_bonus = 1.0 + 0.02 × bc

            score = demand_pct × lot_fill × bc_bonus

        Pick highest scoring → deduct demand and supply → add to assignments
```

### Scoring Rationale

| Component | Purpose |
|-----------|---------|
| `demand_pct` | Favor ratios that address the largest remaining sizes |
| `lot_fill` | Favor ratios that consume most of the lot — prevent small scraps |
| `bc_bonus` | Small bonus for higher BC (more garments/marker = more efficient) |
| Product | A ratio must score well on **both** demand and fill axes |

### Lot Processing Order

Biggest lots first — they have the most flexibility and can absorb high-BC markers with many plies. Smaller lots get whatever demand remains.

### Tail Fill

After greedy, check for unfulfilled sizes. For each:
- Scan BC 1-2 ratios only (single-size or paired markers)
- Find the lot with enough spare capacity
- Add minimal plies to fulfill remaining demand

## Phase 4: GA Polish

### Representation
Chromosome = list of assignments `[{ratio, lot, plies, ...}, ...]`

### Fitness (lower = better)
```
fitness = total_fabric
        + 100 × demand_miss            # per garment short or over 3%
        + 50 × n_assignments           # fewer markers preferred
        + 500 × supply_violation        # hard penalty for exceeding lot supply
```

### Mutation Operators

| Op | Probability | Action |
|----|-------------|--------|
| Adjust plies | 30% | Random assignment ±1..10 plies |
| Swap ratio | 25% | Transfer 1 bundle between two sizes (neighbor ratio) |
| Move lot | 15% | Reassign a marker to a different lot |
| Remove | 15% | Delete smallest assignment (if >2 remain) |
| Split | 15% | Split largest into two, second goes to random lot |

### Parameters

| Parameter | Value | Notes |
|-----------|-------|-------|
| Population | 50 | Trades off diversity vs speed |
| Generations | 200 | Converges by ~150-200 |
| Elitism | Top 5 | Survive unchanged each generation |
| Selection | Tournament of 3 | Good pressure without premature convergence |
| Seed | 42 | Reproducible results |

### What the GA Improves

The greedy makes locally optimal decisions. The GA discovers global improvements:
- Trimming plies from over-filled lots to tighten demand
- Shifting fabric between lots for better overall balance
- Removing redundant tail-fill markers by adjusting main marker plies

Typical improvement: 1-2% reduction in total fabric.

## Validated Results

### Color 1: 210BONEWHI (4,545 garments, 7 lots, 4,200 yd supply)

```
Our solver:  8 markers | 4,120 yd | 0.906 yd/gmt
SS benchmark: 8 markers | 4,120 yd | 0.901 yd/gmt
```

Demand: XS +0, S +1, M +13, L +12, XL +0, XXL +3, XXXL +2
All lots 97-100% utilized. Solved in 1.9s.

### Color 2: 100BWNW (4,936 garments, 9 lots, 4,907 yd supply)

```
Our solver:  7 markers | 4,434 yd | 0.898 yd/gmt
SS benchmark: 7 markers | 4,443 yd | 0.900 yd/gmt
```

Demand: XS +5, S +1, M +3, L +8, XL +0, XXL +0, XXXL +3
Large lots 99-100% utilized. 3 small exotic lots unused (demand met). Solved in 4.0s.

## CPU Nesting Validation

After the solver produces a cutplan, each unique marker is CPU-nested (Spyrrow, 30 min) to get:
- **Actual length** (validates the area model's 2-3% error)
- **DXF file** for production marker export
- **SVG preview** for UI display

This is a post-processing step — the solver's cutplan is structurally final.

## Key Files

| File | Purpose |
|------|---------|
| `scripts/linear_lot_ilp.py` | 210BONEWHI solver (greedy + GA) |
| `scripts/multilot_solver_100bwnw.py` | 100BWNW solver (lot grouping + greedy + GA) |
| `scripts/cpu_nest_cutplan_markers.py` | CPU nesting validation (30 min per marker) |
| `docs/multilot_cutplan_solver.md` | This document |
| `memory/multilot_grouping.md` | Lot grouping algorithm details |

## Future Work

1. **Parameterize solver** — extract constants (demand, lots, patterns) to JSON config, make solver generic
2. **Integrate into webapp** — expose via API, store cutplans in DB
3. **Multi-color joint solving** — share markers across colors to reduce unique marker count
4. **Roll-level assignment** — after lot-level cutplan, assign specific rolls to markers
5. **EndBit optimization** — use roll end-bits for small tail markers instead of wasting fabric
