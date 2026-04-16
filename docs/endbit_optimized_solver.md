# EndBit Optimized Cutplan Solver

Two-phase cutplan strategy that reduces fabric waste by using short markers to consume end-bits (fabric left over on rolls after cutting main markers) instead of discarding them.

**Implementation**: `backend/backend/services/endbit_solver.py`
**UI**: Option D in the cutplan strategy selector
**Strategy key**: `endbit_optimized`

## Background & Motivation

In a conventional cutplan, long markers (4-6 bundles) are cut from fresh rolls. After each roll is consumed, the leftover fragment — the "end-bit" — is typically too short for another long marker and becomes Type 2 waste. Experiments on the PBPP2 order showed this end-bit waste accounts for ~4.7% of total fabric cost.

The key insight: if short markers (1-2 bundle) are reserved exclusively for end-bits and never cut from fresh rolls, they can reclaim most of this waste. When fabric allocation is sized for main markers only (+buffer%), short markers naturally get pushed to end-bits because fresh rolls are exhausted by the time they're needed (shortest markers are cut last).

**Experimental result** (PBPP2 order): waste dropped from ~4.7% to ~1.1%, saving ~100 yards of fabric (6% reduction).

## Waste Classification

The solver uses three waste categories from floor simulation:

| Type | Definition | Example |
|------|-----------|---------|
| **Type 1** | Fragment shorter than one garment's fabric consumption | < ~1.3 yd — unusable scrap |
| **Type 2** | Fragment between one garment and the longest marker | ~1.3 to ~8 yd — the target for EB markers |
| **Type 3** | Fragment longer than the longest marker | Excess whole rolls — indicates over-allocation |

## Algorithm (7 Steps)

### Step 1: Long-Marker-Only ILP

Solve the ILP with markers filtered to `bundle_count >= 4` only.

```python
plan_long, _ = solve_ilp(
    demand=demand,
    all_markers=marker_objs_long,  # bc >= 4 only
    objective="max_efficiency",
    marker_penalty=0,
    min_plies_by_bundle={4: 1, 5: 1, 6: 1},  # no min-ply constraints
)
```

This produces the "main cutplan" — no 1/2/3-bundle markers. The ILP must fulfill exact demand using only long markers. If demand can't be met with bc >= 4 markers alone, the solver raises an error.

### Steps 2-3: Minimum Buffer via Floor MC

Find the minimum fabric buffer needed for the long-marker-only cutplan to complete on the cutting floor.

1. Start at **2% buffer**: `roll_target = cutplan_fabric × 1.02`
2. Generate synthetic rolls summing to `roll_target` (default: uniform 50-110 yd range)
3. Run **100 Monte Carlo floor simulations** — each simulation:
   - Processes markers longest-first (realistic floor order)
   - Randomly picks rolls from the pool
   - Tracks continuation remnants across cuts
   - Records Type 1, Type 2, Type 3 waste
   - Records end-bit lengths for Step 5 analysis
4. Check: does **100%** of runs complete the full order?
   - If NO: add **1% more buffer**, regenerate rolls, re-run MC
   - If YES: buffer is sufficient, proceed
5. Safety cap at 10% buffer

This finds the tightest realistic fabric allocation, which minimizes Type 3 waste and produces the most representative end-bit profile for the next step.

### Step 4: End-Bit Opportunity Analysis

From the MC runs, compute:
- Average Type 1 and Type 2 waste
- Overall waste % = (T1 + T2) / (cutplan_fabric + T1 + T2)
- End-bit length distribution (used in Step 5)

### Step 5: End-Bit Marker Selection

Pick the best short markers to consume end-bits:

1. **2-bundle**: Highest efficiency marker from the bank with `bundle_count == 2`
2. **3-bundle**: Highest efficiency marker with `bundle_count == 3`
3. **1-bundle**: Shortest marker with `bundle_count == 1` (smallest size = least waste per ply)

**3-bundle decision**: Use 3-bundle markers only if `eff_3b - eff_2b >= 2pp`. If yes, use layered approach (3-bundle first from end-bits, then 2-bundle for remainder, then 1-bundle). Otherwise just 2-bundle + 1-bundle.

**Ply computation from end-bit availability**:

For each MC run's end-bit lengths, greedily fit markers (3b → 2b → 1b) and count plies. Average across all MC runs to get expected plies per marker type. Then cap by demand (don't over-produce any size).

```
For each MC run:
  remaining_bits = sorted(end_bit_lengths, descending)
  Fit 3-bundle markers → remaining_bits
  Fit 2-bundle markers → remaining_bits
  Fit 1-bundle markers → remaining_bits
  Record plies per type

avg_plies = mean across MC runs
capped_plies = min(avg_plies, max_demand_allows)
```

### Step 6: Re-solve Main Cutplan

Subtract EB marker garments from total demand and re-solve:

```python
plan_main, _ = solve_ilp(
    demand=reduced_demand,        # demand minus EB marker garments
    all_markers=all_marker_objs,  # full pool bc >= 2
    objective="balanced",
    marker_penalty=20.0,          # high penalty → fewer markers
    min_plies_by_bundle=effective_min_plies,  # user-configured or defaults
)
```

The high marker penalty (20.0) pushes the solver toward fewer, longer markers. The min-ply constraints (default: 6-bndl:50, 5-bndl:40, 4-bndl:30, 3-bndl:10, 2-bndl:1, 1-bndl:1) prevent wasteful small-ply large markers.

If infeasible with min-ply constraints: halve all minimums and retry.

Combined cutplan = re-solved main markers + EB markers.

### Step 7: Final MC Validation

Final validation using the **same natural floor simulation** as all other cutplans (`_floor_mc` via `estimate_floor_waste`). All markers (main + EB) are combined and processed longest-first. Fabric is sized for the combined cutplan + buffer. Because EB markers are short, they are processed last and naturally consume end-bits left by longer markers — no artificial constraints.

1. Start at **1% buffer** over combined cutplan fabric (or factory-configured default)
2. `roll_target = combined_fabric × (1 + buffer%)`
3. Run 100 MC floor simulations (same `_floor_mc` used everywhere)
4. Check: **100% completion** — all MC runs must fully cut the order
   - If NO: add **1% more buffer**, regenerate, re-run
   - If YES: record final waste %
5. Safety cap at 10% buffer

**Key design principle**: The MC simulation logic is identical everywhere in the app. The only lever is the buffer value and increment step size. No artificial constraints force markers to use end-bits — the natural floor dynamics (longest-first processing + tight fabric allocation) handle it.

## Data Flow

```
                    ┌──────────────────────┐
                    │ GPU Marker Bank       │
                    │ (all ratio→eff/length)│
                    └──────────┬───────────┘
                               │
               ┌───────────────┼───────────────┐
               │               │               │
          bc >= 4          bc == 2/3        bc == 1
          markers          markers          markers
               │               │               │
     ┌─────────▼─────────┐     │               │
     │ Step 1: ILP        │     │               │
     │ (long-only)        │     │               │
     └─────────┬─────────┘     │               │
               │               │               │
     ┌─────────▼─────────┐     │               │
     │ Steps 2-3: MC      │     │               │
     │ (find buffer)      │     │               │
     └─────────┬─────────┘     │               │
               │               │               │
     ┌─────────▼─────────┐     │               │
     │ Step 4: Analyze    │     │               │
     │ end-bit profile    │     │               │
     └─────────┬─────────┘     │               │
               │               │               │
               │     ┌─────────▼───────────────▼──┐
               │     │ Step 5: Pick EB markers     │
               │     │ (best 2b, maybe 3b, min 1b) │
               │     └─────────┬──────────────────┘
               │               │
     ┌─────────▼───────────────▼──┐
     │ Step 6: Re-solve ILP        │
     │ (reduced demand, all bc>=2) │
     └─────────┬──────────────────┘
               │
     ┌─────────▼─────────┐
     │ Step 7: Two-phase  │
     │ MC validation      │
     └─────────┬─────────┘
               │
     ┌─────────▼──────────────────┐
     │ Combined Cutplan            │
     │ main markers + EB markers   │
     │ + waste %, fill rate, buffer│
     └────────────────────────────┘
```

## Integration Points

### Cutplan Service (`cutplan_service.py`)

The `endbit_optimized` strategy is split from regular ILP strategies and run via its own solver:

```python
# In run_multi_strategy_optimization():
regular_strategies = [s for s in strategies if s != "endbit_optimized"]
has_endbit = "endbit_optimized" in strategies

# Regular ILP strategies run via optimize_cutplan()
# EndBit runs via solve_endbit_optimized() from endbit_solver.py
```

The result is converted to the standard `optimize_cutplan` dict format so it integrates with the existing cutplan save/display pipeline. Extra fields (`mc_waste_pct`, `endbit_fill_rate`, `main_fabric_yards`, `endbit_fabric_yards`) are stored in `solver_config`.

### ILP Solver (`ilp_solver_runner.py`)

The EndBit solver calls `solve_ilp()` directly — it does not go through `optimize_cutplan()`. It also uses:
- `markers_from_nesting_results()` — convert raw marker dicts to `Marker` objects
- `filter_markers_for_ilp()` — remove dominated/duplicate markers
- `generate_all_1_2_bundle_markers()` — generate synthetic 1-2 bundle markers for completeness

### Rollplan Simulator (`rollplan_simulator.py`)

Uses `MarkerSpec` and `RollSpec` dataclasses for MC simulation. The floor MC and two-phase MC are self-contained in `endbit_solver.py` (adapted from experiment scripts), not the production `rollplan_simulator.py`.

## Shared Floor Waste Estimation (`estimate_floor_waste`)

The iterative-buffer floor MC logic from Steps 2-3 is extracted into a shared function in `endbit_solver.py`:

```python
def estimate_floor_waste(
    mc_specs, max_ply_height=100, avg_roll_length=80.0, n_sims=50,
    start_buffer_pct=0.02, max_buffer_pct=0.15, target_completion=0.90,
) -> dict:
    # Returns: {mc_waste_pct, mc_waste_yards, buffer_pct_used, completion_rate, _mc_result}
```

**Used by three callers:**

| Caller | When | Purpose |
|--------|------|---------|
| EndBit solver (Steps 2-3) | During endbit solve | Profile end-bit waste for long-marker-only cutplan |
| `cutplan_service.py` Phase 1 | After ILP solve (GPU lengths) | Initial Est. Floor Waste for all non-endbit cutplans |
| `cutplan_service.py` Phase 2 | After quick nest (CPU lengths) | Updated Est. Floor Waste with accurate marker lengths |

### Buffer Iteration Strategy

Different contexts use different iteration granularity:

| Context | Start | Step | Max | Completion | Rationale |
|---------|-------|------|-----|------------|-----------|
| **EndBit Steps 2-3** (profiling) | 2% | +1% | 10% | 100% | Coarse — just profiling to find end-bit opportunity |
| **EndBit Step 7** (final validation) | 1% | +0.2% | 10% | 100% | Fine-grained — precise waste measurement for EB optimized |
| **Non-endbit cutplans** (A/B/C) | 2% | +1% | 15% | 100% | Coarse — these cutplans inherently waste more |

All three use the same `estimate_floor_waste` function wrapping `_floor_mc`. The tuning levers are `start_buffer_pct`, `max_buffer_pct`, and `buffer_step_pct`. The MC simulation logic is identical everywhere.

### EndBit vs Non-EndBit Floor Waste

The EndBit solver's final `mc_waste_pct` (from Step 7) is **not overwritten** by `cutplan_service.py` Phase 2. Detection: `solver_config.endbit_fill_rate` is present only on endbit cutplans. This preserves the EndBit solver's more accurate two-phase MC result, which models short markers being cut from end-bits (not fresh rolls).

### UI Display

All cutplans show two floor-waste metrics (when available):

| Metric | Pill color | Formula |
|--------|-----------|---------|
| **Est. Floor Waste** | Emerald | `mc_waste_pct` (%) and `mc_waste_yards` (yd) from MC simulation |
| **Total Cost (Incl. Est. Waste)** | Orange | `total_cost + mc_waste_yards × fabric_cost_per_yard` |

Both are stored in `solver_config` JSON on the `Cutplan` model.

## Result Structure

```python
@dataclass
class EndbitSolverResult:
    main_markers: List[Dict]       # bc >= 4 markers (ILP output)
    endbit_markers: List[Dict]     # bc <= 2/3 markers from end-bits
    combined_markers: List[Dict]   # main + endbit (for saving)
    main_fabric_yards: float
    endbit_fabric_yards: float     # planned from end-bits
    total_fabric_yards: float
    mc_waste_pct: float            # final validation waste %
    mc_type1_avg: float
    mc_type2_avg: float
    endbit_fill_rate: float        # % of EB plies actually cut from end-bits
    demand_fulfilled: bool
    buffer_pct: float              # final buffer % used
    efficiency: float              # weighted average
    total_plies: int
    total_cuts: int
    bundle_cuts: int
    unique_markers: int
    solve_time: float
```

EB markers in `combined_markers` are tagged with `"is_endbit_marker": True`.

## Configuration

| Parameter | Default | Source | Description |
|-----------|---------|--------|-------------|
| `avg_roll_length` | 80.0 yd | Order settings | Average roll length for synthetic roll generation |
| `roll_length_variation` | 0.0 | Order settings | Half-range of variation. 0 = default uniform(50, 110) |
| `n_simulations` | 100 | Hardcoded | MC simulations per phase |
| `max_ply_height` | 100 | Order settings | Max plies per cut |
| `min_plies_by_bundle` | {6:50, 5:40, 4:30, 3:10, 2:1, 1:1} | Order settings | Min-ply constraints for Step 6 re-solve |
| `default_buffer_pct` | 1.0% | Order settings | Starting buffer for Step 7 validation |
| `marker_penalty` | 20.0 | Hardcoded | High penalty in Step 6 to favor fewer markers |

## Roll Generation

Synthetic rolls are generated to sum to a target yardage:

```python
def _generate_synthetic_rolls(target_total_yards, avg_length=80.0, variation=0.0):
    # variation == 0 → uniform(50, 110)  — factory realistic wide range
    # variation > 0  → uniform(avg - variation, avg + variation)
```

Rolls are deterministic (seeded RNG) for reproducibility.

## Floor Simulation Details

Each MC run simulates a realistic cutting floor:

1. **Marker ordering**: Longest markers cut first (standard factory practice)
2. **Roll selection**: Random pick from eligible rolls (roll length >= marker length)
3. **Continuation**: After cutting plies from a roll, the leftover (continuation remnant) is carried to the next cut of the same marker. Only becomes an end-bit when switching markers.
4. **End-bit fallback**: When no fresh rolls are eligible, the simulator tries end-bits (sorted longest first) before declaring the marker incomplete.
5. **Waste classification**: After all cuts, remaining fragments (continuation, end-bits, uncut rolls) are classified as Type 1/2/3.

## Experiment History

The algorithm was developed through iterative experiments:

| Script | Purpose |
|--------|---------|
| `scripts/experiment_real_order_mc.py` | Initial floor MC simulation for waste analysis |
| `scripts/experiment_twophase_mc.py` | Two-phase MC proving EB markers reduce waste |
| `scripts/experiment_endbit_solver.py` | First end-bit solver prototype |
| `scripts/experiment_endbit_solver_v2.py` | Refined version with buffer search |

## Known Limitations

1. **Synthetic rolls only**: Currently uses generated rolls, not actual roll inventory. Future: integrate with rollplan stage for real rolls.
2. **Single EB marker per bundle count**: Picks one best 2-bundle, one best 3-bundle, one shortest 1-bundle. Could benefit from trying multiple candidates.
3. **Greedy EB fitting**: End-bit consumption is greedy (largest first). Optimal bin-packing could extract more plies.
4. **100% completion threshold**: All MC runs must fully cut the order. Conservative but truthful — the buffer search adds fabric until every simulation succeeds.
5. **No cross-order optimization**: Operates on a single order. Multi-order EB pooling could improve fill rates.
