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
  yield_per_garment = total_fabric_yards / total_garments
    where total_garments = Σ(bundles_per_marker × plies_per_marker)

  Type 1 (unusable):   remnant < yield_per_garment.  Too short to cut even
                        one garment.  Unavoidable scraps.
  Type 2 (end-bit):    yield_per_garment <= remnant < longest marker.  COULD have
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
    def bundles(self) -> int:
        """Number of garment bundles per ply (sum of ratio components)."""
        if not self.ratio_str:
            return 1
        try:
            return max(1, sum(int(x) for x in self.ratio_str.split('-') if x.strip()))
        except (ValueError, TypeError):
            return 1

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
    fabric_used_yards: float = 0.0  # plies × marker_length


@dataclass
class CutDocket:
    """Per-cut report for the cutting room."""
    cut_number: int
    marker_label: str
    ratio_str: str
    marker_length_yards: float
    plies: int                         # Plies required for this cut
    plies_planned: int = 0             # Plies actually planned (may be < plies if shortfall)
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

    # Use _S suffix for shortfall-fill rolls, PSEUDO-N for pure pseudo mode
    use_s_suffix = existing_rolls is not None

    pseudo_rolls: List[RollSpec] = []
    accumulated = 0.0
    idx = 1
    while accumulated < shortfall:
        length = random.uniform(avg - delta, avg + delta)
        length = max(length, 1.0)  # Floor at 1 yard
        roll_id = f"R{idx:03d}_S" if use_s_suffix else f"PSEUDO-{idx}"
        pseudo_rolls.append(RollSpec(
            roll_id=roll_id,
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
# Pre-flight validation
# ---------------------------------------------------------------------------


@dataclass
class PreflightWarning:
    """A single pre-flight warning."""
    level: str       # "warning" or "error"
    message: str


@dataclass
class PreflightResult:
    """Result of pre-flight validation before simulation."""
    valid: bool = True
    warnings: List[PreflightWarning] = field(default_factory=list)
    total_fabric_needed: float = 0.0
    total_roll_fabric: float = 0.0
    longest_marker: float = 0.0
    longest_roll: float = 0.0


def validate_rollplan_inputs(
    markers: List[MarkerSpec],
    rolls: Optional[List[RollSpec]] = None,
    pseudo_config: Optional[PseudoRollConfig] = None,
) -> PreflightResult:
    """
    Pre-flight check: are rolls sufficient and compatible with markers?

    Pure function, no DB deps. Returns warnings (not errors that block simulation).
    """
    result = PreflightResult()

    if not markers:
        result.valid = False
        result.warnings.append(PreflightWarning("error", "No markers found for simulation"))
        return result

    result.total_fabric_needed = sum(m.total_fabric_yards for m in markers)
    result.longest_marker = max(m.length_yards for m in markers)

    if rolls:
        result.total_roll_fabric = sum(r.length_yards for r in rolls)
        result.longest_roll = max(r.length_yards for r in rolls) if rolls else 0

        shortfall = result.total_fabric_needed - result.total_roll_fabric
        if shortfall > 0:
            pct = shortfall / result.total_fabric_needed * 100
            result.warnings.append(PreflightWarning(
                "warning",
                f"Roll inventory short by {shortfall:.1f} yd ({pct:.0f}%). "
                f"Pseudo-rolls will fill the gap."
            ))

        if result.longest_marker > result.longest_roll:
            result.warnings.append(PreflightWarning(
                "warning",
                f"Longest marker ({result.longest_marker:.1f} yd) exceeds longest roll "
                f"({result.longest_roll:.1f} yd). Some rolls may be too short."
            ))

        # Check how many rolls can't fit even 1 ply of the shortest marker
        shortest_marker = min(m.length_yards for m in markers)
        too_short = sum(1 for r in rolls if r.length_yards < shortest_marker)
        if too_short > 0:
            result.warnings.append(PreflightWarning(
                "warning",
                f"{too_short} roll(s) shorter than the shortest marker "
                f"({shortest_marker:.1f} yd) — will become scrap."
            ))
    else:
        # Pseudo-only mode
        if pseudo_config:
            min_pseudo = pseudo_config.avg_length_yards - pseudo_config.delta_yards
            if min_pseudo < result.longest_marker:
                result.warnings.append(PreflightWarning(
                    "warning",
                    f"Some pseudo-rolls may be shorter than the longest marker "
                    f"({result.longest_marker:.1f} yd). Min pseudo roll: {min_pseudo:.1f} yd."
                ))

    return result


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


def _compute_future_min_marker_lengths(
    cuts: List[Tuple[MarkerSpec, int]],
) -> List[float]:
    """
    For each cut index i, compute the minimum marker length among cuts[i+1:].

    Built right-to-left in O(n).  Used as dynamic reuse threshold — an end-bit
    is worth saving only if it's at least as long as some future marker.
    Last element is inf (no future cuts after the last one).
    """
    n = len(cuts)
    result = [float("inf")] * n
    if n <= 1:
        return result
    for i in range(n - 2, -1, -1):
        result[i] = min(cuts[i + 1][0].length_yards, result[i + 1])
    return result


def _classify_remnant(
    length: float,
    piece_consumption: float,
    max_marker_length: float,
) -> int:
    """
    Classify a fabric remnant into waste type.

    Args:
      piece_consumption: Yield per garment — fabric to cut 1 garment.
                         Remnants shorter than this are unusable.
      max_marker_length: Longest marker in the cutplan.
                         Remnants >= this can be returned to warehouse.

    Returns:
      1 = unusable (< yield per garment) — unavoidable scrap
      2 = end-bit  (>= yield per garment, < max_marker) — optimizable waste
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
    Allocate rolls to cuts using remainder-aware pool-based selection.

    Returns (waste_breakdown, end_bits, reused_count, rolls_consumed, dockets).

    Args:
      piece_consumption: Length to cut 1 piece/garment.  Remnants shorter
                         than this are unusable (Type 1).
      max_marker_length: Longest marker.  Remnants >= this are returnable (Type 3).

    Algorithm:
      Pool of available rolls.  For each cut (largest markers first):
        1. Try saved end-bits, sorted by (length % marker_length) ascending
           — prefer end-bits that divide evenly into the marker.
        2. Score pool rolls by remainder = roll_len % marker_len.
           Sort ascending (smallest waste first), GA order as tiebreaker.
           Rolls too short for this marker stay in pool for later cuts.
        3. Dynamic reuse threshold: save end-bits only if they're at least
           as long as the shortest remaining future marker.
      After all cuts: remaining saved end-bits → classify as waste.
    """
    future_min_ml = _compute_future_min_marker_lengths(cuts)

    saved_end_bits: List[EndBit] = []
    all_end_bits: List[EndBit] = []
    waste = WasteBreakdown()
    reused_count = 0
    rolls_consumed = 0
    dockets: List[CutDocket] = []
    cut_number = 0

    # Pool of available roll indices — rolls stay here until consumed
    pool = set(range(len(rolls)))

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

    # Track last roll of each cut for floor continuity:
    # the roll still on the spreading machine should be used first in the next cut.
    continuation_roll_id: Optional[str] = None

    for cut_idx, (marker, plies_needed) in enumerate(cuts):
        cut_number += 1
        assignments: List[RollAssignment] = []
        plies_remaining = plies_needed
        ml = marker.length_yards

        # Dynamic reuse threshold: only save end-bits >= shortest future marker
        effective_threshold = max(min_reuse_length, future_min_ml[cut_idx])

        # --- Phase 1: Try end-bits ---
        # Continuation roll first (still on the machine), then by remainder
        saved_end_bits.sort(key=lambda e: (
            0 if e.source_roll_id == continuation_roll_id else 1,
            e.length_yards % ml,
        ))
        reuse_indices = []
        for i, eb in enumerate(saved_end_bits):
            if plies_remaining <= 0:
                break
            if eb.length_yards >= ml:
                plies_from_eb = int(eb.length_yards // ml)
                plies_from_eb = min(plies_from_eb, plies_remaining)
                used_length = plies_from_eb * ml
                leftover = eb.length_yards - used_length

                # Tag with -bit suffix so cutting room knows it's a remnant
                bit_id = eb.source_roll_id if "-bit" in eb.source_roll_id else f"{eb.source_roll_id}-bit"

                assignments.append(RollAssignment(
                    roll_id=bit_id,
                    roll_length_yards=eb.length_yards,
                    plies_from_roll=plies_from_eb,
                    end_bit_yards=leftover,
                    is_pseudo=False,
                    fabric_used_yards=used_length,
                ))
                eb.reused = True
                reused_count += 1
                reuse_indices.append(i)
                plies_remaining -= plies_from_eb

                # Handle leftover from reused end-bit
                if leftover > 0:
                    if leftover >= effective_threshold:
                        saved_end_bits.append(EndBit(
                            source_roll_id=bit_id,
                            source_marker=marker.marker_label,
                            length_yards=leftover,
                        ))
                    else:
                        new_eb = _account_remnant(leftover, bit_id, marker.marker_label)
                        all_end_bits.append(new_eb)

        # Remove consumed end-bits (reverse order to preserve indices)
        for i in sorted(reuse_indices, reverse=True):
            saved_end_bits.pop(i)

        # --- Phase 2: Fresh roll selection (two-pass, opportunity-aware) ---
        #
        # Pass A: Bulk — consume rolls where we use ALL their plies.
        #         Scored by opportunity cost: a roll that fits a future
        #         marker much better than the current one is deferred.
        # Pass B: Last-roll — pick the single roll that minimizes actual
        #         leftover for the exact remaining plies.
        if plies_remaining > 0:
            # Collect future marker lengths for opportunity-cost scoring
            future_mls_set: set = set()
            for fi in range(cut_idx + 1, len(cuts)):
                future_mls_set.add(cuts[fi][0].length_yards)
            future_mls_list = list(future_mls_set)

            # Score each roll by opportunity cost:
            #   current_rem = waste if used for current marker
            #   best_future_rem = waste if saved for best future marker
            #   score = current_rem + (current_rem - best_future_rem)
            #         = 2 * current_rem - best_future_rem
            # Low score = good: either fits current marker well, or no
            # better alternative exists.  High score = defer: much better
            # fit for a future marker.
            candidates = []
            for ri in pool:
                rl = rolls[ri].length_yards
                if rl < ml:
                    continue
                current_rem = rl % ml
                best_future_rem = current_rem  # default: no improvement
                for fml in future_mls_list:
                    if rl >= fml:
                        fr = rl % fml
                        if fr < best_future_rem:
                            best_future_rem = fr
                score = 2.0 * current_rem - best_future_rem
                candidates.append((score, ri))
            candidates.sort()

            def _consume_roll(ri_: int, plies_take: int):
                nonlocal plies_remaining, rolls_consumed
                roll_ = rolls[ri_]
                used_ = plies_take * ml
                left_ = roll_.length_yards - used_
                assignments.append(RollAssignment(
                    roll_id=roll_.roll_id,
                    roll_length_yards=roll_.length_yards,
                    plies_from_roll=plies_take,
                    end_bit_yards=left_,
                    is_pseudo=roll_.is_pseudo,
                    fabric_used_yards=used_,
                ))
                plies_remaining -= plies_take
                pool.discard(ri_)
                rolls_consumed += 1
                if left_ > 0:
                    if left_ >= effective_threshold:
                        saved_end_bits.append(EndBit(
                            source_roll_id=roll_.roll_id,
                            source_marker=marker.marker_label,
                            length_yards=left_,
                        ))
                    else:
                        new_eb_ = _account_remnant(left_, roll_.roll_id, marker.marker_label)
                        all_end_bits.append(new_eb_)

            # Pass A: Bulk — consume rolls that give full capacity
            for _score, ri in candidates:
                if plies_remaining <= 0:
                    break
                max_p = int(rolls[ri].length_yards // ml)
                if max_p <= plies_remaining:
                    _consume_roll(ri, max_p)

            # Pass B: Last-roll — find best fit for exact remaining plies
            if plies_remaining > 0:
                best_ri = None
                best_leftover = float("inf")
                for _score, ri in candidates:
                    if ri not in pool:
                        continue
                    rl = rolls[ri].length_yards
                    actual_p = min(int(rl // ml), plies_remaining)
                    leftover = rl - actual_p * ml
                    if leftover < best_leftover:
                        best_leftover = leftover
                        best_ri = ri
                if best_ri is not None:
                    take = min(int(rolls[best_ri].length_yards // ml), plies_remaining)
                    _consume_roll(best_ri, take)

        # Track continuation roll — last roll is still on the machine
        continuation_roll_id = None
        if assignments and assignments[-1].end_bit_yards > 0:
            continuation_roll_id = assignments[-1].roll_id

        # Build docket
        actual_plies = plies_needed - plies_remaining
        total_fabric = sum(a.roll_length_yards for a in assignments) if assignments else 0
        total_eb = sum(a.end_bit_yards for a in assignments) if assignments else 0
        dockets.append(CutDocket(
            cut_number=cut_number,
            marker_label=marker.marker_label,
            ratio_str=marker.ratio_str,
            marker_length_yards=ml,
            plies=plies_needed,
            plies_planned=actual_plies,
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
    total_garments = sum(m.bundles * m.plies for m in markers)
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
# ILP-based optimal roll allocation (cutting-stock formulation)
# ---------------------------------------------------------------------------
#
# Solves the roll-to-marker assignment as a cutting-stock ILP:
#   Variables: x[i][j] = plies of marker j cut from roll i
#   Objective: maximize total fabric utilization (= minimize waste)
#   Constraints: roll capacity + demand limits
#
# A single roll CAN serve multiple markers through the end-bit reuse chain
# (cut longest marker first, reuse remainder for shorter markers).
# The ILP captures this naturally: Σ_j x[i][j] * ml[j] <= L[i].
#
# With N_rolls=50, N_markers=7 → 350 integer variables, solves in <1s.
# ---------------------------------------------------------------------------


def _optimal_allocation_ilp(
    markers: List[MarkerSpec],
    rolls: List[RollSpec],
    piece_consumption: float,
    max_marker_length: float,
    progress_callback: Optional[Callable[[int, str], None]] = None,
) -> Optional[Tuple[WasteBreakdown, List[EndBit], int, int, List[CutDocket]]]:
    """
    Globally optimal roll-to-marker allocation via cutting-stock ILP.

    For each roll, decides how many plies of each marker to cut,
    minimizing total waste while maximizing demand fulfillment.
    Handles multi-marker reuse automatically (a roll can serve multiple
    markers via end-bit chain — cut longest first, reuse remainder).

    Returns same tuple as _run_allocation, or None if solver unavailable/fails.
    """
    try:
        from scipy.optimize import milp as scipy_milp, LinearConstraint, Bounds
        from scipy.sparse import csc_matrix
        import numpy as np
    except ImportError:
        return None

    n_rolls = len(rolls)
    n_markers = len(markers)
    if n_rolls == 0 or n_markers == 0:
        return None

    # Size guard: skip ILP for very large problems (GA fallback)
    # 100K variables is the practical ceiling for sub-60s solve times.
    if n_rolls * n_markers > 100_000:
        return None

    ml = [m.length_yards for m in markers]
    demand = [m.plies for m in markers]
    L = [r.length_yards for r in rolls]
    max_L = max(L)

    # Sort marker indices by length descending (physical cutting order)
    marker_order = sorted(range(n_markers), key=lambda j: ml[j], reverse=True)

    if progress_callback:
        progress_callback(5, "Solving ILP roll assignment...")

    # --- Formulate ILP ---
    # Variables: x[i * n_markers + j] = plies of marker j from roll i
    n_vars = n_rolls * n_markers

    # Objective: maximize Σ x[i][j] * (ml[j] + ply_bonus)
    # ply_bonus ensures demand fulfillment is always preferred over waste reduction.
    # One extra ply always beats any waste saving from a single roll.
    ply_bonus = max_L + 1.0
    c = np.zeros(n_vars)
    for i in range(n_rolls):
        for j in range(n_markers):
            c[i * n_markers + j] = -(ml[j] + ply_bonus)

    # Bounds: 0 <= x[i][j] <= floor(L[i] / ml[j])
    ub = np.zeros(n_vars)
    for i in range(n_rolls):
        for j in range(n_markers):
            ub[i * n_markers + j] = float(int(L[i] / ml[j]))

    # Constraint 1: roll capacity — Σ_j x[i][j] * ml[j] <= L[i]
    rows1, cols1, data1 = [], [], []
    for i in range(n_rolls):
        for j in range(n_markers):
            if ub[i * n_markers + j] > 0:
                rows1.append(i)
                cols1.append(i * n_markers + j)
                data1.append(ml[j])
    A1 = csc_matrix((data1, (rows1, cols1)), shape=(n_rolls, n_vars))

    # Constraint 2: demand — Σ_i x[i][j] <= demand[j]
    rows2, cols2, data2 = [], [], []
    for j in range(n_markers):
        for i in range(n_rolls):
            if ub[i * n_markers + j] > 0:
                rows2.append(j)
                cols2.append(i * n_markers + j)
                data2.append(1.0)
    A2 = csc_matrix((data2, (rows2, cols2)), shape=(n_markers, n_vars))

    constraints = [
        LinearConstraint(A1, ub=np.array(L, dtype=float)),
        LinearConstraint(A2, ub=np.array(demand, dtype=float)),
    ]

    bounds = Bounds(lb=np.zeros(n_vars), ub=ub)
    integrality = np.ones(n_vars)

    result = scipy_milp(
        c, constraints=constraints,
        integrality=integrality, bounds=bounds,
        options={"time_limit": 60},
    )

    if not result.success:
        return None

    if progress_callback:
        progress_callback(50, "ILP solved — building dockets...")

    # Parse solution into 2D matrix
    x = [[int(round(result.x[i * n_markers + j])) for j in range(n_markers)]
         for i in range(n_rolls)]

    # --- Build dockets from ILP solution ---

    # Step 1: Build per-roll segment chains (process markers longest-first)
    # Each segment: (marker_idx, plies, avail_length, fabric_used, end_bit, is_reuse)
    roll_chains = []
    for i in range(n_rolls):
        remaining = L[i]
        chain = []
        for j in marker_order:
            p = x[i][j]
            if p <= 0:
                continue
            used = p * ml[j]
            eb = remaining - used
            chain.append((j, p, remaining, used, eb, len(chain) > 0))
            remaining = eb
        # Final remainder (waste)
        chain.append((-1, 0, remaining, 0, remaining, True))
        roll_chains.append(chain)

    # Step 2: Collect per-marker roll assignments
    marker_assigns: dict = {j: [] for j in range(n_markers)}
    for i, chain in enumerate(roll_chains):
        for (mj, p, avail, used, eb, is_reuse) in chain:
            if mj < 0:
                continue
            marker_assigns[mj].append({
                'roll_idx': i,
                'plies': p,
                'avail_length': avail,
                'fabric_used': used,
                'end_bit': eb,
                'is_reuse': is_reuse,
            })

    # Step 3: Build CutDockets
    waste = WasteBreakdown()
    all_end_bits: List[EndBit] = []
    reused_count = 0
    rolls_used: set = set()
    dockets: List[CutDocket] = []
    cut_number = 0

    for j in marker_order:
        m = markers[j]
        assigns = marker_assigns[j]
        total_plies_ilp = sum(a['plies'] for a in assigns)

        # Build target cut list from demand (same as _build_cut_list)
        targets = []
        rem_d = m.plies
        while rem_d > 0:
            targets.append(min(rem_d, MAX_PLY_HEIGHT))
            rem_d -= targets[-1]

        if not assigns:
            # Complete shortfall — build empty dockets
            for t in targets:
                cut_number += 1
                dockets.append(CutDocket(
                    cut_number=cut_number,
                    marker_label=m.marker_label,
                    ratio_str=m.ratio_str,
                    marker_length_yards=ml[j],
                    plies=t,
                    plies_planned=0,
                    assigned_rolls=[],
                    total_fabric_yards=0,
                    total_end_bit_yards=0,
                ))
            continue

        # Sort by avail_length descending (spread longest rolls first)
        assigns.sort(key=lambda a: a['avail_length'], reverse=True)

        # Bin-pack rolls into cuts (first-fit, max MAX_PLY_HEIGHT per cut)
        cut_bins = [0] * len(targets)
        cut_roll_lists: List[list] = [[] for _ in targets]

        for a in assigns:
            rolls_used.add(a['roll_idx'])
            if a['is_reuse']:
                reused_count += 1
            placed = False
            for ci in range(len(targets)):
                if cut_bins[ci] + a['plies'] <= MAX_PLY_HEIGHT:
                    cut_roll_lists[ci].append(a)
                    cut_bins[ci] += a['plies']
                    placed = True
                    break
            if not placed:
                # Overflow: add to last cut (shouldn't happen with valid ILP)
                cut_roll_lists[-1].append(a)
                cut_bins[-1] += a['plies']

        # Build CutDocket for each cut
        for ci, target in enumerate(targets):
            cut_number += 1
            ca = cut_roll_lists[ci]
            achieved = sum(a['plies'] for a in ca)

            roll_assignments = []
            for a in ca:
                ri = a['roll_idx']
                roll_id = rolls[ri].roll_id
                if a['is_reuse'] and "-bit" not in roll_id:
                    roll_id = f"{roll_id}-bit"

                roll_assignments.append(RollAssignment(
                    roll_id=roll_id,
                    roll_length_yards=round(a['avail_length'], 4),
                    plies_from_roll=a['plies'],
                    end_bit_yards=round(a['end_bit'], 4),
                    is_pseudo=rolls[ri].is_pseudo,
                    fabric_used_yards=round(a['fabric_used'], 4),
                ))

            total_fab = sum(ra.roll_length_yards for ra in roll_assignments)
            total_eb = sum(ra.end_bit_yards for ra in roll_assignments)

            dockets.append(CutDocket(
                cut_number=cut_number,
                marker_label=m.marker_label,
                ratio_str=m.ratio_str,
                marker_length_yards=ml[j],
                plies=target,
                plies_planned=achieved,
                assigned_rolls=roll_assignments,
                total_fabric_yards=round(total_fab, 4),
                total_end_bit_yards=round(total_eb, 4),
            ))

    # Step 4: Account for waste from final remainders
    for i, chain in enumerate(roll_chains):
        final = chain[-1]  # (-1, 0, remaining, 0, remaining, True)
        rem = final[2]
        if rem < 0.001:
            continue
        wtype = _classify_remnant(rem, piece_consumption, max_marker_length)
        if wtype == 1:
            waste.unusable_yards += rem
            waste.unusable_count += 1
        elif wtype == 2:
            waste.endbit_yards += rem
            waste.endbit_count += 1
        else:
            waste.returnable_yards += rem
            waste.returnable_count += 1
        all_end_bits.append(EndBit(
            source_roll_id=rolls[i].roll_id,
            source_marker="final",
            length_yards=round(rem, 4),
            waste_type=wtype,
        ))

    waste.unusable_yards = round(waste.unusable_yards, 4)
    waste.endbit_yards = round(waste.endbit_yards, 4)
    waste.returnable_yards = round(waste.returnable_yards, 4)

    if progress_callback:
        progress_callback(
            90,
            f"ILP allocation: waste={waste.real_waste_yards:.2f}yd, "
            f"scrap={waste.unusable_yards:.2f}yd"
        )

    return waste, all_end_bits, reused_count, len(rolls_used), dockets


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
    Roll-to-marker optimization.

    Tries ILP (cutting-stock formulation) first for globally optimal
    assignment. Falls back to GA heuristic if ILP solver is unavailable
    or fails.
    """
    n = len(rolls)
    total_fabric = sum(m.total_fabric_yards for m in markers)

    if n == 0 or not markers:
        return GAResult(
            cut_dockets=[], total_fabric_used=0,
            waste=WasteBreakdown(), reused_end_bits=[],
        )

    total_garments = sum(m.bundles * m.plies for m in markers)
    pc = total_fabric / total_garments if total_garments > 0 else 1.0
    max_ml = max(m.length_yards for m in markers)

    # --- Try ILP first (globally optimal) ---
    ilp_result = _optimal_allocation_ilp(
        markers, rolls, pc, max_ml, progress_callback
    )
    if ilp_result is not None:
        ilp_wb, ilp_end_bits, ilp_reused, ilp_consumed, ilp_dockets = ilp_result
        if progress_callback:
            progress_callback(
                100,
                f"ILP optimal: waste={ilp_wb.real_waste_yards:.2f}yd "
                f"({ilp_wb.real_waste_yards / (total_fabric + ilp_wb.real_waste_yards) * 100:.1f}%), "
                f"scrap={ilp_wb.unusable_yards:.2f}yd"
            )
        total_used = total_fabric + ilp_wb.real_waste_yards
        return GAResult(
            cut_dockets=ilp_dockets,
            total_fabric_used=round(total_used, 4),
            waste=ilp_wb,
            reused_end_bits=[eb for eb in ilp_end_bits if eb.reused],
            generations_run=0,
            best_fitness=round(-(ilp_wb.unusable_yards + ilp_wb.endbit_yards), 4),
        )

    # --- Fallback: GA heuristic ---
    if progress_callback:
        progress_callback(5, "ILP unavailable, falling back to GA...")

    cuts = _build_cut_list(markers)

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

    # Seed 3: weighted multi-marker remainder
    # Rolls that fit well across many markers (weighted by plies) sort first
    if cuts:
        _marker_lengths = list(set(m.length_yards for m, _ in cuts))
        _marker_plies: dict[float, int] = {}
        for m, p in cuts:
            _marker_plies[m.length_yards] = _marker_plies.get(m.length_yards, 0) + p
        _total_plies = sum(_marker_plies.values())

        def _weighted_remainder(ri: int) -> float:
            rl = rolls[ri].length_yards
            score = 0.0
            for _ml in _marker_lengths:
                w = _marker_plies[_ml] / _total_plies
                score += (rl % _ml) / _ml * w
            return score

        seed_weighted = sorted(range(n), key=_weighted_remainder)
        population.append(seed_weighted)

    # Seed 4: min remainder across any marker (exact-fit rolls first)
    if cuts:
        _all_mls = list(set(m.length_yards for m, _ in cuts))
        seed_min_rem = sorted(
            range(n),
            key=lambda i: min(rolls[i].length_yards % ml for ml in _all_mls)
        )
        population.append(seed_min_rem)

    # Seed 5: reverse remainder for longest marker (contrarian diversity)
    if cuts:
        _longest_ml = cuts[0][0].length_yards
        seed_rev_rem = sorted(
            range(n),
            key=lambda i: rolls[i].length_yards % _longest_ml,
            reverse=True
        )
        population.append(seed_rev_rem)

    # Fill rest with random permutations
    while len(population) < pop_size:
        perm = list(range(n))
        random.shuffle(perm)
        population.append(perm)

    # --- Fitness function ---
    def _fitness(wb: WasteBreakdown, dockets: List[CutDocket]) -> float:
        """
        Fitness = -(total_real_waste + shortfall_penalty).

        Optimizes total waste (unusable + end-bit), and heavily penalizes
        any ply shortfall so the GA strongly prefers full-order solutions.
        """
        shortfall_yd = sum(
            max(0, d.plies - (d.plies_planned or d.plies)) * d.marker_length_yards
            for d in dockets
        )
        return -(wb.unusable_yards + wb.endbit_yards + shortfall_yd * 100)

    # --- Evaluate initial population ---
    fitnesses: List[float] = []
    best_score = float("-inf")
    best_wb = WasteBreakdown()
    best_dockets: List[CutDocket] = []
    best_end_bits: List[EndBit] = []

    for chrom in population:
        wb, dockets, end_bits, reused, consumed = _evaluate_chromosome(
            chrom, rolls, cuts, min_reuse_length, pc, max_ml
        )
        score = _fitness(wb, dockets)
        fitnesses.append(score)
        if score > best_score:
            best_score = score
            best_wb = wb
            best_dockets = dockets
            best_end_bits = end_bits

    # --- Evolve ---
    gens_run = 0
    stagnation = 0
    prev_best = best_score

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
            score = _fitness(wb, dockets)
            new_pop.append(child)
            new_fit.append(score)

            if score > best_score:
                best_score = score
                best_wb = wb
                best_dockets = dockets
                best_end_bits = end_bits

        population = new_pop
        fitnesses = new_fit

        # Early stopping on stagnation
        if abs(best_score - prev_best) < 0.001:
            stagnation += 1
        else:
            stagnation = 0
        prev_best = best_score

        if stagnation >= 15:
            break

        if progress_callback and (gen + 1) % max(1, generations // 10) == 0:
            pct = int((gen + 1) / generations * 100)
            progress_callback(
                pct,
                f"Optimizing: iteration {gen + 1}/{generations}, "
                f"waste={best_wb.real_waste_yards:.2f}yd, "
                f"scrap={best_wb.unusable_yards:.2f}yd"
            )

    total_used = total_fabric + best_wb.real_waste_yards

    return GAResult(
        cut_dockets=best_dockets,
        total_fabric_used=round(total_used, 4),
        waste=best_wb,
        reused_end_bits=[eb for eb in best_end_bits if eb.reused],
        generations_run=gens_run,
        best_fitness=round(best_score, 4),
    )
