"""
VT DXF Parser for Optitex "Graded Nest" pattern files.

Parses DXF files exported from Optitex where each material is a separate file
and all sizes are pre-graded as separate blocks. No RUL file needed.

Supports **two export variants**:

**Variant A — Split per material** (e.g. ``25528-101.dxf``):
- 112 blocks = 56 primary + 56 shadow
- Primary blocks (odd-numbered): 8 TEXT annotations including
  ``Piece Name:``, ``Size:``, ``Quantity:``, ``Material:``
- Shadow blocks (even-numbered): only 2 TEXT annotations, identical
  geometry -> skipped
- Filter: skip blocks missing ``Quantity:`` annotation
- Piece Name format: ``PieceNum_SizeLabel`` (e.g. ``4_M``)

**Variant B — All materials in one file** (e.g. ``25528XX.dxf``):
- N*S blocks (N pieces × S sizes), no shadow blocks
- Full metadata (``Quantity:``, ``Material:``, etc.) only on the
  **sample-size** block (marked with ``Sample Size`` text); other
  sizes have only ``Piece Name:`` and ``Size:``
- Piece Name format: ``PieceNum`` only (e.g. ``4``); size is in
  the separate ``Size:`` annotation

**Layers**:
- Layer 1 POLYLINE: pre-graded boundary vertices
- Layer 7 LINE: grain direction

**Units**: centimetres (multiplied by 10 for mm)

**Quantity**: ``Qty=2`` means the piece needs a mirrored copy (L/R pair);
the DXF only contains one shape per block.

Example::

    >>> from nesting_engine.io.vt_dxf_parser import parse_vt_dxf
    >>> pieces, sizes, qty_map = parse_vt_dxf("25528-101.dxf")
    >>> print(f"{len(pieces)} pieces, sizes={sizes}")
    56 pieces, sizes=['XS', 'S', 'M', 'L', 'XL', '2XL', '3XL']
"""

from __future__ import annotations

import logging
from typing import Dict, List, Optional, Tuple

import ezdxf

from nesting_engine.core.piece import Piece, PieceIdentifier

logger = logging.getLogger(__name__)

# Canonical garment size ordering (XS..5XL).
# Sizes not in this list are appended alphabetically at the end.
_SIZE_ORDER = [
    "XXS", "XS", "S", "M", "L", "XL", "2XL", "3XL", "4XL", "5XL",
]


def _size_sort_key(size: str) -> Tuple[int, str]:
    """Return a sort key that puts standard garment sizes in order."""
    upper = size.upper()
    for idx, canonical in enumerate(_SIZE_ORDER):
        if upper == canonical:
            return (idx, "")
    return (len(_SIZE_ORDER), size)


def parse_vt_dxf(
    dxf_path: str,
    rotations: List[float] = [0, 180],
    allow_flip: bool = True,
) -> Tuple[List[Piece], List[str], Dict[str, int], str]:
    """
    Parse an Optitex Graded Nest DXF file.

    Args:
        dxf_path: Path to DXF file (one material per file).
        rotations: Allowed rotation angles in degrees.
        allow_flip: Whether pieces can be flipped during nesting.

    Returns:
        (pieces, sorted_sizes, piece_quantities, material)
        - pieces: One ``Piece`` per primary block (shadow blocks skipped).
        - sorted_sizes: Size labels in garment order (e.g. XS..3XL).
        - piece_quantities: ``{piece_name: qty}`` from the DXF annotation.
          ``qty=2`` means L/R pair (the nesting engine should create a
          mirrored copy).
        - material: Material code from the DXF annotation (e.g. ``"101"``).
    """
    doc = ezdxf.readfile(dxf_path)
    blocks = [b for b in doc.blocks if not b.name.startswith("*")]

    # ---------------------------------------------------------------
    # Unit detection: if the max coordinate is < 1000 assume cm (x10).
    # Typical garment pieces in cm have coords 0-100.
    # ---------------------------------------------------------------
    unit_scale = _detect_unit_scale(blocks)

    # ---------------------------------------------------------------
    # Detect variant: A (primary/shadow with Quantity on every primary)
    # or B (pre-graded, Quantity only on sample-size block).
    # Heuristic: if fewer than half the blocks have "Quantity:", it's B.
    # ---------------------------------------------------------------
    qty_count = sum(
        1 for b in blocks if "Quantity" in _read_annotations(b)
    )
    variant_b = qty_count > 0 and qty_count < len(blocks) // 2

    if variant_b:
        return _parse_variant_b(blocks, unit_scale)
    else:
        return _parse_variant_a(blocks, unit_scale)


def _parse_variant_a(
    blocks,
    unit_scale: float,
) -> Tuple[List[Piece], List[str], Dict[str, int], str]:
    """Parse Variant A: primary/shadow blocks, Quantity on every primary."""
    pieces: List[Piece] = []
    sizes_seen: set[str] = set()
    piece_quantities: Dict[str, int] = {}
    material: Optional[str] = None

    for block in blocks:
        annotations = _read_annotations(block)

        # Shadow blocks lack "Quantity:" — skip them
        if "Quantity" not in annotations:
            continue

        piece_name_raw = annotations.get("Piece Name", "")
        qty_str = annotations.get("Quantity", "1")
        mat = annotations.get("Material", "")

        if not piece_name_raw:
            logger.warning(f"Block {block.name}: missing Piece Name, skipping")
            continue

        # Parse piece_name: "4_M" -> piece_num="4", size="M"
        # Fallback: if no underscore, use the Size: annotation
        piece_num, size = _parse_piece_name(piece_name_raw)
        if not size:
            size = annotations.get("Size", "").strip()
        if not piece_num or not size:
            logger.warning(
                f"Block {block.name}: cannot parse piece name '{piece_name_raw}', skipping"
            )
            continue

        qty = int(qty_str) if qty_str.isdigit() else 1
        sizes_seen.add(size)
        piece_quantities[piece_num] = qty
        if material is None and mat:
            material = mat

        # Extract boundary vertices from Layer 1 POLYLINE
        boundary_verts = _extract_boundary(block, unit_scale)
        if not boundary_verts or len(boundary_verts) < 3:
            logger.warning(f"Block {block.name}: no valid boundary, skipping")
            continue

        # Close polygon if needed
        if boundary_verts[0] != boundary_verts[-1]:
            boundary_verts.append(boundary_verts[0])

        # Extract grain line from Layer 7 LINE
        grain_line = _extract_grain_line(block, unit_scale)

        piece = Piece(
            vertices=boundary_verts,
            identifier=PieceIdentifier(
                piece_name=piece_num,
                size=size,
            ),
            grain=grain_line,
        )
        pieces.append(piece)

    sorted_sizes = sorted(sizes_seen, key=_size_sort_key)
    material_name = material or "MAIN"

    logger.info(
        f"VT DXF (variant A): {len(pieces)} pieces from {len(blocks)} blocks, "
        f"{len(sizes_seen)} sizes={sorted_sizes}, "
        f"material={material_name}, unit_scale={unit_scale}"
    )

    return pieces, sorted_sizes, piece_quantities, material_name


def _parse_variant_b(
    blocks,
    unit_scale: float,
) -> Tuple[List[Piece], List[str], Dict[str, int], str]:
    """
    Parse Variant B: all sizes pre-graded, metadata only on sample-size block.

    Piece Name has no underscore-size suffix. Size comes from ``Size:`` TEXT.
    Quantity/Material are propagated from the sample-size block to all blocks
    of the same piece number.
    """
    # First pass: collect metadata from sample-size blocks (those with Quantity)
    piece_quantities: Dict[str, int] = {}   # piece_num -> qty
    piece_materials: Dict[str, str] = {}    # piece_num -> material
    for block in blocks:
        annotations = _read_annotations(block)
        if "Quantity" not in annotations:
            continue
        piece_num = annotations.get("Piece Name", "").strip()
        if not piece_num:
            continue
        qty_str = annotations.get("Quantity", "1")
        piece_quantities[piece_num] = int(qty_str) if qty_str.isdigit() else 1
        mat = annotations.get("Material", "")
        if mat:
            piece_materials[piece_num] = mat

    # Second pass: build Piece objects from ALL blocks with Size + Piece Name
    pieces: List[Piece] = []
    sizes_seen: set[str] = set()

    for block in blocks:
        annotations = _read_annotations(block)

        piece_num = annotations.get("Piece Name", "").strip()
        size = annotations.get("Size", "").strip()

        if not piece_num or not size:
            continue

        sizes_seen.add(size)

        boundary_verts = _extract_boundary(block, unit_scale)
        if not boundary_verts or len(boundary_verts) < 3:
            logger.warning(f"Block {block.name}: no valid boundary, skipping")
            continue

        if boundary_verts[0] != boundary_verts[-1]:
            boundary_verts.append(boundary_verts[0])

        grain_line = _extract_grain_line(block, unit_scale)

        piece = Piece(
            vertices=boundary_verts,
            identifier=PieceIdentifier(
                piece_name=piece_num,
                size=size,
            ),
            grain=grain_line,
        )
        pieces.append(piece)

    sorted_sizes = sorted(sizes_seen, key=_size_sort_key)

    # Determine material
    materials = sorted(set(piece_materials.values())) if piece_materials else []
    if len(materials) == 1:
        material_name = materials[0]
    else:
        material_name = "MAIN"

    logger.info(
        f"VT DXF (variant B): {len(pieces)} pieces from {len(blocks)} blocks, "
        f"{len(piece_quantities)} unique piece types, "
        f"{len(sizes_seen)} sizes={sorted_sizes}, "
        f"materials={materials or ['MAIN']}, unit_scale={unit_scale}"
    )

    return pieces, sorted_sizes, piece_quantities, material_name


# -------------------------------------------------------------------
# Internal helpers
# -------------------------------------------------------------------

def _read_annotations(block) -> Dict[str, str]:
    """Read TEXT entities from a block and return key-value annotation dict."""
    annotations: Dict[str, str] = {}
    for entity in block:
        if entity.dxftype() == "TEXT":
            text = entity.dxf.text
            if ":" in text:
                key, _, val = text.partition(":")
                annotations[key.strip()] = val.strip()
    return annotations


def _parse_piece_name(raw: str) -> Tuple[str, str]:
    """
    Parse ``"4_M"`` into ``("4", "M")``.

    Uses rsplit to handle piece names that might contain underscores
    (e.g. ``"BACK_YOKE_M"`` -> ``("BACK_YOKE", "M")``).
    """
    if "_" not in raw:
        return (raw, "")
    piece_num, size = raw.rsplit("_", 1)
    return (piece_num, size)


def _detect_unit_scale(blocks) -> float:
    """
    Detect whether coordinates are in cm or mm.

    Heuristic: sample a few Layer 1 POLYLINEs.
    - max coord < 1000 -> cm (scale = 10)
    - max coord >= 2000 -> mm (scale = 1)
    - between 1000 and 2000 -> cm (scale = 10), conservative
    """
    for block in blocks[:20]:
        for entity in block:
            if entity.dxftype() == "POLYLINE" and entity.dxf.layer == "1":
                verts = [
                    (v.dxf.location.x, v.dxf.location.y)
                    for v in entity.vertices
                ]
                if verts:
                    max_coord = max(
                        max(abs(v[0]) for v in verts),
                        max(abs(v[1]) for v in verts),
                    )
                    if max_coord >= 2000:
                        logger.info(
                            f"VT DXF: max_coord={max_coord:.1f}, assuming mm"
                        )
                        return 1.0
                    else:
                        logger.info(
                            f"VT DXF: max_coord={max_coord:.1f}, assuming cm (x10)"
                        )
                        return 10.0
    # Default: cm
    return 10.0


def _extract_boundary(
    block,
    unit_scale: float,
) -> Optional[List[Tuple[float, float]]]:
    """
    Extract the Layer 1 POLYLINE with the most vertices from a block.

    Supports both POLYLINE (2D) and LWPOLYLINE entities.
    Coordinates are multiplied by ``unit_scale`` to convert to mm.
    """
    best_verts: Optional[List[Tuple[float, float]]] = None
    best_count = 0

    for entity in block:
        if entity.dxftype() == "POLYLINE" and entity.dxf.layer == "1":
            verts = [
                (v.dxf.location.x * unit_scale, v.dxf.location.y * unit_scale)
                for v in entity.vertices
            ]
            if len(verts) > best_count:
                best_count = len(verts)
                best_verts = verts
        elif entity.dxftype() == "LWPOLYLINE" and entity.dxf.layer == "1":
            verts = [
                (p[0] * unit_scale, p[1] * unit_scale)
                for p in entity.get_points(format="xy")
            ]
            if len(verts) > best_count:
                best_count = len(verts)
                best_verts = verts

    return best_verts


def _extract_grain_line(
    block,
    unit_scale: float,
) -> Optional[Tuple[Tuple[float, float], Tuple[float, float]]]:
    """Extract the grain direction LINE from Layer 7, if present."""
    for entity in block:
        if entity.dxftype() == "LINE" and entity.dxf.layer == "7":
            return (
                (entity.dxf.start.x * unit_scale, entity.dxf.start.y * unit_scale),
                (entity.dxf.end.x * unit_scale, entity.dxf.end.y * unit_scale),
            )
    return None


# ---------------------------------------------------------------------------
# Vertex cleaning for Spyrrow compatibility
# ---------------------------------------------------------------------------

def clean_vertices_for_spyrrow(
    vertices: List[Tuple[float, float]],
    tolerance: float = 0.01,
) -> List[Tuple[float, float]]:
    """
    Prepare VT DXF vertices for the Spyrrow/jagua-rs solver.

    VT DXF pieces come from Optitex export with clean polyline geometry.
    Only duplicate removal is needed (occasional closing vertex or grading
    artifacts). No simplification is applied.

    This function is specific to VT DXF parser output. Other parsers have
    their own cleaning functions.
    """
    if len(vertices) < 3:
        return vertices

    verts = list(vertices)
    # Remove closing vertex if present
    if (len(verts) > 1 and
            abs(verts[0][0] - verts[-1][0]) < tolerance and
            abs(verts[0][1] - verts[-1][1]) < tolerance):
        verts = verts[:-1]

    # Remove non-consecutive duplicates (keep first occurrence)
    seen: List[Tuple[float, float]] = []
    for v in verts:
        is_dup = False
        for s in seen:
            if abs(v[0] - s[0]) < tolerance and abs(v[1] - s[1]) < tolerance:
                is_dup = True
                break
        if not is_dup:
            seen.append(v)

    # Remove consecutive duplicates that might remain
    cleaned: List[Tuple[float, float]] = [seen[0]] if seen else []
    for v in seen[1:]:
        if not (abs(v[0] - cleaned[-1][0]) < tolerance and
                abs(v[1] - cleaned[-1][1]) < tolerance):
            cleaned.append(v)

    if len(cleaned) < 3:
        return vertices  # don't break the polygon

    cleaned.append(cleaned[0])
    return cleaned
