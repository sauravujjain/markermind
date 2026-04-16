"""
Gerber AccuMark "Expanded Shapes" DXF Parser.

Parses DXF files exported from Gerber AccuMark where each piece × size
is stored as a separate named block (e.g., ``BK LWR_XS``, ``BK LWR_S``).

Rich metadata is embedded as TEXT entities on Layer 1:
- ``Piece Name: BK LWR``
- ``SIZE: XS``
- ``ANNOTATION: S10,11-1`` (material grouping)
- ``Quantity: 1``
- ``CATEGORY: 3``

Each block's Layer 1 POLYLINEs form a connected chain of boundary segments
that are concatenated into a single closed polygon.

L/R pairing is encoded in the ANNOTATION suffix: ``(L-N-R-N)`` means the
piece has N left and N right copies per bundle.

No RUL file is needed — pieces are already at final graded size.

Self-contained parser per CLAUDE.md architecture rules.

Example:
    >>> from nesting_engine.io.gerber_accumark_parser import parse_gerber_accumark_dxf
    >>> pieces, sizes, mat_map = parse_gerber_accumark_dxf("ARFW2410011_original.dxf")
    >>> print(f"Loaded {len(pieces)} pieces across {len(sizes)} sizes")
"""

from __future__ import annotations

import logging
import math
import re
from typing import Dict, List, Optional, Tuple

import ezdxf

from nesting_engine.core.piece import Piece, PieceIdentifier

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Annotation / L-R parsing
# ---------------------------------------------------------------------------

_LR_PATTERN = re.compile(r'\(L-(\d+)-R-(\d+)\)')


_TRAILING_DEMAND = re.compile(r'-(\d+)$')


def _parse_annotation(annotation: str) -> Tuple[str, int, int, int]:
    """
    Parse a Gerber AccuMark ANNOTATION string.

    Returns:
        (material_code, left_qty, right_qty, demand)

    The trailing ``-N`` suffix on non-L/R annotations is the demand count,
    not part of the material code.

    Examples:
        "S10,11-1"             -> ("S10,11", 0, 0, 1)
        "S10,11(L-1-R-1)"     -> ("S10,11", 1, 1, 0)
        "S10,11(L-2-R-2)"     -> ("S10,11", 2, 2, 0)
        "L100(L-1-R-1)"       -> ("L100", 1, 1, 0)
        "Z LINER-1"           -> ("Z LINER", 0, 0, 1)
        "EL-1"                -> ("EL", 0, 0, 1)
    """
    m = _LR_PATTERN.search(annotation)
    if m:
        left_qty = int(m.group(1))
        right_qty = int(m.group(2))
        material = annotation[:m.start()].rstrip('-')
        return material, left_qty, right_qty, 0

    # Non-L/R: strip trailing -N (demand count)
    dm = _TRAILING_DEMAND.search(annotation)
    if dm:
        demand = int(dm.group(1))
        material = annotation[:dm.start()]
        return material, 0, 0, demand

    return annotation, 0, 0, 1


# ---------------------------------------------------------------------------
# Boundary extraction: concatenate Layer 1 POLYLINEs
# ---------------------------------------------------------------------------

def _extract_boundary(block, unit_scale: float) -> Optional[List[Tuple[float, float]]]:
    """
    Extract the closed boundary polygon from a block's Layer 1 POLYLINEs.

    In Gerber AccuMark, the boundary is split across multiple POLYLINE
    segments that share endpoints (end of one == start of next). We
    concatenate all segments into a single polygon, removing duplicate
    junction points.

    Returns None if no valid boundary can be built.
    """
    # Collect all Layer 1 polyline segments in entity order
    segments: List[List[Tuple[float, float]]] = []
    for entity in block:
        layer = entity.dxf.layer
        if layer != '1':
            continue
        if entity.dxftype() == 'POLYLINE':
            verts = [
                (v.dxf.location.x * unit_scale, v.dxf.location.y * unit_scale)
                for v in entity.vertices
            ]
            if len(verts) >= 2:
                segments.append(verts)
        elif entity.dxftype() == 'LWPOLYLINE':
            verts = [
                (p[0] * unit_scale, p[1] * unit_scale)
                for p in entity.get_points(format='xy')
            ]
            if len(verts) >= 2:
                segments.append(verts)

    if not segments:
        return None

    # Concatenate segments, removing duplicate junction points
    boundary: List[Tuple[float, float]] = list(segments[0])
    for seg in segments[1:]:
        if not seg:
            continue
        # If the start of this segment matches the end of boundary, skip the dup
        if _pt_eq(seg[0], boundary[-1]):
            boundary.extend(seg[1:])
        else:
            boundary.extend(seg)

    if len(boundary) < 3:
        return None

    # Close polygon if needed
    if not _pt_eq(boundary[0], boundary[-1]):
        boundary.append(boundary[0])

    return boundary


def _extract_grain_line(block, unit_scale: float) -> Optional[Tuple[Tuple[float, float], Tuple[float, float]]]:
    """Extract grain line direction from Layer 5 LINE entity."""
    for entity in block:
        if entity.dxftype() == 'LINE' and entity.dxf.layer == '5':
            start = (entity.dxf.start.x * unit_scale, entity.dxf.start.y * unit_scale)
            end = (entity.dxf.end.x * unit_scale, entity.dxf.end.y * unit_scale)
            return (start, end)
    return None


# ---------------------------------------------------------------------------
# Main parser
# ---------------------------------------------------------------------------

def parse_gerber_accumark_dxf(
    dxf_path: str,
    size_names: Optional[List[str]] = None,
) -> Tuple[List[Piece], List[str], Dict[str, str], Dict[str, dict]]:
    """
    Parse a Gerber AccuMark "Expanded Shapes" DXF.

    Args:
        dxf_path: Path to DXF file.
        size_names: Optional ordered list of size labels.  If None, sizes
                    are derived from the DXF and ordered by piece area
                    (smallest to largest).

    Returns:
        (pieces, sizes, piece_to_material, piece_config)
        - pieces: One Piece per (piece_name, size).
        - sizes: Ordered list of size labels.
        - piece_to_material: {piece_name: material_code} derived from
          ANNOTATION (the material code portion with L-R suffix stripped).
        - piece_config: {piece_name: {demand: int, flipped: bool}}
          For nesting runners. L/R pieces: demand=left_qty, flipped=True.
          Non-L/R pieces: demand=qty, flipped=False.
    """
    doc = ezdxf.readfile(dxf_path)
    blocks = [b for b in doc.blocks if not b.name.startswith('*')]

    # Detect unit scale (to mm)
    insunits = doc.header.get('$INSUNITS', 0) if '$INSUNITS' in doc.header else 0
    unit_scale = {1: 25.4, 4: 1.0, 5: 10.0, 6: 1000.0}.get(insunits, 1.0)

    # Heuristic: if INSUNITS is unset (0) and coordinates are small, assume inches
    if insunits == 0 and unit_scale == 1.0:
        for b in blocks[:10]:
            for entity in b:
                if entity.dxftype() == 'POLYLINE' and entity.dxf.layer == '1':
                    sample_verts = [
                        (v.dxf.location.x, v.dxf.location.y)
                        for v in entity.vertices
                    ]
                    if sample_verts:
                        max_coord = max(
                            max(abs(v[0]) for v in sample_verts),
                            max(abs(v[1]) for v in sample_verts),
                        )
                        if max_coord < 200:
                            unit_scale = 25.4
                            logger.info(
                                f"AccuMark DXF: INSUNITS not set, max_coord={max_coord:.1f}, "
                                f"assuming inches"
                            )
                        else:
                            logger.info(
                                f"AccuMark DXF: INSUNITS not set, max_coord={max_coord:.1f}, "
                                f"assuming mm"
                            )
                    break
            if unit_scale != 1.0:
                break

    # First pass: extract metadata from blocks that have ANNOTATION
    # (only one size per piece has the metadata in Gerber AccuMark)
    piece_annotations: Dict[str, str] = {}  # piece_name -> raw annotation
    piece_quantities: Dict[str, int] = {}   # piece_name -> quantity

    for b in blocks:
        pname, annot, qty = None, None, None
        for entity in b:
            if entity.dxftype() != 'TEXT' or entity.dxf.layer != '1':
                continue
            text = entity.dxf.text
            if text.startswith('Piece Name: '):
                pname = text[len('Piece Name: '):]
            elif text.startswith('ANNOTATION: '):
                annot = text[len('ANNOTATION: '):]
            elif text.startswith('Quantity: '):
                try:
                    qty = int(text[len('Quantity: '):])
                except ValueError:
                    pass
        if pname and annot:
            piece_annotations[pname] = annot
        if pname and qty is not None:
            piece_quantities[pname] = qty

    # Build piece_to_material map (strip L-R suffix from annotation)
    piece_to_material: Dict[str, str] = {}
    piece_lr: Dict[str, Tuple[int, int]] = {}  # piece_name -> (left, right)
    piece_demand: Dict[str, int] = {}           # piece_name -> total demand
    for pname, annot in piece_annotations.items():
        mat, left, right, demand = _parse_annotation(annot)
        piece_to_material[pname] = mat
        if left > 0 or right > 0:
            piece_lr[pname] = (left, right)
            piece_demand[pname] = left + right
        else:
            piece_demand[pname] = max(demand, 1)

    # Second pass: extract boundaries and build Piece objects
    raw_sizes: set = set()
    piece_list: List[Piece] = []
    # For area-based size ordering
    area_by_size: Dict[str, List[float]] = {}

    for b in blocks:
        # Parse metadata from this block
        pname, size = None, None
        for entity in b:
            if entity.dxftype() != 'TEXT' or entity.dxf.layer != '1':
                continue
            text = entity.dxf.text
            if text.startswith('Piece Name: '):
                pname = text[len('Piece Name: '):]
            elif text.startswith('SIZE: '):
                size = text[len('SIZE: '):]

        if not pname or not size:
            continue

        raw_sizes.add(size)

        # Extract boundary
        boundary = _extract_boundary(b, unit_scale)
        if boundary is None or len(boundary) < 4:  # need at least 3 unique + close
            logger.warning(f"AccuMark: no valid boundary for {pname}_{size}")
            continue

        piece = Piece(
            vertices=boundary,
            identifier=PieceIdentifier(
                piece_name=pname,
                size=size,
            ),
        )
        piece_list.append(piece)

        # Track area for size ordering
        area = _shoelace_area(boundary)
        if size not in area_by_size:
            area_by_size[size] = []
        area_by_size[size].append(area)

    # Determine size order
    if size_names:
        all_sizes = list(size_names)
    else:
        # Order by average piece area (smallest first)
        avg_area = {s: sum(areas) / len(areas) for s, areas in area_by_size.items() if areas}
        all_sizes = sorted(avg_area.keys(), key=lambda s: avg_area.get(s, 0))

    # Build piece_config for nesting runners
    piece_config: Dict[str, dict] = {}
    for pname in set(p.identifier.piece_name for p in piece_list):
        if pname in piece_lr:
            left, right = piece_lr[pname]
            piece_config[pname] = {'demand': left, 'flipped': True}
        else:
            piece_config[pname] = {
                'demand': piece_demand.get(pname, 1),
                'flipped': False,
            }

    logger.info(
        f"AccuMark DXF: {len(piece_list)} pieces, "
        f"{len(set(p.identifier.piece_name for p in piece_list))} unique piece types, "
        f"{len(all_sizes)} sizes ({', '.join(all_sizes)}), "
        f"{len(piece_to_material)} material mappings"
    )

    return piece_list, all_sizes, piece_to_material, piece_config


# ---------------------------------------------------------------------------
# Vertex cleaning for Spyrrow compatibility
# ---------------------------------------------------------------------------

def clean_vertices_for_spyrrow(
    vertices: List[Tuple[float, float]],
    tolerance: float = 0.01,
) -> List[Tuple[float, float]]:
    """
    Prepare AccuMark vertices for the Spyrrow/jagua-rs solver.

    AccuMark DXF pieces have densely discretized curves (arcs/splines
    converted to short line segments in the POLYLINE chain). Only
    duplicate removal is applied — no simplification, same rationale
    as the block parser (prevents geometry corruption).

    This function is specific to AccuMark parser output.
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


# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------

def _pt_eq(a: Tuple[float, float], b: Tuple[float, float], tol: float = 0.05) -> bool:
    """Check point equality within tolerance."""
    return abs(a[0] - b[0]) < tol and abs(a[1] - b[1]) < tol


def _shoelace_area(vertices: List[Tuple[float, float]]) -> float:
    """Compute polygon area via shoelace formula."""
    n = len(vertices)
    if n < 3:
        return 0.0
    area = 0.0
    for i in range(n):
        x1, y1 = vertices[i]
        x2, y2 = vertices[(i + 1) % n]
        area += x1 * y2 - x2 * y1
    return abs(area) / 2.0
