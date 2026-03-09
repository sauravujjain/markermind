"""
ILP Solver Runner - Parameterized ILP optimization for cutplan generation.

Extracted and parameterized from scripts/marker_selection_optimizer_v2.py
for integration into the MarkerMind backend services.
"""

import time
from typing import Dict, List, Tuple, Optional, Callable
from dataclasses import dataclass, field
from itertools import combinations_with_replacement

import numpy as np

# Default constraints
MAX_PLIES_PER_CUT = 100

# ILP solver time limit per strategy (seconds)
ILP_TIME_LIMIT = 120  # 2 minutes max per strategy

# Marker filtering: top % per bundle count (with floor)
MARKER_RETENTION_PERCENT = 0.25  # Keep top 25%
MARKER_RETENTION_FLOOR = 25     # Always keep at least 25 per bundle count

# Minimum plies by bundle count (to avoid wasteful small-ply large markers)
MIN_PLIES_BY_BUNDLE = {
    1: 1,
    2: 1,
    3: 10,
    4: 30,
    5: 40,
    6: 50,
}


def get_min_plies(bundle_count: int, custom_min_plies: Optional[Dict[int, int]] = None) -> int:
    """Get minimum plies for a marker based on its bundle count."""
    lookup = custom_min_plies if custom_min_plies else MIN_PLIES_BY_BUNDLE
    return lookup.get(bundle_count, 1)


@dataclass
class Marker:
    """A marker definition."""
    ratio: Dict[str, int]      # size -> bundle count
    ratio_str: str             # e.g., "0-3-1-1-1-0-0"
    efficiency: float          # 0.0 - 1.0
    bundle_count: int          # total bundles in marker
    length_yards: float = 0.0  # estimated length in yards
    perimeter_cm: float = 0.0  # total piece perimeter in cm (computed post-nesting)

    def produces(self, plies: int, sizes: List[str]) -> Dict[str, int]:
        """Calculate garments produced for given plies."""
        return {size: self.ratio.get(size, 0) * plies for size in sizes}

    def __hash__(self):
        return hash(self.ratio_str)

    def __eq__(self, other):
        return self.ratio_str == other.ratio_str


@dataclass
class MarkerAssignment:
    """A marker with assigned plies."""
    marker: Marker
    plies: int
    _max_ply_height: int = 100

    @property
    def cuts(self) -> int:
        return (self.plies + self._max_ply_height - 1) // self._max_ply_height

    def produces(self, sizes: List[str]) -> Dict[str, int]:
        return self.marker.produces(self.plies, sizes)


@dataclass
class CutPlan:
    """Complete cutting plan."""
    name: str
    strategy: str
    assignments: List[MarkerAssignment] = field(default_factory=list)
    sizes: List[str] = field(default_factory=list)
    max_ply_height: int = 100

    @property
    def total_plies(self) -> int:
        return sum(a.plies for a in self.assignments)

    @property
    def total_cuts(self) -> int:
        return sum(a.cuts for a in self.assignments)

    @property
    def total_bundle_cuts(self) -> int:
        """Total cutting work = sum(bundles x cuts)"""
        return sum(a.marker.bundle_count * a.cuts for a in self.assignments)

    @property
    def unique_markers(self) -> int:
        return len(self.assignments)

    @property
    def weighted_efficiency(self) -> float:
        if self.total_plies == 0:
            return 0.0
        return sum(a.marker.efficiency * a.plies for a in self.assignments) / self.total_plies

    @property
    def total_yards(self) -> float:
        """Total fabric yards."""
        return sum(a.marker.length_yards * a.plies for a in self.assignments)

    def total_produced(self) -> Dict[str, int]:
        produced = {size: 0 for size in self.sizes}
        for a in self.assignments:
            for size, qty in a.produces(self.sizes).items():
                produced[size] += qty
        return produced

    def to_dict(self) -> Dict:
        """Convert to dictionary for JSON serialization."""
        return {
            "name": self.name,
            "strategy": self.strategy,
            "efficiency": self.weighted_efficiency,
            "total_plies": self.total_plies,
            "total_cuts": self.total_cuts,
            "bundle_cuts": self.total_bundle_cuts,
            "unique_markers": self.unique_markers,
            "total_yards": self.total_yards,
            "markers": [
                {
                    "ratio_str": a.marker.ratio_str,
                    "efficiency": a.marker.efficiency,
                    "length_yards": a.marker.length_yards,
                    "bundle_count": a.marker.bundle_count,
                    "perimeter_cm": a.marker.perimeter_cm,
                    "total_plies": a.plies,
                    "cuts": a.cuts,
                }
                for a in self.assignments
            ],
        }


def generate_all_1_2_bundle_markers(
    sizes: List[str],
    efficiency_lookup: Optional[Dict[str, float]] = None,
    length_lookup: Optional[Dict[str, float]] = None,
) -> List[Marker]:
    """
    Generate all possible 1-bundle and 2-bundle markers.
    Use efficiencies and lengths from lookup where available.

    For markers not in the lookup, length is estimated from the average
    length-per-bundle of known markers with the same bundle count.
    """
    if efficiency_lookup is None:
        efficiency_lookup = {}
    if length_lookup is None:
        length_lookup = {}

    # Compute average length-per-bundle from known markers for fallback estimation
    avg_length_per_bundle = {1: 0.0, 2: 0.0}
    for bc in [1, 2]:
        lengths = []
        for ratio_str, length_yd in length_lookup.items():
            if length_yd > 0:
                parts = ratio_str.split("-")
                total = sum(int(x) for x in parts)
                if total == bc:
                    lengths.append(length_yd)
        if lengths:
            avg_length_per_bundle[bc] = sum(lengths) / len(lengths)

    # Global fallback: average across all known markers
    all_known = [ly for ly in length_lookup.values() if ly > 0]
    global_avg_per_bundle = 0.0
    if all_known:
        total_bundles = 0
        total_length = 0.0
        for ratio_str, length_yd in length_lookup.items():
            if length_yd > 0:
                parts = ratio_str.split("-")
                bc = sum(int(x) for x in parts)
                if bc > 0:
                    total_bundles += bc
                    total_length += length_yd
        if total_bundles > 0:
            global_avg_per_bundle = total_length / total_bundles

    markers = []

    # 1-bundle markers (one per size)
    for size in sizes:
        ratio = {s: (1 if s == size else 0) for s in sizes}
        ratio_str = "-".join(str(ratio[s]) for s in sizes)
        eff = efficiency_lookup.get(ratio_str, 0.70)
        length_yd = length_lookup.get(ratio_str, 0.0)
        if length_yd <= 0:
            # Estimate from average 1-bundle length, or global average
            length_yd = avg_length_per_bundle.get(1, 0.0) or (global_avg_per_bundle * 1)
        markers.append(Marker(
            ratio=ratio,
            ratio_str=ratio_str,
            efficiency=eff,
            bundle_count=1,
            length_yards=length_yd,
        ))

    # 2-bundle markers
    for combo in combinations_with_replacement(sizes, 2):
        ratio = {s: 0 for s in sizes}
        for size in combo:
            ratio[size] += 1
        ratio_str = "-".join(str(ratio[s]) for s in sizes)
        eff = efficiency_lookup.get(ratio_str, 0.75)
        length_yd = length_lookup.get(ratio_str, 0.0)
        if length_yd <= 0:
            # Estimate from average 2-bundle length, or global average
            length_yd = avg_length_per_bundle.get(2, 0.0) or (global_avg_per_bundle * 2)
        markers.append(Marker(
            ratio=ratio,
            ratio_str=ratio_str,
            efficiency=eff,
            bundle_count=2,
            length_yards=length_yd,
        ))

    return markers


def filter_markers_for_ilp(
    markers: List['Marker'],
    retention_pct: float = MARKER_RETENTION_PERCENT,
    floor: int = MARKER_RETENTION_FLOOR,
) -> List['Marker']:
    """
    Filter markers to keep top % per bundle count for ILP solver performance.

    - 1-2 bundle markers: ALL kept (small search space, needed for exact fulfillment)
    - 3+ bundle markers: top retention_pct%, with a minimum floor per bundle count

    Args:
        markers: List of Marker objects (should already be sorted by efficiency)
        retention_pct: Fraction of markers to keep per bundle count (0.25 = 25%)
        floor: Minimum markers to keep per bundle count

    Returns:
        Filtered list of Marker objects
    """
    # Group by bundle count
    by_bundle: Dict[int, List['Marker']] = {}
    for m in markers:
        bc = m.bundle_count
        if bc not in by_bundle:
            by_bundle[bc] = []
        by_bundle[bc].append(m)

    filtered = []
    stats = {}
    for bc in sorted(by_bundle.keys()):
        group = sorted(by_bundle[bc], key=lambda m: -m.efficiency)
        if bc <= 2:
            # Keep all 1-2 bundle markers
            filtered.extend(group)
            stats[bc] = (len(group), len(group))
        else:
            keep = max(floor, int(len(group) * retention_pct))
            kept = group[:keep]
            filtered.extend(kept)
            stats[bc] = (len(kept), len(group))

    total_before = len(markers)
    total_after = len(filtered)
    print(f"[ILP] Marker filtering: {total_before} → {total_after} markers "
          f"({', '.join(f'{bc}-bndl: {k}/{t}' for bc, (k, t) in sorted(stats.items()))})")

    return filtered


def markers_from_nesting_results(
    nesting_results: List[Dict],
    sizes: List[str],
    pattern_sizes: Optional[List[str]] = None,
) -> List[Marker]:
    """
    Convert nesting job results to Marker objects.

    Handles the case where the pattern has more sizes than the order demands.
    E.g., pattern has 7 sizes (XS..3XL) but order only demands 6 (XS..2XL).
    GPU nesting ratio strings use all pattern sizes, so we map and trim them.

    Args:
        nesting_results: List of dicts with ratio_str, efficiency, length_yards, bundle_count
        sizes: List of size codes in order (from order demand)
        pattern_sizes: Optional list of all sizes in the pattern (for ratio_str parsing)

    Returns:
        List of Marker objects
    """
    markers = []
    order_sizes_set = set(sizes)

    for result in nesting_results:
        ratio_str = result.get("ratio_str", "")
        if not ratio_str:
            continue

        ratio_parts = ratio_str.split("-")

        if len(ratio_parts) == len(sizes):
            # Exact match — parse directly
            ratio = {size: int(ratio_parts[i]) for i, size in enumerate(sizes)}
        elif pattern_sizes and len(ratio_parts) == len(pattern_sizes):
            # Pattern has extra sizes not in order — map and filter
            full_ratio = {size: int(ratio_parts[i]) for i, size in enumerate(pattern_sizes)}
            # Skip markers that use sizes not in the order (can't fulfill those)
            has_extra = any(full_ratio[s] > 0 for s in pattern_sizes if s not in order_sizes_set)
            if has_extra:
                continue
            ratio = {s: full_ratio[s] for s in sizes}
            ratio_str = "-".join(str(ratio[s]) for s in sizes)
        else:
            continue  # Truly incompatible

        markers.append(Marker(
            ratio=ratio,
            ratio_str=ratio_str,
            efficiency=result.get("efficiency", 0.75),
            bundle_count=result.get("bundle_count", sum(ratio.values())),
            length_yards=result.get("length_yards", 0.0),
            perimeter_cm=result.get("perimeter_cm") or 0.0,
        ))

    return markers


def solve_ilp(
    demand: Dict[str, int],
    all_markers: List[Marker],
    sizes: List[str],
    objective: str = "max_efficiency",
    marker_penalty: float = 5.0,
    name: str = "ILP Solution",
    max_ply_height: int = 100,
    min_plies_by_bundle: Optional[Dict[int, int]] = None,
    avg_roll_length_yards: Optional[float] = None,
) -> Tuple[CutPlan, float]:
    """
    Unified ILP solver with different objective functions.

    Objectives:
      - "max_efficiency": Minimize sum((1 - eff[m]) x plies[m])
      - "min_markers": Minimize number of unique markers (uses binary vars)
      - "min_plies": Minimize sum(plies[m]) - proxy for minimizing cuts
      - "min_bundle_cuts": Minimize sum(bundles[m] x cuts[m]) - actual cutting work
      - "balanced": Max efficiency + penalty for each marker used

    Args:
        demand: Size -> quantity mapping
        all_markers: List of Marker objects
        sizes: List of size codes
        objective: Optimization objective
        marker_penalty: Penalty per marker used (for "balanced" objective)
        name: Name for the cutplan

    Returns:
        Tuple of (CutPlan, solve_time_seconds)
    """
    try:
        from scipy.optimize import LinearConstraint, Bounds
    except ImportError:
        raise ImportError("scipy.optimize not available")

    t0 = time.time()
    n = len(all_markers)

    if n == 0:
        return CutPlan(name=name, strategy=objective, sizes=sizes), 0.0

    M = max(demand.values()) + 100  # Big-M
    max_cuts = (M + max_ply_height - 1) // max_ply_height + 1

    # Get minimum plies for each marker (use custom if provided)
    min_plies = [get_min_plies(m.bundle_count, min_plies_by_bundle) for m in all_markers]

    # Build objective function based on type
    # All strategies now include efficiency weighting for better results:
    #   A: max_efficiency  → eff_weight + penalty=1 (slight marker penalty)
    #   B: min_markers     → eff_weight + penalty=10 (strongly favor fewer markers)
    #   C: min_end_cuts    → handled externally as two-stage
    #   D: min_bundle_cuts → eff_weight + cutting_work_weight (3 variable model)
    #   E: balanced        → eff_weight + penalty=5 (trade-off)

    if objective == "min_bundle_cuts":
        # Variables: [plies_0..n-1, used_0..n-1, cuts_0..n-1]
        num_vars = 3 * n
        c = np.zeros(num_vars)
        for i, m in enumerate(all_markers):
            c[i] = 1 - m.efficiency       # Efficiency loss term
            c[2*n + i] = 2 * m.bundle_count  # Cutting work term (weight=2)
    elif objective == "max_efficiency":
        # Efficiency-focused with slight marker penalty (penalty=1)
        num_vars = 2 * n
        c = np.zeros(num_vars)
        for i, m in enumerate(all_markers):
            c[i] = 1 - m.efficiency
        c[n:2*n] = 1  # Light penalty per marker
    elif objective == "min_markers":
        # Strongly favor fewer markers but still consider efficiency (penalty=10)
        num_vars = 2 * n
        c = np.zeros(num_vars)
        for i, m in enumerate(all_markers):
            c[i] = 1 - m.efficiency
        c[n:2*n] = 10  # Heavy penalty per marker
    elif objective == "balanced":
        # Trade-off between efficiency and marker count (penalty=5)
        num_vars = 2 * n
        c = np.zeros(num_vars)
        for i, m in enumerate(all_markers):
            c[i] = 1 - m.efficiency
        c[n:2*n] = marker_penalty
    elif objective == "roll_optimized":
        # Penalize markers whose lengths produce large roll remainders
        num_vars = 2 * n
        c = np.zeros(num_vars)
        roll_len = avg_roll_length_yards or 100.0
        roll_penalty_weight = marker_penalty  # reuse penalty param
        for i, m in enumerate(all_markers):
            # Base efficiency cost
            eff_cost = 1 - m.efficiency
            # Roll remainder penalty: how poorly marker length divides into roll
            if m.length_yards > 0 and roll_len > 0:
                remainder = roll_len % m.length_yards
                remainder_frac = remainder / roll_len
            else:
                remainder_frac = 0.0
            c[i] = eff_cost + roll_penalty_weight * remainder_frac
        c[n:2*n] = marker_penalty
    else:
        raise ValueError(f"Unknown objective: {objective}")

    # Equality constraints: production = demand
    A_eq = []
    b_eq = []
    for size in sizes:
        row = [m.ratio.get(size, 0) for m in all_markers]
        row += [0] * (num_vars - n)
        A_eq.append(row)
        b_eq.append(demand.get(size, 0))

    # Inequality constraints
    A_ub = []
    b_ub = []

    # Constraint 1: plies[m] <= M * used[m]
    for i in range(n):
        row = [0] * num_vars
        row[i] = 1
        row[n + i] = -M
        A_ub.append(row)
        b_ub.append(0)

    # Constraint 2: plies[m] >= min_plies[m] * used[m]
    for i in range(n):
        row = [0] * num_vars
        row[i] = -1
        row[n + i] = min_plies[i]
        A_ub.append(row)
        b_ub.append(0)

    # Constraint 3 (for min_bundle_cuts): cuts[m] >= plies[m] / max_ply_height
    if objective == "min_bundle_cuts":
        for i in range(n):
            row = [0] * num_vars
            row[i] = 1
            row[2*n + i] = -max_ply_height
            A_ub.append(row)
            b_ub.append(0)

    # Bounds
    lb = np.zeros(num_vars)
    if objective == "min_bundle_cuts":
        ub = np.array([M] * n + [1] * n + [max_cuts] * n)
    else:
        ub = np.array([M] * n + [1] * n)
    bounds = Bounds(lb=lb, ub=ub)

    # All integer
    integrality = np.ones(num_vars)

    # Solve
    constraints = [
        LinearConstraint(np.array(A_eq), b_eq, b_eq),
        LinearConstraint(np.array(A_ub), -np.inf, b_ub),
    ]
    from scipy.optimize import milp as scipy_milp
    options_dict = {"time_limit": ILP_TIME_LIMIT, "disp": False}
    result = scipy_milp(c, constraints=constraints, bounds=bounds, integrality=integrality, options=options_dict)
    solve_time = time.time() - t0

    if not result.success:
        # If time limit reached but we have a feasible solution, use it
        if result.x is not None and "time limit" in str(result.message).lower():
            print(f"[ILP] {objective}: Time limit reached ({solve_time:.1f}s), using best feasible solution")
        else:
            raise RuntimeError(f"ILP failed ({solve_time:.1f}s): {result.message}")

    # Build plan
    plan = CutPlan(name=name, strategy=objective, sizes=sizes, max_ply_height=max_ply_height)
    x = np.round(result.x).astype(int)
    plies_vals = x[:n]

    for i, plies in enumerate(plies_vals):
        if plies > 0:
            plan.assignments.append(MarkerAssignment(all_markers[i], plies, _max_ply_height=max_ply_height))

    plan.assignments.sort(key=lambda a: (-a.marker.bundle_count, -a.marker.efficiency))

    return plan, solve_time


def _solve_min_end_cuts(
    demand: Dict[str, int],
    all_markers: List[Marker],
    sizes: List[str],
    penalty: float = 5.0,
    max_ply_height: int = 100,
    min_plies_by_bundle: Optional[Dict[int, int]] = None,
    name: str = "Option C: Min End Cuts",
) -> Tuple[CutPlan, float]:
    """
    Two-stage solver to minimize end cuts:
      Stage 1: Balanced ILP for ~95% of demand using larger markers (3+ bundles)
      Stage 2: Fill the remaining ~5% with small 1-2 bundle markers

    This ensures the tail end of demand is handled by short markers that are
    easy to cut, while the bulk uses efficient larger markers.
    """
    t0 = time.time()

    # Split markers into large (3+ bundles) and small (1-2 bundles)
    large_markers = [m for m in all_markers if m.bundle_count >= 3]
    small_markers = [m for m in all_markers if m.bundle_count <= 2]

    # Stage 1: Solve balanced for 95% of demand using large markers
    demand_95 = {}
    for size, qty in demand.items():
        demand_95[size] = max(1, int(qty * 0.95))

    plan = CutPlan(name=name, strategy="min_end_cuts", sizes=sizes, max_ply_height=max_ply_height)

    if large_markers:
        try:
            plan_stage1, _ = solve_ilp(
                demand=demand_95,
                all_markers=large_markers,
                sizes=sizes,
                objective="balanced",
                marker_penalty=penalty,
                max_ply_height=max_ply_height,
                min_plies_by_bundle=min_plies_by_bundle,
                name=f"{name} (Stage 1)",
            )

            # Compute what stage 1 produced
            produced = plan_stage1.total_produced()
            for a in plan_stage1.assignments:
                plan.assignments.append(MarkerAssignment(a.marker, a.plies, _max_ply_height=max_ply_height))

        except Exception as e:
            print(f"[ILP] Min End Cuts stage 1 failed: {e}, falling back to full balanced")
            produced = {s: 0 for s in sizes}
    else:
        produced = {s: 0 for s in sizes}

    # Stage 2: Fill remainder with small markers (relaxed min_plies: 1 for all)
    remainder = {}
    for size in sizes:
        rem = demand[size] - produced.get(size, 0)
        if rem > 0:
            remainder[size] = rem

    if remainder and any(v > 0 for v in remainder.values()) and small_markers:
        # Relaxed min plies for small markers
        relaxed_min_plies = {1: 1, 2: 1, 3: 1, 4: 1, 5: 1, 6: 1}
        try:
            plan_stage2, _ = solve_ilp(
                demand=remainder,
                all_markers=small_markers,
                sizes=sizes,
                objective="balanced",
                marker_penalty=1,  # Light penalty for remainder
                max_ply_height=max_ply_height,
                min_plies_by_bundle=relaxed_min_plies,
                name=f"{name} (Stage 2)",
            )

            for a in plan_stage2.assignments:
                plan.assignments.append(MarkerAssignment(a.marker, a.plies, _max_ply_height=max_ply_height))
        except Exception as e:
            print(f"[ILP] Min End Cuts stage 2 failed: {e}")

    plan.assignments.sort(key=lambda a: (-a.marker.bundle_count, -a.marker.efficiency))
    solve_time = time.time() - t0
    return plan, solve_time


def optimize_cutplan(
    demand: Dict[str, int],
    markers: List[Dict],
    sizes: List[str],
    options: List[str] = None,
    penalty: float = 5.0,
    strategy_callback: Optional[Callable[[str, Dict], None]] = None,
    cancel_check: Optional[Callable[[], bool]] = None,
    pattern_sizes: Optional[List[str]] = None,
    max_ply_height: int = 100,
    min_plies_by_bundle_str: Optional[str] = None,
    avg_roll_length_yards: Optional[float] = None,
) -> List[Dict]:
    """
    Run ILP optimization with multiple strategies.

    Args:
        demand: Size -> quantity mapping
        markers: List of marker dicts from nesting results
        sizes: List of size codes in order
        options: List of strategies to run (default: ["max_efficiency", "balanced", "min_markers"])
        penalty: Penalty for balanced objective
        strategy_callback: Called after each strategy completes with (strategy_name, result_dict)
        cancel_check: Returns True if job should be cancelled
        pattern_sizes: Optional list of all sizes in the pattern (for ratio_str parsing
                       when pattern has more sizes than the order demands)

    Returns:
        List of cutplan option dicts with cost breakdowns
    """
    if options is None:
        options = ["max_efficiency", "balanced", "min_markers"]

    print(f"[ILP] Starting optimization: {len(markers)} raw markers, {len(options)} strategies, demand={demand}")
    if pattern_sizes and len(pattern_sizes) != len(sizes):
        print(f"[ILP] Pattern has {len(pattern_sizes)} sizes {pattern_sizes}, order has {len(sizes)} sizes {sizes} — remapping ratio strings")

    # Convert markers to Marker objects
    marker_objects = markers_from_nesting_results(markers, sizes, pattern_sizes=pattern_sizes)
    print(f"[ILP] Parsed {len(marker_objects)} markers from {len(markers)} raw results")

    # Generate all 1-2 bundle markers for completeness
    efficiency_lookup = {m.ratio_str: m.efficiency for m in marker_objects}
    length_lookup = {m.ratio_str: m.length_yards for m in marker_objects}
    small_markers = generate_all_1_2_bundle_markers(sizes, efficiency_lookup, length_lookup)

    # Combine (avoiding duplicates)
    existing_ratios = {m.ratio_str for m in marker_objects}
    for sm in small_markers:
        if sm.ratio_str not in existing_ratios:
            marker_objects.append(sm)

    print(f"[ILP] After adding 1-2 bundle completions: {len(marker_objects)} markers")

    # Filter markers for solver performance (top 25% per bundle, floor 25)
    marker_objects = filter_markers_for_ilp(marker_objects)

    # Sort by efficiency
    marker_objects.sort(key=lambda m: -m.efficiency)

    print(f"[ILP] Final marker pool: {len(marker_objects)} markers → "
          f"{len(marker_objects) * 2} ILP variables")

    # Run each strategy
    cutplan_options = []

    strategy_names = {
        "max_efficiency": "Option A: Max Efficiency",
        "min_markers": "Option B: Min Markers",
        "min_end_cuts": "Option C: Min End Cuts",
        "min_bundle_cuts": "Option D: Min Cutting Work",
        "balanced": "Option E: Balanced",
        "roll_optimized": "Option F: Roll Optimized",
    }

    # Parse min_plies_by_bundle from custom string if provided
    custom_min_plies = None
    if min_plies_by_bundle_str:
        try:
            custom_min_plies = {}
            for part in min_plies_by_bundle_str.split(","):
                bc, mp = part.strip().split(":")
                custom_min_plies[int(bc)] = int(mp)
        except Exception:
            custom_min_plies = None

    for idx, option in enumerate(options):
        # Check for cancellation
        if cancel_check and cancel_check():
            print(f"[ILP] Cancelled before strategy {option}")
            break

        print(f"[ILP] Running strategy {idx+1}/{len(options)}: {option}...")
        try:
            if option == "min_end_cuts":
                # Two-stage: balanced for 95% demand, small markers for remainder
                plan, solve_time = _solve_min_end_cuts(
                    demand=demand,
                    all_markers=marker_objects,
                    sizes=sizes,
                    penalty=penalty,
                    max_ply_height=max_ply_height,
                    min_plies_by_bundle=custom_min_plies,
                    name=strategy_names[option],
                )
            elif option == "min_plies":
                # Legacy alias → redirect to min_end_cuts
                plan, solve_time = _solve_min_end_cuts(
                    demand=demand,
                    all_markers=marker_objects,
                    sizes=sizes,
                    penalty=penalty,
                    max_ply_height=max_ply_height,
                    min_plies_by_bundle=custom_min_plies,
                    name=strategy_names.get("min_end_cuts", "Option C: Min End Cuts"),
                )
            else:
                plan, solve_time = solve_ilp(
                    demand=demand,
                    all_markers=marker_objects,
                    sizes=sizes,
                    objective=option,
                    marker_penalty=penalty,
                    max_ply_height=max_ply_height,
                    min_plies_by_bundle=custom_min_plies,
                    name=strategy_names.get(option, f"Option: {option}"),
                    avg_roll_length_yards=avg_roll_length_yards,
                )
            result = plan.to_dict()
            result["solve_time"] = solve_time
            cutplan_options.append(result)
            print(f"[ILP] Strategy {option}: {plan.weighted_efficiency*100:.1f}% eff, "
                  f"{plan.unique_markers} markers, solved in {solve_time:.1f}s")

            # Notify of incremental result
            if strategy_callback:
                strategy_callback(option, result)

        except Exception as e:
            print(f"[ILP] Strategy {option} failed ({type(e).__name__}): {e}")
            continue

    return cutplan_options


def calculate_cutplan_costs(
    cutplan: Dict,
    fabric_cost_per_yard: float = 3.0,
    max_ply_height: int = 100,
    spreading_cost_per_yard: float = 0.00122,
    spreading_cost_per_ply: float = 0.013,
    cutting_cost_per_cm: float = 0.0000278,
    prep_cost_per_meter: float = 0.25,
    perimeter_by_size: Optional[Dict[str, float]] = None,
    sizes: Optional[List[str]] = None,
) -> Dict:
    """
    Calculate cost breakdown for a cutplan.

    Formulas:
      Fabric    = total_yards × fabric_cost_per_yard
      Spreading = (total_yards × spreading_cost_per_yard) + (total_plies × spreading_cost_per_ply)
      Cutting   = Σ(marker_perimeter_cm × cuts × cutting_cost_per_cm) per marker
                  marker_perimeter = sum of perimeter_by_size[size] * ratio[size] for each size
      Prep      = Σ(marker_length_m × cuts × prep_cost_per_meter) per marker

    Args:
        cutplan: Cutplan dict from optimize_cutplan
        fabric_cost_per_yard: Cost per yard of fabric
        max_ply_height: Maximum plies per cut
        spreading_cost_per_yard: Cost per yard for spreading (area component)
        spreading_cost_per_ply: Cost per ply for spreading (layer component)
        cutting_cost_per_cm: Cost per cm of perimeter per cut
        prep_cost_per_meter: Per-meter prep material cost (sum of enabled paper layers)
        perimeter_by_size: Dict mapping size -> total perimeter in cm for one bundle
        sizes: Ordered list of sizes matching the ratio_str positions

    Returns:
        Dictionary with cost breakdown
    """
    total_yards = cutplan.get("total_yards", 0)
    total_plies = cutplan.get("total_plies", 0)
    unique_markers = cutplan.get("unique_markers", 0)
    total_cuts = cutplan.get("total_cuts", 0)
    markers = cutplan.get("markers", [])

    # Fabric cost: total_yards × rate
    fabric_cost = total_yards * fabric_cost_per_yard

    # Spreading cost: area component + per-ply component
    spreading_cost = (total_yards * spreading_cost_per_yard) + (total_plies * spreading_cost_per_ply)

    # Cutting cost: actual perimeter × cuts × rate per marker
    YARDS_TO_METERS = 0.9144
    AVG_PERIMETER_PER_BUNDLE_CM = 2540  # fallback: ~1000 inches in cm
    cutting_cost = 0.0
    prep_cost = 0.0

    for m in markers:
        ratio_str = m.get("ratio_str", "0")
        ratio_counts = [int(x) for x in ratio_str.split("-")]
        marker_plies = m.get("total_plies", 0)
        marker_cuts = (marker_plies + max_ply_height - 1) // max_ply_height if marker_plies > 0 else 0

        # Prefer marker-level perimeter (computed post-nesting from actual placed pieces)
        marker_perimeter_cm = m.get("perimeter_cm") or 0.0

        if marker_perimeter_cm <= 0:
            # Fallback: compute from per-size perimeters (pattern parse data)
            if perimeter_by_size and sizes and len(sizes) == len(ratio_counts):
                marker_perimeter_cm = 0.0
                for i, count in enumerate(ratio_counts):
                    if count > 0:
                        size = sizes[i]
                        size_perim = perimeter_by_size.get(size, AVG_PERIMETER_PER_BUNDLE_CM)
                        marker_perimeter_cm += size_perim * count
            else:
                # Last resort: estimate from bundle count
                bundle_count = m.get("bundle_count", sum(ratio_counts))
                marker_perimeter_cm = bundle_count * AVG_PERIMETER_PER_BUNDLE_CM

        cutting_cost += marker_perimeter_cm * marker_cuts * cutting_cost_per_cm

        # Prep cost: marker_length_m × cuts × prep_cost_per_meter
        marker_length_yards = m.get("length_yards", 0)
        marker_length_m = marker_length_yards * YARDS_TO_METERS
        prep_cost += marker_length_m * marker_cuts * prep_cost_per_meter

    total_cost = fabric_cost + spreading_cost + cutting_cost + prep_cost

    return {
        "total_cost": total_cost,
        "fabric_cost": fabric_cost,
        "spreading_cost": spreading_cost,
        "cutting_cost": cutting_cost,
        "prep_cost": prep_cost,
        "total_yards": total_yards,
        "total_plies": total_plies,
        "total_cuts": total_cuts,
        "unique_markers": unique_markers,
        "efficiency": cutplan.get("efficiency", 0),
    }
