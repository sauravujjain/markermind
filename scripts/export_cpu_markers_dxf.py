#!/usr/bin/env python3
"""
Export CPU Nested Markers as DXF files.

Uses the SAME 30 combinations (seed=42) as the GPU raster experiment
for direct comparison.

Outputs:
    experiment_results/cpu_markers_dxf/
        marker_00_XS2_S2_M1_XXL1.dxf
        marker_01_XS1_XXL1.dxf
        ...

Usage:
    python scripts/export_cpu_markers_dxf.py
"""

import json
import math
import multiprocessing as mp
import random
import time
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Tuple, Any, Optional

import ezdxf
from ezdxf.enums import TextEntityAlignment

# Configuration - MUST match gpu_raster_experiment.py
DXF_PATH = Path("data/24_2506_7_S-AN1000.DXF")
OUTPUT_DIR = Path("experiment_results/cpu_markers_dxf")
RESULTS_DIR = Path("experiment_results")

SIZES = ["XS", "S", "M", "L", "XL", "XXL"]
NUM_COMBINATIONS = 30
RANDOM_SEED = 42  # Same seed as GPU experiment
TIME_LIMIT = 30  # seconds per CPU nest
NUM_WORKERS = 6
CONTAINER_WIDTH_MM = 1524.0  # 60 inches

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


def generate_combinations(n: int, seed: int = 42) -> List[Dict[str, int]]:
    """Generate n random size combinations (1-6 garments total)."""
    random.seed(seed)
    combinations = []

    for _ in range(n):
        total = random.randint(1, 6)
        size_counts = {size: 0 for size in SIZES}

        for _ in range(total):
            size = random.choice(SIZES)
            size_counts[size] += 1

        # Remove zeros for cleaner representation
        size_counts = {k: v for k, v in size_counts.items() if v > 0}
        combinations.append(size_counts)

    return combinations


def transform_vertices(
    vertices: List[Tuple[float, float]],
    x: float,
    y: float,
    rotation: float,
    flipped: bool = False
) -> List[Tuple[float, float]]:
    """
    Transform piece vertices with translation, rotation, and optional flip.

    The transformation order is:
    1. Flip (if needed) - around origin
    2. Rotate around origin
    3. Translate to final position
    """
    # First normalize to origin (get bounds)
    min_x = min(v[0] for v in vertices)
    min_y = min(v[1] for v in vertices)
    normalized = [(vx - min_x, vy - min_y) for vx, vy in vertices]

    # Get centroid of normalized piece
    cx = sum(v[0] for v in normalized) / len(normalized)
    cy = sum(v[1] for v in normalized) / len(normalized)

    result = []
    angle_rad = math.radians(rotation)
    cos_a = math.cos(angle_rad)
    sin_a = math.sin(angle_rad)

    for vx, vy in normalized:
        # Center on origin
        px = vx - cx
        py = vy - cy

        # Flip if needed
        if flipped:
            px = -px

        # Rotate around origin
        rx = px * cos_a - py * sin_a
        ry = px * sin_a + py * cos_a

        # Translate to final position
        final_x = rx + x
        final_y = ry + y

        result.append((final_x, final_y))

    return result


def nest_and_export_worker(args: Tuple) -> Tuple[int, Dict[str, int], float, float, str, str]:
    """
    Worker function - runs nesting and exports DXF.
    """
    combo_id, size_mix, time_limit, piece_vertices_by_size, output_dir = args

    start_time = time.time()

    try:
        from nesting_engine.engine.spyrrow_engine import SpyrrowEngine, SpyrrowConfig
        from nesting_engine.core.instance import Container, NestingItem, NestingInstance, FlipMode
        from nesting_engine.core.piece import Piece, PieceIdentifier, OrientationConstraint

        # Build pieces from primitive vertex data
        items = []
        piece_registry = {}  # Store piece info for DXF export

        for size, count in size_mix.items():
            if count == 0 or size not in piece_vertices_by_size:
                continue

            for piece_type, config in PIECE_CONFIG.items():
                if piece_type not in piece_vertices_by_size[size]:
                    continue

                vertices = piece_vertices_by_size[size][piece_type]
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

                total_demand = config["demand"] * count
                flip_mode = FlipMode.PAIRED if config["flip"] else FlipMode.NONE

                item = NestingItem(
                    piece=piece,
                    demand=total_demand,
                    flip_mode=flip_mode
                )
                items.append(item)

                # Store for DXF export
                piece_registry[piece_id] = {
                    "vertices": vertices,
                    "size": size,
                    "piece_type": piece_type
                }

        if not items:
            return combo_id, size_mix, 0.0, 0.0, "", "No items created"

        # Create container (60 inches = 1524mm)
        container = Container(width=CONTAINER_WIDTH_MM, height=None)

        instance = NestingInstance.create(
            name=f"CPUMarker_{combo_id}",
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

        # Generate filename from size mix
        mix_str = "_".join(f"{s}{n}" for s, n in sorted(size_mix.items()))
        filename = f"marker_{combo_id:02d}_{mix_str}.dxf"
        filepath = Path(output_dir) / filename

        # Export to DXF
        export_solution_to_dxf(
            filepath=filepath,
            solution=solution,
            piece_registry=piece_registry,
            container_width=CONTAINER_WIDTH_MM,
            combo_id=combo_id,
            size_mix=size_mix
        )

        return combo_id, size_mix, solution.utilization_percent, solution.strip_length, str(filepath), ""

    except Exception as e:
        import traceback
        duration = time.time() - start_time
        return combo_id, size_mix, 0.0, 0.0, "", f"{str(e)}\n{traceback.format_exc()}"


def export_solution_to_dxf(
    filepath: Path,
    solution: Any,
    piece_registry: Dict,
    container_width: float,
    combo_id: int,
    size_mix: Dict[str, int]
):
    """
    Export nesting solution to DXF file.
    """
    # Create new DXF document
    doc = ezdxf.new('R2010')
    msp = doc.modelspace()

    # Set up layers with colors
    colors = {
        "BK": 1,    # Red
        "FRT": 3,   # Green
        "SL": 5,    # Blue
    }

    for piece_type, color in colors.items():
        doc.layers.add(piece_type, color=color)

    doc.layers.add("CONTAINER", color=7)  # White
    doc.layers.add("INFO", color=7)

    # Draw container rectangle
    strip_length = solution.strip_length
    container_pts = [
        (0, 0),
        (strip_length, 0),
        (strip_length, container_width),
        (0, container_width),
        (0, 0)
    ]
    msp.add_lwpolyline(container_pts, dxfattribs={"layer": "CONTAINER"})

    # Draw each placed piece
    for placement in solution.placements:
        piece_id = placement.piece_id

        # Find piece info - piece_id format is "TYPE_SIZE_COMBOID"
        if piece_id not in piece_registry:
            # Try without _flipped suffix
            base_id = piece_id.replace("_flipped", "")
            if base_id not in piece_registry:
                continue
            piece_info = piece_registry[base_id]
        else:
            piece_info = piece_registry[piece_id]

        vertices = piece_info["vertices"]
        piece_type = piece_info["piece_type"]

        # Transform vertices
        transformed = transform_vertices(
            vertices,
            placement.x,
            placement.y,
            placement.rotation,
            placement.flipped
        )

        # Close the polygon
        if transformed[0] != transformed[-1]:
            transformed.append(transformed[0])

        # Add to DXF
        layer = piece_type if piece_type in colors else "0"
        msp.add_lwpolyline(transformed, dxfattribs={"layer": layer})

    # Add info text
    mix_str = ", ".join(f"{s}:{n}" for s, n in sorted(size_mix.items()))
    info_text = f"Combo {combo_id}: {mix_str} | Util: {solution.utilization_percent:.1f}% | Length: {strip_length:.1f}mm"
    msp.add_text(
        info_text,
        dxfattribs={
            "layer": "INFO",
            "height": 30,
            "insert": (10, container_width + 50)
        }
    )

    # Save
    filepath.parent.mkdir(parents=True, exist_ok=True)
    doc.saveas(str(filepath))


def run_export():
    """Main export function."""
    print("=" * 80)
    print("CPU MARKER DXF EXPORT")
    print("Using same 30 combinations (seed=42) as GPU experiment")
    print("=" * 80)

    # Setup
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    print(f"\nConfiguration:")
    print(f"  DXF source: {DXF_PATH}")
    print(f"  Output dir: {OUTPUT_DIR}")
    print(f"  Combinations: {NUM_COMBINATIONS} (seed={RANDOM_SEED})")
    print(f"  CPU time limit: {TIME_LIMIT}s")
    print(f"  Workers: {NUM_WORKERS}")

    # Check DXF exists
    if not DXF_PATH.exists():
        print(f"\nERROR: DXF file not found at {DXF_PATH}")
        return

    # Load pieces
    print(f"\n" + "-" * 80)
    piece_vertices = load_piece_vertices(DXF_PATH)

    if not piece_vertices:
        print("ERROR: No valid pieces loaded from DXF")
        return

    # Generate combinations (SAME as GPU experiment)
    combinations = generate_combinations(NUM_COMBINATIONS, seed=RANDOM_SEED)
    print(f"\nGenerated {len(combinations)} combinations (matching GPU experiment)")

    # Prepare worker arguments
    worker_args = [
        (i, combo, TIME_LIMIT, piece_vertices, str(OUTPUT_DIR))
        for i, combo in enumerate(combinations)
    ]

    # Run parallel nesting and export
    print(f"\n" + "-" * 80)
    print(f"Running CPU nests and exporting DXF ({NUM_WORKERS} workers)...")
    print("-" * 80)

    ctx = mp.get_context('spawn')
    results = []

    with ProcessPoolExecutor(max_workers=NUM_WORKERS, mp_context=ctx) as executor:
        futures = {executor.submit(nest_and_export_worker, args): args[0] for args in worker_args}

        for future in as_completed(futures):
            combo_id, size_mix, utilization, strip_length, filepath, error = future.result()

            mix_str = ", ".join(f"{s}:{n}" for s, n in sorted(size_mix.items()))

            if error:
                print(f"  [{combo_id+1:2d}/{NUM_COMBINATIONS}] {mix_str:<35} ERROR: {error[:50]}")
            else:
                print(f"  [{combo_id+1:2d}/{NUM_COMBINATIONS}] {mix_str:<35} -> {utilization:5.1f}% -> {Path(filepath).name}")

            results.append({
                "combo_id": combo_id,
                "size_mix": size_mix,
                "utilization": utilization,
                "strip_length": strip_length,
                "filepath": filepath,
                "error": error
            })

    # Sort results by combo_id
    results.sort(key=lambda x: x["combo_id"])

    # Summary
    print(f"\n" + "=" * 80)
    print("EXPORT COMPLETE")
    print("=" * 80)

    successful = [r for r in results if not r["error"]]
    failed = [r for r in results if r["error"]]

    print(f"\nSuccessful: {len(successful)}/{len(results)}")
    print(f"Failed: {len(failed)}/{len(results)}")

    if successful:
        utils = [r["utilization"] for r in successful]
        print(f"\nUtilization range: {min(utils):.1f}% - {max(utils):.1f}%")
        print(f"Average: {sum(utils)/len(utils):.1f}%")

    print(f"\nDXF files saved to: {OUTPUT_DIR}/")

    # Save results JSON
    results_file = RESULTS_DIR / "cpu_markers_export.json"
    with open(results_file, 'w') as f:
        json.dump({
            "timestamp": datetime.now().isoformat(),
            "dxf_source": str(DXF_PATH),
            "output_dir": str(OUTPUT_DIR),
            "random_seed": RANDOM_SEED,
            "num_combinations": NUM_COMBINATIONS,
            "results": results
        }, f, indent=2)

    print(f"Results saved to: {results_file}")


if __name__ == '__main__':
    run_export()
