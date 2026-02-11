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


def get_min_plies(bundle_count: int) -> int:
    """Get minimum plies for a marker based on its bundle count."""
    return MIN_PLIES_BY_BUNDLE.get(bundle_count, 1)


@dataclass
class Marker:
    """A marker definition."""
    ratio: Dict[str, int]      # size -> bundle count
    ratio_str: str             # e.g., "0-3-1-1-1-0-0"
    efficiency: float          # 0.0 - 1.0
    bundle_count: int          # total bundles in marker
    length_yards: float = 0.0  # estimated length in yards

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

    @property
    def cuts(self) -> int:
        return (self.plies + MAX_PLIES_PER_CUT - 1) // MAX_PLIES_PER_CUT

    def produces(self, sizes: List[str]) -> Dict[str, int]:
        return self.marker.produces(self.plies, sizes)


@dataclass
class CutPlan:
    """Complete cutting plan."""
    name: str
    strategy: str
    assignments: List[MarkerAssignment] = field(default_factory=list)
    sizes: List[str] = field(default_factory=list)

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
                    "total_plies": a.plies,
                    "cuts": a.cuts,
                }
                for a in self.assignments
            ],
        }


def generate_all_1_2_bundle_markers(
    sizes: List[str],
    efficiency_lookup: Optional[Dict[str, float]] = None,
) -> List[Marker]:
    """
    Generate all possible 1-bundle and 2-bundle markers.
    Use efficiencies from lookup where available.
    """
    if efficiency_lookup is None:
        efficiency_lookup = {}

    markers = []

    # 1-bundle markers (one per size)
    for size in sizes:
        ratio = {s: (1 if s == size else 0) for s in sizes}
        ratio_str = "-".join(str(ratio[s]) for s in sizes)
        eff = efficiency_lookup.get(ratio_str, 0.70)
        markers.append(Marker(
            ratio=ratio,
            ratio_str=ratio_str,
            efficiency=eff,
            bundle_count=1,
        ))

    # 2-bundle markers
    for combo in combinations_with_replacement(sizes, 2):
        ratio = {s: 0 for s in sizes}
        for size in combo:
            ratio[size] += 1
        ratio_str = "-".join(str(ratio[s]) for s in sizes)
        eff = efficiency_lookup.get(ratio_str, 0.75)
        markers.append(Marker(
            ratio=ratio,
            ratio_str=ratio_str,
            efficiency=eff,
            bundle_count=2,
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
) -> List[Marker]:
    """
    Convert nesting job results to Marker objects.

    Args:
        nesting_results: List of dicts with ratio_str, efficiency, length_yards, bundle_count
        sizes: List of size codes in order

    Returns:
        List of Marker objects
    """
    markers = []

    for result in nesting_results:
        ratio_str = result.get("ratio_str", "")
        if not ratio_str:
            continue

        ratio_parts = ratio_str.split("-")
        if len(ratio_parts) != len(sizes):
            continue

        ratio = {size: int(ratio_parts[i]) for i, size in enumerate(sizes)}

        markers.append(Marker(
            ratio=ratio,
            ratio_str=ratio_str,
            efficiency=result.get("efficiency", 0.75),
            bundle_count=result.get("bundle_count", sum(ratio.values())),
            length_yards=result.get("length_yards", 0.0),
        ))

    return markers


def solve_ilp(
    demand: Dict[str, int],
    all_markers: List[Marker],
    sizes: List[str],
    objective: str = "max_efficiency",
    marker_penalty: float = 5.0,
    name: str = "ILP Solution",
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
    max_cuts = (M + MAX_PLIES_PER_CUT - 1) // MAX_PLIES_PER_CUT + 1

    # Get minimum plies for each marker
    min_plies = [get_min_plies(m.bundle_count) for m in all_markers]

    # Build objective function based on type
    if objective == "min_bundle_cuts":
        # Variables: [plies_0..n-1, used_0..n-1, cuts_0..n-1]
        num_vars = 3 * n
        c = np.zeros(num_vars)
        for i, m in enumerate(all_markers):
            c[2*n + i] = m.bundle_count  # Minimize bundle_count * cuts
    elif objective == "balanced":
        # Variables: [plies_0..n-1, used_0..n-1]
        num_vars = 2 * n
        c = np.zeros(num_vars)
        for i, m in enumerate(all_markers):
            c[i] = 1 - m.efficiency  # Efficiency loss
        c[n:2*n] = marker_penalty  # Penalty per marker
    else:
        # Variables: [plies_0..n-1, used_0..n-1]
        num_vars = 2 * n

        if objective == "max_efficiency":
            c = np.zeros(num_vars)
            for i, m in enumerate(all_markers):
                c[i] = 1 - m.efficiency
        elif objective == "min_markers":
            c = np.zeros(num_vars)
            c[n:2*n] = 1
        elif objective == "min_plies":
            c = np.zeros(num_vars)
            c[:n] = 1
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

    # Constraint 3 (for min_bundle_cuts): cuts[m] >= plies[m] / MAX_PLIES_PER_CUT
    if objective == "min_bundle_cuts":
        for i in range(n):
            row = [0] * num_vars
            row[i] = 1
            row[2*n + i] = -MAX_PLIES_PER_CUT
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
    plan = CutPlan(name=name, strategy=objective, sizes=sizes)
    x = np.round(result.x).astype(int)
    plies_vals = x[:n]

    for i, plies in enumerate(plies_vals):
        if plies > 0:
            plan.assignments.append(MarkerAssignment(all_markers[i], plies))

    plan.assignments.sort(key=lambda a: (-a.marker.bundle_count, -a.marker.efficiency))

    return plan, solve_time


def optimize_cutplan(
    demand: Dict[str, int],
    markers: List[Dict],
    sizes: List[str],
    options: List[str] = None,
    penalty: float = 5.0,
    strategy_callback: Optional[Callable[[str, Dict], None]] = None,
    cancel_check: Optional[Callable[[], bool]] = None,
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

    Returns:
        List of cutplan option dicts with cost breakdowns
    """
    if options is None:
        options = ["max_efficiency", "balanced", "min_markers"]

    print(f"[ILP] Starting optimization: {len(markers)} raw markers, {len(options)} strategies, demand={demand}")

    # Convert markers to Marker objects
    marker_objects = markers_from_nesting_results(markers, sizes)

    # Generate all 1-2 bundle markers for completeness
    efficiency_lookup = {m.ratio_str: m.efficiency for m in marker_objects}
    small_markers = generate_all_1_2_bundle_markers(sizes, efficiency_lookup)

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
        "min_plies": "Option C: Min Plies",
        "min_bundle_cuts": "Option D: Min Bundle-Cuts",
        "balanced": f"Option E: Balanced (penalty={penalty})",
    }

    for idx, option in enumerate(options):
        # Check for cancellation
        if cancel_check and cancel_check():
            print(f"[ILP] Cancelled before strategy {option}")
            break

        print(f"[ILP] Running strategy {idx+1}/{len(options)}: {option}...")
        try:
            plan, solve_time = solve_ilp(
                demand=demand,
                all_markers=marker_objects,
                sizes=sizes,
                objective=option,
                marker_penalty=penalty,
                name=strategy_names.get(option, f"Option: {option}"),
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
    cutting_cost_per_inch: float = 0.000424,
    prep_cost_per_marker: float = 0.03,
) -> Dict:
    """
    Calculate cost breakdown for a cutplan.

    Formulas (from docs/cutting_costs.md):
      Fabric    = total_yards × fabric_cost_per_yard
      Spreading = (total_yards × 0.00122) + (total_plies × 0.013)
      Cutting   = Σ(marker_perimeter × cuts × 0.000424) per marker
      Prep      = unique_markers × 0.03

    Args:
        cutplan: Cutplan dict from optimize_cutplan
        fabric_cost_per_yard: Cost per yard of fabric
        max_ply_height: Maximum plies per cut
        spreading_cost_per_yard: Cost per yard for spreading (area component)
        spreading_cost_per_ply: Cost per ply for spreading (layer component)
        cutting_cost_per_inch: Cost per inch of perimeter per cut
        prep_cost_per_marker: Cost per unique marker for preparation

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

    # Cutting cost: perimeter × cuts × rate per marker
    # Use actual bundle perimeters if available, otherwise estimate from bundle count
    # Average perimeter per bundle ~1,000 inches (from docs: sizes range 988-1055")
    AVG_PERIMETER_PER_BUNDLE = 1000  # inches
    cutting_cost = 0.0
    for m in markers:
        bundle_count = m.get("bundle_count", sum(int(x) for x in m.get("ratio_str", "0").split("-")))
        marker_perimeter = bundle_count * AVG_PERIMETER_PER_BUNDLE
        marker_plies = m.get("total_plies", 0)
        marker_cuts = (marker_plies + max_ply_height - 1) // max_ply_height if marker_plies > 0 else 0
        cutting_cost += marker_perimeter * marker_cuts * cutting_cost_per_inch

    # Prep cost: per unique marker
    prep_cost = unique_markers * prep_cost_per_marker

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
