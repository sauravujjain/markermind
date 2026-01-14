"""
Shared utilities for GPU/CPU nesting experiments.

This module provides common functions used by both GPU rasterization
and CPU Spyrrow experiments to ensure consistent behavior.
"""

import random
from pathlib import Path
from typing import Dict, List, Tuple, Optional
from collections import defaultdict

SIZES = ["XS", "S", "M", "L", "XL", "XXL"]

PIECE_CONFIG = {
    "BK": {"demand": 1, "flip": False},
    "FRT": {"demand": 1, "flip": False},
    "SL": {"demand": 2, "flip": True},  # left/right pair
}


def clean_vertices(vertices: List[Tuple[float, float]]) -> List[Tuple[float, float]]:
    """
    Remove duplicate vertices that cause spyrrow to fail.
    Matches DXFParser._clean_vertices() exactly.

    Args:
        vertices: List of (x, y) coordinate tuples

    Returns:
        Cleaned list of vertices with duplicates removed
    """
    if len(vertices) < 3:
        return vertices

    cleaned = []
    seen_approx = set()

    for x, y in vertices:
        # Round to 3 decimal places for duplicate detection (~0.001mm precision)
        key = (round(x, 3), round(y, 3))
        if key not in seen_approx:
            seen_approx.add(key)
            cleaned.append((x, y))

    return cleaned


def extract_piece_type(piece_name: str) -> Optional[str]:
    """Extract piece type (BK, FRT, SL) from piece name."""
    name_upper = piece_name.upper()
    for piece_type in PIECE_CONFIG.keys():
        if piece_type in name_upper:
            return piece_type
    return None


def load_piece_vertices(dxf_path: Path) -> Dict[str, Dict[str, List[Tuple[float, float]]]]:
    """
    Load pieces from DXF and extract CLEANED vertices.

    Args:
        dxf_path: Path to DXF file

    Returns:
        {size: {piece_type: [(x, y), ...]}}
    """
    from nesting_engine.io.dxf_parser import DXFParser
    from nesting_engine.core.units import LengthUnit

    parser = DXFParser(str(dxf_path))
    result = parser.parse()

    # Determine conversion factor
    if result.unit == LengthUnit.INCH:
        to_mm = 25.4
    elif result.unit == LengthUnit.CENTIMETER:
        to_mm = 10.0
    elif result.unit == LengthUnit.METER:
        to_mm = 1000.0
    else:
        to_mm = 1.0  # Assume mm

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
            continue  # Skip duplicates

        # Convert to mm
        vertices_mm = [(x * to_mm, y * to_mm) for x, y in parsed.vertices]

        # CRITICAL: Clean vertices using same logic as DXFParser
        vertices_mm = clean_vertices(vertices_mm)

        if len(vertices_mm) < 3:
            continue

        pieces_by_size[size][piece_type] = vertices_mm

    return dict(pieces_by_size)


def get_piece_bounds(vertices: List[Tuple[float, float]]) -> Tuple[float, float, float, float]:
    """
    Get bounding box of piece.

    Returns:
        (min_x, min_y, max_x, max_y)
    """
    xs = [v[0] for v in vertices]
    ys = [v[1] for v in vertices]
    return min(xs), min(ys), max(xs), max(ys)


def get_piece_dimensions(vertices: List[Tuple[float, float]]) -> Tuple[float, float]:
    """
    Get width and height of piece in mm.

    Returns:
        (width, height)
    """
    min_x, min_y, max_x, max_y = get_piece_bounds(vertices)
    return max_x - min_x, max_y - min_y


def get_piece_area(vertices: List[Tuple[float, float]]) -> float:
    """
    Calculate polygon area using shoelace formula.

    Returns:
        Area in mm^2
    """
    n = len(vertices)
    if n < 3:
        return 0.0

    area = 0.0
    for i in range(n):
        j = (i + 1) % n
        area += vertices[i][0] * vertices[j][1]
        area -= vertices[j][0] * vertices[i][1]

    return abs(area) / 2.0


# Fixed 10 test combinations for reproducible comparison
TEST_COMBINATIONS = [
    {"M": 1},                           # 1 garment, 4 pieces - simplest case
    {"S": 2},                           # 2 garments, 8 pieces
    {"XS": 1, "XXL": 1},                # 2 garments, mixed sizes (small + large)
    {"M": 2, "L": 1},                   # 3 garments, medium complexity
    {"XS": 2, "S": 1, "M": 1},          # 4 garments, small sizes
    {"S": 1, "M": 1, "L": 1, "XL": 1},  # 4 garments, size gradient
    {"M": 1, "S": 2, "XS": 2, "XXL": 1},# 6 garments - the failing case from verification
    {"L": 2, "XL": 2, "XXL": 1},        # 5 garments, larger sizes
    {"XS": 3, "S": 2, "M": 1},          # 6 garments, small-heavy
    {"S": 2, "M": 2, "L": 1, "XL": 1},  # 6 garments, balanced
]


def count_expected_pieces(combo: Dict[str, int]) -> int:
    """
    Calculate expected piece count for a combination.

    Each garment has: 1 BK + 1 FRT + 2 SL = 4 pieces
    """
    total_garments = sum(combo.values())
    return total_garments * 4


def combo_to_string(combo: Dict[str, int]) -> str:
    """Convert combo dict to readable string."""
    return ", ".join(f"{s}:{n}" for s, n in sorted(combo.items()) if n > 0)


def combo_to_filename(combo: Dict[str, int]) -> str:
    """Convert combo dict to filename-safe string."""
    return "_".join(f"{s}{n}" for s, n in sorted(combo.items()) if n > 0)


def generate_random_combinations(n: int, seed: int = 42) -> List[Dict[str, int]]:
    """
    Generate n random size combinations (1-6 garments total).

    Uses deterministic seed for reproducibility.
    """
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
