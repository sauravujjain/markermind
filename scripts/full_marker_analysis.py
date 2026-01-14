#!/usr/bin/env python3
"""
Full Marker Analysis: GPU screening + CPU refinement

1. Generate all marker combinations (1-6 bundles)
2. Run fast GPU raster nesting on all
3. Select top 3 for each bundle count
4. Run CPU Spyrrow nesting on top picks (60s each)
5. Save results
"""

import sys
import json
import time
from pathlib import Path
from datetime import datetime
from itertools import combinations_with_replacement

# Add parent to path
sys.path.insert(0, str(Path(__file__).parent.parent))

import numpy as np

# Import GPU nesting functions from v9
from scripts.gpu_raster_experiment_v9 import (
    load_piece_vertices, prepare_pieces_for_combination, pack_strip,
    STRIP_WIDTH_PX, STRIP_WIDTH_INCH, GLOBAL_SCALE, DXF_PATH
)

# Import CPU nesting
from nesting_engine.engine.spyrrow_engine import SpyrrowEngine, SpyrrowConfig
from nesting_engine.core.piece import Piece, PieceIdentifier
from nesting_engine.core.instance import Container, NestingItem, NestingInstance, FlipMode
from nesting_engine.io.dxf_parser import load_pieces_from_dxf

# Configuration
SIZES = ['XS', 'S', 'M', 'L', 'XL', 'XXL']
MAX_BUNDLES = 6
CPU_TIME_LIMIT = 60  # seconds per marker
TOP_N = 3  # top markers per bundle count

OUTPUT_DIR = Path("experiment_results/full_marker_analysis")


def generate_all_combinations(max_bundles: int = 6):
    """Generate all possible marker combinations from 1 to max_bundles."""
    all_combos = []

    for n_bundles in range(1, max_bundles + 1):
        # Generate combinations with replacement
        for combo in combinations_with_replacement(SIZES, n_bundles):
            # Convert to dict format
            size_counts = {}
            for size in combo:
                size_counts[size] = size_counts.get(size, 0) + 1
            all_combos.append({
                'n_bundles': n_bundles,
                'combination': size_counts,
                'combo_str': ', '.join(f"{s}:{n}" for s, n in sorted(size_counts.items()))
            })

    return all_combos


def run_gpu_screening(combinations, piece_vertices):
    """Run fast GPU nesting on all combinations."""
    print(f"\n{'='*60}")
    print(f"GPU SCREENING: {len(combinations)} combinations")
    print(f"{'='*60}")

    results = []
    start_time = time.time()

    for i, combo_info in enumerate(combinations):
        combo = combo_info['combination']
        n_bundles = combo_info['n_bundles']

        t0 = time.time()

        # Prepare pieces
        pieces = prepare_pieces_for_combination(piece_vertices, combo)

        if not pieces:
            results.append({
                **combo_info,
                'efficiency': 0.0,
                'length_inch': 0.0,
                'time_ms': 0,
                'num_pieces': 0
            })
            continue

        # Run GPU packing
        efficiency, placements, container, strip_area, strip_length = pack_strip(pieces)

        duration_ms = (time.time() - t0) * 1000
        length_inch = strip_length / GLOBAL_SCALE / 25.4

        results.append({
            **combo_info,
            'efficiency': efficiency * 100,
            'length_inch': length_inch,
            'time_ms': duration_ms,
            'num_pieces': len(placements)
        })

        # Progress update every 50 combinations
        if (i + 1) % 50 == 0 or i == len(combinations) - 1:
            elapsed = time.time() - start_time
            rate = (i + 1) / elapsed
            remaining = (len(combinations) - i - 1) / rate if rate > 0 else 0
            print(f"  [{i+1}/{len(combinations)}] {elapsed:.1f}s elapsed, ~{remaining:.1f}s remaining")

    total_time = time.time() - start_time
    print(f"\nGPU screening complete: {len(combinations)} combinations in {total_time:.1f}s")
    print(f"Average: {total_time/len(combinations)*1000:.1f}ms per marker")

    return results


def select_top_markers(results, top_n=3):
    """Select top N markers for each bundle count."""
    top_markers = {}

    for n_bundles in range(1, MAX_BUNDLES + 1):
        # Filter by bundle count
        bundle_results = [r for r in results if r['n_bundles'] == n_bundles]

        # Sort by efficiency descending
        bundle_results.sort(key=lambda x: -x['efficiency'])

        # Take top N
        top_markers[n_bundles] = bundle_results[:top_n]

    return top_markers


def run_cpu_nesting(combo_info, piece_vertices_raw, time_limit=60):
    """Run CPU Spyrrow nesting on a single combination."""
    combo = combo_info['combination']

    # Load pieces from DXF for CPU nesting
    all_pieces, _ = load_pieces_from_dxf(str(DXF_PATH))

    # Group pieces by size
    pieces_by_size = {}
    for piece in all_pieces:
        size = piece.identifier.size
        if size not in pieces_by_size:
            pieces_by_size[size] = []
        pieces_by_size[size].append(piece)

    # Create nesting items based on combination
    items = []
    for size, count in combo.items():
        if size in pieces_by_size:
            for piece in pieces_by_size[size]:
                items.append(NestingItem(
                    piece=piece,
                    demand=count,
                    flip_mode=FlipMode.NONE
                ))

    if not items:
        return None

    # Create container (60" width, strip packing)
    container = Container(
        width=STRIP_WIDTH_INCH * 25.4,  # Convert to mm
        height=None  # Strip packing
    )

    # Create nesting instance
    instance = NestingInstance.create(
        name=f"Marker_{combo_info['combo_str']}",
        container=container,
        items=items,
        piece_buffer=2.0,
        edge_buffer=5.0
    )

    # Run Spyrrow
    engine = SpyrrowEngine()
    config = SpyrrowConfig(time_limit=time_limit)

    t0 = time.time()
    solution = engine.solve(instance, config=config)
    cpu_time = time.time() - t0

    if solution:
        # Use solution's built-in metrics
        efficiency = solution.utilization_percent
        length_inch = solution.strip_length / 25.4

        return {
            'efficiency': efficiency,
            'length_inch': length_inch,
            'time_s': cpu_time,
            'num_placements': len(solution.placements)
        }

    return None


def run_cpu_refinement(top_markers, piece_vertices):
    """Run CPU nesting on top markers."""
    print(f"\n{'='*60}")
    print(f"CPU REFINEMENT: Top {TOP_N} markers per bundle count")
    print(f"Time limit: {CPU_TIME_LIMIT}s per marker")
    print(f"{'='*60}")

    cpu_results = {}
    total_markers = sum(len(markers) for markers in top_markers.values())
    processed = 0

    for n_bundles in range(1, MAX_BUNDLES + 1):
        print(f"\n--- {n_bundles} Bundle(s) ---")
        cpu_results[n_bundles] = []

        for i, marker in enumerate(top_markers[n_bundles]):
            processed += 1
            print(f"  [{processed}/{total_markers}] {marker['combo_str']} (GPU: {marker['efficiency']:.1f}%)")

            try:
                result = run_cpu_nesting(marker, piece_vertices, CPU_TIME_LIMIT)

                if result:
                    cpu_results[n_bundles].append({
                        **marker,
                        'cpu_efficiency': result['efficiency'],
                        'cpu_length_inch': result['length_inch'],
                        'cpu_time_s': result['time_s']
                    })
                    print(f"      -> CPU: {result['efficiency']:.1f}% ({result['time_s']:.1f}s)")
                else:
                    cpu_results[n_bundles].append({
                        **marker,
                        'cpu_efficiency': None,
                        'cpu_length_inch': None,
                        'cpu_time_s': None
                    })
                    print(f"      -> CPU: FAILED")
            except Exception as e:
                print(f"      -> CPU: ERROR - {e}")
                cpu_results[n_bundles].append({
                    **marker,
                    'cpu_efficiency': None,
                    'cpu_error': str(e)
                })

    return cpu_results


def main():
    print("="*60)
    print("FULL MARKER ANALYSIS")
    print("="*60)

    # Setup output directory
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # Check if GPU results already exist
    gpu_results_file = OUTPUT_DIR / 'gpu_screening_results.json'

    if gpu_results_file.exists():
        print("\nLoading existing GPU screening results...")
        with open(gpu_results_file, 'r') as f:
            gpu_output = json.load(f)
        gpu_results = gpu_output['results']
        combinations = gpu_results  # Use results as combinations
        print(f"Loaded {len(gpu_results)} GPU results from cache")
    else:
        # Generate all combinations
        print("\nGenerating combinations...")
        combinations = generate_all_combinations(MAX_BUNDLES)

        combo_counts = {}
        for c in combinations:
            n = c['n_bundles']
            combo_counts[n] = combo_counts.get(n, 0) + 1

        print(f"Total combinations: {len(combinations)}")
        for n, count in sorted(combo_counts.items()):
            print(f"  {n} bundle(s): {count}")

        # Estimate time
        est_gpu_time = len(combinations) * 0.126  # 126ms average
        est_cpu_time = TOP_N * MAX_BUNDLES * CPU_TIME_LIMIT
        print(f"\nEstimated time:")
        print(f"  GPU screening: ~{est_gpu_time:.0f}s ({est_gpu_time/60:.1f} min)")
        print(f"  CPU refinement: ~{est_cpu_time:.0f}s ({est_cpu_time/60:.1f} min)")
        print(f"  Total: ~{(est_gpu_time + est_cpu_time)/60:.1f} min")

        # Load piece vertices
        print(f"\nLoading pieces from {DXF_PATH}...")
        piece_vertices = load_piece_vertices(DXF_PATH)

        if not piece_vertices:
            print("ERROR: No pieces loaded")
            return

        # Run GPU screening
        gpu_results = run_gpu_screening(combinations, piece_vertices)

        # Save GPU results
        gpu_output = {
            'metadata': {
                'timestamp': datetime.now().isoformat(),
                'total_combinations': len(combinations),
                'strip_width_inch': STRIP_WIDTH_INCH,
                'resolution': GLOBAL_SCALE
            },
            'results': gpu_results
        }

        with open(gpu_results_file, 'w') as f:
            json.dump(gpu_output, f, indent=2)
        print(f"\nGPU results saved to {gpu_results_file}")

    # Select top markers
    top_markers = select_top_markers(gpu_results, TOP_N)

    print(f"\n{'='*60}")
    print("TOP MARKERS BY GPU EFFICIENCY")
    print(f"{'='*60}")

    for n_bundles in range(1, MAX_BUNDLES + 1):
        print(f"\n{n_bundles} Bundle(s):")
        for i, m in enumerate(top_markers[n_bundles], 1):
            print(f"  #{i}: {m['combo_str']:<25} {m['efficiency']:.1f}% (L={m['length_inch']:.1f}\")")

    # Run CPU refinement
    cpu_results = run_cpu_refinement(top_markers, None)

    # Save final results
    final_output = {
        'metadata': {
            'timestamp': datetime.now().isoformat(),
            'gpu_combinations': len(combinations),
            'cpu_time_limit': CPU_TIME_LIMIT,
            'top_n': TOP_N,
            'strip_width_inch': STRIP_WIDTH_INCH
        },
        'top_markers': cpu_results
    }

    with open(OUTPUT_DIR / 'final_results.json', 'w') as f:
        json.dump(final_output, f, indent=2)

    # Print final summary
    print(f"\n{'='*60}")
    print("FINAL RESULTS: GPU vs CPU")
    print(f"{'='*60}")

    for n_bundles in range(1, MAX_BUNDLES + 1):
        print(f"\n{n_bundles} Bundle(s):")
        print(f"  {'Combo':<25} {'GPU %':<10} {'CPU %':<10} {'Diff':<10}")
        print(f"  {'-'*55}")
        for m in cpu_results[n_bundles]:
            gpu_eff = m['efficiency']
            cpu_eff = m.get('cpu_efficiency')
            if cpu_eff is not None:
                diff = cpu_eff - gpu_eff
                print(f"  {m['combo_str']:<25} {gpu_eff:<10.1f} {cpu_eff:<10.1f} {diff:+.1f}")
            else:
                print(f"  {m['combo_str']:<25} {gpu_eff:<10.1f} {'N/A':<10}")

    print(f"\n\nResults saved to: {OUTPUT_DIR}")
    print(f"  - gpu_screening_results.json (all {len(combinations)} combinations)")
    print(f"  - final_results.json (top {TOP_N} per bundle with CPU results)")


if __name__ == "__main__":
    main()
