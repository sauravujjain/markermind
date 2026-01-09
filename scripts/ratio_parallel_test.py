#!/usr/bin/env python3
"""
Parallel Nesting Test - Validates process-based parallelization with spyrrow.

Tests that we can run 6 nests simultaneously using ProcessPoolExecutor.
This is a quick validation before running the full experiment.

Usage:
    python scripts/ratio_parallel_test.py
"""

import multiprocessing as mp
import random
import time
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Dict, List, Tuple, Any

# Configuration
DXF_PATH = Path("data/24_2506_7_S-AN1000.DXF")
SIZES = ["XS", "S", "M", "L", "XL", "XXL"]
TIME_LIMIT = 30  # seconds per nest
NUM_WORKERS = 6
CONTAINER_WIDTH_MM = 1524.0  # 60 inches in mm

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
    # Import here to keep main process clean
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


def generate_random_size_mixes(n: int) -> List[Dict[str, int]]:
    """Generate n random size combinations (1-6 garments total)."""
    random.seed(42)  # Reproducible for testing
    mixes = []

    for _ in range(n):
        total = random.randint(1, 6)
        size_counts = {size: 0 for size in SIZES}

        for _ in range(total):
            size = random.choice(SIZES)
            size_counts[size] += 1

        # Remove zeros for cleaner output
        size_counts = {k: v for k, v in size_counts.items() if v > 0}
        mixes.append(size_counts)

    return mixes


def nest_worker(args: Tuple[int, Dict[str, int], int, Dict[str, Dict[str, List[Tuple[float, float]]]]]) -> Tuple[int, float, float, str]:
    """
    Worker function - imports happen inside each process.

    Args:
        args: (combo_id, size_mix, time_limit, piece_vertices_by_size)

    Returns:
        (combo_id, utilization, duration, error_msg or "")
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
            return combo_id, 0.0, time.time() - start_time, "No items created"

        # Create container (60 inches = 1524mm)
        container = Container(width=CONTAINER_WIDTH_MM, height=None)

        instance = NestingInstance.create(
            name=f"ParallelTest_{combo_id}",
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
        return combo_id, solution.utilization_percent, duration, ""

    except Exception as e:
        duration = time.time() - start_time
        return combo_id, 0.0, duration, str(e)


def main():
    print("=" * 70)
    print("PARALLEL NESTING TEST")
    print("=" * 70)
    print(f"DXF file: {DXF_PATH}")
    print(f"Workers: {NUM_WORKERS}")
    print(f"Time limit: {TIME_LIMIT}s per nest")
    print(f"Container width: {CONTAINER_WIDTH_MM}mm (60 inches)")
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

    # Generate 6 random size combinations
    size_mixes = generate_random_size_mixes(NUM_WORKERS)

    print("\nSize combinations to test:")
    for i, mix in enumerate(size_mixes):
        mix_str = ", ".join(f"{k}:{v}" for k, v in sorted(mix.items()))
        total = sum(mix.values())
        print(f"  {i+1}: {mix_str} ({total} garments)")

    # Prepare worker arguments
    worker_args = [
        (i, mix, TIME_LIMIT, piece_vertices)
        for i, mix in enumerate(size_mixes)
    ]

    # Run parallel nesting using spawn context
    print(f"\nStarting {NUM_WORKERS} parallel nests...")
    print("-" * 70)

    overall_start = time.time()

    # Use spawn context to avoid fork issues with spyrrow
    ctx = mp.get_context('spawn')

    with ProcessPoolExecutor(max_workers=NUM_WORKERS, mp_context=ctx) as executor:
        results = list(executor.map(nest_worker, worker_args))

    overall_duration = time.time() - overall_start

    # Print results
    print("\nRESULTS")
    print("-" * 70)
    print(f"{'ID':<4} {'Size Mix':<30} {'Util%':<10} {'Time':<10} {'Status':<15}")
    print("-" * 70)

    successful = 0
    for combo_id, utilization, duration, error in results:
        mix = size_mixes[combo_id]
        mix_str = ", ".join(f"{k}:{v}" for k, v in sorted(mix.items()))

        if error:
            status = f"ERROR: {error[:15]}"
        else:
            status = "OK"
            successful += 1

        time_str = f"{duration:.1f}s"
        print(f"{combo_id:<4} {mix_str:<30} {utilization:<10.2f} {time_str:<10} {status:<15}")

    print("-" * 70)
    print(f"\nSUMMARY")
    print(f"  Total time (parallel): {overall_duration:.1f}s")
    print(f"  Sequential would be:   ~{TIME_LIMIT * NUM_WORKERS}s")
    print(f"  Speedup:               ~{(TIME_LIMIT * NUM_WORKERS) / overall_duration:.1f}x")
    print(f"  Successful:            {successful}/{NUM_WORKERS}")

    if successful == NUM_WORKERS:
        print("\n[PASS] All parallel nests completed successfully!")
    else:
        print(f"\n[FAIL] {NUM_WORKERS - successful} nests failed")


if __name__ == "__main__":
    main()
