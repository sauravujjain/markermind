"""
Gerber AccuMark AAMA DXF+RUL Grading Parser.

Self-contained parser for Gerber AccuMark-exported AAMA/ANSI pattern files.
Forked from optitex_aama_parser.py to handle Gerber AccuMark-specific format
differences:

Key differences from OptiTex AAMA:
- Boundary extraction: multiple L1 POLYLINE segments chained end-to-end
  (OptiTex uses a single L1 POLYLINE)
- Grade point assignment: NO "# N" TEXT labels on most blocks (WNF0172).
  Some blocks (10336810 GUS) have explicit "# N" labels. The parser handles
  both cases.
- Metadata: 6 standard L1 TEXT fields (Piece Name, Quantity, ANNOTATION,
  CATEGORY, Material, SIZE)
- Block names: {piece_name}_{sample_size} (e.g., SXF-WNF0172-FA26-BULK-FRT X1_M)
- Units: always ENGLISH (inches)
- RUL header: ANSI/AAMA VERSION: 1.0.0, PRODUCT: ACCUMARK

Per CLAUDE.md architecture rules, this is a self-contained, independently
deployable parser with no cross-parser imports.

Example:
    >>> from nesting_engine.io.gerber_aama_parser import parse_gerber_aama, GerberAAMAGrader
    >>>
    >>> pieces, rules = parse_gerber_aama("style.dxf", "style.rul")
    >>> print(f"Loaded {len(pieces)} pieces")
    >>> print(f"Available sizes: {rules.header.size_list}")
    >>>
    >>> grader = GerberAAMAGrader(pieces, rules)
    >>> for size in ["XS", "M", "XL"]:
    ...     graded = grader.grade(size)
    ...     print(f"Size {size}: {len(graded)} pieces")
"""

from __future__ import annotations

import math
import re
import logging
from pathlib import Path
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Tuple

import ezdxf
from shapely.geometry import Polygon as ShapelyPolygon
from shapely.validation import make_valid

from nesting_engine.core.piece import (
    Piece, PieceIdentifier, OrientationConstraint, GrainConstraint, GrainDirection
)

logger = logging.getLogger(__name__)


# =============================================================================
# Data Structures
# =============================================================================

@dataclass
class GradingRuleHeader:
    """Metadata from RUL file header."""
    author: str
    product: str
    version: str
    creation_date: str
    creation_time: str
    units: str  # "ENGLISH" for Gerber AccuMark
    grade_rule_table: str
    num_sizes: int
    size_list: List[str]
    sample_size: str
    sample_size_index: int


@dataclass
class GradingRule:
    """A single DELTA rule with offsets for each size."""
    rule_id: int  # 1-based rule number
    deltas: List[Tuple[float, float]]  # [(dx, dy) for each size in size_list order]

    def get_delta(self, size_index: int) -> Tuple[float, float]:
        """Get (dx, dy) for a specific size index."""
        return self.deltas[size_index]


@dataclass
class GradingRules:
    """Complete grading rules from a RUL file."""
    header: GradingRuleHeader
    rules: Dict[int, GradingRule]  # rule_id -> GradingRule

    def get_delta_for_size(self, rule_id: int, target_size: str) -> Tuple[float, float]:
        """Get delta for a specific rule and target size."""
        size_index = self.header.size_list.index(target_size)
        return self.rules[rule_id].get_delta(size_index)

    @property
    def num_rules(self) -> int:
        return len(self.rules)


@dataclass
class GradePoint:
    """A vertex that has an associated grading rule."""
    vertex_index: int  # Index in the piece's boundary vertex list
    x: float
    y: float
    rule_id: int  # Which DELTA rule applies


@dataclass
class GerberAAMAPiece:
    """A piece extracted from Gerber AccuMark AAMA DXF with grade point information."""
    name: str  # e.g., "SXF-WNF0172-FA26-BULK-FRT X1"
    block_name: str  # e.g., "SXF-WNF0172-FA26-BULK-FRT X1_M"
    size: str  # e.g., "M"
    vertices: List[Tuple[float, float]]  # Boundary vertices (closed polygon)
    grade_points: List[GradePoint]  # Grade points matched to boundary vertices
    material: Optional[str] = None  # From "Material:" field (e.g., "S", "C")
    category: Optional[str] = None
    annotation: Optional[str] = None
    quantity: int = 1  # From "Quantity:" field
    grain_line: Optional[Tuple[Tuple[float, float], Tuple[float, float]]] = None
    sew_lines: List[List[Tuple[float, float]]] = field(default_factory=list)
    internal_points: List[Tuple[float, float]] = field(default_factory=list)

    @property
    def num_vertices(self) -> int:
        return len(self.vertices)

    @property
    def num_grade_points(self) -> int:
        return len(self.grade_points)


@dataclass
class GradedPiece:
    """A piece graded to a specific size."""
    name: str
    size: str
    vertices: List[Tuple[float, float]]
    source_piece: str
    grain_line: Optional[Tuple[Tuple[float, float], Tuple[float, float]]] = None


# =============================================================================
# RUL Parser
# =============================================================================

class GerberRuleParser:
    """
    Parser for Gerber AccuMark .rul grading rule files.

    Format: ANSI/AAMA VERSION 1.0.0, PRODUCT: ACCUMARK.
    Multi-pair delta lines (same as OptiTex layout).
    """

    def __init__(self, rul_path: str):
        self.rul_path = Path(rul_path)
        if not self.rul_path.exists():
            raise FileNotFoundError(f"RUL file not found: {rul_path}")

    def parse(self) -> GradingRules:
        with open(self.rul_path, 'r', encoding='utf-8', errors='replace') as f:
            lines = f.readlines()

        header, rules_start_idx = self._parse_header(lines)
        rules = self._parse_rules(lines, rules_start_idx, header.num_sizes)

        logger.info(
            f"Parsed {len(rules)} rules for {header.num_sizes} sizes "
            f"from {self.rul_path.name}"
        )

        return GradingRules(header=header, rules=rules)

    def _parse_header(self, lines: List[str]) -> Tuple[GradingRuleHeader, int]:
        header_data = {
            'author': '', 'product': '', 'version': '',
            'creation_date': '', 'creation_time': '',
            'units': 'ENGLISH', 'grade_rule_table': '',
            'num_sizes': 0, 'size_list': [], 'sample_size': '',
        }
        rules_start_idx = 0

        for i, line in enumerate(lines):
            line = line.strip()
            if line.startswith('RULE: DELTA'):
                rules_start_idx = i
                break
            if line.startswith('AUTHOR:'):
                header_data['author'] = line.split(':', 1)[1].strip()
            elif line.startswith('PRODUCT:'):
                header_data['product'] = line.split(':', 1)[1].strip()
            elif line.startswith('VERSION:'):
                header_data['version'] = line.split(':', 1)[1].strip()
            elif line.startswith('CREATION DATE:'):
                header_data['creation_date'] = line.split(':', 1)[1].strip()
            elif line.startswith('CREATION TIME:'):
                header_data['creation_time'] = line.split(':', 1)[1].strip()
            elif line.startswith('UNITS:'):
                header_data['units'] = line.split(':', 1)[1].strip()
            elif line.startswith('GRADE RULE TABLE:'):
                header_data['grade_rule_table'] = line.split(':', 1)[1].strip()
            elif line.startswith('NUMBER OF SIZES:'):
                header_data['num_sizes'] = int(line.split(':', 1)[1].strip())
            elif line.startswith('SIZE LIST:'):
                sizes_str = line.split(':', 1)[1].strip()
                header_data['size_list'] = sizes_str.split()
            elif line.startswith('SAMPLE SIZE:'):
                header_data['sample_size'] = line.split(':', 1)[1].strip()

        sample_size_index = 0
        if header_data['sample_size'] in header_data['size_list']:
            sample_size_index = header_data['size_list'].index(header_data['sample_size'])

        header = GradingRuleHeader(
            author=header_data['author'],
            product=header_data['product'],
            version=header_data['version'],
            creation_date=header_data['creation_date'],
            creation_time=header_data['creation_time'],
            units=header_data['units'],
            grade_rule_table=header_data['grade_rule_table'],
            num_sizes=header_data['num_sizes'],
            size_list=header_data['size_list'],
            sample_size=header_data['sample_size'],
            sample_size_index=sample_size_index
        )
        return header, rules_start_idx

    def _parse_rules(
        self, lines: List[str], start_idx: int, num_sizes: int
    ) -> Dict[int, GradingRule]:
        """Parse all DELTA rules. Handles multi-pair-per-line layout."""
        rules = {}
        pair_re = re.compile(r'(-?[\d.]+)\s*,\s*(-?[\d.]+)')

        i = start_idx
        while i < len(lines):
            line = lines[i].strip()
            if line == 'END':
                break
            if line.startswith('RULE: DELTA'):
                match = re.match(r'RULE: DELTA (\d+)', line)
                if match:
                    rule_id = int(match.group(1))
                    deltas = []
                    j = i + 1
                    while j < len(lines) and len(deltas) < num_sizes:
                        delta_line = lines[j].strip()
                        if not delta_line or delta_line.startswith('RULE:') or delta_line == 'END':
                            break
                        for m in pair_re.finditer(delta_line):
                            deltas.append((float(m.group(1)), float(m.group(2))))
                        j += 1
                    if len(deltas) >= num_sizes:
                        deltas = deltas[:num_sizes]
                        rules[rule_id] = GradingRule(rule_id=rule_id, deltas=deltas)
                    i = j - 1
            i += 1

        return rules


# =============================================================================
# DXF Parser
# =============================================================================

# Point matching tolerance
_TOLERANCE = 0.01

# Junction duplicate tolerance (tighter)
_JUNCTION_TOL = 0.001


def _pt_eq(
    a: Tuple[float, float], b: Tuple[float, float], tol: float = _JUNCTION_TOL
) -> bool:
    """Check point equality within tolerance."""
    return abs(a[0] - b[0]) < tol and abs(a[1] - b[1]) < tol


def _extract_boundary(block) -> Optional[List[Tuple[float, float]]]:
    """
    Extract closed boundary polygon from a block's Layer 1 POLYLINEs.

    Gerber AccuMark splits the boundary across multiple POLYLINE segments
    chained end-to-end. We concatenate all segments, removing duplicate
    junction vertices where seg[i].last == seg[i+1].first.
    """
    segments: List[List[Tuple[float, float]]] = []
    for entity in block:
        layer = entity.dxf.layer if hasattr(entity.dxf, 'layer') else ''
        if layer != '1':
            continue
        if entity.dxftype() == 'POLYLINE':
            verts = [
                (v.dxf.location.x, v.dxf.location.y)
                for v in entity.vertices
            ]
            if len(verts) >= 2:
                segments.append(verts)
        elif entity.dxftype() == 'LWPOLYLINE':
            verts = [
                (p[0], p[1])
                for p in entity.get_points(format='xy')
            ]
            if len(verts) >= 2:
                segments.append(verts)

    if not segments:
        return None

    # Concatenate segments, removing junction duplicates
    boundary: List[Tuple[float, float]] = list(segments[0])
    for seg in segments[1:]:
        if not seg:
            continue
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


def _extract_grain_line(
    block,
) -> Optional[Tuple[Tuple[float, float], Tuple[float, float]]]:
    """Extract grain line from Layer 7 LINE or Layer 5 LINE."""
    for entity in block:
        layer = entity.dxf.layer if hasattr(entity.dxf, 'layer') else ''
        if layer not in ('5', '7'):
            continue
        try:
            if entity.dxftype() == 'LINE':
                start = (entity.dxf.start.x, entity.dxf.start.y)
                end = (entity.dxf.end.x, entity.dxf.end.y)
                return (start, end)
            elif entity.dxftype() in ('LWPOLYLINE', 'POLYLINE'):
                if entity.dxftype() == 'LWPOLYLINE':
                    points = list(entity.get_points('xy'))
                else:
                    points = [
                        (v.dxf.location.x, v.dxf.location.y)
                        for v in entity.vertices
                    ]
                if len(points) >= 2:
                    return ((points[0][0], points[0][1]),
                            (points[-1][0], points[-1][1]))
        except Exception as e:
            logger.debug(f"Error extracting grain line: {e}")
            continue
    return None


def _extract_piece_metadata(block) -> Dict[str, str]:
    """
    Extract Gerber AccuMark metadata from L1 TEXT entities.

    Expected fields:
        Piece Name: {name}
        Quantity: {N}
        ANNOTATION: {text}
        CATEGORY: {text}
        Material: {code}
        SIZE: {sample_size}
    """
    metadata = {}
    for entity in block:
        if entity.dxftype() != 'TEXT':
            continue
        try:
            layer = entity.dxf.layer if hasattr(entity.dxf, 'layer') else ''
            if layer != '1':
                continue
            text = entity.dxf.text.strip()
            if ':' not in text:
                continue
            key, value = text.split(':', 1)
            key = key.strip()
            value = value.strip()

            key_lower = key.lower()
            if key_lower == 'piece name':
                metadata['piece_name'] = value
            elif key_lower == 'quantity':
                metadata['quantity'] = value
            elif key_lower == 'annotation':
                metadata['annotation'] = value
            elif key_lower == 'category':
                metadata['category'] = value
            elif key_lower == 'material':
                metadata['material'] = value
            elif key_lower == 'size':
                metadata['size'] = value
        except Exception:
            continue
    return metadata


# Layers whose geometry updates the "parent context" for grade points.
# L14 (pre-computed graded outlines) is intentionally EXCLUDED — its
# POLYLINEs appear between L1 boundary and L2 grade points in TUKAcad
# format and must NOT reset the parent context.
_GEOMETRY_LAYERS = {'1', '5', '7', '8'}

# Layers for grade point POINT entities.
_GRADE_POINT_LAYERS = {'2', '3'}


def _parse_rule_id_text(text: str) -> Optional[int]:
    """Parse a rule ID from a TEXT entity like '# 52'."""
    m = re.match(r'#\s*(\d+)', text.strip())
    return int(m.group(1)) if m else None


def _block_has_rule_text_labels(block) -> bool:
    """
    Check if a block has explicit "# N" TEXT labels on Layer 2.

    Returns True if at least one L2 TEXT with "# N" format is found.
    This determines whether to use explicit rule IDs (10336810 GUS)
    or sequential assignment (WNF0172).
    """
    for entity in block:
        if entity.dxftype() != 'TEXT':
            continue
        layer = entity.dxf.layer if hasattr(entity.dxf, 'layer') else ''
        if layer != '2':
            continue
        try:
            text = entity.dxf.text.strip()
            if _parse_rule_id_text(text) is not None:
                return True
        except Exception:
            continue
    return False


def _extract_boundary_grade_points_with_labels(
    block,
    boundary_vertices: List[Tuple[float, float]],
) -> List[GradePoint]:
    """
    Extract grade points using explicit "# N" TEXT labels (10336810 GUS-style).

    Walks block entities in order. For each L2 POINT in the boundary zone
    (after L1 geometry), looks for an adjacent "# N" TEXT on L1 or L2 to
    determine the rule ID. Matches the POINT to the nearest boundary vertex.
    """
    entities = []
    for entity in block:
        try:
            layer = entity.dxf.layer if hasattr(entity.dxf, 'layer') else ''
            etype = entity.dxftype()
            entities.append((entity, etype, layer))
        except Exception:
            continue

    current_parent_layer: Optional[str] = None
    pending_rule_id: Optional[int] = None
    pending_point: Optional[Tuple[float, float]] = None

    grade_points: List[GradePoint] = []
    claimed_vertices: set = set()

    for entity, etype, layer in entities:
        # Geometry entity: update current parent
        if layer in _GEOMETRY_LAYERS and etype in ('POLYLINE', 'LWPOLYLINE', 'LINE'):
            current_parent_layer = layer
            pending_point = None
            pending_rule_id = None
            continue

        # TEXT with "# N" on L1 or L2 — capture rule ID
        if etype == 'TEXT' and layer in ('1', '2', '3'):
            try:
                text = entity.dxf.text.strip()
                rid = _parse_rule_id_text(text)
                if rid is not None:
                    pending_rule_id = rid
            except Exception:
                pass
            continue

        # POINT on L2 in boundary zone
        if etype == 'POINT' and layer == '2':
            if current_parent_layer != '1':
                pending_point = None
                pending_rule_id = None
                continue
            try:
                coord = (entity.dxf.location.x, entity.dxf.location.y)
            except Exception:
                continue

            if pending_rule_id is not None:
                # Match to nearest boundary vertex
                best_idx, best_dist = _find_nearest_vertex(coord, boundary_vertices)
                if best_idx is not None and best_dist < _TOLERANCE * 2:
                    if best_idx not in claimed_vertices:
                        grade_points.append(GradePoint(
                            vertex_index=best_idx,
                            x=boundary_vertices[best_idx][0],
                            y=boundary_vertices[best_idx][1],
                            rule_id=pending_rule_id,
                        ))
                        claimed_vertices.add(best_idx)

            pending_point = coord
            # Don't reset pending_rule_id — next POINT might also use it
            # (Gerber sometimes emits TEXT # N, POINT, TEXT # N, POINT for junctions)
            continue

        # L3 POINT — skip (curve point, interpolated)
        if etype == 'POINT' and layer == '3':
            continue

        # L4 POINT — skip (drill hole)
        if etype == 'POINT' and layer == '4':
            continue

    grade_points.sort(key=lambda gp: gp.vertex_index)
    return grade_points


def _extract_boundary_grade_points_sequential(
    block,
    boundary_vertices: List[Tuple[float, float]],
    rule_counter_start: int,
) -> Tuple[List[GradePoint], int]:
    """
    Extract grade points using sequential rule assignment (WNF0172-style).

    For blocks without "# N" TEXT labels, assigns sequential rule IDs
    to boundary L2 POINT entities, skipping junction duplicates.

    The rule counter persists across blocks: block 1 uses rules 1..N,
    block 2 uses rules N+1..M, etc.

    Only L2 POINTs (turn/grade points) in the L1 boundary zone get rules.
    L3 POINTs (curve points) and L4 POINTs (drill holes) do not consume rules.

    Returns:
        (grade_points, next_rule_counter) — the updated counter for the next block
    """
    current_parent_layer: Optional[str] = None
    prev_l2_coord: Optional[Tuple[float, float]] = None
    rule_counter = rule_counter_start

    grade_points: List[GradePoint] = []
    claimed_vertices: set = set()

    for entity in block:
        try:
            etype = entity.dxftype()
            layer = entity.dxf.layer if hasattr(entity.dxf, 'layer') else ''
        except Exception:
            continue

        # Geometry entity: update current parent layer
        if etype in ('POLYLINE', 'LWPOLYLINE', 'LINE') and layer in _GEOMETRY_LAYERS:
            current_parent_layer = layer
            continue

        # Skip non-POINT entities
        if etype != 'POINT':
            continue

        # Skip L3 (curve) and L4 (drill) — they don't consume rules
        if layer in ('3', '4'):
            continue

        # Only process L2 POINT entities
        if layer != '2':
            continue

        try:
            coord = (entity.dxf.location.x, entity.dxf.location.y)
        except Exception:
            continue

        # Only boundary L2 POINTs (in L1 zone) consume sequential rules.
        # Non-boundary L2 POINTs (L5/L7/L8/L14 zones) are for internal
        # features and graded outlines — they don't map to RUL rules.
        if current_parent_layer != '1':
            continue

        # Match to nearest boundary vertex
        best_idx, best_dist = _find_nearest_vertex(coord, boundary_vertices)
        if best_idx is not None and best_dist < _TOLERANCE * 2:
            if best_idx not in claimed_vertices:
                # New unique boundary grade point — assign next rule
                rule_counter += 1
                grade_points.append(GradePoint(
                    vertex_index=best_idx,
                    x=boundary_vertices[best_idx][0],
                    y=boundary_vertices[best_idx][1],
                    rule_id=rule_counter,
                ))
                claimed_vertices.add(best_idx)
            # else: junction duplicate (matches already-claimed vertex) —
            # does NOT consume a new rule, shares the existing one

    grade_points.sort(key=lambda gp: gp.vertex_index)
    return grade_points, rule_counter


def _find_nearest_vertex(
    coord: Tuple[float, float],
    vertices: List[Tuple[float, float]],
) -> Tuple[Optional[int], float]:
    """Find nearest vertex index and Manhattan distance."""
    best_idx: Optional[int] = None
    best_dist = float('inf')
    for idx, (vx, vy) in enumerate(vertices):
        d = abs(coord[0] - vx) + abs(coord[1] - vy)
        if d < best_dist:
            best_dist = d
            best_idx = idx
    return best_idx, best_dist


class GerberAAMADXFParser:
    """
    Parser for Gerber AccuMark AAMA DXF pattern files.

    Handles two grade point assignment modes:
    1. Explicit "# N" TEXT labels (10336810 GUS-style)
    2. Sequential assignment (WNF0172-style, no TEXT labels)

    The mode is auto-detected per block.
    """

    def __init__(self, dxf_path: str):
        self.dxf_path = Path(dxf_path)
        if not self.dxf_path.exists():
            raise FileNotFoundError(f"DXF file not found: {dxf_path}")
        self.doc = ezdxf.readfile(str(self.dxf_path))

    def parse(self) -> List[GerberAAMAPiece]:
        """Parse the DXF file and extract all pieces with grade points."""
        pieces = []
        rule_counter = 0  # Global sequential counter (for blocks without TEXT labels)

        for block in self.doc.blocks:
            if block.name.startswith('*'):
                continue

            piece, rule_counter = self._parse_block(block, rule_counter)
            if piece is not None:
                pieces.append(piece)

        total_gp = sum(p.num_grade_points for p in pieces)
        logger.info(
            f"Parsed {len(pieces)} pieces with {total_gp} boundary grade points "
            f"from {self.dxf_path.name}"
        )
        return pieces

    def _parse_block(
        self, block, rule_counter: int
    ) -> Tuple[Optional[GerberAAMAPiece], int]:
        """Parse a single block into a GerberAAMAPiece."""
        block_name = block.name

        # Extract metadata
        metadata = _extract_piece_metadata(block)
        piece_name = metadata.get('piece_name')
        size = metadata.get('size')

        if not piece_name or not size:
            # Try parsing from block name: {piece_name}_{size}
            if '_' in block_name:
                parts = block_name.rsplit('_', 1)
                if len(parts) == 2:
                    piece_name = piece_name or parts[0]
                    size = size or parts[1]

        if not piece_name or not size:
            logger.debug(f"Skipping block {block_name}: no piece name or size")
            return None, rule_counter

        # Extract boundary
        boundary = _extract_boundary(block)
        if boundary is None or len(boundary) < 4:
            logger.warning(f"Skipping block {block_name}: no valid boundary")
            return None, rule_counter

        # Remove closing vertex for grade point matching (we'll re-add later)
        # Keep original with closing vertex for the piece
        open_boundary = list(boundary)
        if len(open_boundary) > 1 and _pt_eq(open_boundary[0], open_boundary[-1]):
            open_boundary = open_boundary[:-1]

        # Extract grade points — auto-detect mode
        has_labels = _block_has_rule_text_labels(block)

        if has_labels:
            grade_points = _extract_boundary_grade_points_with_labels(
                block, open_boundary
            )
            logger.debug(
                f"Block {block_name}: {len(grade_points)} grade points "
                f"(explicit TEXT labels)"
            )
        else:
            grade_points, rule_counter = _extract_boundary_grade_points_sequential(
                block, open_boundary, rule_counter
            )
            logger.debug(
                f"Block {block_name}: {len(grade_points)} grade points "
                f"(sequential, counter now {rule_counter})"
            )

        # Extract grain line
        grain_line = _extract_grain_line(block)

        # Parse quantity
        qty = 1
        qty_str = metadata.get('quantity', '1')
        try:
            qty = int(qty_str)
        except ValueError:
            logger.debug(f"Could not parse quantity '{qty_str}' for {piece_name}")

        piece = GerberAAMAPiece(
            name=piece_name,
            block_name=block_name,
            size=size,
            vertices=boundary,
            grade_points=grade_points,
            material=metadata.get('material'),
            category=metadata.get('category'),
            annotation=metadata.get('annotation'),
            quantity=qty,
            grain_line=grain_line,
        )

        return piece, rule_counter


# =============================================================================
# Grader
# =============================================================================

class GerberAAMAGrader:
    """
    Apply grading rules to generate sized patterns.

    Takes base pieces (sample size) and grading rules, produces pieces
    for any target size using distance-based linear interpolation for
    non-grade-point vertices.
    """

    def __init__(self, pieces: List[GerberAAMAPiece], rules: GradingRules):
        self.pieces = pieces
        self.rules = rules

    def grade(self, target_size: str) -> List[GradedPiece]:
        """Generate all pieces for a target size."""
        if target_size not in self.rules.header.size_list:
            raise ValueError(
                f"Unknown size '{target_size}'. "
                f"Available sizes: {self.rules.header.size_list}"
            )

        graded_pieces = []
        for piece in self.pieces:
            graded = self.grade_piece(piece, target_size)
            graded_pieces.append(graded)
        return graded_pieces

    def grade_piece(self, piece: GerberAAMAPiece, target_size: str) -> GradedPiece:
        """Grade a single piece to target size."""
        # If target is sample size, no grading needed
        if target_size == self.rules.header.sample_size:
            return GradedPiece(
                name=piece.name,
                size=target_size,
                vertices=list(piece.vertices),
                source_piece=piece.name,
                grain_line=piece.grain_line
            )

        # Apply deltas to get new vertices
        new_vertices = self._apply_deltas(
            piece.vertices,
            piece.grade_points,
            target_size
        )

        # Grade grain line if present
        graded_grain_line = None
        if piece.grain_line:
            graded_grain_line = self._grade_grain_line(
                piece.grain_line,
                piece.vertices,
                new_vertices,
                piece.grade_points
            )

        return GradedPiece(
            name=piece.name,
            size=target_size,
            vertices=new_vertices,
            source_piece=piece.name,
            grain_line=graded_grain_line
        )

    def _apply_deltas(
        self,
        vertices: List[Tuple[float, float]],
        grade_points: List[GradePoint],
        target_size: str
    ) -> List[Tuple[float, float]]:
        """
        Apply grade point deltas to ALL vertices with interpolation.

        Algorithm:
        1. Grade point vertices: apply delta directly from RUL
        2. Non-grade-point vertices: distance-based linear interpolation
           between neighboring grade points along the boundary arc length
        """
        if not grade_points:
            return list(vertices)

        n = len(vertices)
        cumulative_distances = self._calculate_cumulative_distances(vertices)

        # Build lookup: vertex_index -> (dx, dy).
        # Keep first match when multiple grade points hit same vertex.
        gp_deltas: Dict[int, Tuple[float, float]] = {}
        size_index = self.rules.header.size_list.index(target_size)

        for gp in grade_points:
            if gp.vertex_index in gp_deltas:
                continue
            if gp.rule_id in self.rules.rules:
                delta = self.rules.rules[gp.rule_id].get_delta(size_index)
                gp_deltas[gp.vertex_index] = delta
            else:
                gp_deltas[gp.vertex_index] = (0.0, 0.0)

        gp_indices = sorted(gp_deltas.keys())

        new_vertices = []
        for i, (x, y) in enumerate(vertices):
            if i in gp_deltas:
                dx, dy = gp_deltas[i]
            else:
                dx, dy = self._interpolate_vertex_delta(
                    i, vertices, cumulative_distances, gp_indices, gp_deltas
                )
            new_vertices.append((x + dx, y + dy))

        return new_vertices

    def _calculate_cumulative_distances(
        self, vertices: List[Tuple[float, float]]
    ) -> List[float]:
        """Calculate cumulative arc length along the boundary."""
        distances = [0.0]
        for i in range(1, len(vertices)):
            x1, y1 = vertices[i - 1]
            x2, y2 = vertices[i]
            segment_length = math.sqrt((x2 - x1) ** 2 + (y2 - y1) ** 2)
            distances.append(distances[-1] + segment_length)
        return distances

    def _interpolate_vertex_delta(
        self,
        vertex_index: int,
        vertices: List[Tuple[float, float]],
        cumulative_distances: List[float],
        grade_point_indices: List[int],
        grade_point_deltas: Dict[int, Tuple[float, float]]
    ) -> Tuple[float, float]:
        """
        Distance-based linear interpolation between neighboring grade points.

        Finds the previous (G1) and next (G2) grade points walking along
        the boundary, calculates proportional position t based on arc length,
        and interpolates: delta = (1-t) * delta_G1 + t * delta_G2.
        """
        if not grade_point_indices:
            return (0.0, 0.0)

        # Find bracketing grade points
        prev_gp_idx = None
        next_gp_idx = None

        for i, gp_idx in enumerate(grade_point_indices):
            if gp_idx > vertex_index:
                next_gp_idx = gp_idx
                prev_gp_idx = (
                    grade_point_indices[i - 1] if i > 0
                    else grade_point_indices[-1]
                )
                break

        if next_gp_idx is None:
            prev_gp_idx = grade_point_indices[-1]
            next_gp_idx = grade_point_indices[0]

        if vertex_index < grade_point_indices[0]:
            prev_gp_idx = grade_point_indices[-1]
            next_gp_idx = grade_point_indices[0]

        # Calculate position ratio using arc length distance
        dist_to_vertex = cumulative_distances[vertex_index]
        dist_to_prev_gp = cumulative_distances[prev_gp_idx]
        dist_to_next_gp = cumulative_distances[next_gp_idx]

        if next_gp_idx > prev_gp_idx:
            span = dist_to_next_gp - dist_to_prev_gp
            if span > 0:
                t = (dist_to_vertex - dist_to_prev_gp) / span
            else:
                t = 0.0
        else:
            # Wrap-around case
            total_perimeter = cumulative_distances[-1]
            x1, y1 = vertices[-1]
            x2, y2 = vertices[0]
            closing_dist = math.sqrt((x2 - x1) ** 2 + (y2 - y1) ** 2)
            total_perimeter += closing_dist

            span = (total_perimeter - dist_to_prev_gp) + dist_to_next_gp
            if vertex_index >= prev_gp_idx:
                dist_from_prev = dist_to_vertex - dist_to_prev_gp
            else:
                dist_from_prev = (total_perimeter - dist_to_prev_gp) + dist_to_vertex

            if span > 0:
                t = dist_from_prev / span
            else:
                t = 0.0

        t = max(0.0, min(1.0, t))

        dx1, dy1 = grade_point_deltas.get(prev_gp_idx, (0.0, 0.0))
        dx2, dy2 = grade_point_deltas.get(next_gp_idx, (0.0, 0.0))

        dx = (1 - t) * dx1 + t * dx2
        dy = (1 - t) * dy1 + t * dy2
        return (dx, dy)

    def _grade_grain_line(
        self,
        grain_line: Tuple[Tuple[float, float], Tuple[float, float]],
        old_vertices: List[Tuple[float, float]],
        new_vertices: List[Tuple[float, float]],
        grade_points: List[GradePoint]
    ) -> Tuple[Tuple[float, float], Tuple[float, float]]:
        """Grade grain line by centroid translation."""
        if not grade_points:
            return grain_line

        old_cx = sum(v[0] for v in old_vertices) / len(old_vertices)
        old_cy = sum(v[1] for v in old_vertices) / len(old_vertices)
        new_cx = sum(v[0] for v in new_vertices) / len(new_vertices)
        new_cy = sum(v[1] for v in new_vertices) / len(new_vertices)

        dx = new_cx - old_cx
        dy = new_cy - old_cy

        start = (grain_line[0][0] + dx, grain_line[0][1] + dy)
        end = (grain_line[1][0] + dx, grain_line[1][1] + dy)
        return (start, end)

    def get_available_sizes(self) -> List[str]:
        return self.rules.header.size_list

    def get_sample_size(self) -> str:
        return self.rules.header.sample_size


# =============================================================================
# Grain axis detection and orientation
# =============================================================================

def _detect_grain_axis(pieces) -> str:
    """
    Detect whether grain lines run along DXF X or DXF Y axis.

    Votes across all pieces. Returns 'x' if grain is predominantly
    horizontal in DXF coords, 'y' if predominantly vertical.
    """
    x_votes = 0
    y_votes = 0
    for piece in pieces:
        gl = getattr(piece, 'grain_line', None)
        if not gl:
            continue
        gx = abs(gl[1][0] - gl[0][0])
        gy = abs(gl[1][1] - gl[0][1])
        if gx > gy:
            x_votes += 1
        elif gy > gx:
            y_votes += 1
    return 'x' if x_votes >= y_votes else 'y'


def _orient_for_grain(
    vertices: List[Tuple[float, float]],
    grain_line,
    pattern_grain_axis: str,
    scale: float,
) -> List[Tuple[float, float]]:
    """
    Convert vertices to mm and swap coordinates if grain runs along DXF Y.

    After conversion, translates to origin so absolute DXF positions
    don't interfere with nesting.
    """
    piece_axis = pattern_grain_axis
    if grain_line:
        gx = abs(grain_line[1][0] - grain_line[0][0])
        gy = abs(grain_line[1][1] - grain_line[0][1])
        if gx > gy:
            piece_axis = 'x'
        elif gy > gx:
            piece_axis = 'y'

    if piece_axis == 'y':
        scaled = [(y * scale, x * scale) for x, y in vertices]
    else:
        scaled = [(x * scale, y * scale) for x, y in vertices]

    min_x = min(v[0] for v in scaled)
    min_y = min(v[1] for v in scaled)
    return [(v[0] - min_x, v[1] - min_y) for v in scaled]


def _orient_grain_line_coords(
    grain_line,
    vertices: List[Tuple[float, float]],
    pattern_grain_axis: str,
    scale: float,
) -> Tuple[Tuple[float, float], Tuple[float, float]]:
    """
    Apply the same grain-aware swap + origin translation to grain line coords.
    """
    piece_axis = pattern_grain_axis
    if grain_line:
        gx = abs(grain_line[1][0] - grain_line[0][0])
        gy = abs(grain_line[1][1] - grain_line[0][1])
        if gx > gy:
            piece_axis = 'x'
        elif gy > gx:
            piece_axis = 'y'

    if piece_axis == 'y':
        g_start = (grain_line[0][1] * scale, grain_line[0][0] * scale)
        g_end = (grain_line[1][1] * scale, grain_line[1][0] * scale)
        min_x = min(y * scale for _, y in vertices)
        min_y = min(x * scale for x, _ in vertices)
    else:
        g_start = (grain_line[0][0] * scale, grain_line[0][1] * scale)
        g_end = (grain_line[1][0] * scale, grain_line[1][1] * scale)
        min_x = min(x * scale for x, _ in vertices)
        min_y = min(y * scale for _, y in vertices)

    return (
        (g_start[0] - min_x, g_start[1] - min_y),
        (g_end[0] - min_x, g_end[1] - min_y),
    )


# =============================================================================
# Vertex cleaning
# =============================================================================

def _clean_vertices(
    vertices: List[Tuple[float, float]],
    tolerance: float = 0.001
) -> List[Tuple[float, float]]:
    """Remove consecutive duplicate vertices."""
    if len(vertices) < 2:
        return vertices
    cleaned = [vertices[0]]
    for x, y in vertices[1:]:
        prev_x, prev_y = cleaned[-1]
        dist = math.sqrt((x - prev_x) ** 2 + (y - prev_y) ** 2)
        if dist > tolerance:
            cleaned.append((x, y))
    return cleaned


def clean_vertices_for_spyrrow(
    vertices: List[Tuple[float, float]],
    tolerance: float = 0.01,
) -> List[Tuple[float, float]]:
    """
    Prepare Gerber AAMA-graded vertices for the Spyrrow/jagua-rs solver.

    Removes duplicate vertices (consecutive and non-consecutive) and
    re-closes the polygon. No simplification — every vertex is
    geometrically significant.

    This function is specific to Gerber AAMA parser output.
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


# =============================================================================
# Public API — Convenience functions
# =============================================================================

def parse_gerber_aama(
    dxf_path: str,
    rul_path: str,
) -> Tuple[List[GerberAAMAPiece], GradingRules]:
    """
    Load a Gerber AccuMark AAMA pattern file pair.

    Args:
        dxf_path: Path to .dxf file
        rul_path: Path to .rul file

    Returns:
        (pieces, grading_rules)
    """
    dxf_parser = GerberAAMADXFParser(dxf_path)
    pieces = dxf_parser.parse()

    rul_parser = GerberRuleParser(rul_path)
    rules = rul_parser.parse()

    total_gp = sum(p.num_grade_points for p in pieces)
    logger.info(
        f"Gerber AAMA: {total_gp} boundary grade points across "
        f"{len(pieces)} pieces, {rules.num_rules} RUL rules"
    )

    return pieces, rules


def get_available_materials(pieces: List[GerberAAMAPiece]) -> List[str]:
    """
    Get sorted list of unique materials in the pattern.

    Material is extracted from the "Material:" DXF TEXT field.
    """
    materials = set()
    for p in pieces:
        mat = p.material
        if mat:
            materials.add(mat.upper())
    return sorted(materials)


def _get_pieces_by_material(
    pieces: List[GerberAAMAPiece],
) -> Dict[str, List[GerberAAMAPiece]]:
    """Group pieces by material code."""
    result: Dict[str, List[GerberAAMAPiece]] = {}
    for p in pieces:
        mat = (p.material or "UNKNOWN").upper()
        if mat not in result:
            result[mat] = []
        result[mat].append(p)
    return dict(sorted(result.items()))


def grade_material_to_nesting_pieces(
    dxf_path: str,
    rul_path: str,
    material: str,
    target_sizes: List[str],
    rotations: List[float] = [0, 180],
    allow_flip: bool = False
) -> List[Piece]:
    """
    Load Gerber AAMA pattern, filter by material, grade, and return Piece objects.

    Main entry point for multi-material workflow. Call once per material.

    Args:
        dxf_path: Path to .dxf file
        rul_path: Path to .rul file
        material: Material code to filter (case-insensitive, e.g., "S", "C")
        target_sizes: Sizes to generate
        rotations: Allowed rotation angles (default [0, 180] for grain)
        allow_flip: Whether to allow flipping (for L/R pairing)

    Returns:
        List of Piece objects for the specified material, ready for nesting
    """
    pieces, rules = parse_gerber_aama(dxf_path, rul_path)

    material_upper = material.upper()
    pieces_by_material = _get_pieces_by_material(pieces)

    if material_upper not in pieces_by_material:
        available = list(pieces_by_material.keys())
        logger.warning(
            f"Material '{material}' not found. Available: {available}"
        )
        return []

    filtered = pieces_by_material[material_upper]
    return _grade_pieces_to_nesting(
        filtered, rules, target_sizes, rotations, allow_flip
    )


def grade_to_nesting_pieces(
    dxf_path: str,
    rul_path: str,
    target_sizes: List[str],
    rotations: List[float] = [0, 180],
    allow_flip: bool = False
) -> List[Piece]:
    """
    Load Gerber AAMA pattern, grade ALL pieces (all materials), return Piece objects.

    Args:
        dxf_path: Path to .dxf file
        rul_path: Path to .rul file
        target_sizes: Sizes to generate
        rotations: Allowed rotation angles
        allow_flip: Whether to allow flipping

    Returns:
        List of Piece objects ready for nesting engine
    """
    pieces, rules = parse_gerber_aama(dxf_path, rul_path)
    return _grade_pieces_to_nesting(
        pieces, rules, target_sizes, rotations, allow_flip
    )


def _grade_pieces_to_nesting(
    pieces: List[GerberAAMAPiece],
    rules: GradingRules,
    target_sizes: List[str],
    rotations: List[float],
    allow_flip: bool,
) -> List[Piece]:
    """
    Internal: grade a list of pieces and convert to nesting Piece objects.

    Converts from DXF units to mm (ENGLISH=25.4, METRIC=1.0).
    """
    grader = GerberAAMAGrader(pieces, rules)

    # Unit conversion: ENGLISH (inches) -> mm, METRIC already mm
    if rules.header.units.upper() == 'METRIC':
        to_mm = 1.0
    else:
        to_mm = 25.4

    # Detect grain axis from original pieces
    pattern_grain_axis = _detect_grain_axis(pieces)
    if pattern_grain_axis == 'y':
        logger.info("Grain detected along DXF Y — will swap coordinates")

    nesting_pieces = []

    for target_size in target_sizes:
        if target_size not in grader.get_available_sizes():
            logger.warning(f"Skipping unknown size: {target_size}")
            continue

        graded = grader.grade(target_size)

        for gp in graded:
            # Find original piece to get quantity info
            original = next(
                (p for p in pieces if p.name == gp.source_piece), None
            )

            # Convert to mm with grain-aware orientation
            vertices_mm = _orient_for_grain(
                gp.vertices, gp.grain_line, pattern_grain_axis, to_mm
            )
            vertices_mm = _clean_vertices(vertices_mm)

            if len(vertices_mm) < 3:
                logger.warning(f"Skipping piece {gp.name} - too few vertices")
                continue

            # Ensure closed
            if vertices_mm[0] != vertices_mm[-1]:
                vertices_mm.append(vertices_mm[0])

            # Validate with shapely
            try:
                poly = ShapelyPolygon(vertices_mm)
                if not poly.is_valid:
                    poly = make_valid(poly)
                if poly.area <= 0:
                    logger.warning(f"Skipping piece {gp.name} - invalid polygon")
                    continue
            except Exception as e:
                logger.warning(f"Skipping piece {gp.name}: {e}")
                continue

            # Determine flip based on quantity
            piece_allow_flip = allow_flip
            if original and original.quantity >= 2:
                piece_allow_flip = True

            identifier = PieceIdentifier(
                piece_name=gp.name,
                size=target_size
            )

            orientation = OrientationConstraint(
                allowed_rotations=rotations,
                allow_flip=piece_allow_flip
            )

            grain = GrainConstraint(direction=GrainDirection.LENGTHWISE)
            if gp.grain_line:
                start, end = _orient_grain_line_coords(
                    gp.grain_line, gp.vertices, pattern_grain_axis, to_mm
                )
                grain.grain_line_start = start
                grain.grain_line_end = end

            try:
                piece = Piece(
                    vertices=vertices_mm,
                    identifier=identifier,
                    orientation=orientation,
                    grain=grain
                )
                nesting_pieces.append(piece)
            except Exception as e:
                logger.warning(f"Failed to create piece {gp.name}: {e}")

    logger.info(
        f"Generated {len(nesting_pieces)} nesting pieces "
        f"across {len(target_sizes)} sizes"
    )
    return nesting_pieces


# =============================================================================
# Summary / debug
# =============================================================================

def print_gerber_aama_summary(dxf_path: str, rul_path: str) -> None:
    """Print summary statistics for Gerber AccuMark AAMA pattern files."""
    print(f"\n{'=' * 60}")
    print("Gerber AccuMark AAMA Pattern Summary")
    print(f"{'=' * 60}")

    # Parse RUL
    print(f"\nRUL File: {rul_path}")
    rul_parser = GerberRuleParser(rul_path)
    rules = rul_parser.parse()

    print(f"  Author: {rules.header.author}")
    print(f"  Product: {rules.header.product}")
    print(f"  Units: {rules.header.units}")
    print(f"  Number of sizes: {rules.header.num_sizes}")
    print(f"  Size list: {' '.join(rules.header.size_list)}")
    print(f"  Sample size: {rules.header.sample_size} "
          f"(index {rules.header.sample_size_index})")
    print(f"  Number of rules: {rules.num_rules}")

    # Verify sample size has zero deltas
    if rules.num_rules > 0:
        rule_1 = rules.rules.get(1)
        if rule_1:
            sample_delta = rule_1.get_delta(rules.header.sample_size_index)
            print(f"  Rule 1 sample delta: {sample_delta}")

    # Parse DXF
    print(f"\nDXF File: {dxf_path}")
    dxf_parser = GerberAAMADXFParser(dxf_path)
    pieces = dxf_parser.parse()

    print(f"  Pieces: {len(pieces)}")
    materials = get_available_materials(pieces)
    print(f"  Materials: {', '.join(materials)}")

    for p in pieces:
        print(f"\n  [{p.block_name}]")
        print(f"    Name: {p.name}")
        print(f"    Size: {p.size}")
        print(f"    Material: {p.material}")
        print(f"    Category: {p.category}")
        print(f"    Annotation: {p.annotation}")
        print(f"    Quantity: {p.quantity}")
        print(f"    Vertices: {p.num_vertices}")
        print(f"    Grade points: {p.num_grade_points}")
        if p.grade_points:
            gp_rules = [gp.rule_id for gp in p.grade_points[:5]]
            print(f"    First rule IDs: {gp_rules}")
        if p.grain_line:
            print(f"    Grain line: {p.grain_line}")

    # Grading test
    print(f"\n{'=' * 60}")
    print("Grading Test")
    print(f"{'=' * 60}")

    grader = GerberAAMAGrader(pieces, rules)
    for size in rules.header.size_list:
        graded = grader.grade(size)
        # Check area of first piece
        if graded:
            gp0 = graded[0]
            area = _shoelace_area(gp0.vertices)
            print(f"  Size {size}: {len(graded)} pieces, "
                  f"first piece area={area:.4f} sq in")


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
