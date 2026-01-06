"""
DXF Parser for garment pattern files.

Supports:
- Gerber Technology DXF format (primary)
- AAMA/ASTM DXF format (secondary)

This parser extracts closed polylines as piece boundaries and associates
text labels found inside each piece to determine piece names and sizes.

Unit Handling:
- Reads $INSUNITS from DXF header
- Converts all coordinates to millimeters internally
- Common values: 1=inches, 4=mm, 5=cm

Example:
    >>> from nesting_engine.io.dxf_parser import DXFParser
    >>> parser = DXFParser("marker.dxf")
    >>> pieces = parser.extract_pieces()
    >>> for piece in pieces:
    ...     print(f"{piece.name}: {piece.area_mm2:.0f} mm²")
"""

from __future__ import annotations

import re
import logging
from pathlib import Path
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Tuple, Any

import ezdxf
from shapely.geometry import Polygon, Point
from shapely.validation import make_valid

from nesting_engine.core.units import LengthUnit, UnitConverter
from nesting_engine.core.geometry import Polygon as NestingPolygon
from nesting_engine.core.piece import (
    Piece, PieceIdentifier, OrientationConstraint, GrainConstraint, GrainDirection
)

logger = logging.getLogger(__name__)


# DXF $INSUNITS codes
DXF_UNIT_MAP = {
    0: None,  # Unitless
    1: LengthUnit.INCH,
    2: LengthUnit.INCH,  # Feet - treat as inches for now (will convert)
    4: LengthUnit.MILLIMETER,
    5: LengthUnit.CENTIMETER,
    6: LengthUnit.METER,
}


@dataclass
class ParsedPiece:
    """
    Raw piece data extracted from DXF before conversion to Piece object.
    """
    vertices: List[Tuple[float, float]]  # In original DXF units
    layer: str
    piece_name: Optional[str] = None
    size: Optional[str] = None
    pattern_id: Optional[str] = None  # A, B, C... letter
    raw_texts: List[str] = field(default_factory=list)
    area_dxf_units: float = 0.0
    bounds: Optional[Tuple[float, float, float, float]] = None  # minx, miny, maxx, maxy


@dataclass
class DXFParseResult:
    """
    Complete result of parsing a DXF file.
    """
    pieces: List[ParsedPiece]
    unit: Optional[LengthUnit]
    marker_info: Optional[Dict[str, Any]] = None  # Width, length, utilization if found
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    
    @property
    def piece_count(self) -> int:
        return len(self.pieces)


class DXFParser:
    """
    Parser for garment pattern DXF files.
    
    Handles both Gerber Technology format and AAMA/ASTM format.
    
    Gerber format characteristics:
    - All boundaries on layer like 'T001L001'
    - Text on 'ABC' layer
    - Piece names inside boundaries
    
    AAMA/ASTM format characteristics:
    - Layer 1: Piece boundary
    - Layer 4: Sew line
    - Layer 6: Mirror/fold line
    - Layer 7: Grain line
    - Layer 8: Annotation
    
    Example:
        >>> parser = DXFParser("marker.dxf")
        >>> result = parser.parse()
        >>> print(f"Found {result.piece_count} pieces")
        >>> 
        >>> # Convert to nesting Pieces
        >>> pieces = parser.to_nesting_pieces(result)
    """
    
    # Minimum area threshold (in square inches) to filter noise
    MIN_AREA_SQ_IN = 1.0
    
    # Size keywords to identify size labels
    SIZE_KEYWORDS = [
        'XXXS', 'XXS', 'XS', 'S', 'M', 'L', 'XL', 'XXL', 'XXXL', 'XXXXL',
        'S/T', 'M/T', 'L/T', 'XL/T', 'XXL/T',  # Tall variants
        '0', '2', '4', '6', '8', '10', '12', '14', '16', '18', '20',  # Numeric sizes
    ]
    
    # Pattern ID letters
    PATTERN_ID_LETTERS = list('ABCDEFGHIJKLMNOPQRSTUVWXYZ')
    
    def __init__(self, dxf_path: str):
        """
        Initialize parser with DXF file path.
        
        Args:
            dxf_path: Path to DXF file
        """
        self.dxf_path = Path(dxf_path)
        if not self.dxf_path.exists():
            raise FileNotFoundError(f"DXF file not found: {dxf_path}")
        
        self.doc = ezdxf.readfile(str(self.dxf_path))
        self.modelspace = self.doc.modelspace()
        
    def get_units(self) -> Optional[LengthUnit]:
        """
        Get the unit of measurement from DXF header.
        
        Returns:
            LengthUnit or None if unitless/unknown
        """
        try:
            insunits = self.doc.header.get('$INSUNITS', 0)
            return DXF_UNIT_MAP.get(insunits)
        except Exception:
            return None
    
    def parse(self) -> DXFParseResult:
        """
        Parse the DXF file and extract all pieces.
        
        Returns:
            DXFParseResult with pieces, units, and any errors/warnings
        """
        errors = []
        warnings = []
        
        # Get units
        unit = self.get_units()
        if unit is None:
            warnings.append("Could not determine units from DXF header, assuming inches")
            unit = LengthUnit.INCH
        
        # Extract text labels first
        text_labels = self._extract_text_labels()
        
        # Extract marker info from text (if present)
        marker_info = self._extract_marker_info(text_labels)
        
        # Extract closed polylines
        polylines = self._extract_polylines()
        
        if not polylines:
            errors.append("No closed polylines found in DXF file")
            return DXFParseResult([], unit, marker_info, errors, warnings)
        
        # Find the container (largest polyline) if this is a nested marker
        container_idx = self._find_container(polylines)
        
        # Convert polylines to ParsedPiece objects
        pieces = []
        for idx, pdata in enumerate(polylines):
            # Skip container
            if idx == container_idx:
                continue
            
            # Filter by minimum area
            if pdata['area'] < self.MIN_AREA_SQ_IN:
                continue
            
            # Match text labels to this piece
            text_data = self._match_texts_to_piece(pdata, text_labels)
            
            piece = ParsedPiece(
                vertices=pdata['vertices'],
                layer=pdata['layer'],
                piece_name=text_data.get('piece_name'),
                size=text_data.get('size'),
                pattern_id=text_data.get('pattern_id'),
                raw_texts=text_data.get('raw_texts', []),
                area_dxf_units=pdata['area'],
                bounds=pdata['bounds']
            )
            pieces.append(piece)
        
        logger.info(f"Parsed {len(pieces)} pieces from {self.dxf_path.name}")
        
        return DXFParseResult(pieces, unit, marker_info, errors, warnings)
    
    def _extract_text_labels(self) -> List[Dict]:
        """Extract all text entities and their positions."""
        labels = []
        
        for entity in self.modelspace:
            if entity.dxftype() == 'TEXT':
                try:
                    labels.append({
                        'text': entity.dxf.text.strip(),
                        'x': entity.dxf.insert.x,
                        'y': entity.dxf.insert.y,
                        'layer': entity.dxf.layer
                    })
                except Exception:
                    continue
            elif entity.dxftype() == 'MTEXT':
                try:
                    labels.append({
                        'text': entity.text.strip(),
                        'x': entity.dxf.insert.x,
                        'y': entity.dxf.insert.y,
                        'layer': entity.dxf.layer
                    })
                except Exception:
                    continue
        
        return labels
    
    def _extract_marker_info(self, text_labels: List[Dict]) -> Optional[Dict]:
        """
        Extract marker info from text labels.
        
        Looks for patterns like: W=70.000IN L=17.9997 YD U=71.623%
        """
        for label in text_labels:
            text = label['text']
            
            # Look for marker info pattern
            if 'W=' in text and 'U=' in text:
                info = {}
                
                # Width
                width_match = re.search(r'W=([\d.]+)\s*IN', text)
                if width_match:
                    info['width_inches'] = float(width_match.group(1))
                
                # Length
                length_match = re.search(r'L=([\d.]+)\s*YD', text)
                if length_match:
                    info['length_yards'] = float(length_match.group(1))
                
                # Utilization
                util_match = re.search(r'U=([\d.]+)%', text)
                if util_match:
                    info['utilization_percent'] = float(util_match.group(1))
                
                # Model info
                model_match = re.search(r'MODEL:([^\s]+)', text)
                if model_match:
                    info['model'] = model_match.group(1)
                
                if info:
                    return info
        
        return None
    
    def _extract_polylines(self) -> List[Dict]:
        """Extract all closed polylines as potential pieces."""
        polylines = []
        
        for entity in self.modelspace:
            vertices = None
            layer = None
            
            try:
                if entity.dxftype() == 'LWPOLYLINE':
                    # Modern lightweight polyline
                    vertices = list(entity.get_points('xy'))
                    layer = entity.dxf.layer
                    is_closed = entity.closed or entity.is_closed
                    
                elif entity.dxftype() == 'POLYLINE':
                    # Legacy polyline with VERTEX sub-entities
                    vertices = [(v.dxf.location.x, v.dxf.location.y) for v in entity.vertices]
                    layer = entity.dxf.layer
                    is_closed = entity.is_closed
                
                if vertices and len(vertices) >= 3:
                    # Check if implicitly closed
                    if not is_closed and len(vertices) >= 3:
                        first, last = vertices[0], vertices[-1]
                        dist = ((first[0]-last[0])**2 + (first[1]-last[1])**2)**0.5
                        is_closed = dist < 0.01
                    
                    if is_closed:
                        # Create shapely polygon for area and validation
                        try:
                            poly = Polygon(vertices)
                            if not poly.is_valid:
                                poly = make_valid(poly)
                            
                            if poly.area > 0:
                                bounds = poly.bounds
                                polylines.append({
                                    'vertices': [(float(x), float(y)) for x, y in vertices],
                                    'layer': layer,
                                    'area': poly.area,
                                    'bounds': bounds,
                                    'polygon': poly
                                })
                        except Exception as e:
                            logger.debug(f"Failed to create polygon: {e}")
                            
            except Exception as e:
                logger.debug(f"Failed to extract {entity.dxftype()}: {e}")
        
        return polylines
    
    def _find_container(self, polylines: List[Dict]) -> Optional[int]:
        """
        Find the container (bounding rectangle) in a nested marker.
        
        The container is typically:
        - The largest polyline by area
        - Contains most other polylines
        
        Returns:
            Index of container polyline, or None if not found
        """
        if len(polylines) < 2:
            return None
        
        # Find largest by area
        areas = [p['area'] for p in polylines]
        max_idx = areas.index(max(areas))
        max_area = areas[max_idx]
        
        # Check if it's significantly larger (>10x) than the next largest
        sorted_areas = sorted(areas, reverse=True)
        if len(sorted_areas) > 1:
            ratio = sorted_areas[0] / sorted_areas[1]
            if ratio > 10:
                return max_idx
        
        # Check containment
        largest_poly = polylines[max_idx]['polygon']
        contained_count = 0
        for i, pdata in enumerate(polylines):
            if i != max_idx:
                if largest_poly.contains(pdata['polygon']):
                    contained_count += 1
        
        # If it contains most pieces, it's the container
        if contained_count > len(polylines) * 0.5:
            return max_idx
        
        return None
    
    def _match_texts_to_piece(self, piece_data: Dict, text_labels: List[Dict]) -> Dict:
        """
        Find text labels that are inside a piece polygon.
        
        Returns dict with:
        - piece_name: Pattern name (e.g., "24-0391-P2-BKX1")
        - size: Size label (e.g., "M", "XL")
        - pattern_id: Letter identifier (e.g., "A", "B")
        - raw_texts: All texts found inside
        """
        result = {
            'piece_name': None,
            'size': None,
            'pattern_id': None,
            'raw_texts': []
        }
        
        poly = piece_data.get('polygon')
        if poly is None:
            return result
        
        for label in text_labels:
            try:
                pt = Point(label['x'], label['y'])
                if poly.contains(pt):
                    text = label['text']
                    result['raw_texts'].append(text)
                    
                    text_upper = text.upper().strip()
                    
                    # Check for size
                    if text_upper in self.SIZE_KEYWORDS:
                        result['size'] = text_upper
                    
                    # Check for pattern ID (single letter)
                    elif len(text_upper) == 1 and text_upper in self.PATTERN_ID_LETTERS:
                        result['pattern_id'] = text_upper
                    
                    # Check for piece name (longer identifier)
                    elif len(text) > 5 and '-' in text:
                        result['piece_name'] = text
                        
            except Exception:
                continue
        
        return result
    
    def _clean_vertices(self, vertices: List[Tuple[float, float]]) -> List[Tuple[float, float]]:
        """
        Remove duplicate vertices (non-consecutive) that cause spyrrow to fail.
        Keeps the polygon valid by removing points that are too close together.
        
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
    
    def to_nesting_pieces(
        self,
        result: Optional[DXFParseResult] = None,
        default_rotations: List[float] = [0, 180],
        allow_flip: bool = False
    ) -> List[Piece]:
        """
        Convert parsed pieces to nesting engine Piece objects.
        
        Args:
            result: Parse result (if None, will parse the file)
            default_rotations: Allowed rotation angles in degrees
            allow_flip: Whether pieces can be flipped during nesting
            
        Returns:
            List of Piece objects ready for nesting
        """
        if result is None:
            result = self.parse()
        
        # Determine conversion factor to mm
        if result.unit == LengthUnit.INCH:
            to_mm = 25.4
        elif result.unit == LengthUnit.CENTIMETER:
            to_mm = 10.0
        elif result.unit == LengthUnit.METER:
            to_mm = 1000.0
        else:
            to_mm = 1.0  # Assume mm
        
        pieces = []
        piece_counter = {}  # Track duplicate names
        
        for parsed in result.pieces:
            # Convert vertices to mm
            vertices_mm = [(x * to_mm, y * to_mm) for x, y in parsed.vertices]
            
            # Clean vertices: remove non-consecutive duplicates that cause spyrrow to fail
            vertices_mm = self._clean_vertices(vertices_mm)
            
            # Skip if too few vertices after cleaning
            if len(vertices_mm) < 3:
                logger.warning(f"Skipping piece with insufficient vertices after cleaning")
                continue
            
            # Generate piece name
            base_name = parsed.piece_name or f"Piece_{len(pieces)+1}"
            
            # Handle duplicate names
            if base_name in piece_counter:
                piece_counter[base_name] += 1
                unique_name = f"{base_name}_{piece_counter[base_name]}"
            else:
                piece_counter[base_name] = 1
                unique_name = base_name
            
            # Create identifier
            identifier = PieceIdentifier(
                piece_name=unique_name,
                size=parsed.size,
                style_name=self._extract_style_from_name(parsed.piece_name)
            )
            
            # Create orientation constraint
            orientation = OrientationConstraint(
                allowed_rotations=default_rotations,
                allow_flip=allow_flip
            )
            
            # Create grain constraint (default to lengthwise)
            grain = GrainConstraint(direction=GrainDirection.LENGTHWISE)
            
            try:
                piece = Piece(
                    vertices=vertices_mm,
                    identifier=identifier,
                    orientation=orientation,
                    grain=grain
                )
                pieces.append(piece)
            except Exception as e:
                logger.warning(f"Failed to create piece {unique_name}: {e}")
        
        return pieces
    
    def _extract_style_from_name(self, name: Optional[str]) -> Optional[str]:
        """Extract style number from piece name like '24-0391-P2-BKX1'."""
        if not name:
            return None
        
        # Try to extract style number (e.g., "24-0391")
        match = re.match(r'^(\d+-\d+)', name)
        if match:
            return match.group(1)
        
        return None


def load_pieces_from_dxf(
    dxf_path: str,
    rotations: List[float] = [0, 180],
    allow_flip: bool = False
) -> Tuple[List[Piece], DXFParseResult]:
    """
    Convenience function to load pieces from a DXF file.
    
    Args:
        dxf_path: Path to DXF file
        rotations: Allowed rotation angles
        allow_flip: Whether to allow flipping
        
    Returns:
        Tuple of (pieces list, parse result)
        
    Example:
        >>> pieces, result = load_pieces_from_dxf("marker.dxf")
        >>> print(f"Loaded {len(pieces)} pieces")
        >>> if result.marker_info:
        ...     print(f"Utilization: {result.marker_info['utilization_percent']}%")
    """
    parser = DXFParser(dxf_path)
    result = parser.parse()
    pieces = parser.to_nesting_pieces(result, rotations, allow_flip)
    return pieces, result
