"""
Block-based DXF Parser for production/pre-sized pattern files.

Parses DXF files where pieces are stored as INSERT references to named blocks.
Each block contains the piece boundary as a Layer 1 POLYLINE.

Block naming convention: ``PIECE_NAMEXqty-index``
(e.g. ``BACK YOKEX1-0``, ``LO BACKX2-35``).

Blocks are grouped into sizes by index stride. The parser auto-detects whether
size groups run smallest-first or largest-first by comparing piece areas across
the first and last size groups.

Common sources: Gerber AccuMark, OptiTex, and similar CAD system exports
where all sizes are already graded and stored as separate blocks.

Example:
    >>> from nesting_engine.io.dxf_block_parser import parse_block_dxf
    >>> pieces, sizes = parse_block_dxf(
    ...     "production.dxf",
    ...     size_names=["S", "M", "L", "XL", "2X"],
    ... )
    >>> print(f"Loaded {len(pieces)} pieces across {len(sizes)} sizes")
"""

from __future__ import annotations

import re
import logging
from typing import List, Dict, Optional, Tuple

import ezdxf

from nesting_engine.core.piece import Piece, PieceIdentifier

logger = logging.getLogger(__name__)


def parse_block_dxf(
    dxf_path: str,
    size_names: Optional[List[str]] = None,
    rotations: List[float] = [0, 180],
    allow_flip: bool = True,
) -> Tuple[List[Piece], List[str]]:
    """
    Parse a production/block-based DXF where pieces are stored as INSERT
    references to named blocks.

    Block naming convention: ``PIECE_NAMEXqty-index``
    (e.g. ``BACK YOKEX1-0``, ``LO BACKX2-35``).

    Layer 1 POLYLINE inside each block = piece boundary.
    Blocks are grouped by index in equal-sized size groups (index // group_size).

    Args:
        dxf_path: Path to DXF file
        size_names: Optional list of size labels (e.g. ["28","30","32",...]).
                    If None, sizes are auto-generated as "SIZE_1", "SIZE_2", ...
        rotations: Allowed rotation angles
        allow_flip: Whether to allow flipping

    Returns:
        (nesting_pieces, all_sizes)
    """
    doc = ezdxf.readfile(dxf_path)
    blocks = [b for b in doc.blocks if not b.name.startswith('*')]

    # Detect unit scale (to mm)
    insunits = doc.header.get('$INSUNITS', 0) if '$INSUNITS' in doc.header else 0
    unit_scale = {1: 25.4, 4: 1.0, 5: 10.0, 6: 1000.0}.get(insunits, 1.0)

    # Heuristic: if INSUNITS is unset (0) and coordinates are small,
    # assume inches (garment DXFs are typically < 100 inches)
    if insunits == 0 and unit_scale == 1.0:
        for b in blocks[:10]:
            for entity in b:
                if entity.dxftype() == 'POLYLINE' and entity.dxf.layer == '1':
                    sample_verts = [(v.dxf.location.x, v.dxf.location.y) for v in entity.vertices]
                    if sample_verts:
                        max_coord = max(max(abs(v[0]) for v in sample_verts),
                                        max(abs(v[1]) for v in sample_verts))
                        if max_coord < 200:  # Likely inches
                            unit_scale = 25.4
                            logger.info(f"Block DXF: INSUNITS not set, max_coord={max_coord:.1f}, assuming inches")
                        else:
                            logger.info(f"Block DXF: INSUNITS not set, max_coord={max_coord:.1f}, assuming mm")
                    break
            if unit_scale != 1.0:
                break

    # Parse block names: PIECE_NAMEXqty-index
    block_info = []
    for b in blocks:
        m = re.match(r'^(.+?)X(\d+)-(\d+)$', b.name)
        if not m:
            continue
        piece_name = m.group(1)
        qty = int(m.group(2))
        idx = int(m.group(3))
        block_info.append((piece_name, qty, idx, b))

    if not block_info:
        return [], []

    # Determine size groups from index pattern
    max_idx = max(info[2] for info in block_info)
    # Count unique piece types (base names)
    unique_pieces = set()
    for piece_name, qty, idx, b in block_info:
        unique_pieces.add(piece_name)

    # Pieces per size group = count of blocks in first group (indices 0..N-1)
    # Find the gap: first index of group 1 = pieces_per_group
    indices_sorted = sorted(set(info[2] for info in block_info))
    # Group size = number of unique piece instances (counting qty duplicates)
    # e.g., 22 unique names with some having qty=2 or 3 = 32 total
    first_group_count = 0
    for info in block_info:
        if info[2] < indices_sorted[len(unique_pieces)] if len(indices_sorted) > len(unique_pieces) else info[2] < 999999:
            first_group_count += 1

    # Simpler: detect group size by finding the stride between same-piece instances
    # Take the first piece name and find its indices
    first_piece_name = block_info[0][0]
    first_piece_indices = sorted(info[2] for info in block_info if info[0] == first_piece_name)
    if len(first_piece_indices) >= 2:
        group_size = first_piece_indices[1] - first_piece_indices[0]
    else:
        group_size = max_idx + 1

    num_sizes = (max_idx + 1) // group_size if group_size > 0 else 1

    logger.info(f"Block DXF: {len(block_info)} blocks, {len(unique_pieces)} unique pieces, "
                f"group_size={group_size}, num_sizes={num_sizes}")

    # Assign size labels
    if size_names and len(size_names) == num_sizes:
        all_sizes = list(size_names)
    else:
        all_sizes = [f"SIZE_{i+1}" for i in range(num_sizes)]

    # Detect if DXF size groups are ordered largest-first by checking area trend.
    # Pick a piece that appears in at least 2 size groups, compute its area per group,
    # and reverse labels if areas decrease (largest index = smallest piece).
    if num_sizes >= 2:
        def _block_boundary_area(block) -> float:
            """Compute area of the largest layer-1 polyline in a block."""
            best_verts = None
            best_count = 0
            for entity in block:
                if entity.dxftype() == 'POLYLINE' and entity.dxf.layer == '1':
                    verts = [(v.dxf.location.x, v.dxf.location.y) for v in entity.vertices]
                    if len(verts) > best_count:
                        best_count = len(verts)
                        best_verts = verts
                elif entity.dxftype() == 'LWPOLYLINE' and entity.dxf.layer == '1':
                    verts = list(entity.get_points(format='xy'))
                    if len(verts) > best_count:
                        best_count = len(verts)
                        best_verts = verts
            if not best_verts or len(best_verts) < 3:
                return 0.0
            # Shoelace formula
            area = 0.0
            n = len(best_verts)
            for i in range(n):
                x1, y1 = best_verts[i]
                x2, y2 = best_verts[(i + 1) % n]
                area += x1 * y2 - x2 * y1
            return abs(area) / 2.0

        # Build {piece_name: {size_idx: area}}
        area_by_size: Dict[str, Dict[int, float]] = {}
        for piece_name, qty, idx, block in block_info:
            sidx = idx // group_size
            if sidx >= num_sizes:
                continue
            if piece_name not in area_by_size:
                area_by_size[piece_name] = {}
            if sidx not in area_by_size[piece_name]:
                area_by_size[piece_name][sidx] = _block_boundary_area(block)

        # Pick a piece that spans the first and last size group
        for pname, size_areas in area_by_size.items():
            if 0 in size_areas and (num_sizes - 1) in size_areas:
                first_area = size_areas[0]
                last_area = size_areas[num_sizes - 1]
                if first_area > 0 and last_area > 0 and first_area > last_area * 1.01:
                    # Areas decrease with index -> DXF is largest-first, reverse labels
                    all_sizes = all_sizes[::-1]
                    logger.info(f"Block DXF: size groups are largest-first "
                                f"(area[0]={first_area:.0f} > area[{num_sizes-1}]={last_area:.0f}), "
                                f"reversed size labels to {all_sizes}")
                break

    # Build Piece objects
    nesting_pieces = []
    for piece_name, qty, idx, block in block_info:
        size_idx = idx // group_size
        if size_idx >= len(all_sizes):
            continue
        size_label = all_sizes[size_idx]

        # Extract boundary: layer 1 POLYLINE with most vertices
        boundary_verts = None
        max_verts = 0
        for entity in block:
            if entity.dxftype() == 'POLYLINE' and entity.dxf.layer == '1':
                verts = [(v.dxf.location.x * unit_scale, v.dxf.location.y * unit_scale)
                         for v in entity.vertices]
                if len(verts) > max_verts:
                    max_verts = len(verts)
                    boundary_verts = verts
            elif entity.dxftype() == 'LWPOLYLINE' and entity.dxf.layer == '1':
                verts = [(p[0] * unit_scale, p[1] * unit_scale)
                         for p in entity.get_points(format='xy')]
                if len(verts) > max_verts:
                    max_verts = len(verts)
                    boundary_verts = verts

        if not boundary_verts or len(boundary_verts) < 3:
            continue

        # Close polygon if needed
        if boundary_verts[0] != boundary_verts[-1]:
            boundary_verts.append(boundary_verts[0])

        # Use block index as unique suffix for piece name
        unique_name = f"{piece_name}_{idx}"

        piece = Piece(
            vertices=boundary_verts,
            identifier=PieceIdentifier(
                piece_name=unique_name,
                size=size_label,
            ),
        )
        nesting_pieces.append(piece)

    return nesting_pieces, all_sizes


# ---------------------------------------------------------------------------
# Vertex cleaning for Spyrrow compatibility
# ---------------------------------------------------------------------------

def clean_vertices_for_spyrrow(
    vertices: List[Tuple[float, float]],
    tolerance: float = 0.01,
) -> List[Tuple[float, float]]:
    """
    Prepare block-parser vertices for the Spyrrow/jagua-rs solver.

    Block parser pieces have densely discretized curves (arcs/splines
    converted to many short line segments). Only duplicate removal is
    applied — no simplification, which was found to corrupt geometry
    on rectangular pocket shapes (up to 49% area loss).

    This function is specific to block parser output. Other parsers have
    their own cleaning functions.
    """
    return _dedup(vertices, tolerance)


def _dedup(
    vertices: List[Tuple[float, float]],
    tolerance: float = 0.01,
) -> List[Tuple[float, float]]:
    """Remove duplicate vertices (consecutive and non-consecutive), re-close."""
    if len(vertices) < 3:
        return vertices

    verts = list(vertices)
    # Remove closing vertex if present
    if len(verts) > 1 and _pt_eq(verts[0], verts[-1], tolerance):
        verts = verts[:-1]

    # Remove non-consecutive duplicates (keep first occurrence)
    seen: List[Tuple[float, float]] = []
    for v in verts:
        if not any(_pt_eq(v, s, tolerance) for s in seen):
            seen.append(v)

    # Remove consecutive duplicates that might remain
    cleaned: List[Tuple[float, float]] = [seen[0]] if seen else []
    for v in seen[1:]:
        if not _pt_eq(v, cleaned[-1], tolerance):
            cleaned.append(v)

    if len(cleaned) < 3:
        logger.warning(
            f"Polygon reduced to {len(cleaned)} vertices after dedup "
            f"(original had {len(vertices)})"
        )
        return vertices

    cleaned.append(cleaned[0])
    return cleaned



def _pt_eq(a: Tuple[float, float], b: Tuple[float, float], tol: float) -> bool:
    return abs(a[0] - b[0]) < tol and abs(a[1] - b[1]) < tol
