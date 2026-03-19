"""
EndBit Optimized Cutplan Solver.

Two-phase strategy:
  Phase 1: Cut long markers (bc >= 4) from fresh rolls
  Phase 2: Cut short markers (bc <= 2/3) from end-bits ONLY

Algorithm (7 steps):
  1. Solve ILP with bc >= 4 markers (max_efficiency, no min-ply constraints)
  2-3. Find minimum fabric buffer via floor MC (start at 2%, increment 1%)
  4. Analyze end-bit profile (bucket by cuttable bundles)
  5. Pick end-bit markers (best 2b, optionally 3b, shortest 1b)
  6. Re-solve main cutplan for reduced demand (full marker pool, min-ply constraints)
  7. Final two-phase MC validation (start at 1% buffer, increment 0.2%)

Pure Python module, no DB dependencies.
"""
from __future__ import annotations

import math
import random
import statistics
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

from .ilp_solver_runner import (
    Marker,
    CutPlan,
    MarkerAssignment,
    solve_ilp,
    markers_from_nesting_results,
    filter_markers_for_ilp,
    generate_all_1_2_bundle_markers,
)
from .rollplan_simulator import MarkerSpec, RollSpec


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------

@dataclass
class EndbitSolverResult:
    main_markers: List[Dict]       # bc >= 4 markers (ILP output format)
    endbit_markers: List[Dict]     # bc <= 2/3 markers from end-bits
    combined_markers: List[Dict]   # main + endbit (for saving to cutplan)
    main_fabric_yards: float
    endbit_fabric_yards: float     # planned from end-bits, not fresh rolls
    total_fabric_yards: float
    mc_waste_pct: float            # final validation waste %
    mc_type1_avg: float
    mc_type2_avg: float
    endbit_fill_rate: float        # % of EB plies actually cut from end-bits
    demand_fulfilled: bool
    buffer_pct: float              # final buffer % used
    # Summary fields matching optimize_cutplan dict format
    efficiency: float
    total_plies: int
    total_cuts: int
    bundle_cuts: int
    unique_markers: int
    solve_time: float


# ---------------------------------------------------------------------------
# Roll generation
# ---------------------------------------------------------------------------

def _generate_synthetic_rolls(
    target_total_yards: float,
    avg_length: float = 80.0,
    variation: float = 0.0,
    seed: int = 42,
) -> List[RollSpec]:
    """Generate synthetic rolls summing to target_total_yards.

    Args:
        avg_length: Mean roll length in yards
        variation: Half-range around mean. 0 → uniform(50, 110).
                   Positive → uniform(avg - variation, avg + variation).
    """
    rng = random.Random(seed)
    if variation > 0:
        min_len = max(10, avg_length - variation)
        max_len = avg_length + variation
    else:
        # Default: wide range (factory realistic)
        min_len = 50.0
        max_len = 110.0

    rolls = []
    accumulated = 0.0
    idx = 1
    while accumulated < target_total_yards:
        length = rng.uniform(min_len, max_len)
        remaining = target_total_yards - accumulated
        if remaining < min_len:
            length = remaining
        rolls.append(RollSpec(
            roll_id=f"R{idx:03d}_S",
            length_yards=round(length, 2),
        ))
        accumulated += length
        idx += 1
    return rolls


# ---------------------------------------------------------------------------
# Floor MC simulation (adapted from experiment_real_order_mc.py)
# ---------------------------------------------------------------------------

@dataclass
class _FloorRunResult:
    type1: float
    type2: float
    type3: float
    type2_lengths: List[float]
    completed: bool  # True if all markers were cut


def _floor_simulate_once(
    markers: List[MarkerSpec],
    rolls: List[RollSpec],
    rng: random.Random,
    max_ply_height: int = 100,
    endbit_priority: bool = False,
) -> _FloorRunResult:
    """Single floor-realistic MC run. Returns waste + completion status.

    Args:
        endbit_priority: False = "Planning" mode (fresh rolls first, maximizes
            T2 waste for endbit solver analysis). True = "Evaluation" mode
            (end-bits first, minimizes T2 waste for realistic floor behavior).
    """
    cuts = []
    for m in sorted(markers, key=lambda x: x.length_yards, reverse=True):
        remaining = m.plies
        while remaining > 0:
            batch = min(remaining, max_ply_height)
            cuts.append((m, batch))
            remaining -= batch

    total_fabric = sum(m.length_yards * m.plies for m in markers)
    total_garments = sum(
        sum(int(x) for x in m.ratio_str.split("-")) * m.plies
        for m in markers
    )
    piece_consumption = total_fabric / total_garments if total_garments > 0 else 1.0
    max_ml = max(m.length_yards for m in markers) if markers else 1.0

    pool = list(range(len(rolls)))
    end_bits: List[Tuple[float, str]] = []
    continuation: Optional[Tuple[float, str]] = None

    type1 = 0.0
    type2 = 0.0
    type3 = 0.0
    type2_lengths: List[float] = []
    all_completed = True

    def classify(length: float):
        nonlocal type1, type2, type3
        if length < piece_consumption:
            type1 += length
        elif length < max_ml:
            type2 += length
            type2_lengths.append(round(length, 4))
        else:
            type3 += length

    def _use_end_bits(ml: float, plies_remaining: int) -> int:
        """Try to cut plies from end-bits. Returns remaining plies."""
        nonlocal end_bits, continuation
        end_bits.sort(key=lambda x: -x[0])
        new_end_bits = []
        for eb_len, eb_id in end_bits:
            if plies_remaining <= 0 or eb_len < ml:
                new_end_bits.append((eb_len, eb_id))
                continue
            p = min(int(eb_len // ml), plies_remaining)
            leftover = eb_len - p * ml
            plies_remaining -= p
            if leftover > 0:
                if plies_remaining == 0:
                    continuation = (leftover, eb_id)
                else:
                    new_end_bits.append((leftover, eb_id))
        end_bits = new_end_bits
        return plies_remaining

    def _use_fresh_rolls(ml: float, plies_remaining: int) -> int:
        """Cut plies from fresh rolls. Returns remaining plies."""
        nonlocal continuation
        while plies_remaining > 0:
            eligible = [i for i, ri in enumerate(pool) if rolls[ri].length_yards >= ml]
            if not eligible:
                break
            pick = rng.choice(eligible)
            ri = pool.pop(pick)
            rl = rolls[ri].length_yards
            p = min(int(rl // ml), plies_remaining)
            leftover = rl - p * ml
            plies_remaining -= p
            if leftover > 0:
                if plies_remaining == 0:
                    continuation = (leftover, rolls[ri].roll_id)
                else:
                    end_bits.append((leftover, rolls[ri].roll_id))
        return plies_remaining

    for marker, plies_needed in cuts:
        ml = marker.length_yards
        plies_remaining = plies_needed

        # Phase 1: Always use continuation from previous cut
        if continuation is not None:
            c_len, c_id = continuation
            continuation = None
            if c_len >= ml:
                p = min(int(c_len // ml), plies_remaining)
                leftover = c_len - p * ml
                plies_remaining -= p
                if leftover > 0:
                    if plies_remaining == 0:
                        continuation = (leftover, c_id)
                    else:
                        end_bits.append((leftover, c_id))
            else:
                end_bits.append((c_len, c_id))

        # Phase 2+3: Order depends on mode
        if endbit_priority:
            # Evaluation mode: end-bits first, fresh rolls as fallback
            if plies_remaining > 0 and end_bits:
                plies_remaining = _use_end_bits(ml, plies_remaining)
            if plies_remaining > 0:
                plies_remaining = _use_fresh_rolls(ml, plies_remaining)
        else:
            # Planning mode: fresh rolls first, end-bits only when exhausted
            if plies_remaining > 0:
                plies_remaining = _use_fresh_rolls(ml, plies_remaining)
            if plies_remaining > 0 and end_bits:
                plies_remaining = _use_end_bits(ml, plies_remaining)

        if plies_remaining > 0:
            all_completed = False

    # Classify remaining
    if continuation is not None:
        classify(continuation[0])
    for eb_len, _ in end_bits:
        classify(eb_len)
    for ri in pool:
        classify(rolls[ri].length_yards)

    return _FloorRunResult(
        type1=round(type1, 4),
        type2=round(type2, 4),
        type3=round(type3, 4),
        type2_lengths=type2_lengths,
        completed=all_completed,
    )


def _floor_mc(
    markers: List[MarkerSpec],
    rolls: List[RollSpec],
    n_sims: int = 100,
    max_ply_height: int = 100,
    seed: int = 42,
    endbit_priority: bool = False,
) -> Dict:
    """Run N floor-realistic MC simulations. Returns waste stats + end-bit details."""
    results = []
    for i in range(n_sims):
        rng = random.Random(seed + i)
        r = _floor_simulate_once(markers, rolls, rng, max_ply_height, endbit_priority=endbit_priority)
        results.append(r)

    completion_rate = sum(1 for r in results if r.completed) / len(results)

    return {
        "type1_avg": statistics.mean(r.type1 for r in results),
        "type2_avg": statistics.mean(r.type2 for r in results),
        "type3_avg": statistics.mean(r.type3 for r in results),
        "type1_std": statistics.stdev(r.type1 for r in results) if len(results) > 1 else 0,
        "type2_std": statistics.stdev(r.type2 for r in results) if len(results) > 1 else 0,
        "type2_lengths_per_run": [r.type2_lengths for r in results],
        "completion_rate": completion_rate,
        "runs": [
            {"type1": r.type1, "type2": r.type2, "type3": r.type3, "completed": r.completed}
            for r in results
        ],
    }


def floor_mc_with_rolls(
    mc_specs: List[MarkerSpec],
    rolls: List[RollSpec],
    n_sims: int = 50,
    max_ply_height: int = 100,
    seed: int = 42,
    endbit_priority: bool = False,
) -> Dict:
    """Run floor MC using pre-prepared rolls (already trimmed/padded by caller).

    Returns the full _floor_mc result dict (includes per-run data in 'runs').
    """
    total_fabric = sum(m.length_yards * m.plies for m in mc_specs)
    if total_fabric <= 0:
        return {"runs": [], "completion_rate": 0}

    return _floor_mc(mc_specs, rolls, n_sims=n_sims, max_ply_height=max_ply_height,
                     seed=seed, endbit_priority=endbit_priority)


def estimate_floor_waste(
    mc_specs: List[MarkerSpec],
    max_ply_height: int = 100,
    avg_roll_length: float = 80.0,
    n_sims: int = 50,
    start_buffer_pct: float = 0.005,
    max_buffer_pct: float = 0.15,
    buffer_step_pct: float = 0.002,
    buffer_phase2_threshold: float = 0.03,
    buffer_step_pct_phase2: float = 0.005,
    target_completion: float = 1.0,
    endbit_priority: bool = False,
) -> Dict:
    """Iterative-buffer floor MC with two-phase stepping.

    Phase 1: start at start_buffer_pct, step by buffer_step_pct until
             buffer_phase2_threshold is reached.
    Phase 2: step by buffer_step_pct_phase2 until max_buffer_pct.

    Defaults: 0.5% start, +0.2% steps to 3%, then +0.5% steps to 15%.

    Returns dict with mc_waste_pct, mc_waste_yards, or empty dict on failure.
    Shared by cutplan_service (all strategies) and endbit solver.

    endbit_priority=False (default) for cutplan creation (maximizes T2 for planning).
    endbit_priority=True for evaluation (minimizes T2 for realistic floor behavior).
    """
    total_fabric = sum(s.length_yards * s.plies for s in mc_specs)
    if total_fabric <= 0:
        return {}

    buffer_pct = start_buffer_pct
    mc_result = None

    while buffer_pct <= max_buffer_pct:
        rolls = _generate_synthetic_rolls(total_fabric * (1 + buffer_pct), avg_length=avg_roll_length)
        mc_result = _floor_mc(mc_specs, rolls, n_sims=n_sims, max_ply_height=max_ply_height,
                              endbit_priority=endbit_priority)
        if mc_result["completion_rate"] >= target_completion:
            break
        # Two-phase stepping: fine steps below threshold, coarser above
        if buffer_pct < buffer_phase2_threshold:
            buffer_pct += buffer_step_pct
        else:
            buffer_pct += buffer_step_pct_phase2

    if mc_result is None:
        return {}

    t1, t2 = mc_result["type1_avg"], mc_result["type2_avg"]
    denom = total_fabric + t1 + t2
    mc_waste = (t1 + t2) / denom * 100 if denom > 0 else 0
    return {
        "mc_waste_pct": round(mc_waste, 2),
        "mc_waste_yards": round(t1 + t2, 2),
        "buffer_pct_used": round(buffer_pct, 4),
        "completion_rate": mc_result["completion_rate"],
        "_mc_result": mc_result,  # full result for callers that need type2_lengths etc.
    }


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------

def _max_plies_for_demand(ratio_str: str, demand: Dict[str, int], sizes: List[str]) -> int:
    """Max plies that can be cut without exceeding demand for any size."""
    parts = ratio_str.split("-")
    max_p = float('inf')
    for i, s in enumerate(sizes):
        if i < len(parts):
            r = int(parts[i])
            if r > 0:
                max_p = min(max_p, demand[s] // r)
    return int(max_p) if max_p < float('inf') else 0


def _subtract_demand(demand: Dict[str, int], ratio_str: str, plies: int, sizes: List[str]) -> Dict[str, int]:
    """Subtract garments produced by plies of a marker from demand."""
    new_demand = dict(demand)
    parts = ratio_str.split("-")
    for i, s in enumerate(sizes):
        if i < len(parts):
            new_demand[s] -= int(parts[i]) * plies
    return new_demand


def _marker_dict_to_spec(marker_dict: Dict, label: str) -> MarkerSpec:
    """Convert a marker dict to a MarkerSpec."""
    return MarkerSpec(
        marker_label=label,
        length_yards=marker_dict["length_yards"],
        plies=marker_dict["total_plies"],
        ratio_str=marker_dict["ratio_str"],
    )


def _cutplan_to_specs(plan: CutPlan) -> List[MarkerSpec]:
    """Convert a CutPlan to a list of MarkerSpec."""
    specs = []
    for i, a in enumerate(sorted(plan.assignments, key=lambda x: -x.marker.length_yards)):
        specs.append(MarkerSpec(
            marker_label=f"M{i+1}",
            length_yards=a.marker.length_yards,
            plies=a.plies,
            ratio_str=a.marker.ratio_str,
        ))
    return specs


def _build_marker_objects(
    marker_dicts: List[Dict],
    sizes: List[str],
    min_bc: int = 1,
    pattern_sizes: Optional[List[str]] = None,
) -> List[Marker]:
    """Build and filter Marker objects from raw dicts."""
    filtered = [m for m in marker_dicts if m["bundle_count"] >= min_bc]
    objs = markers_from_nesting_results(filtered, sizes, pattern_sizes=pattern_sizes)
    objs = filter_markers_for_ilp(objs)
    objs.sort(key=lambda m: -m.efficiency)
    return objs


# ---------------------------------------------------------------------------
# Main solver
# ---------------------------------------------------------------------------

def solve_endbit_optimized(
    demand: Dict[str, int],
    markers: List[Dict],
    sizes: List[str],
    avg_roll_length: float = 80.0,
    roll_length_variation: float = 0.0,
    n_simulations: int = 100,
    max_ply_height: int = 100,
    min_plies_by_bundle: Optional[Dict[int, int]] = None,
    default_buffer_pct: float = 1.0,
    pattern_sizes: Optional[List[str]] = None,
    progress_callback: Optional[Callable] = None,
    cancel_check: Optional[Callable] = None,
    real_rolls: Optional[List[RollSpec]] = None,
    endbit_pad_pct: float = 0.0,
) -> EndbitSolverResult:
    """
    EndBit Optimized cutplan strategy.

    Steps 1-7 of the algorithm described in the module docstring.

    Args:
        demand: Size -> quantity mapping
        markers: Raw marker dicts from MarkerBank
        sizes: Ordered list of size codes
        avg_roll_length: Average roll length in yards
        roll_length_variation: Half-range of roll length variation (0 = default 50-110yd)
        n_simulations: Number of MC simulations per phase
        max_ply_height: Max plies per cut
        min_plies_by_bundle: Custom min-ply constraints for re-solve (Step 6)
        default_buffer_pct: Default buffer % for final validation
        pattern_sizes: Pattern's canonical sizes for ratio_str parsing
        progress_callback: (progress_pct, message) callback
        cancel_check: Returns True to cancel
        real_rolls: Actual fabric rolls from roll plan (bypasses synthetic roll generation)
        endbit_pad_pct: Pad end-bit ply counts by this fraction (e.g. 0.02 = +2%)

    Returns:
        EndbitSolverResult with combined cutplan and MC validation
    """
    import time
    t0 = time.time()

    def _progress(pct: int, msg: str):
        if progress_callback:
            progress_callback(pct, msg)

    _progress(5, "Step 1: Solving long-marker-only cutplan (bc >= 4)...")

    # ── STEP 1: Solve with bc >= 4 markers only ──
    marker_objs_long = _build_marker_objects(markers, sizes, min_bc=4, pattern_sizes=pattern_sizes)

    # Also need 1-2 bundle markers for efficiency/length lookups later
    efficiency_lookup = {m["ratio_str"]: m["efficiency"] for m in markers}
    length_lookup = {m["ratio_str"]: m["length_yards"] for m in markers}

    if not marker_objs_long:
        raise ValueError("No markers with bundle_count >= 4 available. Need long markers for EndBit strategy.")

    plan_long, _ = solve_ilp(
        demand=demand,
        all_markers=marker_objs_long,
        sizes=sizes,
        objective="max_efficiency",
        marker_penalty=0,
        name="EndBit Long-Only",
        max_ply_height=max_ply_height,
        min_plies_by_bundle={4: 1, 5: 1, 6: 1},
    )

    if not plan_long.assignments:
        raise ValueError("ILP solver could not find a solution with bc >= 4 markers only.")

    # Verify demand is fulfilled
    produced_long = plan_long.total_produced()
    gap = {s: demand[s] - produced_long.get(s, 0) for s in sizes}
    if any(v != 0 for v in gap.values()):
        raise ValueError(f"Long-only ILP did not fulfill demand exactly. Gap: {gap}")

    specs_long = _cutplan_to_specs(plan_long)
    fabric_long = sum(s.length_yards * s.plies for s in specs_long)

    print(f"[EndBit] Step 1: {len(specs_long)} long markers, fabric={fabric_long:.1f}yd")

    if cancel_check and cancel_check():
        raise RuntimeError("Cancelled")

    # ── STEPS 2-3: Find minimum buffer via floor MC ──
    # This step uses endbit_priority=False (default) — fresh rolls first,
    # maximizes T2 waste so we know how much end-bit material is available.
    if real_rolls:
        _progress(15, f"Steps 2-3: Running MC with {len(real_rolls)} rolls...")
        # Real rolls may not cover the long-marker-only solution (Step 1 solves
        # a DIFFERENT cutplan than what was evaluated). Pad with synthetic rolls
        # if completion is too low, so end-bit data is produced.
        mc_rolls = list(real_rolls)
        mc_profile = _floor_mc(specs_long, mc_rolls, n_sims=n_simulations,
                               max_ply_height=max_ply_height)
        completion = mc_profile.get("completion_rate", 0)

        if completion < 0.90:
            # Not enough fabric — pad with synthetic rolls
            real_total = sum(r.length_yards for r in mc_rolls)
            shortfall = fabric_long * 1.05 - real_total  # 5% buffer over long-marker fabric
            if shortfall > 0:
                pad_rolls = _generate_synthetic_rolls(shortfall, avg_length=avg_roll_length)
                mc_rolls = mc_rolls + pad_rolls
                mc_profile = _floor_mc(specs_long, mc_rolls, n_sims=n_simulations,
                                       max_ply_height=max_ply_height)
                completion = mc_profile.get("completion_rate", 0)
                print(f"[EndBit] Steps 2-3: Padded with {len(pad_rolls)} synthetic rolls "
                      f"(+{shortfall:.0f}yd) → {completion*100:.0f}% completion")

        buffer_pct = 0.0
        print(f"[EndBit] Steps 2-3: Using {len(mc_rolls)} rolls → {completion*100:.0f}% completion rate")
    else:
        _progress(15, "Steps 2-3: Finding minimum buffer via MC simulation...")
        waste_est = estimate_floor_waste(
            specs_long,
            max_ply_height=max_ply_height,
            avg_roll_length=avg_roll_length,
            n_sims=n_simulations,
            start_buffer_pct=0.02,
            max_buffer_pct=0.10,
        )
        mc_profile = waste_est.get("_mc_result")
        buffer_pct = waste_est.get("buffer_pct_used", 0.02)
        completion = waste_est.get("completion_rate", 0)

        if completion >= 0.90:
            print(f"[EndBit] Steps 2-3: {buffer_pct*100:.0f}% buffer → {completion*100:.0f}% completion rate (OK)")
        else:
            print(f"[EndBit] Warning: could not reach 90% completion at {buffer_pct*100:.0f}% buffer, proceeding anyway")

    # ── STEP 4: Analyze end-bit opportunity ──
    _progress(30, "Step 4: Analyzing end-bit opportunity...")

    # Waste analysis from the profiling MC
    t1_avg = mc_profile["type1_avg"] if mc_profile else 0
    t2_avg = mc_profile["type2_avg"] if mc_profile else 0
    total_cost = fabric_long + t1_avg + t2_avg
    waste_pct = (t1_avg + t2_avg) / total_cost * 100 if total_cost > 0 else 0

    print(f"[EndBit] Step 4: T1={t1_avg:.1f}yd, T2={t2_avg:.1f}yd, waste={waste_pct:.1f}%")

    # ── STEP 5: Pick end-bit markers ──
    _progress(40, "Step 5: Selecting end-bit markers...")

    # Collect ALL small markers by bundle count, sorted by efficiency desc
    markers_by_bc: Dict[int, List[Dict]] = {1: [], 2: [], 3: []}
    for m in markers:
        bc = m["bundle_count"]
        if bc in markers_by_bc:
            markers_by_bc[bc].append(m)
    for bc in markers_by_bc:
        markers_by_bc[bc].sort(key=lambda x: -x["efficiency"])

    all_small = markers_by_bc[1] + markers_by_bc[2] + markers_by_bc[3]
    if not all_small:
        # No small markers available — fall back to just the long-only cutplan
        print("[EndBit] No 1/2/3-bundle markers available, returning long-only cutplan")
        result_markers = []
        for i, a in enumerate(plan_long.assignments):
            result_markers.append({
                "ratio_str": a.marker.ratio_str,
                "efficiency": a.marker.efficiency,
                "length_yards": a.marker.length_yards,
                "bundle_count": a.marker.bundle_count,
                "perimeter_cm": a.marker.perimeter_cm,
                "total_plies": a.plies,
                "cuts": (a.plies + max_ply_height - 1) // max_ply_height,
            })
        return EndbitSolverResult(
            main_markers=result_markers,
            endbit_markers=[],
            combined_markers=result_markers,
            main_fabric_yards=fabric_long,
            endbit_fabric_yards=0,
            total_fabric_yards=fabric_long,
            mc_waste_pct=waste_pct,
            mc_type1_avg=t1_avg,
            mc_type2_avg=t2_avg,
            endbit_fill_rate=0,
            demand_fulfilled=True,
            buffer_pct=buffer_pct,
            efficiency=plan_long.weighted_efficiency,
            total_plies=plan_long.total_plies,
            total_cuts=plan_long.total_cuts,
            bundle_cuts=plan_long.total_bundle_cuts,
            unique_markers=plan_long.unique_markers,
            solve_time=time.time() - t0,
        )

    # Estimate total end-bit plies INDEPENDENTLY for each BC tier.
    # Each tier sees the FULL end-bit pool (not cascaded leftovers), because
    # on the real floor, end-bits are a shared resource — the demand allocation
    # step below decides which markers actually get assigned.
    shortest_by_bc: Dict[int, float] = {}
    for bc in (3, 2, 1):
        if markers_by_bc[bc]:
            shortest_by_bc[bc] = min(m["length_yards"] for m in markers_by_bc[bc])

    plies_by_bc_per_run: Dict[int, List[int]] = {1: [], 2: [], 3: []}

    for run_lengths in mc_profile["type2_lengths_per_run"]:
        for bc in (3, 2, 1):
            if bc not in shortest_by_bc:
                plies_by_bc_per_run[bc].append(0)
                continue
            ml = shortest_by_bc[bc]
            # Each BC independently counts how many plies fit in the FULL end-bit pool
            p = sum(int(eb_len // ml) for eb_len in run_lengths if eb_len >= ml)
            plies_by_bc_per_run[bc].append(p)

    avg_by_bc: Dict[int, float] = {}
    for bc in (3, 2, 1):
        runs = plies_by_bc_per_run[bc]
        avg = statistics.mean(runs) if runs else 0
        if avg > 0:
            avg_by_bc[bc] = avg

    # Apply end-bit ply padding (more aggressive end-bit consumption)
    if endbit_pad_pct > 0 and avg_by_bc:
        for bc in avg_by_bc:
            avg_by_bc[bc] = math.ceil(avg_by_bc[bc] * (1.0 + endbit_pad_pct))
        print(f"[EndBit] Step 5: Applied +{endbit_pad_pct*100:.0f}% padding → "
              + ", ".join(f"{bc}b={int(v)}" for bc, v in sorted(avg_by_bc.items(), reverse=True)))

    print(f"[EndBit] Step 5: MC end-bit capacity (independent) → "
          + ", ".join(f"{bc}b={avg_by_bc.get(bc, 0):.0f}" for bc in (3, 2, 1)))

    # Allocate plies across markers, SMALLEST bundle count first (1b → 2b → 3b).
    # Rationale: 1-bundle markers are the ONLY way to consume short end-bits that
    # can't fit a 2b or 3b marker. Allocate them first, then 2b from remaining
    # demand, then 3b. Each tier's budget is capped by both the MC estimate
    # and the remaining demand. The total end-bit yardage consumed across all
    # tiers is naturally limited by the actual end-bit pool.
    eb_specs = []
    reduced_demand = dict(demand)

    for bc in (1, 2, 3):
        budget = int(avg_by_bc.get(bc, 0))
        if budget <= 0:
            continue
        for m in markers_by_bc[bc]:  # sorted by efficiency desc
            if budget <= 0:
                break
            rs = m["ratio_str"]
            max_demand = _max_plies_for_demand(rs, reduced_demand, sizes)
            plies = min(budget, max_demand)
            if plies <= 0:
                continue
            eb_specs.append(MarkerSpec(
                marker_label=f"EB-{bc}b-{rs}",
                length_yards=m["length_yards"],
                plies=plies,
                ratio_str=rs,
            ))
            reduced_demand = _subtract_demand(reduced_demand, rs, plies, sizes)
            budget -= plies

    eb_fabric = sum(s.length_yards * s.plies for s in eb_specs)
    eb_garments = sum(
        sum(int(x) for x in s.ratio_str.split("-")) * s.plies
        for s in eb_specs
    )

    print(f"[EndBit] Step 5: {len(eb_specs)} EB markers, {eb_garments} garments from {eb_fabric:.1f}yd of end-bits")
    for s in eb_specs:
        bc = sum(int(x) for x in s.ratio_str.split("-"))
        print(f"  {s.marker_label}: {s.ratio_str} ({bc}-bndl) × {s.plies} plies, {s.length_yards:.2f}yd")

    if cancel_check and cancel_check():
        raise RuntimeError("Cancelled")

    # ── STEP 6: Re-solve main cutplan for reduced demand ──
    _progress(55, "Step 6: Re-solving main cutplan for reduced demand...")

    # Check if reduced demand has any remaining need
    remaining_total = sum(max(0, v) for v in reduced_demand.values())
    if remaining_total == 0:
        # EB markers fully cover demand (unlikely but handle it)
        specs_main = []
        fabric_main = 0.0
        plan_main = CutPlan(name="EndBit Main (empty)", strategy="endbit_optimized", sizes=sizes,
                           max_ply_height=max_ply_height)
    else:
        # Build full marker pool (bc >= 2) for re-solve
        all_marker_objs = _build_marker_objects(markers, sizes, min_bc=2, pattern_sizes=pattern_sizes)

        # Add generated 1-2 bundle markers for completeness
        small_markers = generate_all_1_2_bundle_markers(sizes, efficiency_lookup, length_lookup)
        existing = {m.ratio_str for m in all_marker_objs}
        for sm in small_markers:
            if sm.ratio_str not in existing:
                all_marker_objs.append(sm)

        # Use user's min-ply constraints with high penalty to favor fewer, longer markers
        effective_min_plies = min_plies_by_bundle or {1: 1, 2: 1, 3: 10, 4: 30, 5: 40, 6: 50}

        try:
            plan_main, _ = solve_ilp(
                demand=reduced_demand,
                all_markers=all_marker_objs,
                sizes=sizes,
                objective="balanced",
                marker_penalty=20.0,  # High penalty to favor fewer markers
                name="EndBit Re-solved Main",
                max_ply_height=max_ply_height,
                min_plies_by_bundle=effective_min_plies,
            )
        except Exception as e:
            print(f"[EndBit] Step 6: Re-solve with min-ply failed ({e}), relaxing constraints...")
            # Relax: halve all minimums
            relaxed = {bc: max(1, mp // 2) for bc, mp in effective_min_plies.items()}
            plan_main, _ = solve_ilp(
                demand=reduced_demand,
                all_markers=all_marker_objs,
                sizes=sizes,
                objective="balanced",
                marker_penalty=20.0,
                name="EndBit Re-solved Main (relaxed)",
                max_ply_height=max_ply_height,
                min_plies_by_bundle=relaxed,
            )

        specs_main = _cutplan_to_specs(plan_main)
        fabric_main = sum(s.length_yards * s.plies for s in specs_main)

    # Verify combined demand fulfillment
    combined_specs = specs_main + eb_specs
    fulfilled = {}
    for m in combined_specs:
        parts = m.ratio_str.split("-")
        for i, s in enumerate(sizes):
            if i < len(parts):
                fulfilled[s] = fulfilled.get(s, 0) + int(parts[i]) * m.plies

    demand_gap = {s: demand[s] - fulfilled.get(s, 0) for s in sizes}
    demand_ok = all(v == 0 for v in demand_gap.values())

    if not demand_ok:
        print(f"[EndBit] Warning: demand gap after re-solve: {demand_gap}")

    print(f"[EndBit] Step 6: {len(specs_main)} main + {len(eb_specs)} EB markers, "
          f"main fabric={fabric_main:.1f}yd, demand {'OK' if demand_ok else 'GAP'}")

    if cancel_check and cancel_check():
        raise RuntimeError("Cancelled")

    # ── STEP 7: Final MC validation (all markers, natural floor simulation) ──
    _progress(70, "Step 7: Running final MC validation...")

    # Combine main + EB markers for a single natural floor simulation.
    # Fabric is sized for main markers only (+buffer), so fresh rolls are
    # exhausted by the time short EB markers are processed (longest-first order),
    # naturally pushing them to end-bits. No artificial constraints.
    all_specs_combined = specs_main + eb_specs

    if real_rolls:
        # Use real rolls directly — endbit_priority=True for evaluation mode
        # (end-bits consumed first, realistic floor behavior)
        mc_final = _floor_mc(all_specs_combined, real_rolls, n_sims=n_simulations,
                             max_ply_height=max_ply_height, endbit_priority=True)
        final_t1 = mc_final["type1_avg"] if mc_final else 0
        final_t2 = mc_final["type2_avg"] if mc_final else 0
        total_cost_final = fabric_main + final_t1 + final_t2
        final_waste_pct = (final_t1 + final_t2) / total_cost_final * 100 if total_cost_final > 0 else 0
        eb_fill_rate = 0
        print(f"[EndBit] Step 7: real rolls (eval mode), waste={final_waste_pct:.1f}%, "
              f"completion={mc_final.get('completion_rate', 0)*100:.0f}%")
    else:
        final_buffer_start = default_buffer_pct / 100.0 if default_buffer_pct >= 1 else default_buffer_pct
        if final_buffer_start < 0.005:
            final_buffer_start = 0.01

        waste_final = estimate_floor_waste(
            all_specs_combined,
            max_ply_height=max_ply_height,
            avg_roll_length=avg_roll_length,
            n_sims=n_simulations,
            start_buffer_pct=final_buffer_start,
            max_buffer_pct=0.10,
            buffer_step_pct=0.002,  # +0.2% fine steps for EB optimized
        )
        mc_final = waste_final.get("_mc_result")
        final_t1 = mc_final["type1_avg"] if mc_final else 0
        final_t2 = mc_final["type2_avg"] if mc_final else 0
        total_cost_final = fabric_main + final_t1 + final_t2
        final_waste_pct = (final_t1 + final_t2) / total_cost_final * 100 if total_cost_final > 0 else 0
        eb_fill_rate = 0

        print(f"[EndBit] Step 7: buffer={waste_final.get('buffer_pct_used', 0)*100:.1f}%, "
              f"waste={final_waste_pct:.1f}%, completion={waste_final.get('completion_rate', 0)*100:.0f}%")

    _progress(90, "Building combined cutplan result...")

    # Build combined result markers (main + EB) in optimize_cutplan dict format
    result_main_markers = []
    for i, a in enumerate(sorted(plan_main.assignments, key=lambda x: (-x.marker.bundle_count, -x.marker.efficiency))
                          if plan_main.assignments else []):
        result_main_markers.append({
            "ratio_str": a.marker.ratio_str,
            "efficiency": a.marker.efficiency,
            "length_yards": a.marker.length_yards,
            "bundle_count": a.marker.bundle_count,
            "perimeter_cm": a.marker.perimeter_cm,
            "total_plies": a.plies,
            "cuts": (a.plies + max_ply_height - 1) // max_ply_height,
        })

    result_eb_markers = []
    for s in eb_specs:
        bc = sum(int(x) for x in s.ratio_str.split("-"))
        # Look up efficiency from the original marker dict
        eff = efficiency_lookup.get(s.ratio_str, 0.75)
        perim = 0.0
        for m in markers:
            if m["ratio_str"] == s.ratio_str:
                perim = m.get("perimeter_cm") or 0.0
                eff = m["efficiency"]
                break
        result_eb_markers.append({
            "ratio_str": s.ratio_str,
            "efficiency": eff,
            "length_yards": s.length_yards,
            "bundle_count": bc,
            "perimeter_cm": perim,
            "total_plies": s.plies,
            "cuts": (s.plies + max_ply_height - 1) // max_ply_height,
            "is_endbit_marker": True,
        })

    combined = result_main_markers + result_eb_markers

    # Compute summary stats
    total_plies = sum(m["total_plies"] for m in combined)
    total_cuts = sum(m["cuts"] for m in combined)
    bundle_cuts = sum(m["bundle_count"] * m["cuts"] for m in combined)
    total_yards = sum(m["length_yards"] * m["total_plies"] for m in combined)
    weighted_eff = (
        sum(m["efficiency"] * m["total_plies"] for m in combined) / total_plies
        if total_plies > 0 else 0
    )

    solve_time = time.time() - t0

    print(f"[EndBit] Complete: {len(combined)} markers, {weighted_eff*100:.1f}% eff, "
          f"{total_yards:.1f}yd, waste={final_waste_pct:.1f}%, solved in {solve_time:.1f}s")

    return EndbitSolverResult(
        main_markers=result_main_markers,
        endbit_markers=result_eb_markers,
        combined_markers=combined,
        main_fabric_yards=fabric_main,
        endbit_fabric_yards=eb_fabric,
        total_fabric_yards=total_yards,
        mc_waste_pct=final_waste_pct,
        mc_type1_avg=final_t1,
        mc_type2_avg=final_t2,
        endbit_fill_rate=eb_fill_rate,
        demand_fulfilled=demand_ok,
        buffer_pct=waste_final.get("buffer_pct_used", final_buffer_start) if not real_rolls else 0.0,
        efficiency=weighted_eff,
        total_plies=total_plies,
        total_cuts=total_cuts,
        bundle_cuts=bundle_cuts,
        unique_markers=len(combined),
        solve_time=solve_time,
    )
