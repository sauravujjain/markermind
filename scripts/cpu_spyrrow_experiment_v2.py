#!/usr/bin/env python3
"""
CPU Spyrrow Baseline Experiment - V2 (Fixed)

Uses same 10 test combinations as GPU experiment for fair comparison.
Properly cleans vertices before passing to Spyrrow.

Usage:
    PYTHONPATH=. python scripts/cpu_spyrrow_experiment_v2.py
"""

import json
import multiprocessing as mp
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from typing import Dict, List, Tuple, Any

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from scripts.experiment_utils import (
    load_piece_vertices, TEST_COMBINATIONS, count_expected_pieces,
    combo_to_string, combo_to_filename, PIECE_CONFIG
)

# Configuration
DXF_PATH = Path("data/24_2506_7_S-AN1000.DXF")
OUTPUT_DIR = Path("experiment_results/gpu_vs_cpu_v2")
CONTAINER_WIDTH_MM = 1524.0  # 60 inches
TIME_LIMIT = 30  # seconds per nest
NUM_WORKERS = 6


def nest_worker(args: Tuple) -> Dict[str, Any]:
    """
    Worker function for parallel nesting.

    Runs in separate process to allow true parallelism.
    """
    combo_id, combo, time_limit, piece_vertices = args

    start_time = time.time()

    try:
        from nesting_engine.engine.spyrrow_engine import SpyrrowEngine, SpyrrowConfig
        from nesting_engine.core.instance import Container, NestingItem, NestingInstance, FlipMode
        from nesting_engine.core.piece import Piece, PieceIdentifier, OrientationConstraint

        items = []
        piece_areas = {}

        for size, garment_count in combo.items():
            if garment_count == 0 or size not in piece_vertices:
                continue

            for piece_type, config in PIECE_CONFIG.items():
                if piece_type not in piece_vertices.get(size, {}):
                    continue

                # Vertices are already cleaned by load_piece_vertices
                vertices = piece_vertices[size][piece_type]

                piece_id = f"{piece_type}_{size}_{combo_id}"

                identifier = PieceIdentifier(
                    piece_name=piece_id,
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

                total_demand = config["demand"] * garment_count
                flip_mode = FlipMode.PAIRED if config["flip"] else FlipMode.NONE

                item = NestingItem(
                    piece=piece,
                    demand=total_demand,
                    flip_mode=flip_mode
                )
                items.append(item)
                piece_areas[piece_id] = piece.area

        if not items:
            return {
                "combo_id": combo_id,
                "combo": combo,
                "utilization": 0.0,
                "placed_pieces": 0,
                "expected_pieces": count_expected_pieces(combo),
                "strip_length": 0.0,
                "time": time.time() - start_time,
                "error": "No items created"
            }

        container = Container(width=CONTAINER_WIDTH_MM, height=None)

        instance = NestingInstance.create(
            name=f"CPUTest_{combo_id}",
            container=container,
            items=items,
            piece_buffer=2.0,
            edge_buffer=5.0
        )

        engine = SpyrrowEngine()
        config = SpyrrowConfig(time_limit=time_limit)
        solution = engine.solve(instance, config=config)

        duration = time.time() - start_time

        return {
            "combo_id": combo_id,
            "combo": combo,
            "utilization": solution.utilization_percent,
            "placed_pieces": len(solution.placements),
            "expected_pieces": count_expected_pieces(combo),
            "strip_length": solution.strip_length,
            "time": duration,
            "error": ""
        }

    except Exception as e:
        import traceback
        return {
            "combo_id": combo_id,
            "combo": combo,
            "utilization": 0.0,
            "placed_pieces": 0,
            "expected_pieces": count_expected_pieces(combo),
            "strip_length": 0.0,
            "time": time.time() - start_time,
            "error": f"{str(e)}\n{traceback.format_exc()}"
        }


def run_cpu_experiment():
    """Main CPU Spyrrow experiment."""
    print("=" * 80)
    print("CPU SPYRROW BASELINE EXPERIMENT - V2")
    print("=" * 80)

    print(f"\nConfiguration:")
    print(f"  Time limit: {TIME_LIMIT}s per nest")
    print(f"  Workers: {NUM_WORKERS}")
    print(f"  Container width: {CONTAINER_WIDTH_MM}mm ({CONTAINER_WIDTH_MM/25.4:.1f} inches)")
    print(f"  Test combinations: {len(TEST_COMBINATIONS)}")

    # Check DXF exists
    if not DXF_PATH.exists():
        print(f"\nERROR: DXF file not found at {DXF_PATH}")
        return None

    # Load pieces with vertex cleaning
    print(f"\nLoading pieces from {DXF_PATH}...")
    piece_vertices = load_piece_vertices(DXF_PATH)

    if not piece_vertices:
        print("ERROR: No valid pieces loaded from DXF")
        return None

    total_pieces = sum(len(types) for types in piece_vertices.values())
    print(f"  Loaded {total_pieces} unique pieces across {len(piece_vertices)} sizes")

    # Show piece info
    print(f"\n  Pieces per size:")
    for size in sorted(piece_vertices.keys()):
        types = list(piece_vertices[size].keys())
        print(f"    {size}: {', '.join(types)}")

    # Prepare worker arguments
    worker_args = [
        (i, combo, TIME_LIMIT, piece_vertices)
        for i, combo in enumerate(TEST_COMBINATIONS)
    ]

    # Run parallel nesting
    print(f"\n" + "-" * 80)
    print(f"Running CPU nests ({NUM_WORKERS} workers, {TIME_LIMIT}s each)...")
    print("-" * 80)

    ctx = mp.get_context('spawn')
    results = []

    with ProcessPoolExecutor(max_workers=NUM_WORKERS, mp_context=ctx) as executor:
        futures = {executor.submit(nest_worker, args): args[0] for args in worker_args}

        completed = 0
        for future in as_completed(futures):
            result = future.result()
            completed += 1

            combo_id = result["combo_id"]
            combo = result["combo"]
            util = result["utilization"]
            placed = result["placed_pieces"]
            expected = result["expected_pieces"]
            duration = result["time"]
            error = result["error"]

            combo_str = combo_to_string(combo)

            if error:
                print(f"  [{combo_id+1:2d}/{len(TEST_COMBINATIONS)}] {combo_str:<35} -> ERROR: {error[:60]}")
            else:
                if placed == expected:
                    status = "OK"
                else:
                    status = f"PARTIAL ({placed}/{expected})"
                print(f"  [{combo_id+1:2d}/{len(TEST_COMBINATIONS)}] {combo_str:<35} -> {util:5.1f}% | {status} | {duration:.1f}s")

            results.append(result)

    # Sort by combo_id
    results.sort(key=lambda x: x["combo_id"])

    # Summary
    print(f"\n" + "=" * 80)
    print("CPU EXPERIMENT SUMMARY")
    print("=" * 80)

    successful = [r for r in results if not r["error"] and r["placed_pieces"] == r["expected_pieces"]]
    partial = [r for r in results if not r["error"] and 0 < r["placed_pieces"] < r["expected_pieces"]]
    errored = [r for r in results if r["error"]]

    print(f"\n  Fully placed:  {len(successful)}/{len(results)}")
    print(f"  Partial:       {len(partial)}/{len(results)}")
    print(f"  Errors:        {len(errored)}/{len(results)}")

    if successful:
        utils = [r["utilization"] for r in successful]
        print(f"\n  Utilization (successful): {min(utils):.1f}% - {max(utils):.1f}% (avg: {sum(utils)/len(utils):.1f}%)")

    total_time = sum(r["time"] for r in results)
    print(f"\n  Total time: {total_time:.1f}s ({total_time/len(results):.1f}s avg)")

    # Report errors
    if errored:
        print(f"\n  Errors:")
        for r in errored:
            combo_str = combo_to_string(r["combo"])
            print(f"    Combo {r['combo_id']} ({combo_str}): {r['error'][:100]}")

    # Save results
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    results_file = OUTPUT_DIR / "cpu_results.json"

    with open(results_file, "w") as f:
        json.dump({
            "metadata": {
                "time_limit": TIME_LIMIT,
                "num_workers": NUM_WORKERS,
                "container_width_mm": CONTAINER_WIDTH_MM,
                "num_combinations": len(TEST_COMBINATIONS)
            },
            "results": results
        }, f, indent=2)

    print(f"\nResults saved to: {results_file}")

    return results


if __name__ == "__main__":
    run_cpu_experiment()
