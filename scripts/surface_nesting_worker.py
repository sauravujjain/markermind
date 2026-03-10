#!/usr/bin/env python3
"""
Nesting worker for Surface Laptop 2.

Modes:
  CLI batch:  python3 surface_nesting_worker.py jobs.json results.json
  Stdin pipe: python3 surface_nesting_worker.py --stdin
              (reads single job JSON from stdin, writes result JSON to stdout)

Reads job configs from JSON, runs spyrrow nesting, writes results to JSON.
Writes intermediate results after each job so progress can be monitored.
"""
import sys
import json
import time
import argparse
import logging
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stderr,  # Log to stderr so stdout is clean for --stdin mode
)
log = logging.getLogger("nesting-worker")

try:
    import spyrrow as sp
except ImportError:
    log.error("spyrrow not installed. Run: pip install spyrrow==0.8.1")
    sys.exit(1)


# ---------------------------------------------------------------------------
# Core nesting logic (shared across CLI / stdin / API modes)
# ---------------------------------------------------------------------------

def run_nest(job: dict) -> dict:
    """Run a single nesting job and return results dict with placements."""
    pieces = job["pieces"]
    strip_width = job["strip_width_mm"]
    config = job["config"]
    label = job.get("label", "unknown")

    # Build spyrrow items
    items = []
    for i, p in enumerate(pieces):
        verts = [tuple(v) for v in p["vertices"]]
        item = sp.Item(
            str(i),
            verts,
            demand=p.get("demand", 1),
            allowed_orientations=p.get("allowed_orientations", [0.0, 180.0]),
        )
        items.append(item)

    instance = sp.StripPackingInstance("marker", strip_height=strip_width, items=items)

    # Build config
    time_kwargs = {}
    if (
        config.get("exploration_time") is not None
        and config.get("compression_time") is not None
    ):
        time_kwargs["total_computation_time"] = None
        time_kwargs["exploration_time"] = config["exploration_time"]
        time_kwargs["compression_time"] = config["compression_time"]
    else:
        time_kwargs["total_computation_time"] = int(config.get("time_limit_s", 60))

    sp_config = sp.StripPackingConfig(
        early_termination=config.get("early_termination", False),
        **time_kwargs,
        num_workers=config.get("num_workers") or None,
        seed=config.get("seed", 42),
        quadtree_depth=config.get("quadtree_depth", 4),
        min_items_separation=config.get("min_items_separation"),
    )

    t0 = time.time()
    solution = instance.solve(sp_config)
    elapsed = time.time() - t0

    # Compute utilization via shoelace formula
    total_area = 0.0
    for p in pieces:
        verts = p["vertices"]
        n = len(verts)
        area = 0.0
        for j in range(n):
            x1, y1 = verts[j]
            x2, y2 = verts[(j + 1) % n]
            area += x1 * y2 - x2 * y1
        total_area += abs(area) / 2.0

    strip_length = solution.width
    utilization = total_area / (strip_width * strip_length) if strip_length > 0 else 0.0

    # Extract placements for SVG/DXF rendering on the caller side
    placements = []
    for placed in solution.placed_items:
        tx, ty = placed.translation
        placements.append({
            "piece_index": int(placed.id),
            "x": round(tx, 4),
            "y": round(ty, 4),
            "rotation": round(placed.rotation, 4),
        })

    return {
        "label": label,
        "utilization": round(utilization, 6),
        "strip_length_mm": round(strip_length, 2),
        "length_yards": round(strip_length / 914.4, 4),
        "computation_time_s": round(elapsed, 1),
        "placements": placements,
    }


def run_batch(jobs: list[dict], results_file: str | None = None) -> list[dict]:
    """Run a batch of nesting jobs. Writes intermediate results if results_file given."""
    log.info("Starting batch: %d jobs", len(jobs))
    results = []

    for i, job in enumerate(jobs):
        label = job.get("label", f"job-{i}")
        log.info("[%d/%d] %s ...", i + 1, len(jobs), label)

        try:
            result = run_nest(job)
            results.append(result)
            log.info(
                "  eff=%.2f%%  %.4f yd  %.0fs",
                result["utilization"] * 100,
                result["length_yards"],
                result["computation_time_s"],
            )
        except Exception as e:
            results.append({"label": label, "error": str(e)})
            log.error("  ERROR: %s", e)

        # Write intermediate results
        if results_file:
            Path(results_file).write_text(json.dumps(results, indent=2))

    log.info("Batch complete: %d results", len(results))
    return results


# ---------------------------------------------------------------------------
# CLI modes
# ---------------------------------------------------------------------------

def cli_main():
    parser = argparse.ArgumentParser(description="Surface nesting worker")
    parser.add_argument("--stdin", action="store_true",
                        help="Read single job from stdin, write result to stdout")
    parser.add_argument("jobs_file", nargs="?", help="Input JSON file with job definitions")
    parser.add_argument("results_file", nargs="?", help="Output JSON file for results")
    args = parser.parse_args()

    if args.stdin:
        # Pipe mode: read job JSON from stdin, write result JSON to stdout
        job = json.load(sys.stdin)
        result = run_nest(job)
        json.dump(result, sys.stdout)
        sys.stdout.flush()
        return

    if not args.jobs_file or not args.results_file:
        parser.error("jobs_file and results_file are required (or use --stdin)")

    with open(args.jobs_file) as f:
        jobs = json.load(f)

    results = run_batch(jobs, args.results_file)

    # Final write
    Path(args.results_file).write_text(json.dumps(results, indent=2))
    log.info("Results written to %s", args.results_file)


if __name__ == "__main__":
    cli_main()
