#!/usr/bin/env python3
"""
Full Parallel Nesting Experiment - 100 random combinations with 6 workers.

Runs the same experiment as ratio_experiment but with process-based parallelization.
Compares results with the previous sequential experiment.

Usage:
    python scripts/ratio_full_parallel.py

Expected time: 100 combos × 30s ÷ 6 workers ≈ 8-9 minutes
"""

import json
import multiprocessing as mp
import random
import time
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Tuple, Any

# Configuration
DXF_PATH = Path("data/24_2506_7_S-AN1000.DXF")
BASELINE_PATH = Path("experiment_results/ratio_experiment_results.json")
OUTPUT_PATH = Path("experiment_results/parallel_experiment_results.json")
SIZES = ["XS", "S", "M", "L", "XL", "XXL"]
TIME_LIMIT = 30  # seconds per nest
NUM_WORKERS = 6
NUM_COMBINATIONS = 100
RANDOM_SEED = 42  # Same seed as original experiment for comparable combos
CONTAINER_WIDTH_MM = 1524.0  # 60 inches in mm
MIN_GARMENTS = 1
MAX_GARMENTS = 6

# Piece configuration per garment
PIECE_CONFIG = {
    "BK": {"demand": 1, "flip": False},
    "FRT": {"demand": 1, "flip": False},
    "SL": {"demand": 2, "flip": True},  # left/right pair
}


def extract_piece_type(piece_name: str) -> str | None:
    """Extract piece type (BK, FRT, SL) from piece name."""
    name_upper = piece_name.upper()
    for piece_type in PIECE_CONFIG.keys():
        if piece_type in name_upper:
            return piece_type
    return None


def load_piece_vertices(dxf_path: Path) -> Dict[str, Dict[str, List[Tuple[float, float]]]]:
    """
    Load pieces from DXF and extract vertices as primitive data.

    Returns:
        {size: {piece_type: [(x, y), ...]}}
    """
    from nesting_engine.io.dxf_parser import DXFParser

    print(f"Loading pieces from {dxf_path}...")
    parser = DXFParser(str(dxf_path))
    result = parser.parse()

    print(f"  Found {len(result.pieces)} raw pieces")

    pieces_by_size: Dict[str, Dict[str, List[Tuple[float, float]]]] = defaultdict(dict)

    for parsed in result.pieces:
        size = parsed.size
        if size not in SIZES:
            continue

        piece_name = parsed.piece_name or ""
        piece_type = extract_piece_type(piece_name)

        if piece_type is None:
            continue

        if piece_type in pieces_by_size[size]:
            continue

        # Convert to mm
        to_mm = 25.4
        vertices_mm = [(x * to_mm, y * to_mm) for x, y in parsed.vertices]

        # Clean duplicate vertices
        cleaned = []
        seen = set()
        for x, y in vertices_mm:
            key = (round(x, 3), round(y, 3))
            if key not in seen:
                seen.add(key)
                cleaned.append((x, y))

        if len(cleaned) < 3:
            continue

        pieces_by_size[size][piece_type] = cleaned

    total_unique = sum(len(types) for types in pieces_by_size.values())
    print(f"  Organized into {total_unique} unique pieces across {len(pieces_by_size)} sizes")

    return dict(pieces_by_size)


def generate_random_size_mixes(n: int, seed: int) -> List[Dict[str, int]]:
    """Generate n random size combinations (1-6 garments total)."""
    random.seed(seed)
    mixes = []

    for _ in range(n):
        total = random.randint(MIN_GARMENTS, MAX_GARMENTS)
        size_counts = {size: 0 for size in SIZES}

        for _ in range(total):
            size = random.choice(SIZES)
            size_counts[size] += 1

        # Remove zeros for cleaner output
        size_counts = {k: v for k, v in size_counts.items() if v > 0}
        mixes.append(size_counts)

    return mixes


def nest_worker(args: Tuple[int, Dict[str, int], int, Dict[str, Dict[str, List[Tuple[float, float]]]]]) -> Tuple[int, Dict[str, int], float, float, float, str]:
    """
    Worker function - imports happen inside each process.

    Args:
        args: (combo_id, size_mix, time_limit, piece_vertices_by_size)

    Returns:
        (combo_id, size_mix, utilization, strip_length, duration, error_msg or "")
    """
    combo_id, size_mix, time_limit, piece_vertices_by_size = args

    start_time = time.time()

    try:
        # Import inside worker to avoid serialization issues
        from nesting_engine.engine.spyrrow_engine import SpyrrowEngine, SpyrrowConfig
        from nesting_engine.core.instance import Container, NestingItem, NestingInstance, FlipMode
        from nesting_engine.core.piece import Piece, PieceIdentifier, OrientationConstraint

        # Build pieces from primitive vertex data
        items = []

        for size, count in size_mix.items():
            if count == 0 or size not in piece_vertices_by_size:
                continue

            for piece_type, config in PIECE_CONFIG.items():
                if piece_type not in piece_vertices_by_size[size]:
                    continue

                vertices = piece_vertices_by_size[size][piece_type]

                identifier = PieceIdentifier(
                    piece_name=f"{piece_type}_{size}_{combo_id}",
                    size=size
                )

                orientation = OrientationConstraint(
                    allowed_rotations=[0, 180],
                    allow_flip=config["flip"]
                )

                piece = Piece(
                    vertices=vertices,
                    identifier=identifier,
                    orientation=orientation
                )

                total_demand = config["demand"] * count
                flip_mode = FlipMode.PAIRED if config["flip"] else FlipMode.NONE

                item = NestingItem(
                    piece=piece,
                    demand=total_demand,
                    flip_mode=flip_mode
                )
                items.append(item)

        if not items:
            return combo_id, size_mix, 0.0, 0.0, time.time() - start_time, "No items created"

        # Create container (60 inches = 1524mm)
        container = Container(width=CONTAINER_WIDTH_MM, height=None)

        instance = NestingInstance.create(
            name=f"Parallel_{combo_id}",
            container=container,
            items=items,
            piece_buffer=2.0,
            edge_buffer=5.0
        )

        # Solve
        engine = SpyrrowEngine()
        config = SpyrrowConfig(time_limit=time_limit)
        solution = engine.solve(instance, config=config)

        duration = time.time() - start_time
        return combo_id, size_mix, solution.utilization_percent, solution.strip_length, duration, ""

    except Exception as e:
        duration = time.time() - start_time
        return combo_id, size_mix, 0.0, 0.0, duration, str(e)


def load_baseline_results() -> List[Dict[str, Any]]:
    """Load the baseline random experiment results (30s time point only)."""
    if not BASELINE_PATH.exists():
        print(f"WARNING: Baseline file not found at {BASELINE_PATH}")
        return []

    with open(BASELINE_PATH) as f:
        data = json.load(f)

    # Extract 30s results
    baseline = []
    for r in data['results']:
        baseline.append({
            'sizes': r['sizes'],
            'total_garments': r['total_garments'],
            'utilization': r['utilization_by_time']['30'],
            'strip_length': r['strip_length_by_time']['30']
        })

    return baseline


def print_comparison(parallel_results: List[Dict], baseline_results: List[Dict]):
    """Print comparison between parallel and baseline results."""

    print("\n" + "=" * 90)
    print("COMPARISON: Parallel (6 workers) vs Sequential Baseline @ 30s")
    print("=" * 90)

    # Sort by utilization
    parallel_sorted = sorted(parallel_results, key=lambda x: x['utilization'], reverse=True)
    baseline_sorted = sorted(baseline_results, key=lambda x: x['utilization'], reverse=True)

    # Top 20 comparison
    print("\nTOP 20 RESULTS - PARALLEL RUN")
    print("-" * 90)
    print(f"{'Rank':<5} {'Size Mix':<35} {'Garments':<10} {'Util%':<10} {'Strip(mm)':<12}")
    print("-" * 90)

    for i, r in enumerate(parallel_sorted[:20]):
        mix_str = ", ".join(f"{k}:{v}" for k, v in sorted(r['sizes'].items()))
        total = sum(r['sizes'].values())
        print(f"{i+1:<5} {mix_str:<35} {total:<10} {r['utilization']:<10.2f} {r['strip_length']:<12.1f}")

    if baseline_results:
        print("\n\nTOP 20 RESULTS - BASELINE (Sequential)")
        print("-" * 90)
        print(f"{'Rank':<5} {'Size Mix':<35} {'Garments':<10} {'Util%':<10} {'Strip(mm)':<12}")
        print("-" * 90)

        for i, r in enumerate(baseline_sorted[:20]):
            mix_str = ", ".join(f"{k}:{v}" for k, v in sorted(r['sizes'].items()))
            total = sum(r['sizes'].values())
            print(f"{i+1:<5} {mix_str:<35} {total:<10} {r['utilization']:<10.2f} {r['strip_length']:<12.1f}")

    # Summary statistics
    print("\n" + "=" * 90)
    print("SUMMARY STATISTICS")
    print("=" * 90)

    parallel_utils = [r['utilization'] for r in parallel_sorted]
    baseline_utils = [r['utilization'] for r in baseline_sorted] if baseline_results else []

    print(f"\n{'Metric':<35} {'Parallel':<15} {'Baseline':<15} {'Diff':<10}")
    print("-" * 75)

    # Best utilization
    p_best = max(parallel_utils)
    b_best = max(baseline_utils) if baseline_utils else 0
    diff = p_best - b_best if baseline_utils else 0
    print(f"{'Best utilization':<35} {p_best:<15.2f} {b_best:<15.2f} {diff:+.2f}")

    # Average of top 10
    p_top10 = sum(parallel_utils[:10]) / min(10, len(parallel_utils))
    b_top10 = sum(baseline_utils[:10]) / min(10, len(baseline_utils)) if baseline_utils else 0
    diff = p_top10 - b_top10 if baseline_utils else 0
    print(f"{'Average of top 10':<35} {p_top10:<15.2f} {b_top10:<15.2f} {diff:+.2f}")

    # Average of top 20
    p_top20 = sum(parallel_utils[:20]) / min(20, len(parallel_utils))
    b_top20 = sum(baseline_utils[:20]) / min(20, len(baseline_utils)) if baseline_utils else 0
    diff = p_top20 - b_top20 if baseline_utils else 0
    print(f"{'Average of top 20':<35} {p_top20:<15.2f} {b_top20:<15.2f} {diff:+.2f}")

    # Overall average
    p_avg = sum(parallel_utils) / len(parallel_utils)
    b_avg = sum(baseline_utils) / len(baseline_utils) if baseline_utils else 0
    diff = p_avg - b_avg if baseline_utils else 0
    print(f"{'Overall average':<35} {p_avg:<15.2f} {b_avg:<15.2f} {diff:+.2f}")

    # Min utilization
    p_min = min(parallel_utils)
    b_min = min(baseline_utils) if baseline_utils else 0
    diff = p_min - b_min if baseline_utils else 0
    print(f"{'Minimum utilization':<35} {p_min:<15.2f} {b_min:<15.2f} {diff:+.2f}")

    # Overlap analysis (if same seed was used)
    if baseline_results:
        print("\n" + "-" * 75)
        print("COMBINATION OVERLAP ANALYSIS")
        print("-" * 75)

        def combo_key(sizes_dict):
            return frozenset((k, v) for k, v in sizes_dict.items() if v > 0)

        p_top20_combos = set(combo_key(r['sizes']) for r in parallel_sorted[:20])
        b_top20_combos = set(combo_key(r['sizes']) for r in baseline_sorted[:20])

        overlap = p_top20_combos & b_top20_combos
        print(f"Top 20 overlap: {len(overlap)}/20 combinations appear in both lists")

        if overlap:
            print("\nCommon top combinations:")
            for combo in sorted(list(overlap), key=lambda x: sum(v for k, v in x))[:5]:
                combo_str = ", ".join(f"{k}:{v}" for k, v in sorted(combo))
                print(f"  - {combo_str}")


def save_results(results: List[Dict], total_time: float):
    """Save results to JSON."""
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)

    # Sort by utilization for output
    sorted_results = sorted(results, key=lambda x: x['utilization'], reverse=True)

    data = {
        "metadata": {
            "timestamp": datetime.now().isoformat(),
            "dxf_file": str(DXF_PATH),
            "sizes": SIZES,
            "time_limit_seconds": TIME_LIMIT,
            "num_combinations": NUM_COMBINATIONS,
            "num_workers": NUM_WORKERS,
            "random_seed": RANDOM_SEED,
            "min_garments": MIN_GARMENTS,
            "max_garments": MAX_GARMENTS,
            "container_width_mm": CONTAINER_WIDTH_MM,
            "piece_config": PIECE_CONFIG,
            "total_experiment_time_seconds": total_time
        },
        "summary": {
            "best_utilization": max(r['utilization'] for r in results),
            "worst_utilization": min(r['utilization'] for r in results),
            "average_utilization": sum(r['utilization'] for r in results) / len(results),
            "top10_average": sum(r['utilization'] for r in sorted_results[:10]) / 10,
            "top20_average": sum(r['utilization'] for r in sorted_results[:20]) / 20,
        },
        "results": [
            {
                "rank": i + 1,
                "sizes": r['sizes'],
                "total_garments": sum(r['sizes'].values()),
                "utilization": r['utilization'],
                "strip_length": r['strip_length'],
                "duration_seconds": r['duration']
            }
            for i, r in enumerate(sorted_results)
        ]
    }

    with open(OUTPUT_PATH, "w") as f:
        json.dump(data, f, indent=2)

    print(f"\nResults saved to {OUTPUT_PATH}")


def main():
    print("=" * 90)
    print("FULL PARALLEL NESTING EXPERIMENT")
    print("=" * 90)
    print(f"DXF file: {DXF_PATH}")
    print(f"Combinations: {NUM_COMBINATIONS}")
    print(f"Workers: {NUM_WORKERS}")
    print(f"Time limit: {TIME_LIMIT}s per nest")
    print(f"Random seed: {RANDOM_SEED}")
    print(f"Expected time: ~{NUM_COMBINATIONS * TIME_LIMIT / NUM_WORKERS / 60:.1f} minutes")
    print()

    # Check DXF exists
    if not DXF_PATH.exists():
        print(f"ERROR: DXF file not found at {DXF_PATH}")
        return

    # Load piece vertices in main process (primitive data)
    piece_vertices = load_piece_vertices(DXF_PATH)

    if not piece_vertices:
        print("ERROR: No valid pieces loaded from DXF")
        return

    # Load baseline for comparison
    print("\nLoading baseline results...")
    baseline_results = load_baseline_results()
    if baseline_results:
        print(f"  Loaded {len(baseline_results)} baseline results")
    else:
        print("  No baseline results found - will skip comparison")

    # Generate random size combinations (same seed as original for comparison)
    size_mixes = generate_random_size_mixes(NUM_COMBINATIONS, RANDOM_SEED)

    print(f"\nGenerated {len(size_mixes)} size combinations")
    print("Sample combinations:")
    for i in [0, 1, 2, -2, -1]:
        mix = size_mixes[i]
        mix_str = ", ".join(f"{k}:{v}" for k, v in sorted(mix.items()))
        total = sum(mix.values())
        print(f"  {i if i >= 0 else len(size_mixes) + i}: {mix_str} ({total} garments)")

    # Prepare worker arguments
    worker_args = [
        (i, mix, TIME_LIMIT, piece_vertices)
        for i, mix in enumerate(size_mixes)
    ]

    # Run parallel nesting
    print(f"\n" + "-" * 90)
    print(f"Starting {NUM_COMBINATIONS} nests with {NUM_WORKERS} parallel workers...")
    print("-" * 90)

    overall_start = time.time()

    # Use spawn context to avoid fork issues with spyrrow
    ctx = mp.get_context('spawn')

    results = []
    completed = 0

    with ProcessPoolExecutor(max_workers=NUM_WORKERS, mp_context=ctx) as executor:
        futures = {executor.submit(nest_worker, args): args[0] for args in worker_args}

        for future in as_completed(futures):
            combo_id, size_mix, utilization, strip_length, duration, error = future.result()
            completed += 1

            results.append({
                'combo_id': combo_id,
                'sizes': size_mix,
                'utilization': utilization,
                'strip_length': strip_length,
                'duration': duration,
                'error': error
            })

            # Progress update every 10 completions
            if completed % 10 == 0 or completed == NUM_COMBINATIONS:
                elapsed = time.time() - overall_start
                rate = completed / elapsed
                eta = (NUM_COMBINATIONS - completed) / rate if rate > 0 else 0
                print(f"  Progress: {completed}/{NUM_COMBINATIONS} ({completed*100/NUM_COMBINATIONS:.0f}%) "
                      f"| Elapsed: {elapsed/60:.1f}m | ETA: {eta/60:.1f}m")

    overall_duration = time.time() - overall_start

    # Check for errors
    errors = [r for r in results if r['error']]
    if errors:
        print(f"\nWARNING: {len(errors)} nests had errors:")
        for e in errors[:5]:
            print(f"  Combo {e['combo_id']}: {e['error']}")

    # Print timing summary
    print(f"\n" + "=" * 90)
    print("TIMING SUMMARY")
    print("=" * 90)
    print(f"Total time: {overall_duration/60:.1f} minutes ({overall_duration:.1f}s)")
    print(f"Sequential would be: ~{NUM_COMBINATIONS * TIME_LIMIT / 60:.1f} minutes")
    print(f"Speedup: {(NUM_COMBINATIONS * TIME_LIMIT) / overall_duration:.1f}x")
    print(f"Avg time per nest: {sum(r['duration'] for r in results) / len(results):.1f}s")

    # Convert to comparison format
    parallel_results = [
        {
            'sizes': r['sizes'],
            'utilization': r['utilization'],
            'strip_length': r['strip_length']
        }
        for r in results if not r['error']
    ]

    # Print comparison
    print_comparison(parallel_results, baseline_results)

    # Save results
    save_results(results, overall_duration)


if __name__ == "__main__":
    main()
