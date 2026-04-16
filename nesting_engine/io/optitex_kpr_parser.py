"""
OptiTex AAMA DXF+RUL Grading Parser.

Self-contained parser for OptiTex-exported AAMA/ANSI pattern files.
Forked from aama_parser.py (Boke/ASTM variant) to handle OptiTex-specific
format differences:

Key differences from the Boke AAMA parser:
- RUL header: "ANSI/AAMA VERSION" instead of "ASTM/D13Proposal"
- RUL delta lines: multiple dx,dy pairs packed per line (OptiTex)
  vs one pair per line (Boke/ASTM)
- DXF blocks: numeric names ("1", "2", "3") instead of piece names ("CTR HD-M")
- RUL file may have blank first line

Per CLAUDE.md architecture rules, this is a self-contained, independently
deployable parser with no cross-parser imports.

Example:
    >>> from nesting_engine.io.optitex_aama_parser import load_aama_pattern, AAMAGrader
    >>>
    >>> pieces, rules = load_aama_pattern("style.dxf", "style.rul")
    >>> print(f"Loaded {len(pieces)} pieces")
    >>> print(f"Available sizes: {rules.header.size_list}")
    >>>
    >>> grader = AAMAGrader(pieces, rules)
    >>> for size in ["XXS", "S", "XL"]:
    ...     graded = grader.grade(size)
    ...     print(f"Size {size}: {len(graded)} pieces")
"""

from __future__ import annotations

import math
import re
import logging
from pathlib import Path
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Tuple, Any
from enum import Enum

import ezdxf
from shapely.geometry import Polygon as ShapelyPolygon
from shapely.validation import make_valid

from nesting_engine.core.piece import (
    Piece, PieceIdentifier, OrientationConstraint, GrainConstraint, GrainDirection
)

logger = logging.getLogger(__name__)


# =============================================================================
# Enums
# =============================================================================

class LRType(Enum):
    """
    How L/R (Left/Right) is handled for this piece.

    There are three patterns in AAMA DXF files:

    Pattern A - SEPARATE_LEFT/SEPARATE_RIGHT:
        Two separate blocks with "LEFT"/"RIGHT" in the name.
        Geometries are already different (pre-mirrored).
        Use each as-is, NO flip needed.

    Pattern B - FLIP_FOR_LR:
        Single block with annotation like SHELL(L*1-R*1).
        Geometry is symmetric - flip creates the mirror piece.
        Add N× normal + M× flipped to nesting queue.

    Pattern C - NONE:
        No L/R in name, no L/R in annotation.
        Center pieces or fully symmetric (BACK, COLLAR, etc.).
        Use as-is, single piece.
    """
    NONE = "none"              # No L/R (center piece like BACK, COLLAR)
    SEPARATE_LEFT = "left"     # Separate block, this is the LEFT piece
    SEPARATE_RIGHT = "right"   # Separate block, this is the RIGHT piece
    FLIP_FOR_LR = "flip"       # Single block, flip to get L and R


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
    units: str  # "METRIC" or "ENGLISH"
    grade_rule_table: str
    num_sizes: int
    size_list: List[str]  # ["28", "29", ..., "40"]
    sample_size: str  # "32"
    sample_size_index: int  # Index in size_list (e.g., 4 for size 32)


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
        """Number of grading rules."""
        return len(self.rules)


@dataclass
class GradePoint:
    """A vertex that has an associated grading rule."""
    vertex_index: int  # Index in the piece's vertex list
    x: float
    y: float
    rule_id: int  # Which DELTA rule applies


@dataclass
class PieceQuantity:
    """
    Parsed quantity information from AAMA annotation field.

    Supports formats like:
    - "SHELL*2" -> total=2, has_left_right=False
    - "IL(L*1-R*1)" -> total=2, has_left_right=True, left_qty=1, right_qty=1
    - "SHELL(L*2-R*2)" -> total=4, has_left_right=True, left_qty=2, right_qty=2

    Attributes:
        total: Total pieces needed (left_qty + right_qty if has_left_right)
        has_left_right: True if annotation has (L*N-R*M) format
        left_qty: Number of left pieces (0 if no L/R specification)
        right_qty: Number of right pieces (0 if no L/R specification)
        material: Extracted material type (SHELL, IL, FINISH, etc.)
        raw: Original annotation string
    """
    total: int
    has_left_right: bool
    left_qty: int = 0
    right_qty: int = 0
    material: Optional[str] = None
    raw: Optional[str] = None

    @classmethod
    def default(cls) -> "PieceQuantity":
        """Create a default quantity (1 piece, no L/R)."""
        return cls(total=1, has_left_right=False, left_qty=0, right_qty=0)


def parse_annotation(annotation: Optional[str]) -> PieceQuantity:
    """
    Parse AAMA annotation field to extract material and quantity info.

    Supported formats:
    - "SHELL*2" -> material=SHELL, total=2, no L/R
    - "IL(L*1-R*1)" -> material=IL, total=2, left=1, right=1
    - "SHELL(L*2-R*2)" -> material=SHELL, total=4, left=2, right=2
    - "FINISH" -> material=FINISH, total=1, no L/R

    Args:
        annotation: The annotation string from DXF, or None

    Returns:
        PieceQuantity with parsed information

    Note:
        Unknown formats return a default PieceQuantity with total=1
        and the raw annotation preserved for debugging.
    """
    if not annotation:
        return PieceQuantity.default()

    annotation = annotation.strip()
    if not annotation:
        return PieceQuantity.default()

    # Pattern 1: "MATERIAL(L*N-R*M)" format
    # Example: "IL(L*1-R*1)", "SHELL(L*2-R*2)", "SO1(L*1-R*1)"
    # Note: Material codes can contain digits (SO1, FO1, WO2, etc.)
    lr_pattern = re.compile(
        r'^([A-Za-z][A-Za-z0-9]*)\(L\*(\d+)-R\*(\d+)\)$',
        re.IGNORECASE
    )
    match = lr_pattern.match(annotation)
    if match:
        material = match.group(1).upper()
        left_qty = int(match.group(2))
        right_qty = int(match.group(3))
        return PieceQuantity(
            total=left_qty + right_qty,
            has_left_right=True,
            left_qty=left_qty,
            right_qty=right_qty,
            material=material,
            raw=annotation
        )

    # Pattern 2: "MATERIAL*N" format
    # Example: "SHELL*2", "IL*4", "SO1*2"
    qty_pattern = re.compile(r'^([A-Za-z][A-Za-z0-9]*)\*(\d+)$', re.IGNORECASE)
    match = qty_pattern.match(annotation)
    if match:
        material = match.group(1).upper()
        total = int(match.group(2))
        return PieceQuantity(
            total=total,
            has_left_right=False,
            left_qty=0,
            right_qty=0,
            material=material,
            raw=annotation
        )

    # Pattern 3: Just material name (implies quantity 1)
    # Example: "SHELL", "IL", "FINISH", "SO1"
    material_only = re.compile(r'^([A-Za-z][A-Za-z0-9]*)$', re.IGNORECASE)
    match = material_only.match(annotation)
    if match:
        material = match.group(1).upper()
        return PieceQuantity(
            total=1,
            has_left_right=False,
            left_qty=0,
            right_qty=0,
            material=material,
            raw=annotation
        )

    # Unknown format - return default with raw preserved
    logger.debug(f"Unknown annotation format: '{annotation}'")
    result = PieceQuantity.default()
    result.raw = annotation
    return result


def parse_quantity_field(quantity_str: Optional[str]) -> Optional[PieceQuantity]:
    """
    Parse the DXF 'Quantity:' metadata field (separate from annotation).

    Supported formats:
    - "1,1" -> has_left_right=True, left_qty=1, right_qty=1, total=2
    - "2,2" -> has_left_right=True, left_qty=2, right_qty=2, total=4
    - "2"   -> total=2, no L/R
    - "3"   -> total=3, no L/R
    - "1"   -> total=1, no L/R (default, returns None to indicate no override)

    The "N,M" format means N left copies and M right copies (L/R pair).

    Args:
        quantity_str: The raw quantity string from DXF TEXT metadata, or None

    Returns:
        PieceQuantity if the field provides useful info beyond the default,
        or None if the field is absent, empty, or just "1" (no override needed).
    """
    if not quantity_str:
        return None

    quantity_str = quantity_str.strip()
    if not quantity_str:
        return None

    # Pattern: "N,M" — L/R pair with N left, M right
    comma_match = re.match(r'^(\d+)\s*,\s*(\d+)$', quantity_str)
    if comma_match:
        left_qty = int(comma_match.group(1))
        right_qty = int(comma_match.group(2))
        total = left_qty + right_qty
        if total <= 1:
            return None  # No override needed
        return PieceQuantity(
            total=total,
            has_left_right=True,
            left_qty=left_qty,
            right_qty=right_qty,
            raw=quantity_str,
        )

    # Pattern: plain integer "N"
    int_match = re.match(r'^(\d+)$', quantity_str)
    if int_match:
        total = int(int_match.group(1))
        if total <= 1:
            return None  # No override needed
        return PieceQuantity(
            total=total,
            has_left_right=False,
            left_qty=0,
            right_qty=0,
            raw=quantity_str,
        )

    logger.debug(f"Unknown quantity format: '{quantity_str}'")
    return None


def detect_lr_type(piece_name: str, quantity: PieceQuantity) -> LRType:
    """
    Determine how L/R is handled for this piece.

    Decision tree:
    1. If annotation has (L*N-R*M) format → FLIP_FOR_LR
    2. Else if name contains "LEFT" → SEPARATE_LEFT
    3. Else if name contains "RIGHT" → SEPARATE_RIGHT
    4. Else → NONE (center/symmetric piece)

    Args:
        piece_name: The piece name from DXF
        quantity: Parsed PieceQuantity from annotation

    Returns:
        LRType indicating how to handle L/R for this piece

    Examples:
        >>> detect_lr_type("SLEEVE", PieceQuantity(2, True, 1, 1))
        LRType.FLIP_FOR_LR
        >>> detect_lr_type("FRONT LEFT", PieceQuantity(1, False, 0, 0))
        LRType.SEPARATE_LEFT
        >>> detect_lr_type("BACK", PieceQuantity(1, False, 0, 0))
        LRType.NONE
    """
    # Check annotation first (Pattern B) - has L/R quantities
    if quantity.has_left_right:
        return LRType.FLIP_FOR_LR

    # Check name for LEFT/RIGHT (Pattern A) - separate blocks
    name_upper = piece_name.upper()
    if "LEFT" in name_upper:
        return LRType.SEPARATE_LEFT
    if "RIGHT" in name_upper:
        return LRType.SEPARATE_RIGHT

    # Default: center/symmetric piece (Pattern C)
    return LRType.NONE


@dataclass
class AAMAPiece:
    """A piece extracted from AAMA DXF with grade point information."""
    name: str  # e.g., "BK R"
    block_name: str  # e.g., "BK R-32"
    size: str  # e.g., "32"
    vertices: List[Tuple[float, float]]  # All boundary vertices
    grade_points: List[GradePoint]  # Subset of vertices that are grade points
    layer: str = "1"
    material: Optional[str] = None
    category: Optional[str] = None
    annotation: Optional[str] = None
    quantity: PieceQuantity = field(default_factory=PieceQuantity.default)

    # L/R handling
    lr_type: LRType = LRType.NONE

    # Additional geometry from other layers
    grain_line: Optional[Tuple[Tuple[float, float], Tuple[float, float]]] = None
    sew_lines: List[List[Tuple[float, float]]] = field(default_factory=list)
    internal_points: List[Tuple[float, float]] = field(default_factory=list)

    @property
    def num_vertices(self) -> int:
        """Number of boundary vertices."""
        return len(self.vertices)

    @property
    def num_grade_points(self) -> int:
        """Number of grade points."""
        return len(self.grade_points)

    @property
    def display_name(self) -> str:
        """
        Generate display name with L/R indicator.

        Examples:
            - FRONT LEFT → "FRONT LEFT" (already has LEFT)
            - SLEEVE with FLIP_FOR_LR → "SLEEVE (L/R)"
            - BACK with NONE → "BACK"
        """
        if self.lr_type == LRType.FLIP_FOR_LR:
            return f"{self.name} (L/R)"
        # For SEPARATE_LEFT/RIGHT, the name already contains LEFT/RIGHT
        return self.name


@dataclass
class GradedPiece:
    """A piece graded to a specific size."""
    name: str
    size: str
    vertices: List[Tuple[float, float]]
    source_piece: str  # Original piece name

    # Preserved from original
    grain_line: Optional[Tuple[Tuple[float, float], Tuple[float, float]]] = None


@dataclass
class NestingQueueItem:
    """
    A single item in the nesting queue.

    This represents one "cut" needed - a specific piece that needs
    to be placed on the fabric, possibly flipped for L/R pairing.
    """
    piece: AAMAPiece             # Reference to source piece
    graded_piece: Optional[GradedPiece]  # Graded version (if graded)
    display_name: str            # e.g., "SLEEVE (L)" or "SLEEVE (R)"
    quantity: int                # How many to cut
    flip: bool                   # True = mirror the geometry for cutting
    material: str                # For filtering by fabric type

    def __str__(self) -> str:
        flip_marker = " [FLIP]" if self.flip else ""
        return f"{self.display_name} × {self.quantity}{flip_marker}"


# =============================================================================
# AAMARuleParser
# =============================================================================

class AAMARuleParser:
    """
    Parser for AAMA/ASTM .rul grading rule files.

    Example:
        >>> parser = AAMARuleParser("style.rul")
        >>> rules = parser.parse()
        >>> print(f"Found {rules.num_rules} rules for {rules.header.num_sizes} sizes")
    """

    def __init__(self, rul_path: str):
        """
        Initialize parser with path to .rul file.

        Args:
            rul_path: Path to the .rul file
        """
        self.rul_path = Path(rul_path)
        if not self.rul_path.exists():
            raise FileNotFoundError(f"RUL file not found: {rul_path}")

    def parse(self) -> GradingRules:
        """
        Parse the RUL file and return grading rules.

        Returns:
            GradingRules object with header and all delta rules

        Raises:
            ValueError: If file format is invalid
        """
        with open(self.rul_path, 'r', encoding='utf-8', errors='replace') as f:
            lines = f.readlines()

        # Parse header
        header, rules_start_idx = self._parse_header(lines)

        # Parse rules
        rules = self._parse_rules(lines, rules_start_idx, header.num_sizes)

        logger.info(
            f"Parsed {len(rules)} rules for {header.num_sizes} sizes "
            f"from {self.rul_path.name}"
        )

        return GradingRules(header=header, rules=rules)

    def _parse_header(self, lines: List[str]) -> Tuple[GradingRuleHeader, int]:
        """Parse header section, return header and line index where rules start."""
        header_data = {
            'author': '',
            'product': '',
            'version': '',
            'creation_date': '',
            'creation_time': '',
            'units': 'METRIC',
            'grade_rule_table': '',
            'num_sizes': 0,
            'size_list': [],
            'sample_size': '',
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

        # Find sample size index
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
        self,
        lines: List[str],
        start_idx: int,
        num_sizes: int
    ) -> Dict[int, GradingRule]:
        """Parse all DELTA rules starting from start_idx.

        OptiTex packs multiple dx,dy pairs per line (e.g. 2 pairs per line
        for 6 sizes = 3 data lines), unlike Boke which uses one pair per line.
        This parser handles both layouts by extracting all dx,dy pairs from
        each data line via regex.
        """
        rules = {}
        # Regex for a single dx,dy pair (handles spaces around comma)
        pair_re = re.compile(r'(-?[\d.]+)\s*,\s*(-?[\d.]+)')

        i = start_idx
        while i < len(lines):
            line = lines[i].strip()

            if line == 'END':
                break

            if line.startswith('RULE: DELTA'):
                # Extract rule ID
                match = re.match(r'RULE: DELTA (\d+)', line)
                if match:
                    rule_id = int(match.group(1))

                    # Collect all dx,dy pairs from subsequent data lines.
                    # OptiTex packs multiple pairs per line; we read until
                    # we have num_sizes pairs or hit a boundary.
                    deltas = []
                    j = i + 1
                    while j < len(lines) and len(deltas) < num_sizes:
                        delta_line = lines[j].strip()
                        # Stop at next rule, blank line, or END
                        if not delta_line or delta_line.startswith('RULE:') or delta_line == 'END':
                            break
                        # Extract all dx,dy pairs from this line
                        for m in pair_re.finditer(delta_line):
                            deltas.append((float(m.group(1)), float(m.group(2))))
                        j += 1

                    if len(deltas) >= num_sizes:
                        deltas = deltas[:num_sizes]
                        rules[rule_id] = GradingRule(rule_id=rule_id, deltas=deltas)

                    i = j - 1  # Position before the next i += 1

            i += 1

        return rules


# =============================================================================
# AAMADXFParser
# =============================================================================

class AAMADXFParser:
    """
    Parser for AAMA/ASTM DXF pattern files with grade points.

    Extends standard DXF parsing to extract:
    - Piece boundaries from BLOCKS
    - Grade point markers (POINT entities on Layer 2)
    - Grain lines, sew lines, and other geometry

    Example:
        >>> parser = AAMADXFParser("style.dxf")
        >>> pieces = parser.parse()
        >>> for p in pieces:
        ...     print(f"{p.name}: {p.num_vertices} vertices, {p.num_grade_points} grade points")
    """

    # Coordinate matching tolerance
    TOLERANCE = 0.01

    def __init__(self, dxf_path: str):
        """
        Initialize parser with path to DXF file.

        Args:
            dxf_path: Path to the .dxf file
        """
        self.dxf_path = Path(dxf_path)
        if not self.dxf_path.exists():
            raise FileNotFoundError(f"DXF file not found: {dxf_path}")

        self.doc = ezdxf.readfile(str(self.dxf_path))

    def parse(self) -> List[AAMAPiece]:
        """
        Parse the DXF file and extract all pieces with grade points.

        Returns:
            List of AAMAPiece objects
        """
        pieces = []

        # Iterate through all blocks
        for block in self.doc.blocks:
            # Skip special blocks (model space, paper space, etc.)
            if block.name.startswith('*'):
                continue

            piece = self._parse_block(block)
            if piece is not None:
                pieces.append(piece)

        total_gp = sum(p.num_grade_points for p in pieces)
        logger.info(
            f"Parsed {len(pieces)} pieces with "
            f"{total_gp} boundary grade points "
            f"from {self.dxf_path.name}"
        )

        return pieces

    def _parse_block(self, block) -> Optional[AAMAPiece]:
        """Parse a single block into an AAMAPiece."""
        block_name = block.name

        # Extract boundary vertices (Layer 1)
        vertices = self._extract_boundary_vertices(block)
        if not vertices or len(vertices) < 3:
            logger.debug(f"Skipping block {block_name}: no valid boundary")
            return None

        # Extract boundary grade points using entity-order + explicit rule IDs.
        # This walks the block entities in order, tracking which geometry layer
        # is the "current parent".  Grade points (Layer 2 turn points and
        # Layer 3 curve points) that follow the Layer 1 boundary POLYLINE are
        # boundary grade points; those following Layer 8/14 geometry are not.
        grade_points = self._extract_boundary_grade_points(block, vertices)

        # Extract metadata from TEXT entities
        metadata = self._extract_piece_metadata(block)

        # Extract grain line (Layer 7)
        grain_line = self._extract_grain_line(block)

        # Parse piece name and size from block name
        # Format: "PIECE NAME-SIZE" e.g., "BK R-32"
        piece_name = metadata.get('piece_name', block_name)
        size = metadata.get('size', '')

        if '-' in block_name:
            parts = block_name.rsplit('-', 1)
            if len(parts) == 2:
                piece_name = metadata.get('piece_name', parts[0])
                size = metadata.get('size', parts[1])

        # Parse annotation to extract quantity and material info
        annotation_str = metadata.get('annotation')
        parsed_qty = parse_annotation(annotation_str)

        # Apply Quantity: metadata field as override when annotation parsing
        # didn't detect L/R or a higher quantity.  The DXF TEXT "Quantity: 1,1"
        # means 1 left + 1 right (L/R pair), "Quantity: 2" means 2 copies, etc.
        qty_field = metadata.get('quantity')
        if qty_field:
            qty_override = parse_quantity_field(qty_field)
            if qty_override is not None:
                # Override if annotation gave us defaults (total=1, no L/R)
                if not parsed_qty.has_left_right and parsed_qty.total <= 1:
                    # Preserve material from annotation if available
                    qty_override.material = parsed_qty.material or qty_override.material
                    qty_override.raw = f"{annotation_str} [qty:{qty_field}]" if annotation_str else qty_field
                    parsed_qty = qty_override
                    logger.debug(
                        f"Piece '{piece_name}': quantity overridden by Quantity field "
                        f"'{qty_field}' → total={parsed_qty.total}, L/R={parsed_qty.has_left_right}"
                    )

        # Use material from metadata, falling back to annotation-derived material
        material = metadata.get('material') or parsed_qty.material

        # Detect L/R type based on piece name and quantity annotation
        lr_type = detect_lr_type(piece_name, parsed_qty)

        return AAMAPiece(
            name=piece_name,
            block_name=block_name,
            size=size,
            vertices=vertices,
            grade_points=grade_points,
            layer="1",
            material=material,
            category=metadata.get('category'),
            annotation=annotation_str,
            quantity=parsed_qty,
            lr_type=lr_type,
            grain_line=grain_line
        )

    def _extract_boundary_vertices(self, block) -> List[Tuple[float, float]]:
        """Extract boundary vertices from POLYLINE on Layer 1."""
        vertices = []

        for entity in block:
            layer = entity.dxf.layer if hasattr(entity.dxf, 'layer') else ''

            # Only process Layer 1 (boundary)
            if layer != '1':
                continue

            try:
                if entity.dxftype() == 'LWPOLYLINE':
                    vertices = [(p[0], p[1]) for p in entity.get_points('xy')]
                    if vertices:
                        break

                elif entity.dxftype() == 'POLYLINE':
                    vertices = [
                        (v.dxf.location.x, v.dxf.location.y)
                        for v in entity.vertices
                    ]
                    if vertices:
                        break
            except Exception as e:
                logger.debug(f"Error extracting vertices: {e}")
                continue

        return vertices

    # Layers whose geometry (POLYLINE / LINE) defines gradeable features.
    # Grade points appearing AFTER one of these geometries belong to that feature.
    # KPR variant: layer 14 excluded from parent-tracking — in OptiTex KPR exports
    # the layer-14 auxiliary POLYLINE appears immediately after the layer-1 boundary,
    # which would reset current_parent_layer before grade-point POINT+TEXT pairs are read.
    _GEOMETRY_LAYERS = {'1', '8'}

    # Grade point POINT layers (turn points = 2, curve points = 3).
    # KPR variant: grade rule TEXT labels (e.g. "# 42") are on layer 14, not 2/3.
    _GRADE_POINT_LAYERS = {'2', '3', '14'}

    # Tolerance for matching a grade point coordinate to a boundary vertex.
    TOLERANCE = 0.01

    @staticmethod
    def _parse_rule_id_text(text: str) -> Optional[int]:
        """Parse a rule ID from a Layer 2/3 TEXT entity like '# 315'."""
        m = re.match(r'#\s*(\d+)', text.strip())
        return int(m.group(1)) if m else None

    def _extract_boundary_grade_points(
        self,
        block,
        boundary_vertices: List[Tuple[float, float]],
    ) -> List[GradePoint]:
        """
        Extract grade points that belong to the boundary (Layer 1).

        Uses the AAMA DXF entity ordering convention: within each block,
        geometry entities (POLYLINE/LINE on Layers 1, 8, 14) are followed
        immediately by their associated grade point POINT+TEXT pairs on
        Layers 2 and 3.  This means we can determine which feature a grade
        point belongs to simply by tracking which geometry entity precedes
        it — no distance heuristics needed.

        Rule IDs are read from the paired TEXT entity (e.g. "# 315")
        rather than relying on a fragile global sequential counter.

        Layer 2 = turn points (sharp corners).
        Layer 3 = curve points (smooth curves).
        Both are valid boundary grade points when they follow Layer 1
        geometry.
        """
        # -- Pass 1: collect all entities with types and layers ----------
        entities = []
        for entity in block:
            try:
                layer = entity.dxf.layer if hasattr(entity.dxf, 'layer') else ''
                etype = entity.dxftype()
                entities.append((entity, etype, layer))
            except Exception:
                continue

        # -- Pass 2: walk in order, track current parent geometry layer ---
        # Before any geometry is seen, we are in "no parent" state.
        current_parent_layer: Optional[str] = None
        # Pending POINT waiting for its paired TEXT with the rule ID.
        pending_point: Optional[Tuple[float, float]] = None

        grade_points: List[GradePoint] = []
        claimed_vertices: set = set()

        for entity, etype, layer in entities:
            # -- Geometry entity: update current parent --
            if layer in self._GEOMETRY_LAYERS and etype in (
                'POLYLINE', 'LWPOLYLINE', 'LINE',
            ):
                current_parent_layer = layer
                pending_point = None  # reset
                continue

            # -- Grade point POINT entity --
            if etype == 'POINT' and layer in self._GRADE_POINT_LAYERS:
                if current_parent_layer != '1':
                    # Not a boundary grade point — skip
                    pending_point = None
                    continue
                try:
                    pending_point = (
                        entity.dxf.location.x, entity.dxf.location.y
                    )
                except Exception:
                    pending_point = None
                continue

            # -- Grade point TEXT entity (paired with preceding POINT) --
            if etype in ('TEXT', 'MTEXT') and layer in self._GRADE_POINT_LAYERS:
                if pending_point is None:
                    continue
                # Extract rule ID from text like "# 315"
                try:
                    text = (
                        entity.dxf.text.strip()
                        if etype == 'TEXT'
                        else entity.text.strip()
                    )
                except Exception:
                    pending_point = None
                    continue

                rule_id = self._parse_rule_id_text(text)
                if rule_id is None:
                    pending_point = None
                    continue

                # Match this grade point to the closest boundary vertex
                gp_x, gp_y = pending_point
                best_idx: Optional[int] = None
                best_dist = float('inf')
                for idx, (vx, vy) in enumerate(boundary_vertices):
                    d = abs(gp_x - vx) + abs(gp_y - vy)  # Manhattan
                    if d < best_dist:
                        best_dist = d
                        best_idx = idx

                if best_idx is not None and best_dist < self.TOLERANCE * 2:
                    if best_idx not in claimed_vertices:
                        grade_points.append(GradePoint(
                            vertex_index=best_idx,
                            x=boundary_vertices[best_idx][0],
                            y=boundary_vertices[best_idx][1],
                            rule_id=rule_id,
                        ))
                        claimed_vertices.add(best_idx)
                    # else: vertex already has a grade point — skip duplicate

                pending_point = None
                continue

        # Sort by vertex index for consistent ordering
        grade_points.sort(key=lambda gp: gp.vertex_index)

        return grade_points

    def _extract_piece_metadata(self, block) -> Dict[str, str]:
        """Extract TEXT entities with piece name, material, etc."""
        metadata = {}

        for entity in block:
            if entity.dxftype() not in ('TEXT', 'MTEXT'):
                continue

            try:
                if entity.dxftype() == 'TEXT':
                    text = entity.dxf.text.strip()
                else:
                    text = entity.text.strip()

                # Parse "Key: Value" format
                if ':' in text:
                    key, value = text.split(':', 1)
                    key = key.strip().lower().replace(' ', '_')
                    value = value.strip()

                    if key == 'piece_name':
                        metadata['piece_name'] = value
                    elif key == 'size':
                        metadata['size'] = value
                    elif key == 'material':
                        metadata['material'] = value
                    elif key == 'category':
                        metadata['category'] = value
                    elif key == 'annotation':
                        metadata['annotation'] = value
                    elif key == 'quantity':
                        metadata['quantity'] = value

            except Exception as e:
                logger.debug(f"Error extracting metadata: {e}")
                continue

        return metadata

    def _extract_grain_line(
        self, block
    ) -> Optional[Tuple[Tuple[float, float], Tuple[float, float]]]:
        """Extract grain line from Layer 7."""
        for entity in block:
            layer = entity.dxf.layer if hasattr(entity.dxf, 'layer') else ''
            if layer != '7':
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
                        points = [(v.dxf.location.x, v.dxf.location.y)
                                 for v in entity.vertices]

                    if len(points) >= 2:
                        return ((points[0][0], points[0][1]),
                               (points[-1][0], points[-1][1]))
            except Exception as e:
                logger.debug(f"Error extracting grain line: {e}")
                continue

        return None


# =============================================================================
# AAMAGrader
# =============================================================================

class AAMAGrader:
    """
    Apply grading rules to generate sized patterns.

    Takes base pieces (sample size) and grading rules,
    produces pieces for any target size.

    Example:
        >>> grader = AAMAGrader(pieces, rules)
        >>> size_28_pieces = grader.grade("28")
        >>> size_40_pieces = grader.grade("40")
    """

    def __init__(self, pieces: List[AAMAPiece], rules: GradingRules):
        """
        Initialize grader with pieces and rules.

        Args:
            pieces: List of AAMAPiece from AAMADXFParser
            rules: GradingRules from AAMARuleParser
        """
        self.pieces = pieces
        self.rules = rules

    def grade(self, target_size: str) -> List[GradedPiece]:
        """
        Generate all pieces for a target size.

        Args:
            target_size: Size to generate (must be in rules.header.size_list)

        Returns:
            List of GradedPiece objects for the target size

        Raises:
            ValueError: If target_size is not in the size list
        """
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

    def grade_piece(self, piece: AAMAPiece, target_size: str) -> GradedPiece:
        """
        Grade a single piece to target size.

        Args:
            piece: Source piece (sample size)
            target_size: Target size

        Returns:
            GradedPiece with adjusted vertices
        """
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
            # For simplicity, translate grain line by average delta
            # A more sophisticated approach would interpolate based on position
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

        CRITICAL: This method MUST interpolate non-grade-point vertices
        using distance-based linear interpolation.

        Algorithm:
        1. For grade point vertices: apply delta directly from RUL
        2. For non-grade-point vertices: linear interpolation between
           neighboring grade points based on arc length distance
        """
        if not grade_points:
            # No grade points, return vertices unchanged
            return list(vertices)

        n = len(vertices)

        # Pre-compute cumulative distances along boundary
        cumulative_distances = self._calculate_cumulative_distances(vertices)

        # Build lookup for grade point indices and their deltas.
        # IMPORTANT: When multiple grade points match the same vertex (due to
        # loose tolerance matching), keep only the FIRST match.  The first
        # match is the tightest-tolerance hit and therefore most likely to be
        # the correct rule for that vertex.  Overwriting with later (looser)
        # matches causes wild delta jumps → jagged graded outlines.
        gp_deltas = {}  # vertex_index -> (dx, dy)

        size_index = self.rules.header.size_list.index(target_size)

        for gp in grade_points:
            if gp.vertex_index in gp_deltas:
                continue  # Already have a delta for this vertex — keep first
            if gp.rule_id in self.rules.rules:
                delta = self.rules.rules[gp.rule_id].get_delta(size_index)
                gp_deltas[gp.vertex_index] = delta
            else:
                gp_deltas[gp.vertex_index] = (0.0, 0.0)

        gp_indices = sorted(gp_deltas.keys())

        # Apply deltas to each vertex
        new_vertices = []
        for i, (x, y) in enumerate(vertices):
            if i in gp_deltas:
                # Grade point: apply delta directly
                dx, dy = gp_deltas[i]
            else:
                # Non-grade-point: interpolate
                dx, dy = self._interpolate_vertex_delta(
                    i, vertices, cumulative_distances, gp_indices, gp_deltas
                )

            new_vertices.append((x + dx, y + dy))

        return new_vertices

    def _calculate_cumulative_distances(
        self,
        vertices: List[Tuple[float, float]]
    ) -> List[float]:
        """
        Calculate cumulative arc length along the boundary.

        Returns list where distances[i] = total distance from vertex 0 to vertex i.
        """
        distances = [0.0]
        for i in range(1, len(vertices)):
            x1, y1 = vertices[i - 1]
            x2, y2 = vertices[i]
            segment_length = math.sqrt((x2 - x1)**2 + (y2 - y1)**2)
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
        Calculate interpolated delta for a non-grade-point vertex using
        DISTANCE-BASED interpolation.

        1. Find the previous grade point (G1) and next grade point (G2)
           walking around the boundary
        2. Calculate proportional position t based on arc length distance
        3. Linearly interpolate: delta = (1-t) * delta_G1 + t * delta_G2
        """
        if not grade_point_indices:
            return (0.0, 0.0)

        n_gp = len(grade_point_indices)

        # Find bracketing grade points
        prev_gp_idx = None
        next_gp_idx = None

        for i, gp_idx in enumerate(grade_point_indices):
            if gp_idx > vertex_index:
                next_gp_idx = gp_idx
                prev_gp_idx = grade_point_indices[i - 1] if i > 0 else grade_point_indices[-1]
                break

        if next_gp_idx is None:
            # Vertex is after last grade point, wraps to first
            prev_gp_idx = grade_point_indices[-1]
            next_gp_idx = grade_point_indices[0]

        # Handle case where vertex is before first grade point
        if vertex_index < grade_point_indices[0]:
            prev_gp_idx = grade_point_indices[-1]
            next_gp_idx = grade_point_indices[0]

        # Calculate position ratio using DISTANCE
        dist_to_vertex = cumulative_distances[vertex_index]
        dist_to_prev_gp = cumulative_distances[prev_gp_idx]
        dist_to_next_gp = cumulative_distances[next_gp_idx]

        if next_gp_idx > prev_gp_idx:
            # Normal case: no wrap-around
            span = dist_to_next_gp - dist_to_prev_gp
            if span > 0:
                t = (dist_to_vertex - dist_to_prev_gp) / span
            else:
                t = 0.0
        else:
            # Wrapping case: goes past end of vertex list
            total_perimeter = cumulative_distances[-1]
            # Add distance for the closing segment (last vertex to first vertex)
            x1, y1 = vertices[-1]
            x2, y2 = vertices[0]
            closing_dist = math.sqrt((x2 - x1)**2 + (y2 - y1)**2)
            total_perimeter += closing_dist

            # Distance from prev_gp to next_gp going forward (wrapping)
            span = (total_perimeter - dist_to_prev_gp) + dist_to_next_gp

            # Distance from prev_gp to vertex
            if vertex_index >= prev_gp_idx:
                dist_from_prev = dist_to_vertex - dist_to_prev_gp
            else:
                dist_from_prev = (total_perimeter - dist_to_prev_gp) + dist_to_vertex

            if span > 0:
                t = dist_from_prev / span
            else:
                t = 0.0

        # Clamp t to [0, 1] for safety
        t = max(0.0, min(1.0, t))

        # Interpolate deltas
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
        """Grade the grain line based on nearby vertex transformations."""
        # Find average delta from grade points
        if not grade_points:
            return grain_line

        # Simple approach: translate by centroid movement
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
        """Return list of sizes that can be generated."""
        return self.rules.header.size_list

    def get_sample_size(self) -> str:
        """Return the sample/base size."""
        return self.rules.header.sample_size


# =============================================================================
# Convenience Functions
# =============================================================================

def load_aama_pattern(
    dxf_path: str,
    rul_path: str
) -> Tuple[List[AAMAPiece], GradingRules]:
    """
    Load an AAMA pattern file pair.

    Args:
        dxf_path: Path to .dxf file
        rul_path: Path to .rul file

    Returns:
        Tuple of (pieces, grading_rules)

    Example:
        >>> pieces, rules = load_aama_pattern("style.dxf", "style.rul")
        >>> print(f"Loaded {len(pieces)} pieces")
        >>> print(f"Available sizes: {rules.header.size_list}")
    """
    dxf_parser = AAMADXFParser(dxf_path)
    pieces = dxf_parser.parse()

    rul_parser = AAMARuleParser(rul_path)
    rules = rul_parser.parse()

    # Post-parse validation
    total_rul_rules = rules.num_rules
    boundary_gp_count = sum(p.num_grade_points for p in pieces)

    logger.info(
        f"Grading: {boundary_gp_count} boundary grade points across "
        f"{len(pieces)} pieces, {total_rul_rules} RUL rules"
    )

    return pieces, rules


def grade_to_nesting_pieces(
    dxf_path: str,
    rul_path: str,
    target_sizes: List[str],
    rotations: List[float] = [0, 180],
    allow_flip: bool = False,
    units: str = 'METRIC'
) -> List[Piece]:
    """
    Load AAMA pattern and generate Piece objects for nesting.

    Args:
        dxf_path: Path to .dxf file
        rul_path: Path to .rul file
        target_sizes: List of sizes to generate
        rotations: Allowed rotation angles
        allow_flip: Whether to allow flipping
        units: Unit system from RUL file ('METRIC' = mm, 'ENGLISH' = inches)

    Returns:
        List of Piece objects ready for nesting engine

    Example:
        >>> pieces = grade_to_nesting_pieces(
        ...     "style.dxf",
        ...     "style.rul",
        ...     target_sizes=["28", "32", "40"],
        ...     rotations=[0, 180]
        ... )
    """
    # Load pattern
    aama_pieces, rules = load_aama_pattern(dxf_path, rul_path)

    # Create grader
    grader = AAMAGrader(aama_pieces, rules)

    # Determine unit conversion
    if rules.header.units == 'ENGLISH':
        to_mm = 25.4
    else:
        to_mm = 1.0  # Already in mm

    # Detect grain axis from original pieces (before grading)
    pattern_grain_axis = _detect_grain_axis(aama_pieces)
    if pattern_grain_axis == 'y':
        logger.info("Grain detected along DXF Y — will swap coordinates")

    nesting_pieces = []

    for target_size in target_sizes:
        if target_size not in grader.get_available_sizes():
            logger.warning(f"Skipping unknown size: {target_size}")
            continue

        graded = grader.grade(target_size)

        for gp in graded:
            # Convert vertices to mm with grain-aware orientation
            vertices_mm = _orient_for_grain(
                gp.vertices, gp.grain_line, pattern_grain_axis, to_mm
            )

            # Clean vertices (remove consecutive duplicates)
            vertices_mm = _clean_vertices(vertices_mm)

            if len(vertices_mm) < 3:
                logger.warning(f"Skipping piece {gp.name} - too few vertices")
                continue

            # Ensure polygon is closed
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

            # Create identifier
            identifier = PieceIdentifier(
                piece_name=gp.name,
                size=target_size
            )

            # Create orientation constraint
            orientation = OrientationConstraint(
                allowed_rotations=rotations,
                allow_flip=allow_flip
            )

            # Create grain constraint with grain-aware coordinates
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

    return nesting_pieces


def _detect_grain_axis(
    pieces,
) -> str:
    """
    Detect whether grain lines run along DXF X or DXF Y axis.

    Examines grain_line attributes across all pieces and votes.
    Returns 'x' if grain is predominantly horizontal in DXF coords,
    'y' if predominantly vertical.

    This determines whether vertices need a coordinate swap to align
    grain with the nesting strip length (X-axis).
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
    # Default to 'x' (no swap) if tied or no grain lines found
    return 'x' if x_votes >= y_votes else 'y'


def _orient_for_grain(
    vertices: List[Tuple[float, float]],
    grain_line,
    pattern_grain_axis: str,
    scale: float,
) -> List[Tuple[float, float]]:
    """
    Convert vertices to mm and swap coordinates if grain runs along DXF Y.

    Per-piece grain_line overrides the pattern-level default when present.
    After conversion, vertices are translated to the origin so that absolute
    DXF positions don't interfere with nesting (e.g. a piece at DXF X=250"
    would be 6350mm, exceeding any fabric width).
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

    # Translate to origin — only the shape matters for nesting
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

    The translation must match _orient_for_grain so grain line is relative
    to the piece's normalized vertices.
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


def _clean_vertices(
    vertices: List[Tuple[float, float]],
    tolerance: float = 0.001
) -> List[Tuple[float, float]]:
    """
    Remove consecutive duplicate vertices.

    Args:
        vertices: List of (x, y) coordinate tuples
        tolerance: Distance threshold for considering points duplicate

    Returns:
        Cleaned list of vertices with consecutive duplicates removed
    """
    if len(vertices) < 2:
        return vertices

    cleaned = [vertices[0]]
    for x, y in vertices[1:]:
        prev_x, prev_y = cleaned[-1]
        dist = math.sqrt((x - prev_x)**2 + (y - prev_y)**2)
        if dist > tolerance:
            cleaned.append((x, y))

    return cleaned


def clean_vertices_for_spyrrow(
    vertices: List[Tuple[float, float]],
    tolerance: float = 0.01,
) -> List[Tuple[float, float]]:
    """
    Prepare AAMA-graded vertices for the Spyrrow/jagua-rs solver.

    AAMA grader produces clean polygons from rule-based grading. The only
    issue is occasional duplicate vertices (consecutive from grading, or
    non-consecutive from rule intersection). This function removes all
    duplicates and re-closes the polygon.

    No simplification is applied — AAMA curves are already clean polylines
    where every vertex is geometrically significant.

    This function is specific to AAMA parser output. Other parsers have
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


# =============================================================================
# Multi-Material Workflow Functions
# =============================================================================

def get_pieces_by_material(pieces: List[AAMAPiece]) -> Dict[str, List[AAMAPiece]]:
    """
    Group AAMA pieces by material type.

    Material is extracted from:
    1. The piece's `material` field (from DXF TEXT metadata)
    2. The piece's `quantity.material` field (from annotation parsing)
    3. Falls back to "UNKNOWN" if neither is available

    Args:
        pieces: List of AAMAPiece objects

    Returns:
        Dictionary mapping material name (uppercase) to list of pieces.
        Keys are sorted alphabetically.

    Example:
        >>> pieces_by_material = get_pieces_by_material(aama_pieces)
        >>> for material, material_pieces in pieces_by_material.items():
        ...     print(f"{material}: {len(material_pieces)} pieces")
        FINISH: 3 pieces
        IL: 8 pieces
        SHELL: 15 pieces
    """
    result: Dict[str, List[AAMAPiece]] = {}

    for piece in pieces:
        # Determine material (prioritize explicit material field)
        material = piece.material

        if not material and piece.quantity and piece.quantity.material:
            material = piece.quantity.material

        if not material:
            material = "UNKNOWN"

        # Normalize to uppercase
        material = material.upper()

        if material not in result:
            result[material] = []
        result[material].append(piece)

    # Return sorted by material name
    return dict(sorted(result.items()))


def get_available_materials(pieces: List[AAMAPiece]) -> List[str]:
    """
    Get sorted list of unique materials in the pattern.

    Convenience function that returns just the material names.

    Args:
        pieces: List of AAMAPiece objects

    Returns:
        Sorted list of unique material names (uppercase)

    Example:
        >>> materials = get_available_materials(aama_pieces)
        >>> print(materials)
        ['FINISH', 'IL', 'SHELL']
    """
    return list(get_pieces_by_material(pieces).keys())


def generate_nesting_queue(
    pieces: List[AAMAPiece],
    material_filter: Optional[str] = None
) -> List[NestingQueueItem]:
    """
    Generate nesting queue from parsed pieces.

    Handles all three L/R patterns:
    - SEPARATE_LEFT/RIGHT: Add as-is, no flip
    - FLIP_FOR_LR: Add left_qty normal + right_qty flipped
    - NONE: Add as-is, no flip

    Args:
        pieces: List of parsed AAMAPiece objects
        material_filter: Optional material to filter (e.g., "SHELL")

    Returns:
        List of NestingQueueItem ready for nesting

    Example:
        >>> queue = generate_nesting_queue(pieces, "SHELL")
        >>> for item in queue:
        ...     print(f"{item.display_name}: {item.quantity} {'[FLIP]' if item.flip else ''}")
        SLEEVE (L): 1
        SLEEVE (R): 1 [FLIP]
        BACK: 1
    """
    queue: List[NestingQueueItem] = []

    for piece in pieces:
        # Filter by material if specified
        if material_filter:
            piece_material = piece.material or ""
            if piece_material.upper() != material_filter.upper():
                continue

        material = piece.material or "UNKNOWN"

        if piece.lr_type == LRType.FLIP_FOR_LR:
            # Pattern B: Single geometry, add L (normal) and R (flipped)
            if piece.quantity.left_qty > 0:
                queue.append(NestingQueueItem(
                    piece=piece,
                    graded_piece=None,
                    display_name=f"{piece.name} (L)",
                    quantity=piece.quantity.left_qty,
                    flip=False,
                    material=material
                ))
            if piece.quantity.right_qty > 0:
                queue.append(NestingQueueItem(
                    piece=piece,
                    graded_piece=None,
                    display_name=f"{piece.name} (R)",
                    quantity=piece.quantity.right_qty,
                    flip=True,
                    material=material
                ))

        elif piece.lr_type == LRType.SEPARATE_LEFT:
            # Pattern A: Already LEFT geometry
            queue.append(NestingQueueItem(
                piece=piece,
                graded_piece=None,
                display_name=piece.name,  # Name already has LEFT
                quantity=piece.quantity.total,
                flip=False,
                material=material
            ))

        elif piece.lr_type == LRType.SEPARATE_RIGHT:
            # Pattern A: Already RIGHT geometry
            queue.append(NestingQueueItem(
                piece=piece,
                graded_piece=None,
                display_name=piece.name,  # Name already has RIGHT
                quantity=piece.quantity.total,
                flip=False,
                material=material
            ))

        else:
            # Pattern C: Center/symmetric piece (LRType.NONE)
            queue.append(NestingQueueItem(
                piece=piece,
                graded_piece=None,
                display_name=piece.name,
                quantity=piece.quantity.total,
                flip=False,
                material=material
            ))

    return queue


def grade_material_to_nesting_pieces(
    dxf_path: str,
    rul_path: str,
    material: str,
    target_sizes: List[str],
    rotations: List[float] = [0, 180],
    allow_flip: bool = False
) -> List[Piece]:
    """
    Load AAMA pattern, filter by material, grade, and return Piece objects.

    This is the main entry point for multi-material workflow. Call this
    function once per material to generate nesting pieces for separate
    nesting runs.

    Args:
        dxf_path: Path to .dxf file
        rul_path: Path to .rul file
        material: Material type to filter (case-insensitive, e.g., "SHELL", "IL")
        target_sizes: List of sizes to generate
        rotations: Allowed rotation angles (default [0, 180] for grain constraint)
        allow_flip: Whether to allow flipping (consider L/R from annotation)

    Returns:
        List of Piece objects for the specified material, ready for nesting

    Example:
        >>> # Nest shell pieces separately from interlining
        >>> shell_pieces = grade_material_to_nesting_pieces(
        ...     "style.dxf", "style.rul",
        ...     material="SHELL",
        ...     target_sizes=["S", "M", "L"]
        ... )
        >>> il_pieces = grade_material_to_nesting_pieces(
        ...     "style.dxf", "style.rul",
        ...     material="IL",
        ...     target_sizes=["S", "M", "L"]
        ... )

    Note:
        Pieces with L/R specification in annotation (has_left_right=True)
        will be set up for paired flipping automatically if allow_flip=True.
    """
    # Load pattern
    aama_pieces, rules = load_aama_pattern(dxf_path, rul_path)

    # Filter by material (case-insensitive)
    material_upper = material.upper()
    pieces_by_material = get_pieces_by_material(aama_pieces)

    if material_upper not in pieces_by_material:
        available = list(pieces_by_material.keys())
        logger.warning(
            f"Material '{material}' not found. "
            f"Available materials: {available}"
        )
        return []

    filtered_pieces = pieces_by_material[material_upper]

    # Create grader with filtered pieces
    grader = AAMAGrader(filtered_pieces, rules)

    # Determine unit conversion
    if rules.header.units == 'ENGLISH':
        to_mm = 25.4
    else:
        to_mm = 1.0

    # Detect grain axis from filtered pieces
    pattern_grain_axis = _detect_grain_axis(filtered_pieces)
    if pattern_grain_axis == 'y':
        logger.info(
            f"Material '{material}': grain detected along DXF Y "
            f"— will swap coordinates"
        )

    nesting_pieces = []

    for target_size in target_sizes:
        if target_size not in grader.get_available_sizes():
            logger.warning(f"Skipping unknown size: {target_size}")
            continue

        graded = grader.grade(target_size)

        for gp in graded:
            # Find original AAMA piece to get quantity info
            original_piece = next(
                (p for p in filtered_pieces if p.name == gp.source_piece),
                None
            )

            # Convert vertices to mm with grain-aware orientation
            vertices_mm = _orient_for_grain(
                gp.vertices, gp.grain_line, pattern_grain_axis, to_mm
            )
            vertices_mm = _clean_vertices(vertices_mm)

            if len(vertices_mm) < 3:
                logger.warning(f"Skipping piece {gp.name} - too few vertices")
                continue

            # Ensure polygon is closed
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

            # Determine if piece should allow flip based on L/R annotation
            piece_allow_flip = allow_flip
            if original_piece and original_piece.quantity.has_left_right:
                # Piece has L/R specification - should allow flip for pairing
                piece_allow_flip = True

            # Create identifier
            identifier = PieceIdentifier(
                piece_name=gp.name,
                size=target_size
            )

            # Create orientation constraint
            orientation = OrientationConstraint(
                allowed_rotations=rotations,
                allow_flip=piece_allow_flip
            )

            # Create grain constraint with grain-aware coordinates
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
        f"for material '{material}' across {len(target_sizes)} sizes"
    )

    return nesting_pieces


# =============================================================================
# Summary/Stats Function
# =============================================================================

def print_aama_summary(dxf_path: str, rul_path: str) -> None:
    """
    Print summary statistics for AAMA pattern files.

    Useful for verification and debugging.
    """
    print(f"\n{'='*60}")
    print("AAMA Pattern Summary")
    print(f"{'='*60}")

    # Parse RUL
    print(f"\nRUL File: {rul_path}")
    rul_parser = AAMARuleParser(rul_path)
    rules = rul_parser.parse()

    print(f"  Author: {rules.header.author}")
    print(f"  Units: {rules.header.units}")
    print(f"  Number of sizes: {rules.header.num_sizes}")
    print(f"  Size list: {' '.join(rules.header.size_list)}")
    print(f"  Sample size: {rules.header.sample_size} (index {rules.header.sample_size_index})")
    print(f"  Number of rules: {rules.num_rules}")

    # Verify sample size has zero deltas
    if rules.num_rules > 0:
        rule_1 = rules.rules.get(1)
        if rule_1:
            sample_delta = rule_1.get_delta(rules.header.sample_size_index)
            print(f"  Rule 1 sample delta: {sample_delta}")

    # Parse DXF
    print(f"\nDXF File: {dxf_path}")
    dxf_parser = AAMADXFParser(dxf_path)
    pieces = dxf_parser.parse()

    print(f"  Number of pieces: {len(pieces)}")

    total_vertices = sum(p.num_vertices for p in pieces)
    total_grade_points = sum(p.num_grade_points for p in pieces)

    print(f"  Total vertices: {total_vertices}")
    print(f"  Total grade points: {total_grade_points}")

    print(f"\nPiece Details:")
    print(f"  {'Name':<25} {'Vertices':>10} {'Grade Pts':>10} {'Material':>10}")
    print(f"  {'-'*55}")

    for p in pieces[:10]:  # Show first 10
        material = p.material or '-'
        print(f"  {p.name:<25} {p.num_vertices:>10} {p.num_grade_points:>10} {material:>10}")

    if len(pieces) > 10:
        print(f"  ... and {len(pieces) - 10} more pieces")

    # Verify grade point to rule mapping
    print(f"\nGrade Point Rule Mapping:")
    if pieces:
        p = pieces[0]
        print(f"  First piece '{p.name}':")
        print(f"    Vertices: {p.num_vertices}")
        print(f"    Grade points: {p.num_grade_points}")
        if p.grade_points:
            print(f"    First grade point: vertex {p.grade_points[0].vertex_index}, rule {p.grade_points[0].rule_id}")
            print(f"    Last grade point: vertex {p.grade_points[-1].vertex_index}, rule {p.grade_points[-1].rule_id}")

    print(f"\n{'='*60}\n")


if __name__ == "__main__":
    # Example usage
    import sys

    if len(sys.argv) >= 3:
        dxf_path = sys.argv[1]
        rul_path = sys.argv[2]
    else:
        # Use sample files from data/dxf-amaa/
        import os
        data_dir = Path(__file__).parent.parent.parent.parent / "data" / "dxf-amaa"

        # Find files
        dxf_files = list(data_dir.glob("*.dxf"))
        rul_files = list(data_dir.glob("*.rul"))

        if dxf_files and rul_files:
            dxf_path = str(dxf_files[0])
            rul_path = str(rul_files[0])
        else:
            print("No sample files found. Usage: python aama_parser.py <dxf_path> <rul_path>")
            sys.exit(1)

    print_aama_summary(dxf_path, rul_path)
