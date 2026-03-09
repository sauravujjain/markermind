"""
Marker Decomposition Solver — reduce end-bit waste by splitting large markers.

Problem: Large markers (6-8 yd) create large end-bits (~3-8 yd) that nothing
can fill. This solver "peels off" 1-bundle or 2-bundle sub-markers from a
cutplan marker, creating smaller markers that fit into end-bits.

Key invariant: sub_a.ratio + sub_b.ratio = original.ratio, same plies.
Demand is always preserved.

Pure Python module, zero DB dependencies.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from itertools import combinations_with_replacement
from typing import Dict, List, Optional, Tuple

from .rollplan_simulator import MarkerSpec


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class Decomposition:
    """A valid decomposition of a marker into two sub-markers."""
    original_ratio_str: str
    sub_a_ratio_str: str       # The "big piece" (N-1 or N-2 bundles)
    sub_b_ratio_str: str       # The "small piece" (1 or 2 bundles)
    sub_a_efficiency: float    # From GPU pool
    sub_a_length_yards: float  # From GPU pool
    sub_b_efficiency: float    # From GPU pool
    sub_b_length_yards: float  # From GPU pool
    peeled_bundles: int        # 1 or 2


@dataclass
class DecompositionResult:
    """Result of run_decomposition(): a candidate cutplan to MC-validate."""
    original_markers: List[MarkerSpec]
    candidate_markers: List[MarkerSpec]
    decomposed_marker_label: str
    decomposition: Decomposition
    plies_shifted: int
    original_plies: int


# ---------------------------------------------------------------------------
# Core functions
# ---------------------------------------------------------------------------

def find_decompositions(
    marker_ratio_str: str,
    gpu_pool: Dict[str, Tuple[float, float]],
    sizes: List[str],
) -> List[Decomposition]:
    """
    Find all valid 1-bundle and 2-bundle peeloffs for a marker.

    Args:
        marker_ratio_str: e.g. "0-1-1-2-2-0"
        gpu_pool: {ratio_str: (efficiency, length_yards)}
        sizes: ordered size list, e.g. ["XS", "S", "M", "L", "XL", "XXL"]

    Returns:
        List of Decomposition sorted by sub_a efficiency descending.
    """
    ratio = [int(x) for x in marker_ratio_str.split("-")]
    if len(ratio) != len(sizes):
        return []

    results: List[Decomposition] = []

    # --- 1-bundle peeloffs ---
    for i in range(len(sizes)):
        if ratio[i] <= 0:
            continue

        # sub_a: decrement size i by 1
        sub_a_ratio = list(ratio)
        sub_a_ratio[i] -= 1
        sub_a_str = "-".join(str(x) for x in sub_a_ratio)

        # sub_b: 1-bundle marker of size i
        sub_b_ratio = [0] * len(sizes)
        sub_b_ratio[i] = 1
        sub_b_str = "-".join(str(x) for x in sub_b_ratio)

        # Skip if sub_a is all zeros (would mean original was 1-bundle)
        if sum(sub_a_ratio) == 0:
            continue

        # Both must exist in GPU pool
        if sub_a_str not in gpu_pool or sub_b_str not in gpu_pool:
            continue

        sub_a_eff, sub_a_len = gpu_pool[sub_a_str]
        sub_b_eff, sub_b_len = gpu_pool[sub_b_str]

        results.append(Decomposition(
            original_ratio_str=marker_ratio_str,
            sub_a_ratio_str=sub_a_str,
            sub_b_ratio_str=sub_b_str,
            sub_a_efficiency=sub_a_eff,
            sub_a_length_yards=sub_a_len,
            sub_b_efficiency=sub_b_eff,
            sub_b_length_yards=sub_b_len,
            peeled_bundles=1,
        ))

    # --- 2-bundle peeloffs ---
    for i in range(len(sizes)):
        for j in range(i, len(sizes)):
            if i == j:
                # Same size: need ratio[i] >= 2
                if ratio[i] < 2:
                    continue
                sub_a_ratio = list(ratio)
                sub_a_ratio[i] -= 2
            else:
                # Different sizes: need ratio[i] >= 1 and ratio[j] >= 1
                if ratio[i] < 1 or ratio[j] < 1:
                    continue
                sub_a_ratio = list(ratio)
                sub_a_ratio[i] -= 1
                sub_a_ratio[j] -= 1

            sub_a_str = "-".join(str(x) for x in sub_a_ratio)

            # sub_b: 2-bundle marker
            sub_b_ratio = [0] * len(sizes)
            sub_b_ratio[i] += 1
            sub_b_ratio[j] += 1
            sub_b_str = "-".join(str(x) for x in sub_b_ratio)

            # Skip if sub_a is all zeros
            if sum(sub_a_ratio) == 0:
                continue

            if sub_a_str not in gpu_pool or sub_b_str not in gpu_pool:
                continue

            sub_a_eff, sub_a_len = gpu_pool[sub_a_str]
            sub_b_eff, sub_b_len = gpu_pool[sub_b_str]

            results.append(Decomposition(
                original_ratio_str=marker_ratio_str,
                sub_a_ratio_str=sub_a_str,
                sub_b_ratio_str=sub_b_str,
                sub_a_efficiency=sub_a_eff,
                sub_a_length_yards=sub_a_len,
                sub_b_efficiency=sub_b_eff,
                sub_b_length_yards=sub_b_len,
                peeled_bundles=2,
            ))

    # Sort by sub_a efficiency descending (keep the big marker efficient)
    results.sort(key=lambda d: d.sub_a_efficiency, reverse=True)
    return results


def compute_endbit_creation_rate(
    marker_length: float,
    avg_roll_length: float,
) -> float:
    """
    Estimate end-bit yards per roll for a marker length.

    A roll of `avg_roll_length` cut into plies of `marker_length` leaves
    a remainder. This is the expected end-bit per roll.
    """
    if marker_length <= 0:
        return 0.0
    plies_per_roll = int(avg_roll_length / marker_length)
    if plies_per_roll <= 0:
        return avg_roll_length  # Marker longer than roll
    remainder = avg_roll_length - (plies_per_roll * marker_length)
    return remainder


def build_decomposed_cutplan(
    original_markers: List[MarkerSpec],
    decompositions: Dict[str, Tuple[Decomposition, int]],
) -> List[MarkerSpec]:
    """
    Apply decompositions to build a new MarkerSpec list.

    Args:
        original_markers: Original cutplan markers.
        decompositions: {marker_label: (Decomposition, plies_to_shift)}

    Returns:
        New list of MarkerSpec with decompositions applied.
    """
    result: List[MarkerSpec] = []
    next_label_num = max(
        (int(m.marker_label.lstrip("M")) for m in original_markers
         if m.marker_label.startswith("M") and m.marker_label[1:].isdigit()),
        default=0,
    ) + 1

    for m in original_markers:
        if m.marker_label not in decompositions:
            result.append(m)
            continue

        decomp, plies_to_shift = decompositions[m.marker_label]

        # Reduce original marker plies
        remaining_plies = m.plies - plies_to_shift
        if remaining_plies > 0:
            result.append(MarkerSpec(
                marker_label=m.marker_label,
                length_yards=m.length_yards,
                plies=remaining_plies,
                ratio_str=m.ratio_str,
            ))

        # Add sub_a (the big remainder)
        result.append(MarkerSpec(
            marker_label=f"M{next_label_num}",
            length_yards=decomp.sub_a_length_yards,
            plies=plies_to_shift,
            ratio_str=decomp.sub_a_ratio_str,
        ))
        next_label_num += 1

        # Add sub_b (the small filler)
        result.append(MarkerSpec(
            marker_label=f"M{next_label_num}",
            length_yards=decomp.sub_b_length_yards,
            plies=plies_to_shift,
            ratio_str=decomp.sub_b_ratio_str,
        ))
        next_label_num += 1

    return result


def _marker_demand(markers: List[MarkerSpec], sizes: List[str]) -> Dict[str, int]:
    """Compute total garments per size from a marker list."""
    demand: Dict[str, int] = {s: 0 for s in sizes}
    for m in markers:
        ratio = [int(x) for x in m.ratio_str.split("-")]
        for i, s in enumerate(sizes):
            if i < len(ratio):
                demand[s] += ratio[i] * m.plies
    return demand


def validate_demand_preserved(
    original_markers: List[MarkerSpec],
    candidate_markers: List[MarkerSpec],
    sizes: List[str],
) -> Tuple[bool, Dict[str, int], Dict[str, int]]:
    """Check that decomposition preserves demand exactly."""
    orig_demand = _marker_demand(original_markers, sizes)
    cand_demand = _marker_demand(candidate_markers, sizes)
    preserved = all(orig_demand[s] == cand_demand[s] for s in sizes)
    return preserved, orig_demand, cand_demand


def run_decomposition(
    original_markers: List[MarkerSpec],
    gpu_pool: Dict[str, Tuple[float, float]],
    sizes: List[str],
    avg_roll_length: float = 100.0,
    endbit_waste_yards: float = 0.0,
    max_attempts: int = 5,
    max_shift_pct: float = 0.30,
) -> List[DecompositionResult]:
    """
    Main entry point. Find decomposition candidates for MC validation.

    Algorithm:
    1. Sort cutplan markers by endbit_creation_rate (worst first)
    2. For each marker (up to max_attempts), find best decomposition
    3. Calculate plies_to_shift = ceil(endbit_waste / sub_b.length)
    4. Cap at max_shift_pct of marker plies
    5. Build candidate cutplan
    6. Return candidates (caller runs MC to validate)

    Args:
        original_markers: Cutplan markers (MarkerSpec list).
        gpu_pool: {ratio_str: (efficiency, length_yards)} from MarkerBank.
        sizes: Ordered size list.
        avg_roll_length: Average roll length in yards.
        endbit_waste_yards: Baseline endbit waste from MC (Type 2).
        max_attempts: Max decomposition candidates to return.
        max_shift_pct: Max fraction of plies to shift per marker.

    Returns:
        List of DecompositionResult candidates, best first.
    """
    if endbit_waste_yards <= 0:
        return []

    # Sort markers by end-bit creation rate (worst first)
    scored = []
    for m in original_markers:
        rate = compute_endbit_creation_rate(m.length_yards, avg_roll_length)
        # Weight by plies: more plies = more total end-bit waste
        rolls_for_marker = math.ceil(m.total_fabric_yards / avg_roll_length)
        total_endbit = rate * rolls_for_marker
        scored.append((total_endbit, rate, m))

    scored.sort(key=lambda x: x[0], reverse=True)

    results: List[DecompositionResult] = []
    seen_decomps = set()  # Avoid duplicate candidates

    for _, rate, m in scored:
        if len(results) >= max_attempts:
            break

        decomps = find_decompositions(m.ratio_str, gpu_pool, sizes)
        if not decomps:
            continue

        for decomp in decomps:
            key = (m.marker_label, decomp.sub_a_ratio_str, decomp.sub_b_ratio_str)
            if key in seen_decomps:
                continue
            seen_decomps.add(key)

            # Calculate plies to shift
            if decomp.sub_b_length_yards <= 0:
                continue
            plies_needed = math.ceil(endbit_waste_yards / decomp.sub_b_length_yards)
            max_plies = int(m.plies * max_shift_pct)
            plies_to_shift = max(1, min(plies_needed, max_plies))

            # Build candidate cutplan
            decomp_map = {m.marker_label: (decomp, plies_to_shift)}
            candidate = build_decomposed_cutplan(original_markers, decomp_map)

            results.append(DecompositionResult(
                original_markers=original_markers,
                candidate_markers=candidate,
                decomposed_marker_label=m.marker_label,
                decomposition=decomp,
                plies_shifted=plies_to_shift,
                original_plies=m.plies,
            ))

            if len(results) >= max_attempts:
                break

    return results


def run_cascaded_decomposition(
    original_markers: List[MarkerSpec],
    gpu_pool: Dict[str, Tuple[float, float]],
    sizes: List[str],
    avg_roll_length: float = 100.0,
    endbit_waste_yards: float = 0.0,
    max_shift_pct: float = 0.30,
) -> List[DecompositionResult]:
    """
    Cascade: decompose TWO markers from the cutplan.

    Takes the best single decomposition, then tries decomposing a second
    marker from the already-modified cutplan.

    Returns list of cascaded candidates (each has both decompositions applied).
    """
    # First pass: get best single decomposition
    singles = run_decomposition(
        original_markers, gpu_pool, sizes,
        avg_roll_length, endbit_waste_yards,
        max_attempts=1, max_shift_pct=max_shift_pct,
    )
    if not singles:
        return []

    best_single = singles[0]
    # Remaining waste estimate: assume sub_b fills some end-bits
    remaining_waste = max(0, endbit_waste_yards - best_single.plies_shifted * best_single.decomposition.sub_b_length_yards * 0.5)

    if remaining_waste < 1.0:
        return []  # Single decomp likely sufficient

    # Second pass: decompose another marker from the modified cutplan
    seconds = run_decomposition(
        best_single.candidate_markers, gpu_pool, sizes,
        avg_roll_length, remaining_waste,
        max_attempts=3, max_shift_pct=max_shift_pct,
    )

    # Return the cascaded results (these already have both decompositions applied)
    return seconds
