#!/usr/bin/env python3
"""
Optuna Bayesian Optimization Experiment for Size Ratio Optimization.

Compares Optuna (50 trials @ 30s) vs Random (100 trials @ 30s) from previous experiment.

Usage:
    python scripts/ratio_experiment_optuna.py
"""

import json
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Any

import optuna
from optuna.samplers import TPESampler

from nesting_engine.io.dxf_parser import DXFParser
from nesting_engine.engine.spyrrow_engine import SpyrrowEngine, SpyrrowConfig
from nesting_engine.core.piece import Piece, PieceIdentifier, OrientationConstraint
from nesting_engine.core.instance import (
    Container, NestingItem, NestingInstance, FlipMode
)


# Configuration - matches previous experiment
DXF_PATH = Path("data/24_2506_7_S-AN1000.DXF")
BASELINE_PATH = Path("experiment_results/ratio_experiment_results.json")
OUTPUT_PATH = Path("experiment_results/optuna_results.json")
SIZES = ["XS", "S", "M", "L", "XL", "XXL"]
TIME_LIMIT = 30  # seconds - matches 30s from baseline
N_TRIALS = 50
N_JOBS = 1  # Single-threaded (nesting time dominates, parallel doesn't help much)
MIN_GARMENTS = 1
MAX_GARMENTS = 6  # Changed from 8 to 6 per spec
MAX_PER_SIZE = 4  # Maximum of any single size
CONTAINER_WIDTH_INCHES = 60.0
N_STARTUP_TRIALS = 10  # Random trials before Bayesian kicks in

# Piece configuration per garment
PIECE_CONFIG = {
    "BK": {"demand": 1, "flip": False},
    "FRT": {"demand": 1, "flip": False},
    "SL": {"demand": 2, "flip": True},  # left/right pair
}


@dataclass
class TrialResult:
    """Result from a single Optuna trial."""
    trial_number: int
    sizes: Dict[str, int]
    total_garments: int
    utilization: float
    strip_length: float
    duration_seconds: float


# Global state for the objective function (initialized per-process for parallel execution)
_pieces_by_size: Dict[str, Dict[str, Piece]] = {}
_engine: Optional[SpyrrowEngine] = None
_trial_results: List[TrialResult] = []
_trial_counter: int = 0
_initialized: bool = False


def ensure_initialized():
    """Ensure global state is initialized (needed for parallel workers)."""
    global _pieces_by_size, _engine, _initialized
    if not _initialized:
        _pieces_by_size = load_and_organize_pieces(DXF_PATH)
        _engine = SpyrrowEngine()
        _initialized = True


def extract_piece_type(piece_name: str) -> Optional[str]:
    """Extract piece type (BK, FRT, SL) from piece name."""
    name_upper = piece_name.upper()
    for piece_type in PIECE_CONFIG.keys():
        if piece_type in name_upper:
            return piece_type
    return None


def load_and_organize_pieces(dxf_path: Path) -> Dict[str, Dict[str, Piece]]:
    """Load pieces from DXF and organize by size and type."""
    print(f"Loading pieces from {dxf_path}...")
    parser = DXFParser(str(dxf_path))
    result = parser.parse()

    print(f"  Found {len(result.pieces)} raw pieces")

    pieces_by_size: Dict[str, Dict[str, Piece]] = defaultdict(dict)

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

        to_mm = 25.4
        vertices_mm = [(x * to_mm, y * to_mm) for x, y in parsed.vertices]

        cleaned = []
        seen = set()
        for x, y in vertices_mm:
            key = (round(x, 3), round(y, 3))
            if key not in seen:
                seen.add(key)
                cleaned.append((x, y))

        if len(cleaned) < 3:
            continue

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

    total_unique = sum(len(types) for types in pieces_by_size.values())
    print(f"  Organized into {total_unique} unique pieces across {len(pieces_by_size)} sizes")

    return dict(pieces_by_size)


def create_nesting_instance(
    pieces_by_size: Dict[str, Dict[str, Piece]],
    size_counts: Dict[str, int],
    combo_id: str
) -> NestingInstance:
    """Create a NestingInstance for a given size combination."""
    items = []

    for size, count in size_counts.items():
        if count == 0 or size not in pieces_by_size:
            continue

        for piece_type, config in PIECE_CONFIG.items():
            if piece_type not in pieces_by_size[size]:
                continue

            base_piece = pieces_by_size[size][piece_type]

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
        piece_buffer=2.0,
        edge_buffer=5.0
    )

    return instance


def run_nesting(size_counts: Dict[str, int], time_limit: int = TIME_LIMIT) -> tuple[float, float]:
    """
    Run nesting for a size combination.

    Returns:
        (utilization_percent, strip_length_mm)
    """
    global _pieces_by_size, _engine

    combo_id = uuid.uuid4().hex

    try:
        instance = create_nesting_instance(_pieces_by_size, size_counts, combo_id)
        config = SpyrrowConfig(time_limit=time_limit)
        solution = _engine.solve(instance, config=config)
        return solution.utilization_percent, solution.strip_length
    except Exception as e:
        print(f"  ERROR: {e}")
        return 0.0, float('inf')


def objective(trial: optuna.Trial) -> float:
    """Optuna objective function - maximize utilization."""
    global _trial_counter, _trial_results

    # Ensure initialized (needed for parallel workers)
    ensure_initialized()

    start_time = time.time()

    # First decide total garments, then distribute among sizes
    total = trial.suggest_int('total_garments', MIN_GARMENTS, MAX_GARMENTS)

    # Suggest size counts with constraint that they sum to total
    # Use categorical for number of non-zero sizes, then distribute
    xs = trial.suggest_int('XS', 0, min(MAX_PER_SIZE, total))
    remaining = total - xs
    s = trial.suggest_int('S', 0, min(MAX_PER_SIZE, remaining))
    remaining -= s
    m = trial.suggest_int('M', 0, min(MAX_PER_SIZE, remaining))
    remaining -= m
    l = trial.suggest_int('L', 0, min(MAX_PER_SIZE, remaining))
    remaining -= l
    xl = trial.suggest_int('XL', 0, min(MAX_PER_SIZE, remaining))
    remaining -= xl
    xxl = remaining  # Whatever is left goes to XXL

    # Validate XXL doesn't exceed max
    if xxl > MAX_PER_SIZE or xxl < 0:
        raise optuna.TrialPruned()

    actual_total = xs + s + m + l + xl + xxl

    # Safety check - should always equal total, but verify
    if actual_total != total:
        raise optuna.TrialPruned()

    size_counts = {
        'XS': xs, 'S': s, 'M': m,
        'L': l, 'XL': xl, 'XXL': xxl
    }
    # Remove zeros for cleaner output
    size_counts = {k: v for k, v in size_counts.items() if v > 0}

    utilization, strip_length = run_nesting(size_counts, TIME_LIMIT)

    duration = time.time() - start_time
    _trial_counter += 1

    # Store result
    result = TrialResult(
        trial_number=_trial_counter,
        sizes=size_counts,
        total_garments=total,
        utilization=utilization,
        strip_length=strip_length,
        duration_seconds=duration
    )
    _trial_results.append(result)

    # Progress output
    sizes_str = ", ".join(f"{k}:{v}" for k, v in sorted(size_counts.items()))
    print(f"  Trial {_trial_counter:3d}: {utilization:5.2f}% | {total} garments | {sizes_str}")

    return utilization


def load_baseline_results() -> List[Dict[str, Any]]:
    """Load the baseline random experiment results (30s time point only)."""
    if not BASELINE_PATH.exists():
        print(f"WARNING: Baseline file not found at {BASELINE_PATH}")
        return []

    with open(BASELINE_PATH) as f:
        data = json.load(f)

    # Extract 30s results, filtered to 1-6 garments
    baseline = []
    for r in data['results']:
        if r['total_garments'] <= MAX_GARMENTS:
            baseline.append({
                'sizes': r['sizes'],
                'total_garments': r['total_garments'],
                'utilization': r['utilization_by_time']['30']
            })

    return baseline


def compute_parameter_importance(study: optuna.Study) -> Dict[str, float]:
    """Compute importance of each size parameter."""
    try:
        importances = optuna.importance.get_param_importances(study)
        return dict(importances)
    except Exception as e:
        print(f"Could not compute parameter importance: {e}")
        return {}


def print_comparison(
    optuna_results: List[TrialResult],
    baseline_results: List[Dict[str, Any]],
    importances: Dict[str, float]
):
    """Print comparison between Optuna and baseline results."""

    print("\n" + "=" * 90)
    print("COMPARISON: Optuna (50 trials) vs Random (100 trials) @ 30s")
    print("=" * 90)

    # Sort by utilization
    optuna_sorted = sorted(optuna_results, key=lambda x: x.utilization, reverse=True)
    baseline_sorted = sorted(baseline_results, key=lambda x: x['utilization'], reverse=True)

    # Top 20 comparison
    print("\nTOP 20 COMPARISON")
    print("-" * 90)
    print(f"{'Rank':<5} {'Optuna Combo':<30} {'Util%':<8} {'Random Combo':<30} {'Util%':<8}")
    print("-" * 90)

    for i in range(min(20, len(optuna_sorted), len(baseline_sorted))):
        opt = optuna_sorted[i] if i < len(optuna_sorted) else None
        base = baseline_sorted[i] if i < len(baseline_sorted) else None

        opt_combo = ", ".join(f"{k}:{v}" for k, v in sorted(opt.sizes.items())) if opt else "N/A"
        opt_util = f"{opt.utilization:.2f}" if opt else "N/A"

        base_combo = ", ".join(f"{k}:{v}" for k, v in sorted(base['sizes'].items())) if base else "N/A"
        base_util = f"{base['utilization']:.2f}" if base else "N/A"

        print(f"{i+1:<5} {opt_combo:<30} {opt_util:<8} {base_combo:<30} {base_util:<8}")

    # Summary statistics
    print("\n" + "=" * 90)
    print("SUMMARY STATISTICS")
    print("=" * 90)

    opt_utils = [r.utilization for r in optuna_sorted]
    base_utils = [r['utilization'] for r in baseline_sorted]

    print(f"\n{'Metric':<35} {'Optuna (50)':<15} {'Random (100)':<15} {'Winner':<10}")
    print("-" * 75)

    # Best utilization
    opt_best = max(opt_utils) if opt_utils else 0
    base_best = max(base_utils) if base_utils else 0
    winner = "Optuna" if opt_best > base_best else "Random" if base_best > opt_best else "Tie"
    print(f"{'Best utilization':<35} {opt_best:<15.2f} {base_best:<15.2f} {winner:<10}")

    # Average of top 20
    opt_top20_avg = sum(opt_utils[:20]) / min(20, len(opt_utils)) if opt_utils else 0
    base_top20_avg = sum(base_utils[:20]) / min(20, len(base_utils)) if base_utils else 0
    winner = "Optuna" if opt_top20_avg > base_top20_avg else "Random" if base_top20_avg > opt_top20_avg else "Tie"
    print(f"{'Average of top 20':<35} {opt_top20_avg:<15.2f} {base_top20_avg:<15.2f} {winner:<10}")

    # Average of top 10
    opt_top10_avg = sum(opt_utils[:10]) / min(10, len(opt_utils)) if opt_utils else 0
    base_top10_avg = sum(base_utils[:10]) / min(10, len(base_utils)) if base_utils else 0
    winner = "Optuna" if opt_top10_avg > base_top10_avg else "Random" if base_top10_avg > opt_top10_avg else "Tie"
    print(f"{'Average of top 10':<35} {opt_top10_avg:<15.2f} {base_top10_avg:<15.2f} {winner:<10}")

    # Overall average
    opt_avg = sum(opt_utils) / len(opt_utils) if opt_utils else 0
    base_avg = sum(base_utils) / len(base_utils) if base_utils else 0
    winner = "Optuna" if opt_avg > base_avg else "Random" if base_avg > opt_avg else "Tie"
    print(f"{'Overall average':<35} {opt_avg:<15.2f} {base_avg:<15.2f} {winner:<10}")

    # Overlap analysis
    print("\n" + "-" * 75)
    print("OVERLAP ANALYSIS")
    print("-" * 75)

    # Convert to comparable format (frozen sets of tuples)
    def combo_key(sizes_dict):
        return frozenset((k, v) for k, v in sizes_dict.items() if v > 0)

    opt_top20_combos = set(combo_key(r.sizes) for r in optuna_sorted[:20])
    base_top20_combos = set(combo_key(r['sizes']) for r in baseline_sorted[:20])

    overlap = opt_top20_combos & base_top20_combos
    print(f"Top 20 overlap: {len(overlap)}/20 combinations appear in both lists")

    if overlap:
        print("Common combinations:")
        for combo in list(overlap)[:5]:
            combo_str = ", ".join(f"{k}:{v}" for k, v in sorted(combo))
            print(f"  - {combo_str}")

    # Parameter importance
    print("\n" + "=" * 90)
    print("PARAMETER IMPORTANCE (Which sizes matter most)")
    print("=" * 90)

    if importances:
        sorted_imp = sorted(importances.items(), key=lambda x: x[1], reverse=True)
        print(f"\n{'Size':<10} {'Importance':<15} {'Bar':<40}")
        print("-" * 65)
        max_imp = max(importances.values()) if importances else 1
        for size, imp in sorted_imp:
            bar_len = int(40 * imp / max_imp)
            bar = "█" * bar_len
            print(f"{size:<10} {imp:<15.4f} {bar}")
    else:
        print("Parameter importance not available")

    # Efficiency analysis
    print("\n" + "=" * 90)
    print("EFFICIENCY ANALYSIS")
    print("=" * 90)

    opt_total_time = sum(r.duration_seconds for r in optuna_results)
    base_total_time = len(baseline_results) * TIME_LIMIT  # Estimated

    print(f"\nOptuna: {N_TRIALS} trials, ~{opt_total_time/60:.1f} min total")
    print(f"Random: {len(baseline_results)} trials, ~{base_total_time/60:.1f} min total")
    print(f"\nOptuna found top result with {len(optuna_results) - optuna_sorted.index(optuna_sorted[0])} remaining trials")


def save_results(
    optuna_results: List[TrialResult],
    study: optuna.Study,
    importances: Dict[str, float],
    baseline_comparison: Dict[str, Any]
):
    """Save results to JSON."""
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)

    data = {
        "metadata": {
            "timestamp": datetime.now().isoformat(),
            "dxf_file": str(DXF_PATH),
            "sizes": SIZES,
            "time_limit_seconds": TIME_LIMIT,
            "n_trials": N_TRIALS,
            "n_jobs": N_JOBS,
            "n_startup_trials": N_STARTUP_TRIALS,
            "min_garments": MIN_GARMENTS,
            "max_garments": MAX_GARMENTS,
            "max_per_size": MAX_PER_SIZE,
            "container_width_inches": CONTAINER_WIDTH_INCHES,
            "piece_config": PIECE_CONFIG
        },
        "best_trial": {
            "sizes": dict(study.best_params),
            "utilization": study.best_value,
            "trial_number": study.best_trial.number
        },
        "parameter_importance": importances,
        "comparison_with_baseline": baseline_comparison,
        "all_trials": [
            {
                "trial_number": r.trial_number,
                "sizes": r.sizes,
                "total_garments": r.total_garments,
                "utilization": r.utilization,
                "strip_length": r.strip_length,
                "duration_seconds": r.duration_seconds
            }
            for r in optuna_results
        ]
    }

    with open(OUTPUT_PATH, "w") as f:
        json.dump(data, f, indent=2)

    print(f"\nResults saved to {OUTPUT_PATH}")


def main():
    global _pieces_by_size, _engine, _trial_results, _trial_counter

    print("=" * 90)
    print("OPTUNA BAYESIAN OPTIMIZATION: Size Ratio Experiment")
    print("=" * 90)
    print(f"DXF file: {DXF_PATH}")
    print(f"Sizes: {SIZES}")
    print(f"Time limit: {TIME_LIMIT}s per trial")
    print(f"Trials: {N_TRIALS} (Optuna) vs 100 (Random baseline)")
    print(f"Parallel workers: {N_JOBS}")
    print(f"Garments per marker: {MIN_GARMENTS}-{MAX_GARMENTS}")
    print(f"Max per size: {MAX_PER_SIZE}")
    print()

    # Load pieces
    _pieces_by_size = load_and_organize_pieces(DXF_PATH)
    _engine = SpyrrowEngine()

    # Load baseline for comparison
    print("\nLoading baseline results...")
    baseline_results = load_baseline_results()
    print(f"  Loaded {len(baseline_results)} baseline results (1-{MAX_GARMENTS} garments)")

    # Create Optuna study
    print("\n" + "-" * 90)
    print("Running Optuna optimization...")
    print("-" * 90)

    # Suppress Optuna's default logging
    optuna.logging.set_verbosity(optuna.logging.WARNING)

    sampler = TPESampler(
        n_startup_trials=N_STARTUP_TRIALS,
        seed=42
    )

    study = optuna.create_study(
        direction='maximize',
        sampler=sampler,
        study_name='size_ratio_optimization'
    )

    start_time = time.time()

    study.optimize(
        objective,
        n_trials=N_TRIALS,
        n_jobs=N_JOBS,
        show_progress_bar=False
    )

    total_time = time.time() - start_time

    print(f"\nOptimization completed in {total_time/60:.1f} minutes")
    print(f"Best utilization: {study.best_value:.2f}%")
    print(f"Best params: {study.best_params}")

    # Compute parameter importance
    print("\nComputing parameter importance...")
    importances = compute_parameter_importance(study)

    # Print comparison
    print_comparison(_trial_results, baseline_results, importances)

    # Prepare comparison summary for JSON
    opt_sorted = sorted(_trial_results, key=lambda x: x.utilization, reverse=True)
    base_sorted = sorted(baseline_results, key=lambda x: x['utilization'], reverse=True)

    comparison = {
        "optuna_best": opt_sorted[0].utilization if opt_sorted else 0,
        "baseline_best": base_sorted[0]['utilization'] if base_sorted else 0,
        "optuna_top20_avg": sum(r.utilization for r in opt_sorted[:20]) / min(20, len(opt_sorted)) if opt_sorted else 0,
        "baseline_top20_avg": sum(r['utilization'] for r in base_sorted[:20]) / min(20, len(base_sorted)) if base_sorted else 0,
        "optuna_trials": len(_trial_results),
        "baseline_trials": len(baseline_results)
    }

    # Save results
    save_results(_trial_results, study, importances, comparison)


if __name__ == "__main__":
    main()
