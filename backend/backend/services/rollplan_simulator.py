"""
Roll Plan Simulator — Monte Carlo cutplan evaluator + GA roll optimizer.

Two distinct modes:

1. **Monte Carlo** — Evaluate a cutplan's suitability by simulating realistic
   roll consumption (shuffle rolls, cut until exhausted, reuse end-bits for
   smaller markers, repeat N times, classify waste).  The cutplan that
   produces less *end-bit waste* (Type 2) is the better cutplan.

2. **GA Optimizer** — Deterministic roll-to-marker assignment using a
   speed-tuned genetic algorithm.  Produces cutting dockets for the
   cutting room.

Waste classification (thresholds auto-derived from cutplan):
  piece_consumption = total_fabric_yards / total_garments_cut

  Type 1 (unusable):   remnant < piece_consumption.  Unavoidable scraps.
  Type 2 (end-bit):    piece_consumption <= remnant < longest marker.  COULD have
                        been used to cut garments but wasn't.  Optimization target.
  Type 3 (returnable): remnant >= longest marker.  Can go back to warehouse.

  Real waste = Type 1 + Type 2.   Objective = minimize Type 2.

Pure Python module, zero DB dependencies.
"""
from __future__ import annotations

import io
import math
import random
import statistics
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class MarkerSpec:
    marker_label: str          # "M1", "M2"
    length_yards: float        # Marker length
    plies: int                 # Plies needed for this color
    ratio_str: str = ""        # e.g., "1-3-1-0-0-1-0"

    @property
    def total_fabric_yards(self) -> float:
        return self.length_yards * self.plies


@dataclass
class RollSpec:
    roll_id: str               # "R001" or "PSEUDO-1"
    length_yards: float        # Roll length
    is_pseudo: bool = False
    width_inches: Optional[float] = None
    shrinkage_x_pct: Optional[float] = None
    shrinkage_y_pct: Optional[float] = None
    shade_group: Optional[str] = None


@dataclass
class PseudoRollConfig:
    avg_length_yards: float = 100.0
    delta_yards: float = 20.0


@dataclass
class EndBit:
    source_roll_id: str
    source_marker: str         # Which marker produced this end-bit
    length_yards: float
    waste_type: int = 0        # 1=unusable, 2=end-bit, 3=returnable
    reused: bool = False       # Was it reused by a later marker?


@dataclass
class RollAssignment:
    """One roll's contribution to a cut."""
    roll_id: str
    roll_length_yards: float
    plies_from_roll: int
    end_bit_yards: float
    is_pseudo: bool = False


@dataclass
class CutDocket:
    """Per-cut report for the cutting room."""
    cut_number: int
    marker_label: str
    ratio_str: str
    marker_length_yards: float
    plies: int                         # Plies in this cut
    assigned_rolls: List[RollAssignment] = field(default_factory=list)
    total_fabric_yards: float = 0.0
    total_end_bit_yards: float = 0.0


@dataclass
class WasteBreakdown:
    """Classified waste from a single simulation run."""
    # Type 1: remnant < piece_consumption — unusable scraps
    unusable_yards: float = 0.0
    unusable_count: int = 0
    # Type 2: piece_consumption <= remnant < max_marker — end-bit waste (optimization target)
    endbit_yards: float = 0.0
    endbit_count: int = 0
    # Type 3: remnant >= max_marker — returnable to warehouse
    returnable_yards: float = 0.0
    returnable_count: int = 0

    @property
    def real_waste_yards(self) -> float:
        """Type 1 + Type 2 — actual material loss."""
        return self.unusable_yards + self.endbit_yards

    @property
    def total_remnant_yards(self) -> float:
        """All remnants (Types 1+2+3)."""
        return self.unusable_yards + self.endbit_yards + self.returnable_yards


@dataclass
class SimulationRun:
    run_id: int
    waste: WasteBreakdown
    end_bits: List[EndBit]
    reused_count: int
    rolls_consumed: int
    cut_dockets: List[CutDocket] = field(default_factory=list)

    @property
    def total_waste_yards(self) -> float:
        """Backward compat — real waste (Type 1 + Type 2)."""
        return self.waste.real_waste_yards


@dataclass
class WasteStats:
    """Aggregated statistics for a single waste category across MC runs."""
    avg: float = 0.0
    std: float = 0.0
    min: float = 0.0
    max: float = 0.0
    median: float = 0.0
    p95: float = 0.0


@dataclass
class MonteCarloResult:
    """
    Result of Monte Carlo cutplan evaluation.

    The key metric is `endbit_waste` (Type 2) — this is the waste that
    COULD have been used but wasn't.  The cutplan with lower avg endbit_waste
    is the better cutplan.
    """
    num_simulations: int
    total_fabric_required: float       # Sum of marker.length * marker.plies

    # Aggregated waste by category
    unusable_waste: WasteStats         # Type 1
    endbit_waste: WasteStats           # Type 2  ← optimization target
    returnable_waste: WasteStats       # Type 3
    real_waste: WasteStats             # Type 1 + Type 2 combined

    avg_reused_count: float
    runs: List[SimulationRun]

    # Best run = lowest endbit (Type 2) waste
    best_run: SimulationRun


@dataclass
class GAResult:
    """Result from GA roll-to-marker optimization."""
    cut_dockets: List[CutDocket]
    total_fabric_used: float
    waste: WasteBreakdown
    reused_end_bits: List[EndBit]
    generations_run: int = 0
    best_fitness: float = 0.0

    @property
    def total_waste(self) -> float:
        return self.waste.real_waste_yards

    @property
    def waste_percentage(self) -> float:
        if self.total_fabric_used > 0:
            return self.waste.real_waste_yards / self.total_fabric_used * 100
        return 0.0


# ---------------------------------------------------------------------------
# Pseudo-roll generation
# ---------------------------------------------------------------------------


def generate_pseudo_rolls(
    total_fabric_needed: float,
    config: PseudoRollConfig,
    existing_rolls: Optional[List[RollSpec]] = None,
    buffer_pct: float = 0.05,
) -> List[RollSpec]:
    """
    Generate pseudo-rolls to fill shortfall.

    If existing_rolls provided:
      - Calculate total existing length.
      - If shortfall, generate pseudo-rolls from median of existing lengths + 5%.
    If no existing rolls:
      - Generate from avg ± delta until sum >= total * (1 + buffer_pct).
    """
    target = total_fabric_needed * (1 + buffer_pct)

    if existing_rolls:
        existing_total = sum(r.length_yards for r in existing_rolls)
        if existing_total >= target:
            return []  # Enough real rolls
        shortfall = target - existing_total
        median_len = statistics.median(r.length_yards for r in existing_rolls)
        avg = median_len
        delta = config.delta_yards
    else:
        shortfall = target
        avg = config.avg_length_yards
        delta = config.delta_yards

    pseudo_rolls: List[RollSpec] = []
    accumulated = 0.0
    idx = 1
    while accumulated < shortfall:
        length = random.uniform(avg - delta, avg + delta)
        length = max(length, 1.0)  # Floor at 1 yard
        pseudo_rolls.append(RollSpec(
            roll_id=f"PSEUDO-{idx}",
            length_yards=round(length, 2),
            is_pseudo=True,
        ))
        accumulated += length
        idx += 1
    return pseudo_rolls


# ---------------------------------------------------------------------------
# Roll Excel parsing
# ---------------------------------------------------------------------------

_LENGTH_TO_YARDS = {
    "yd": 1.0, "yds": 1.0, "yard": 1.0, "yards": 1.0, "y": 1.0,
    "m": 1.09361, "meter": 1.09361, "meters": 1.09361, "metre": 1.09361,
    "ft": 1.0 / 3.0, "feet": 1.0 / 3.0, "foot": 1.0 / 3.0,
}

_WIDTH_TO_INCHES = {
    "in": 1.0, "inch": 1.0, "inches": 1.0,
    "cm": 1.0 / 2.54, "centimeter": 1.0 / 2.54,
    "m": 39.3701, "meter": 39.3701, "metres": 39.3701,
    "yd": 36.0, "yard": 36.0,
}


def parse_roll_excel(file_bytes: bytes) -> List[RollSpec]:
    """Parse roll inventory from Excel bytes.

    Required columns: Roll Number, Roll Length
    Optional columns: Unit (default yd), Roll Width, Width Unit,
                      Shrinkage X%, Shrinkage Y%, Shade Group
    """
    import openpyxl

    wb = openpyxl.load_workbook(io.BytesIO(file_bytes), read_only=True)
    ws = wb.active
    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        raise ValueError("Empty Excel file")

    # Normalize headers
    raw_headers = [str(h).strip().lower().replace(" ", "_") if h else "" for h in rows[0]]

    # Map common header variations
    header_map = {}
    for idx, h in enumerate(raw_headers):
        if h in ("roll_number", "roll_no", "roll_#", "roll#", "rollno", "rollnumber"):
            header_map["roll_number"] = idx
        elif h in ("roll_length", "length", "roll_len", "rolllength"):
            header_map["roll_length"] = idx
        elif h in ("unit", "length_unit", "uom"):
            header_map["unit"] = idx
        elif h in ("roll_width", "width"):
            header_map["roll_width"] = idx
        elif h in ("width_unit",):
            header_map["width_unit"] = idx
        elif h in ("shrinkage_x%", "shrinkage_x", "shrink_x"):
            header_map["shrinkage_x"] = idx
        elif h in ("shrinkage_y%", "shrinkage_y", "shrink_y"):
            header_map["shrinkage_y"] = idx
        elif h in ("shade_group", "shade", "group"):
            header_map["shade_group"] = idx

    if "roll_number" not in header_map:
        raise ValueError("Missing required column: 'Roll Number'")
    if "roll_length" not in header_map:
        raise ValueError("Missing required column: 'Roll Length'")

    rolls: List[RollSpec] = []
    for row_idx, row in enumerate(rows[1:], start=2):
        roll_num = row[header_map["roll_number"]]
        roll_len = row[header_map["roll_length"]]

        if roll_num is None or roll_len is None:
            continue

        roll_num = str(roll_num).strip()
        try:
            roll_len = float(roll_len)
        except (ValueError, TypeError):
            raise ValueError(f"Row {row_idx}: Invalid roll length '{roll_len}'")

        # Length unit
        unit_str = "yd"
        if "unit" in header_map and row[header_map["unit"]]:
            unit_str = str(row[header_map["unit"]]).strip().lower()
        factor = _LENGTH_TO_YARDS.get(unit_str)
        if factor is None:
            raise ValueError(f"Row {row_idx}: Unknown length unit '{unit_str}'")
        length_yards = roll_len * factor

        # Width
        width_inches = None
        if "roll_width" in header_map and row[header_map["roll_width"]]:
            try:
                raw_width = float(row[header_map["roll_width"]])
                width_unit = "in"
                if "width_unit" in header_map and row[header_map["width_unit"]]:
                    width_unit = str(row[header_map["width_unit"]]).strip().lower()
                w_factor = _WIDTH_TO_INCHES.get(width_unit, 1.0)
                width_inches = raw_width * w_factor
            except (ValueError, TypeError):
                pass

        # Shrinkage
        shrinkage_x = None
        shrinkage_y = None
        if "shrinkage_x" in header_map and row[header_map["shrinkage_x"]]:
            try:
                shrinkage_x = float(row[header_map["shrinkage_x"]])
            except (ValueError, TypeError):
                pass
        if "shrinkage_y" in header_map and row[header_map["shrinkage_y"]]:
            try:
                shrinkage_y = float(row[header_map["shrinkage_y"]])
            except (ValueError, TypeError):
                pass

        # Shade group
        shade = None
        if "shade_group" in header_map and row[header_map["shade_group"]]:
            shade = str(row[header_map["shade_group"]]).strip()

        rolls.append(RollSpec(
            roll_id=roll_num,
            length_yards=round(length_yards, 2),
            is_pseudo=False,
            width_inches=width_inches,
            shrinkage_x_pct=shrinkage_x,
            shrinkage_y_pct=shrinkage_y,
            shade_group=shade,
        ))

    wb.close()
    return rolls


# ---------------------------------------------------------------------------
# Core simulation logic (shared by MC and GA)
# ---------------------------------------------------------------------------

MAX_PLY_HEIGHT = 100  # Max plies per physical cut/spread


def _build_cut_list(markers: List[MarkerSpec]) -> List[Tuple[MarkerSpec, int]]:
    """
    Expand markers into (marker, plies_in_cut) tuples.
    A marker with 150 plies at max_ply=100 → 2 cuts: (marker, 100), (marker, 50).
    Sorted by marker length descending (largest first).
    """
    cuts = []
    for m in sorted(markers, key=lambda x: x.length_yards, reverse=True):
        remaining = m.plies
        while remaining > 0:
            batch = min(remaining, MAX_PLY_HEIGHT)
            cuts.append((m, batch))
            remaining -= batch
    return cuts


def _classify_remnant(
    length: float,
    piece_consumption: float,
    max_marker_length: float,
) -> int:
    """
    Classify a fabric remnant into waste type.

    Args:
      piece_consumption: Fabric length to cut 1 piece (single garment).
                         Remnants shorter than this are unusable.
      max_marker_length: Longest marker in the cutplan.
                         Remnants >= this can be returned to warehouse.

    Returns:
      1 = unusable (< piece_consumption) — unavoidable scrap
      2 = end-bit  (>= piece_consumption, < max_marker) — optimizable waste
      3 = returnable (>= max_marker) — can go back to warehouse
    """
    if length < piece_consumption:
        return 1
    elif length < max_marker_length:
        return 2
    else:
        return 3


def _run_allocation(
    cuts: List[Tuple[MarkerSpec, int]],
    rolls: List[RollSpec],
    min_reuse_length: float,
    piece_consumption: float,
    max_marker_length: float,
) -> Tuple[WasteBreakdown, List[EndBit], int, int, List[CutDocket]]:
    """
    Allocate rolls to cuts.

    Returns (waste_breakdown, end_bits, reused_count, rolls_consumed, dockets).

    Args:
      piece_consumption: Length to cut 1 piece/garment.  Remnants shorter
                         than this are unusable (Type 1).
      max_marker_length: Longest marker.  Remnants >= this are returnable (Type 3).

    Algorithm:
      1. For each cut (marker, plies), largest markers first:
         a. Try saved end-bits (best-fit: smallest end-bit >= marker_length)
         b. Consume fresh rolls: each yields floor(remaining / marker_length) plies
         c. Leftover >= min_reuse → save for later markers; else → classify as waste
      2. After all cuts: remaining saved end-bits → classify as waste
    """
    saved_end_bits: List[EndBit] = []
    all_end_bits: List[EndBit] = []
    waste = WasteBreakdown()
    reused_count = 0
    rolls_consumed = 0
    roll_idx = 0
    dockets: List[CutDocket] = []
    cut_number = 0

    def _account_remnant(length: float, roll_id: str, marker_label: str) -> EndBit:
        """Classify a remnant and update waste breakdown."""
        wtype = _classify_remnant(length, piece_consumption, max_marker_length)
        eb = EndBit(
            source_roll_id=roll_id,
            source_marker=marker_label,
            length_yards=length,
            waste_type=wtype,
        )
        if wtype == 1:
            waste.unusable_yards += length
            waste.unusable_count += 1
        elif wtype == 2:
            waste.endbit_yards += length
            waste.endbit_count += 1
        else:
            waste.returnable_yards += length
            waste.returnable_count += 1
        return eb

    for marker, plies_needed in cuts:
        cut_number += 1
        assignments: List[RollAssignment] = []
        plies_remaining = plies_needed

        # --- Try end-bits first (best-fit: smallest qualifying) ---
        saved_end_bits.sort(key=lambda e: e.length_yards)
        reuse_indices = []
        for i, eb in enumerate(saved_end_bits):
            if plies_remaining <= 0:
                break
            if eb.length_yards >= marker.length_yards:
                plies_from_eb = int(eb.length_yards // marker.length_yards)
                plies_from_eb = min(plies_from_eb, plies_remaining)
                used_length = plies_from_eb * marker.length_yards
                leftover = eb.length_yards - used_length
                assignments.append(RollAssignment(
                    roll_id=eb.source_roll_id,
                    roll_length_yards=eb.length_yards,
                    plies_from_roll=plies_from_eb,
                    end_bit_yards=leftover,
                    is_pseudo=False,
                ))
                eb.reused = True
                reused_count += 1
                reuse_indices.append(i)
                plies_remaining -= plies_from_eb

                # Handle leftover from reused end-bit
                if leftover > 0:
                    if leftover >= min_reuse_length:
                        saved_end_bits.append(EndBit(
                            source_roll_id=eb.source_roll_id,
                            source_marker=marker.marker_label,
                            length_yards=leftover,
                        ))
                    else:
                        new_eb = _account_remnant(leftover, eb.source_roll_id, marker.marker_label)
                        all_end_bits.append(new_eb)

        # Remove consumed end-bits (reverse order to preserve indices)
        for i in sorted(reuse_indices, reverse=True):
            saved_end_bits.pop(i)

        # --- Consume fresh rolls ---
        while plies_remaining > 0 and roll_idx < len(rolls):
            roll = rolls[roll_idx]
            roll_idx += 1
            rolls_consumed += 1

            plies_from_roll = int(roll.length_yards // marker.length_yards)
            plies_from_roll = min(plies_from_roll, plies_remaining)

            if plies_from_roll == 0:
                # Roll too short for even 1 ply — entire roll is waste
                new_eb = _account_remnant(roll.length_yards, roll.roll_id, marker.marker_label)
                all_end_bits.append(new_eb)
                continue

            used_length = plies_from_roll * marker.length_yards
            leftover = roll.length_yards - used_length
            assignments.append(RollAssignment(
                roll_id=roll.roll_id,
                roll_length_yards=roll.length_yards,
                plies_from_roll=plies_from_roll,
                end_bit_yards=leftover,
                is_pseudo=roll.is_pseudo,
            ))
            plies_remaining -= plies_from_roll

            if leftover > 0:
                if leftover >= min_reuse_length:
                    saved_end_bits.append(EndBit(
                        source_roll_id=roll.roll_id,
                        source_marker=marker.marker_label,
                        length_yards=leftover,
                    ))
                else:
                    new_eb = _account_remnant(leftover, roll.roll_id, marker.marker_label)
                    all_end_bits.append(new_eb)

        # Build docket
        total_fabric = sum(a.roll_length_yards for a in assignments) if assignments else 0
        total_eb = sum(a.end_bit_yards for a in assignments) if assignments else 0
        dockets.append(CutDocket(
            cut_number=cut_number,
            marker_label=marker.marker_label,
            ratio_str=marker.ratio_str,
            marker_length_yards=marker.length_yards,
            plies=plies_needed,
            assigned_rolls=assignments,
            total_fabric_yards=total_fabric,
            total_end_bit_yards=total_eb,
        ))

    # Remaining saved end-bits → classify as waste
    for eb in saved_end_bits:
        new_eb = _account_remnant(eb.length_yards, eb.source_roll_id, eb.source_marker)
        all_end_bits.append(new_eb)

    # Round everything
    waste.unusable_yards = round(waste.unusable_yards, 4)
    waste.endbit_yards = round(waste.endbit_yards, 4)
    waste.returnable_yards = round(waste.returnable_yards, 4)

    return waste, all_end_bits, reused_count, rolls_consumed, dockets


# ---------------------------------------------------------------------------
# Monte Carlo simulation — cutplan evaluation tool
# ---------------------------------------------------------------------------


def run_single_simulation(
    markers: List[MarkerSpec],
    rolls: List[RollSpec],
    min_reuse_length: float,
    piece_consumption: float,
    run_id: int = 0,
) -> SimulationRun:
    """Run a single MC simulation with shuffled roll order.

    Args:
      piece_consumption: Fabric length to cut 1 piece/garment.
                         Remnants < this are unusable (Type 1).
    """
    if not markers:
        return SimulationRun(
            run_id=run_id, waste=WasteBreakdown(), end_bits=[],
            reused_count=0, rolls_consumed=0,
        )

    max_ml = max(m.length_yards for m in markers)

    shuffled = list(rolls)
    random.shuffle(shuffled)
    cuts = _build_cut_list(markers)
    wb, end_bits, reused, consumed, dockets = _run_allocation(
        cuts, shuffled, min_reuse_length, piece_consumption, max_ml
    )
    return SimulationRun(
        run_id=run_id,
        waste=wb,
        end_bits=end_bits,
        reused_count=reused,
        rolls_consumed=consumed,
        cut_dockets=dockets,
    )


def _build_waste_stats(values: List[float]) -> WasteStats:
    """Compute aggregate statistics from a list of per-run values."""
    if not values:
        return WasteStats()
    sv = sorted(values)
    p95_idx = max(0, int(math.ceil(0.95 * len(sv))) - 1)
    return WasteStats(
        avg=round(statistics.mean(values), 4),
        std=round(statistics.stdev(values) if len(values) > 1 else 0, 4),
        min=round(min(values), 4),
        max=round(max(values), 4),
        median=round(statistics.median(values), 4),
        p95=round(sv[p95_idx], 4),
    )


def simulate_roll_usage(
    markers: List[MarkerSpec],
    rolls: Optional[List[RollSpec]] = None,
    pseudo_config: Optional[PseudoRollConfig] = None,
    num_simulations: int = 100,
    min_reuse_length: float = 0.5,
    progress_callback: Optional[Callable[[int, str], None]] = None,
    cancel_check: Optional[Callable[[], bool]] = None,
) -> MonteCarloResult:
    """
    Monte Carlo cutplan evaluation.

    Simulates N random roll consumption scenarios and classifies
    the resulting waste.  Use to compare cutplans: the one with
    lower avg endbit_waste (Type 2) is better.

    piece_consumption is auto-derived: total_fabric / total_garments.

    For pseudo-rolls: regenerate each run (captures length randomness).
    For real rolls: shuffle order each run.
    """
    total_fabric = sum(m.total_fabric_yards for m in markers)
    total_garments = sum(m.plies for m in markers)
    piece_consumption = total_fabric / total_garments if total_garments > 0 else 1.0

    runs: List[SimulationRun] = []

    for i in range(num_simulations):
        if cancel_check and cancel_check():
            break

        if rolls:
            run_rolls = list(rolls)
            pseudo = generate_pseudo_rolls(total_fabric, pseudo_config or PseudoRollConfig(), rolls)
            run_rolls.extend(pseudo)
        else:
            run_rolls = generate_pseudo_rolls(total_fabric, pseudo_config or PseudoRollConfig())

        sim = run_single_simulation(markers, run_rolls, min_reuse_length, piece_consumption, run_id=i)
        runs.append(sim)

        if progress_callback and (i + 1) % max(1, num_simulations // 20) == 0:
            pct = int((i + 1) / num_simulations * 100)
            progress_callback(pct, f"Simulation: {i + 1}/{num_simulations} runs")

    # Aggregate per-category stats
    unusable_vals = [r.waste.unusable_yards for r in runs]
    endbit_vals = [r.waste.endbit_yards for r in runs]
    returnable_vals = [r.waste.returnable_yards for r in runs]
    real_vals = [r.waste.real_waste_yards for r in runs]

    # Best run = lowest Type 2 (end-bit) waste — that's the optimization target
    best_run = min(runs, key=lambda r: r.waste.endbit_yards)

    return MonteCarloResult(
        num_simulations=len(runs),
        total_fabric_required=round(total_fabric, 4),
        unusable_waste=_build_waste_stats(unusable_vals),
        endbit_waste=_build_waste_stats(endbit_vals),
        returnable_waste=_build_waste_stats(returnable_vals),
        real_waste=_build_waste_stats(real_vals),
        avg_reused_count=round(statistics.mean(r.reused_count for r in runs), 2) if runs else 0,
        runs=runs,
        best_run=best_run,
    )


# ---------------------------------------------------------------------------
# GA-based roll optimizer (speed-tuned)
# ---------------------------------------------------------------------------
#
# The GA optimizes the ORDER in which rolls are consumed. The allocation
# algorithm (_run_allocation) is deterministic given a roll ordering, so
# different permutations of the same roll set produce different waste.
#
# Chromosome = permutation of roll indices (list[int]).
# Fitness = -endbit_waste (maximize, i.e. minimize Type 2 waste).
#
# Speed knobs (defaults tuned for sub-second on 500 rolls):
#   pop_size=30, generations=50, tournament_k=3, crossover=OX, mutation=swap
# ---------------------------------------------------------------------------


def _evaluate_chromosome(
    chromosome: List[int],
    rolls: List[RollSpec],
    cuts: List[Tuple[MarkerSpec, int]],
    min_reuse_length: float,
    piece_consumption: float,
    max_marker_length: float,
) -> Tuple[WasteBreakdown, List[CutDocket], List[EndBit], int, int]:
    """Evaluate a roll ordering. Returns (waste, dockets, end_bits, reused, consumed)."""
    ordered_rolls = [rolls[i] for i in chromosome]
    wb, end_bits, reused, consumed, dockets = _run_allocation(
        cuts, ordered_rolls, min_reuse_length, piece_consumption, max_marker_length
    )
    return wb, dockets, end_bits, reused, consumed


def _order_crossover(p1: List[int], p2: List[int]) -> List[int]:
    """Order crossover (OX): preserves relative order of elements."""
    n = len(p1)
    start, end = sorted(random.sample(range(n), 2))
    child = [-1] * n
    child[start:end + 1] = p1[start:end + 1]
    p2_remaining = [g for g in p2 if g not in child[start:end + 1]]
    pos = 0
    for i in range(n):
        if child[i] == -1:
            child[i] = p2_remaining[pos]
            pos += 1
    return child


def _swap_mutation(chromosome: List[int]) -> List[int]:
    """Swap two random positions."""
    c = list(chromosome)
    i, j = random.sample(range(len(c)), 2)
    c[i], c[j] = c[j], c[i]
    return c


def _segment_reversal_mutation(chromosome: List[int]) -> List[int]:
    """Reverse a random segment (2-opt style)."""
    c = list(chromosome)
    i, j = sorted(random.sample(range(len(c)), 2))
    c[i:j + 1] = reversed(c[i:j + 1])
    return c


def _tournament_select(
    pop: List[List[int]], fitnesses: List[float], k: int = 3
) -> List[int]:
    """Tournament selection: pick best of k random individuals."""
    indices = random.sample(range(len(pop)), min(k, len(pop)))
    best_idx = max(indices, key=lambda i: fitnesses[i])
    return list(pop[best_idx])


def optimize_rolls_ga(
    markers: List[MarkerSpec],
    rolls: List[RollSpec],
    min_reuse_length: float = 0.5,
    pop_size: int = 30,
    generations: int = 50,
    tournament_k: int = 3,
    crossover_rate: float = 0.8,
    mutation_rate: float = 0.3,
    elitism: int = 2,
    progress_callback: Optional[Callable[[int, str], None]] = None,
    cancel_check: Optional[Callable[[], bool]] = None,
) -> GAResult:
    """
    Speed-tuned GA for roll-to-marker optimization.

    Optimizes roll consumption ORDER to minimize Type 2 (end-bit) waste.
    Runs in <1s for 500 rolls x 20 markers with default parameters.
    """
    cuts = _build_cut_list(markers)
    n = len(rolls)
    total_fabric = sum(m.total_fabric_yards for m in markers)

    if n == 0 or not markers:
        return GAResult(
            cut_dockets=[], total_fabric_used=0,
            waste=WasteBreakdown(), reused_end_bits=[],
        )

    total_garments = sum(m.plies for m in markers)
    pc = total_fabric / total_garments if total_garments > 0 else 1.0
    max_ml = max(m.length_yards for m in markers)

    # --- Seed population ---
    population: List[List[int]] = []

    # Seed 1: sort by roll length descending
    seed_desc = sorted(range(n), key=lambda i: rolls[i].length_yards, reverse=True)
    population.append(seed_desc)

    # Seed 2: sort by least remainder vs. longest marker
    if cuts:
        longest_marker_len = cuts[0][0].length_yards
        seed_remainder = sorted(
            range(n),
            key=lambda i: rolls[i].length_yards % longest_marker_len
        )
        population.append(seed_remainder)

    # Fill rest with random permutations
    while len(population) < pop_size:
        perm = list(range(n))
        random.shuffle(perm)
        population.append(perm)

    # --- Evaluate initial population ---
    fitnesses: List[float] = []
    best_endbit = float("inf")
    best_wb = WasteBreakdown()
    best_dockets: List[CutDocket] = []
    best_end_bits: List[EndBit] = []

    for chrom in population:
        wb, dockets, end_bits, reused, consumed = _evaluate_chromosome(
            chrom, rolls, cuts, min_reuse_length, pc, max_ml
        )
        fitnesses.append(-wb.endbit_yards)  # Minimize Type 2
        if wb.endbit_yards < best_endbit:
            best_endbit = wb.endbit_yards
            best_wb = wb
            best_dockets = dockets
            best_end_bits = end_bits

    # --- Evolve ---
    gens_run = 0
    stagnation = 0
    prev_best = best_endbit

    for gen in range(generations):
        if cancel_check and cancel_check():
            break

        gens_run = gen + 1

        # Sort population by fitness (descending = best first)
        paired = list(zip(population, fitnesses))
        paired.sort(key=lambda x: x[1], reverse=True)
        population = [p[0] for p in paired]
        fitnesses = [p[1] for p in paired]

        new_pop = []
        new_fit = []

        # Elitism
        for i in range(min(elitism, len(population))):
            new_pop.append(list(population[i]))
            new_fit.append(fitnesses[i])

        # Fill rest
        while len(new_pop) < pop_size:
            p1 = _tournament_select(population, fitnesses, tournament_k)
            p2 = _tournament_select(population, fitnesses, tournament_k)

            if random.random() < crossover_rate:
                child = _order_crossover(p1, p2)
            else:
                child = list(p1)

            if random.random() < mutation_rate:
                if random.random() < 0.5:
                    child = _swap_mutation(child)
                else:
                    child = _segment_reversal_mutation(child)

            wb, dockets, end_bits, reused, consumed = _evaluate_chromosome(
                child, rolls, cuts, min_reuse_length, pc, max_ml
            )
            new_pop.append(child)
            new_fit.append(-wb.endbit_yards)

            if wb.endbit_yards < best_endbit:
                best_endbit = wb.endbit_yards
                best_wb = wb
                best_dockets = dockets
                best_end_bits = end_bits

        population = new_pop
        fitnesses = new_fit

        # Early stopping on stagnation
        if abs(best_endbit - prev_best) < 0.001:
            stagnation += 1
        else:
            stagnation = 0
        prev_best = best_endbit

        if stagnation >= 15:
            break

        if progress_callback and (gen + 1) % max(1, generations // 10) == 0:
            pct = int((gen + 1) / generations * 100)
            progress_callback(
                pct,
                f"Optimizing: iteration {gen + 1}/{generations}, "
                f"end-bits={best_endbit:.2f}yd, "
                f"scrap={best_wb.unusable_yards:.2f}yd"
            )

    total_used = total_fabric + best_wb.real_waste_yards

    return GAResult(
        cut_dockets=best_dockets,
        total_fabric_used=round(total_used, 4),
        waste=best_wb,
        reused_end_bits=[eb for eb in best_end_bits if eb.reused],
        generations_run=gens_run,
        best_fitness=round(-best_endbit, 4),
    )
