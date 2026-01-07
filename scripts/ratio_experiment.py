#!/usr/bin/env python3
"""
Ratio Experiment: Test if quick nesting rankings correlate with longer nesting rankings.

This script:
1. Loads pieces from a DXF file
2. Generates 100 random size combinations (1-8 garments per marker)
3. Nests each combination at 4 time points: 5s, 15s, 30s, 60s
4. Computes Spearman rank correlation between time points
5. Answers: Does ranking at 5s predict ranking at 60s?

Usage:
    python scripts/ratio_experiment.py
"""

import json
import random
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
from scipy import stats

from nesting_engine.io.dxf_parser import DXFParser
from nesting_engine.engine.spyrrow_engine import SpyrrowEngine, SpyrrowConfig
from nesting_engine.core.piece import Piece, PieceIdentifier, OrientationConstraint
from nesting_engine.core.instance import (
    Container, NestingItem, NestingInstance, FlipMode
)


# Configuration
DXF_PATH = Path("data/24_2506_7_S-AN1000.DXF")
OUTPUT_PATH = Path("experiment_results/ratio_experiment_10_90s.json")
SIZES = ["XS", "S", "M", "L", "XL", "XXL"]
TIME_POINTS = [10, 90]  # seconds
NUM_COMBINATIONS = 50
MIN_GARMENTS = 1
MAX_GARMENTS = 8
CONTAINER_WIDTH_INCHES = 60.0
RANDOM_SEED = 42

# Piece configuration per garment
PIECE_CONFIG = {
    "BK": {"demand": 1, "flip": False},
    "FRT": {"demand": 1, "flip": False},
    "SL": {"demand": 2, "flip": True},  # left/right pair
}


@dataclass
class CombinationResult:
    """Result for a single size combination."""
    combo_id: str
    sizes: Dict[str, int]  # e.g., {"M": 2, "L": 1}
    total_garments: int
    results_by_time: Dict[int, float]  # time_seconds -> utilization
    strip_lengths_by_time: Dict[int, float]  # time_seconds -> strip_length_mm


def extract_piece_type(piece_name: str) -> Optional[str]:
    """
    Extract piece type (BK, FRT, SL) from piece name.

    Examples:
        "24-2506-P1-BKX1" -> "BK"
        "24-2506-P2-FRTX1" -> "FRT"
        "24-2506-P3-SLX1" -> "SL"
    """
    name_upper = piece_name.upper()
    for piece_type in PIECE_CONFIG.keys():
        if piece_type in name_upper:
            return piece_type
    return None


def load_and_organize_pieces(dxf_path: Path) -> Dict[str, Dict[str, Piece]]:
    """
    Load pieces from DXF and organize by size and type.

    Returns:
        Dict[size][piece_type] -> Piece
        e.g., {"M": {"BK": Piece, "FRT": Piece, "SL": Piece}, ...}
    """
    print(f"Loading pieces from {dxf_path}...")
    parser = DXFParser(str(dxf_path))
    result = parser.parse()

    print(f"  Found {len(result.pieces)} raw pieces")

    # Organize pieces by size and type, deduplicating
    pieces_by_size: Dict[str, Dict[str, Piece]] = defaultdict(dict)

    for parsed in result.pieces:
        size = parsed.size
        if size not in SIZES:
            continue

        piece_name = parsed.piece_name or ""
        piece_type = extract_piece_type(piece_name)

        if piece_type is None:
            continue

        # Skip if we already have this piece type for this size (deduplication)
        if piece_type in pieces_by_size[size]:
            continue

        # Convert to mm
        to_mm = 25.4  # DXF is in inches
        vertices_mm = [(x * to_mm, y * to_mm) for x, y in parsed.vertices]

        # Clean vertices (remove duplicates)
        cleaned = []
        seen = set()
        for x, y in vertices_mm:
            key = (round(x, 3), round(y, 3))
            if key not in seen:
                seen.add(key)
                cleaned.append((x, y))

        if len(cleaned) < 3:
            continue

        # Create piece with unique ID
        identifier = PieceIdentifier(
            piece_name=f"{piece_type}_{size}",
            size=size
        )

        orientation = OrientationConstraint(
            allowed_rotations=[0, 180],
            allow_flip=PIECE_CONFIG[piece_type]["flip"]
        )

        piece = Piece(
            vertices=cleaned,
            identifier=identifier,
            orientation=orientation
        )

        pieces_by_size[size][piece_type] = piece

    # Verify we have all piece types for all sizes
    for size in SIZES:
        if size not in pieces_by_size:
            print(f"  WARNING: No pieces found for size {size}")
            continue
        for piece_type in PIECE_CONFIG.keys():
            if piece_type not in pieces_by_size[size]:
                print(f"  WARNING: Missing {piece_type} for size {size}")

    # Summary
    total_unique = sum(len(types) for types in pieces_by_size.values())
    print(f"  Organized into {total_unique} unique pieces across {len(pieces_by_size)} sizes")

    return dict(pieces_by_size)


def generate_random_combinations(
    num_combinations: int,
    sizes: List[str],
    min_garments: int,
    max_garments: int,
    seed: int
) -> List[Dict[str, int]]:
    """
    Generate random size combinations.

    Returns:
        List of dicts mapping size -> count
        e.g., [{"M": 2, "L": 1}, {"S": 3, "XL": 2}, ...]
    """
    random.seed(seed)
    combinations = []

    for _ in range(num_combinations):
        total = random.randint(min_garments, max_garments)
        combo = defaultdict(int)

        for _ in range(total):
            size = random.choice(sizes)
            combo[size] += 1

        combinations.append(dict(combo))

    return combinations


def create_nesting_instance(
    pieces_by_size: Dict[str, Dict[str, Piece]],
    size_counts: Dict[str, int],
    combo_id: str
) -> NestingInstance:
    """
    Create a NestingInstance for a given size combination.

    Uses unique piece IDs to avoid spyrrow "non-unique ID" errors.
    """
    items = []

    for size, count in size_counts.items():
        if size not in pieces_by_size:
            continue

        for piece_type, config in PIECE_CONFIG.items():
            if piece_type not in pieces_by_size[size]:
                continue

            base_piece = pieces_by_size[size][piece_type]

            # Create piece with unique ID for this combo
            unique_id = f"{piece_type}_{size}_{combo_id[:8]}"
            identifier = PieceIdentifier(
                piece_name=unique_id,
                size=size
            )

            piece = Piece(
                vertices=base_piece.vertices,
                identifier=identifier,
                orientation=base_piece.orientation
            )

            # Calculate total demand: config demand * number of garments of this size
            total_demand = config["demand"] * count

            flip_mode = FlipMode.PAIRED if config["flip"] else FlipMode.NONE

            item = NestingItem(
                piece=piece,
                demand=total_demand,
                flip_mode=flip_mode
            )
            items.append(item)

    container = Container.from_inches(width=CONTAINER_WIDTH_INCHES, height=None)

    instance = NestingInstance.create(
        name=f"Combo_{combo_id[:8]}",
        container=container,
        items=items,
        piece_buffer=2.0,  # 2mm between pieces
        edge_buffer=5.0    # 5mm from edges
    )

    return instance


def run_experiment(
    pieces_by_size: Dict[str, Dict[str, Piece]],
    combinations: List[Dict[str, int]],
    time_points: List[int]
) -> List[CombinationResult]:
    """
    Run the nesting experiment for all combinations at all time points.
    """
    engine = SpyrrowEngine()
    results = []

    total_runs = len(combinations) * len(time_points)
    completed = 0
    start_time = time.time()

    print(f"\nRunning experiment: {len(combinations)} combinations x {len(time_points)} time points = {total_runs} total runs")
    print("-" * 70)

    for combo_idx, size_counts in enumerate(combinations):
        combo_id = uuid.uuid4().hex
        total_garments = sum(size_counts.values())

        results_by_time = {}
        strip_lengths_by_time = {}

        for time_limit in time_points:
            try:
                instance = create_nesting_instance(pieces_by_size, size_counts, combo_id)
                config = SpyrrowConfig(time_limit=time_limit)
                solution = engine.solve(instance, config=config)

                results_by_time[time_limit] = solution.utilization_percent
                strip_lengths_by_time[time_limit] = solution.strip_length

            except Exception as e:
                print(f"  ERROR combo {combo_idx}, {time_limit}s: {e}")
                results_by_time[time_limit] = 0.0
                strip_lengths_by_time[time_limit] = float('inf')

            completed += 1

            # Progress update
            elapsed = time.time() - start_time
            rate = completed / elapsed if elapsed > 0 else 0
            remaining = (total_runs - completed) / rate if rate > 0 else 0

            if completed % 10 == 0 or completed == total_runs:
                print(f"  [{completed:4d}/{total_runs}] "
                      f"Combo {combo_idx+1:3d}, {time_limit:2d}s: "
                      f"{results_by_time[time_limit]:5.1f}% util | "
                      f"ETA: {remaining/60:.1f}min")

        result = CombinationResult(
            combo_id=combo_id,
            sizes=size_counts,
            total_garments=total_garments,
            results_by_time=results_by_time,
            strip_lengths_by_time=strip_lengths_by_time
        )
        results.append(result)

    total_time = time.time() - start_time
    print(f"\nExperiment completed in {total_time/60:.1f} minutes")

    return results


def compute_correlations(
    results: List[CombinationResult],
    time_points: List[int]
) -> Dict[str, float]:
    """
    Compute Spearman rank correlations between all pairs of time points.
    """
    correlations = {}

    for i, t1 in enumerate(time_points):
        for t2 in time_points[i+1:]:
            utils_t1 = [r.results_by_time[t1] for r in results]
            utils_t2 = [r.results_by_time[t2] for r in results]

            # Spearman rank correlation
            rho, p_value = stats.spearmanr(utils_t1, utils_t2)

            key = f"{t1}s_vs_{t2}s"
            correlations[key] = {
                "rho": rho,
                "p_value": p_value
            }

    return correlations


def print_results(
    results: List[CombinationResult],
    correlations: Dict[str, Dict],
    time_points: List[int]
):
    """
    Print analysis results.
    """
    print("\n" + "=" * 70)
    print("RESULTS SUMMARY")
    print("=" * 70)

    # Correlation matrix
    print("\nSpearman Rank Correlation Matrix:")
    print("-" * 50)

    header = "       " + "  ".join(f"{t:>6}s" for t in time_points)
    print(header)

    for t1 in time_points:
        row = f"{t1:>5}s "
        for t2 in time_points:
            if t1 == t2:
                row += f"{'1.000':>8}"
            elif t1 < t2:
                key = f"{t1}s_vs_{t2}s"
                rho = correlations[key]["rho"]
                row += f"{rho:>8.3f}"
            else:
                key = f"{t2}s_vs_{t1}s"
                rho = correlations[key]["rho"]
                row += f"{rho:>8.3f}"
        print(row)

    # Key finding - use first vs last time point
    t_first, t_last = time_points[0], time_points[-1]
    key = f"{t_first}s_vs_{t_last}s"
    key_correlation = correlations[key]["rho"]
    print(f"\n*** Key Finding: {t_first}s vs {t_last}s correlation = {key_correlation:.3f} ***")

    if key_correlation > 0.85:
        print(f"    CONCLUSION: Quick ranking ({t_first}s) DOES predict final ranking ({t_last}s)")
        print("    You can use short nesting times for initial screening!")
    else:
        print(f"    CONCLUSION: Quick ranking ({t_first}s) does NOT reliably predict final ranking")
        print("    Longer nesting times may be needed for accurate comparisons.")

    # Top 10 combinations at each time point
    print("\n" + "-" * 70)
    print("Top 10 Combinations by Utilization at Each Time Point:")
    print("-" * 70)

    for t in time_points:
        print(f"\n{t}s nesting:")
        sorted_results = sorted(
            results,
            key=lambda r: r.results_by_time[t],
            reverse=True
        )[:10]

        for i, r in enumerate(sorted_results, 1):
            sizes_str = ", ".join(f"{s}:{c}" for s, c in sorted(r.sizes.items()))
            print(f"  {i:2d}. {r.results_by_time[t]:5.1f}% | {r.total_garments} garments | {sizes_str}")

    # Check if top 10 at first time matches top 10 at last time
    print("\n" + "-" * 70)
    print(f"Overlap Analysis: Top 10 at {t_first}s vs Top 10 at {t_last}s")
    print("-" * 70)

    top10_first = set(r.combo_id for r in sorted(
        results, key=lambda r: r.results_by_time[t_first], reverse=True
    )[:10])

    top10_last = set(r.combo_id for r in sorted(
        results, key=lambda r: r.results_by_time[t_last], reverse=True
    )[:10])

    overlap = len(top10_first & top10_last)
    print(f"  {overlap}/10 combinations appear in both top-10 lists ({overlap*10}% overlap)")


def save_results(
    results: List[CombinationResult],
    correlations: Dict[str, Dict],
    output_path: Path
):
    """
    Save all results to JSON.
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)

    data = {
        "metadata": {
            "timestamp": datetime.now().isoformat(),
            "dxf_file": str(DXF_PATH),
            "sizes": SIZES,
            "time_points": TIME_POINTS,
            "num_combinations": NUM_COMBINATIONS,
            "random_seed": RANDOM_SEED,
            "piece_config": PIECE_CONFIG
        },
        "correlations": correlations,
        "results": [
            {
                "combo_id": r.combo_id,
                "sizes": r.sizes,
                "total_garments": r.total_garments,
                "utilization_by_time": r.results_by_time,
                "strip_length_by_time": r.strip_lengths_by_time
            }
            for r in results
        ]
    }

    with open(output_path, "w") as f:
        json.dump(data, f, indent=2)

    print(f"\nResults saved to {output_path}")


def main():
    print("=" * 70)
    print("RATIO EXPERIMENT: Quick vs Long Nesting Correlation")
    print("=" * 70)
    print(f"DXF file: {DXF_PATH}")
    print(f"Sizes: {SIZES}")
    print(f"Time points: {TIME_POINTS} seconds")
    print(f"Combinations: {NUM_COMBINATIONS}")
    print(f"Garments per marker: {MIN_GARMENTS}-{MAX_GARMENTS}")
    print()

    # Load pieces
    pieces_by_size = load_and_organize_pieces(DXF_PATH)

    # Validate we have all required sizes
    available_sizes = [s for s in SIZES if s in pieces_by_size and len(pieces_by_size[s]) == len(PIECE_CONFIG)]
    if len(available_sizes) < len(SIZES):
        print(f"\nWARNING: Only {len(available_sizes)} sizes fully available: {available_sizes}")
        print("Continuing with available sizes...")

    # Generate combinations
    combinations = generate_random_combinations(
        NUM_COMBINATIONS,
        available_sizes,
        MIN_GARMENTS,
        MAX_GARMENTS,
        RANDOM_SEED
    )

    # Run experiment
    results = run_experiment(pieces_by_size, combinations, TIME_POINTS)

    # Compute correlations
    correlations = compute_correlations(results, TIME_POINTS)

    # Print results
    print_results(results, correlations, TIME_POINTS)

    # Save results
    save_results(results, correlations, OUTPUT_PATH)


if __name__ == "__main__":
    main()
