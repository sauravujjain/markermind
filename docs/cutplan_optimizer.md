# Cutplan Optimizer (ILP-based Marker Selection)

Integer Linear Programming (ILP) based solver for selecting optimal marker combinations to fulfill production demand.

## Cutplan Pipeline Stages

The cutplan pipeline has 3 stages. Each stage refines the previous stage's results.

### Stage 1: ILP Solve (Ratio + Plies Selection)

**Input**: GPU-nested marker bank (ratios with GPU lengths/efficiencies), order demand
**Output**: Cutplan options (A, B, C, D) — each is a set of marker ratios + ply counts
**What happens**: The ILP solver selects optimal marker ratios and ply counts using GPU lengths as the cost metric. EndBit Optimized (Option D) also runs its own solver.
**Frontend**: Shows cutplan options with ratio/plies only. **No marker lengths, efficiencies, or costs are displayed yet** — GPU lengths are internal to the solver and not shown to the user.

### Stage 2: Quick CPU-Vector Nest (Lengths + Costs)

**Input**: Marker ratios from Stage 1
**Trigger**: Automatic — starts immediately after Stage 1 completes
**Output**: CPU-nested marker lengths, efficiencies, mini SVG previews, cost breakdown, floor waste estimate
**What happens**: Each unique marker ratio is CPU-vector nested for a short duration (~20s). Updated lengths replace GPU lengths. Costs (fabric, spreading, cutting, prep) and floor MC waste are calculated using these CPU lengths. Results are pushed to the frontend as each cutplan completes.
**Frontend**: Marker lengths, efficiencies, SVG thumbnails, cost breakdown, and Est. Floor Waste appear.

### Stage 3: Refine Markers (User-Triggered)

**Input**: Markers from Stage 2
**Trigger**: User clicks "Refine" on a cutplan with advanced nesting settings (time limit, piece buffer, etc.)
**Output**: Refined marker lengths, efficiencies, full SVG previews
**What happens**: Markers are CPU-vector nested for longer duration with user-configured settings to squeeze maximum nesting efficiency. Lengths and efficiencies are updated. Marker is ready for roll plan stage if needed.
**Frontend**: Updated lengths/efficiencies, refined SVG previews, recalculated costs. Card shows gold/amber styling.

---

## Purpose

Given an order demand (garments per size) and a pool of available markers (pre-computed via GPU nesting), select which markers to use and how many plies of each to minimize total fabric usage while meeting exact demand.

## Dependencies

```python
pip install pulp    # ILP modeling library (includes CBC solver)
```

PuLP provides a Python interface to various ILP solvers. The default CBC (COIN-OR Branch and Cut) solver is included and works well for marker selection problems.

## Core Concepts

| Term | Definition |
|------|------------|
| **Bundle** | 1 complete garment (all pieces for one size) |
| **Marker** | Layout pattern with a specific size ratio (e.g., "0-3-1-1-1-0-0" = 6 bundles) |
| **Ratio** | Distribution of sizes in a marker (e.g., 0 × size 46, 3 × size 48, ...) |
| **Plies** | Fabric layers stacked for cutting (typically max 100 per cut) |
| **Efficiency** | (Piece area / Marker area) - how well pieces fill the marker |
| **Length** | Marker length in yards or meters |

## Problem Formulation

### Single-Color (Independent) Optimization

Optimize marker selection for one order/color at a time.

**Variables:**
- `plies[m] ∈ Z≥0` - number of plies for marker m
- `used[m] ∈ {0,1}` - binary: 1 if marker m is used

**Objective:**
```
minimize: Σ_m (length[m] × plies[m]) + penalty × Σ_m used[m]
```

The penalty term encourages using fewer unique markers (simpler cutting plan).

**Constraints:**

1. **Exact Demand Fulfillment** (per size):
```
Σ_m (ratio[m][s] × plies[m]) = demand[s]   ∀ size s
```

2. **Linking Constraint** (connect binary to integer):
```
plies[m] ≤ M × used[m]   ∀ marker m
```
Where M is a "big-M" value (e.g., max_demand + 100).

3. **Optional: Minimum Plies** (prevent inefficient small runs):
```
plies[m] ≥ min_plies[bundle_count] × used[m]   ∀ marker m
```

Typical minimum plies by bundle count:
| Bundle Count | Min Plies |
|--------------|-----------|
| 6-bundle | 50 |
| 5-bundle | 40 |
| 4-bundle | 30 |
| 3-bundle | 10 |
| 1-2 bundle | 1 |

### Multicolor (Joint) Optimization

Optimize marker selection across multiple orders/colors simultaneously. Markers can be shared across colors (same marker used for different fabric colors).

**Variables:**
- `plies[m,c] ∈ Z≥0` - plies of marker m for color c
- `used[m] ∈ {0,1}` - binary: 1 if marker m is used for ANY color

**Objective:**
```
minimize: Σ_m Σ_c (length[m] × plies[m,c]) + penalty × Σ_m used[m]
```

The key insight: `used[m]` is shared across colors, so using the same marker for multiple colors only incurs one penalty.

**Constraints:**

1. **Exact Demand** (per color, per size):
```
Σ_m (ratio[m][s] × plies[m,c]) = demand[c][s]   ∀ color c, size s
```

2. **Linking** (per color):
```
plies[m,c] ≤ M × used[m]   ∀ marker m, color c
```

### Two-Stage Optimization

Split fulfillment into two phases for faster convergence:

**Stage 1**: Use 4,6-bundle (high-efficiency) markers to fulfill 93-96% of demand
- Uses inequality constraints (`≥` target, `≤` demand per size)
- Faster to solve due to relaxed constraints
- Pool: Top 20 markers per bundle type (4 and 6)

**Stage 2**: Use 2-bundle markers to fulfill exact remainder
- Uses equality constraints
- Small problem (few remaining garments)
- Pool: All 2-bundle markers

## Implementation

### Loading Marker Pool

Markers are loaded from the GPU nesting brute force CSV:

```python
@dataclass
class Marker:
    ratio: Dict[str, int]      # e.g., {"46": 0, "48": 3, "50": 1, ...}
    ratio_str: str             # e.g., "0-3-1-1-1-0-0"
    efficiency: float          # e.g., 0.805
    bundle_count: int          # Sum of ratio values
    length_yards: float        # Marker length

def load_markers_from_csv(csv_path: Path) -> List[Marker]:
    markers = []
    with open(csv_path, 'r') as f:
        f.readline()  # skip header
        for line in f:
            ratio_str, bundle_count, efficiency, length_yards = line.strip().split(',')
            ratio = {size: int(v) for size, v in zip(ALL_SIZES, ratio_str.split("-"))}
            markers.append(Marker(
                ratio=ratio,
                ratio_str=ratio_str,
                efficiency=float(efficiency),
                bundle_count=int(bundle_count),
                length_yards=float(length_yards)
            ))
    return markers
```

### Filtering Marker Pool

For tractable ILP solving, filter to top N markers per bundle type:

```python
def get_filtered_pool(markers: List[Marker], top_n_per_bundle: int = 25) -> List[Marker]:
    """
    Top N per bundle type keeps pool manageable.

    With top_n=25 and 3 bundle types (2,4,6):
    - 75 markers → 75×n_colors + 75 = ~375 variables (4 colors)
    - Solves in ~90-120 seconds
    """
    pool = []
    for bc in [2, 4, 6]:  # Or [2, 4, 5, 6] for 5-bundle support
        group = [m for m in markers if m.bundle_count == bc]
        group.sort(key=lambda m: -m.efficiency)
        pool.extend(group[:top_n_per_bundle])
    return pool
```

### Single-Color Solver

```python
def solve_single_color(
    demand: Dict[str, int],
    markers: List[Marker],
    penalty: float = 5.0,
    min_plies: Dict[int, int] = None
) -> Tuple[List[Tuple[Marker, int]], float, int, bool]:
    """
    Solve for single order/color.

    Returns: (assignments, total_length, unique_markers, success)
    """
    n = len(markers)
    M = sum(demand.values()) + 100  # Big-M

    prob = pulp.LpProblem("SingleColor", pulp.LpMinimize)

    # Variables
    plies_vars = [pulp.LpVariable(f"p_{i}", lowBound=0, cat='Integer') for i in range(n)]
    used_vars = [pulp.LpVariable(f"u_{i}", cat='Binary') for i in range(n)]

    # Objective
    prob += (
        pulp.lpSum(markers[i].length_yards * plies_vars[i] for i in range(n)) +
        penalty * pulp.lpSum(used_vars)
    )

    # Exact demand constraints
    for s in ALL_SIZES:
        prob += (
            pulp.lpSum(markers[i].ratio[s] * plies_vars[i] for i in range(n)) == demand[s],
            f"demand_{s}"
        )

    # Linking constraints
    for i in range(n):
        prob += plies_vars[i] <= M * used_vars[i], f"link_{i}"

    # Optional minimum plies
    if min_plies:
        for i in range(n):
            mp = min_plies.get(markers[i].bundle_count, 1)
            prob += plies_vars[i] >= mp * used_vars[i], f"min_{i}"

    # Solve
    solver = pulp.PULP_CBC_CMD(msg=0, timeLimit=120)
    prob.solve(solver)

    if prob.status != pulp.LpStatusOptimal:
        return [], 0.0, 0, False

    # Extract results
    assignments = []
    total_length = 0.0
    for i in range(n):
        p = int(round(plies_vars[i].varValue or 0))
        if p > 0:
            assignments.append((markers[i], p))
            total_length += markers[i].length_yards * p

    return assignments, total_length, len(assignments), True
```

### Multicolor Solver

```python
def solve_multicolor(
    orders: Dict[str, Dict[str, int]],   # {color: {size: qty}}
    markers: List[Marker],
    penalty: float = 3.0
) -> Tuple[Dict[str, List[Tuple[Marker, int]]], float, int, bool]:
    """
    Joint optimization across multiple colors.

    Key: used[m] is SHARED across colors, encouraging marker reuse.
    """
    colors = list(orders.keys())
    n_markers = len(markers)
    n_colors = len(colors)

    M = max(sum(orders[c].values()) for c in colors) + 100

    prob = pulp.LpProblem("Multicolor", pulp.LpMinimize)

    # Variables: plies per marker per color
    plies_vars = {
        (m, c): pulp.LpVariable(f"p_{m}_{c}", lowBound=0, cat='Integer')
        for m in range(n_markers)
        for c in range(n_colors)
    }

    # SHARED binary: used if marker used for ANY color
    used_vars = [pulp.LpVariable(f"u_{m}", cat='Binary') for m in range(n_markers)]

    # Objective: total fabric + penalty × unique markers
    prob += (
        pulp.lpSum(
            markers[m].length_yards * plies_vars[(m, c)]
            for m in range(n_markers)
            for c in range(n_colors)
        ) +
        penalty * pulp.lpSum(used_vars)
    )

    # Exact demand per color per size
    for c_idx, color in enumerate(colors):
        for s in ALL_SIZES:
            prob += (
                pulp.lpSum(markers[m].ratio[s] * plies_vars[(m, c_idx)] for m in range(n_markers))
                == orders[color][s],
                f"demand_{color}_{s}"
            )

    # Linking: if any plies for marker m, used[m] = 1
    for m in range(n_markers):
        for c in range(n_colors):
            prob += plies_vars[(m, c)] <= M * used_vars[m], f"link_{m}_{c}"

    solver = pulp.PULP_CBC_CMD(msg=1, timeLimit=120)
    prob.solve(solver)

    if prob.status != pulp.LpStatusOptimal:
        return {}, 0.0, 0, False

    # Extract results
    assignments = {color: [] for color in colors}
    total_length = 0.0
    markers_used = set()

    for m in range(n_markers):
        for c_idx, color in enumerate(colors):
            p = int(round(plies_vars[(m, c_idx)].varValue or 0))
            if p > 0:
                assignments[color].append((markers[m], p))
                total_length += markers[m].length_yards * p
                markers_used.add(m)

    return assignments, total_length, len(markers_used), True
```

### Two-Stage Solver

```python
def solve_stage1(orders, markers, target_pct=0.95, penalty=3.0):
    """
    Stage 1: 4,6-bundle markers fill target_pct of demand.
    Uses inequality constraints for faster solving.
    """
    targets = {c: int(target_pct * sum(orders[c].values())) for c in orders}

    prob = pulp.LpProblem("Stage1", pulp.LpMinimize)
    # ... variables ...

    # Inequality: total production >= target (not exact)
    for c_idx, color in enumerate(colors):
        prob += (
            pulp.lpSum(sum(markers[m].ratio[s] for s in ALL_SIZES) * plies[(m, c_idx)]
                       for m in range(n_markers)) >= targets[color],
            f"target_{color}"
        )

        # Don't exceed demand per size (prevent overproduction)
        for s in ALL_SIZES:
            prob += (
                pulp.lpSum(markers[m].ratio[s] * plies[(m, c_idx)] for m in range(n_markers))
                <= orders[color][s],
                f"max_{color}_{s}"
            )

    # ... solve and return remaining demand ...


def solve_stage2(remaining, markers, penalty=3.0):
    """
    Stage 2: 2-bundle markers fill exact remainder.
    Small problem, solves quickly.
    """
    # Exact fulfillment constraints
    for c_idx, color in enumerate(colors):
        for s in ALL_SIZES:
            if remaining[color][s] > 0:
                prob += (
                    pulp.lpSum(markers[m].ratio[s] * plies[(m, c_idx)] for m in range(n_markers))
                    == remaining[color][s],
                    f"exact_{color}_{s}"
                )
    # ...
```

## Output Format

### Console Output

```
COMBINED CUTPLAN
================
Color demands:
  8320: 1496 garments
  8535: 678 garments
  8820: 1084 garments
  9990: 678 garments

Summary:
  Unique markers: 9
  Total fabric: 5872.58 yards

Ratio               46  48  50  52  54  56  58    Bndl  Eff%   8320  8535  8820  9990  Total  Length(Y)
-------------------------------------------------------------------------------------------------
1-3-1-0-0-1-0       1   3   1   0   0   1   0     6    80.8    55    17    29    20    121    1021.96
0-0-5-0-0-0-0       0   0   5   0   0   0   0     5    80.5    46     0    50    33    129    1052.43
...
```

### CSV Output

```csv
Ratio,46,48,50,52,54,56,58,Bundles,Efficiency,8320,8535,8820,9990,Total_Plies,Length_Yards
1-3-1-0-0-1-0,1,3,1,0,0,1,0,6,0.8084,55,17,29,20,121,1021.96
0-0-5-0-0-0-0,0,0,5,0,0,0,0,5,0.8050,46,0,50,33,129,1052.43

# Penalty: 3.0
# Unique markers: 9
# Total fabric: 5872.58 yards
```

## Penalty Tuning

The penalty parameter controls the trade-off between fabric usage and cutting complexity:

| Penalty | Effect |
|---------|--------|
| 0 | Minimize fabric only, may use many markers |
| 3.0 | Light encouragement to share markers |
| 5.0 | Balanced (default for single-color) |
| 7.0 | Strong preference for fewer markers |
| 10.0+ | Aggressive marker reduction |

### Example Comparison (4-color order)

| Solver | Penalty | Unique Markers | Total Fabric |
|--------|---------|----------------|--------------|
| Default (independent) | 3.0 | 14 | 5876.91 Y |
| Default (independent) | 7.0 | 16 | 5891.97 Y |
| **Multicolor (joint)** | **3.0** | **9** | **5872.58 Y** |
| **Multicolor (joint)** | **7.0** | **8** | **5876.19 Y** |

The multicolor solver reduces unique markers by 5-8 while maintaining similar or better fabric usage.

## Performance

| Problem | Variables | Constraints | Time |
|---------|-----------|-------------|------|
| Single-color (75 markers) | ~150 | ~14 | ~5-10s |
| Multicolor 4-color (75 markers) | ~375 | ~56 | ~90-120s |
| Two-stage (40+21 markers) | ~250 | ~56 | ~3-30s |

### Scaling Considerations

- **Marker pool size**: Biggest impact on solve time
- **Number of colors**: Linear increase in variables
- **Min plies constraint**: Adds binary variables, can slow solving
- **Two-stage**: Much faster due to smaller per-stage problems

## Reference Implementations

- **Single-color**: `scripts/marker_selection_optimizer_v2.py`
- **Multicolor**: `scripts/multicolor_solver.py`
- **Two-stage**: `scripts/multicolor_solver_twostage.py`

## Usage Example

```python
from pathlib import Path

# Load marker pool from GPU nesting results
markers = load_markers_from_csv(Path("experiment_results/brute_force_improved/all_ratios.csv"))
pool = get_filtered_pool(markers, top_n_per_bundle=25)

# Define orders
orders = {
    "8320": {"46": 74, "48": 244, "50": 347, "52": 342, "54": 265, "56": 162, "58": 62},
    "8535": {"46": 23, "48": 112, "50": 172, "52": 166, "54": 114, "56": 74, "58": 17},
    "8820": {"46": 29, "48": 172, "50": 248, "52": 254, "54": 191, "56": 145, "58": 45},
    "9990": {"46": 20, "48": 104, "50": 167, "52": 162, "54": 114, "56": 78, "58": 33},
}

# Solve
assignments, total_length, unique_markers, success = solve_multicolor(orders, pool, penalty=3.0)

if success:
    print(f"Unique markers: {unique_markers}")
    print(f"Total fabric: {total_length:.2f} yards")
```

## Verification

Always verify demand fulfillment after solving:

```python
def verify_demand(assignments, orders):
    """Check that produced quantities match demand exactly."""
    for color in orders:
        produced = {s: 0 for s in ALL_SIZES}
        for marker, plies in assignments[color]:
            for s in ALL_SIZES:
                produced[s] += marker.ratio[s] * plies

        for s in ALL_SIZES:
            if produced[s] != orders[color][s]:
                print(f"MISMATCH {color} {s}: need {orders[color][s]}, got {produced[s]}")
                return False
    return True
```
