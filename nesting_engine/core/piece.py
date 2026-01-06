"""
Piece definition for the nesting engine.

This module provides the Piece class and related types for representing
garment pattern pieces with full industry metadata.

IMPORTANT NAMING CONVENTION - Two Different "Mirror" Concepts:

1. FOLD LINE (fold_line attribute):
   - A geometric feature INSIDE the pattern (from DXF Layer 6)
   - Represents a line of symmetry where fabric is folded during cutting
   - Stored as two points defining the fold axis
   - This is READ from the pattern file and stored as reference geometry
   - Example: A half-bodice pattern that's cut on a fold

2. FLIP (in OrientationConstraint.allow_flip):
   - A NESTING PLACEMENT decision
   - Controls whether the piece can be reflected during nesting
   - Used for left/right paired pieces (e.g., left and right shirt fronts)
   - The nesting engine decides whether to flip each placement
   - Example: demand=2 with flip=True → 1 normal + 1 flipped placement

These are COMPLETELY SEPARATE concepts:
- A piece can have a fold_line AND allow flipping (rare but possible)
- A piece can have a fold_line but NOT allow flipping (cut-on-fold pieces)
- A piece can have NO fold_line but allow flipping (paired pieces like sleeves)
- A piece can have neither (regular pieces with fixed orientation)

Example:
    >>> from nesting_engine.core.piece import Piece, PieceIdentifier, OrientationConstraint
    
    # A sleeve piece that can be flipped for left/right
    >>> sleeve = Piece(
    ...     vertices=[(0, 0), (100, 0), (80, 150), (20, 150), (0, 0)],
    ...     identifier=PieceIdentifier(piece_name="Sleeve", size="M"),
    ...     orientation=OrientationConstraint(
    ...         allowed_rotations=[0, 180],
    ...         allow_flip=True  # Can flip for left/right placement
    ...     )
    ... )
    
    # A bodice piece cut on fold (has fold_line, no flip)
    >>> bodice = Piece(
    ...     vertices=[(0, 0), (150, 0), (140, 200), (0, 200), (0, 0)],
    ...     identifier=PieceIdentifier(piece_name="Front Bodice", size="M"),
    ...     fold_line=((0, 0), (0, 200)),  # Geometric fold axis from DXF
    ...     orientation=OrientationConstraint(
    ...         allowed_rotations=[0, 180],
    ...         allow_flip=False  # Cannot flip - always same orientation
    ...     )
    ... )
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional, Tuple, Set

from nesting_engine.core.geometry import Polygon, Point, BoundingBox, Coordinate


class GrainDirection(Enum):
    """
    Fabric grain direction constraint.
    
    The grain line indicates how the pattern piece should be aligned
    with the fabric grain (warp/weft direction).
    
    Values:
        LENGTHWISE: Parallel to selvage (most common, along warp)
        CROSSWISE: Perpendicular to selvage (along weft)
        BIAS: 45° to selvage (for stretch/drape)
        ANY: No grain constraint (can be placed in any direction)
    """
    LENGTHWISE = "lengthwise"
    CROSSWISE = "crosswise"
    BIAS = "bias"
    ANY = "any"
    
    def __str__(self) -> str:
        return self.value


@dataclass
class GrainConstraint:
    """
    Defines grain line direction and constraints for a piece.
    
    The grain line is typically marked on pattern pieces (DXF Layer 7)
    to indicate the required alignment with fabric grain direction.
    
    Attributes:
        direction: The required grain direction (lengthwise, crosswise, bias, any)
        grain_line_start: Start point of grain line (from DXF Layer 7), in mm
        grain_line_end: End point of grain line (from DXF Layer 7), in mm
        tolerance_degrees: Allowed deviation from ideal grain direction
        
    Note:
        The grain line points define a LINE SEGMENT on the pattern piece
        that must be aligned with the specified grain direction on the fabric.
        This is different from the fold_line (symmetry axis from Layer 6).
    """
    direction: GrainDirection = GrainDirection.LENGTHWISE
    grain_line_start: Optional[Coordinate] = None
    grain_line_end: Optional[Coordinate] = None
    tolerance_degrees: float = 0.0
    
    @property
    def has_grain_line(self) -> bool:
        """Check if a grain line is defined."""
        return self.grain_line_start is not None and self.grain_line_end is not None
    
    @property
    def grain_line_angle(self) -> Optional[float]:
        """
        Calculate the angle of the grain line in degrees.
        
        Returns angle from horizontal (0° = pointing right, 90° = pointing up).
        Returns None if no grain line is defined.
        """
        if not self.has_grain_line:
            return None
        
        import math
        dx = self.grain_line_end[0] - self.grain_line_start[0]
        dy = self.grain_line_end[1] - self.grain_line_start[1]
        return math.degrees(math.atan2(dy, dx))


@dataclass
class OrientationConstraint:
    """
    Defines allowed orientations for a piece during NESTING.
    
    This controls how the piece can be placed by the nesting engine:
    - Which rotation angles are allowed
    - Whether the piece can be flipped (for left/right paired pieces)
    
    IMPORTANT: 'allow_flip' is a NESTING decision, separate from 'fold_line'.
    
    - allow_flip=True: The nesting engine CAN flip (reflect) this piece
      to create left/right pairs or to achieve better material utilization.
      
    - allow_flip=False: The piece must always be placed in its original
      orientation (possibly rotated, but never flipped/reflected).
    
    Attributes:
        allowed_rotations: List of allowed rotation angles in degrees.
                          Common values: [0, 180] for grain-constrained pieces,
                          [0, 90, 180, 270] for non-constrained pieces.
        allow_flip: If True, the nesting engine can flip/reflect the piece.
                   Used for paired pieces (left/right sleeves, etc.)
                   
    Example:
        # Grain-constrained piece, no flip (typical for most pieces)
        >>> OrientationConstraint(allowed_rotations=[0, 180], allow_flip=False)
        
        # Paired piece (left/right), allow flip
        >>> OrientationConstraint(allowed_rotations=[0, 180], allow_flip=True)
        
        # No grain constraint, allow any orientation
        >>> OrientationConstraint(allowed_rotations=[0, 90, 180, 270], allow_flip=True)
    """
    allowed_rotations: List[float] = field(default_factory=lambda: [0.0, 180.0])
    allow_flip: bool = False
    
    def get_all_orientations(self) -> List[Tuple[float, bool]]:
        """
        Generate all valid (rotation, flipped) combinations for nesting.
        
        Returns:
            List of (rotation_degrees, is_flipped) tuples representing
            all valid placement orientations for the nesting engine.
        """
        orientations = []
        for rotation in self.allowed_rotations:
            orientations.append((rotation, False))
            if self.allow_flip:
                orientations.append((rotation, True))
        return orientations
    
    @property
    def num_orientations(self) -> int:
        """Total number of valid orientations."""
        multiplier = 2 if self.allow_flip else 1
        return len(self.allowed_rotations) * multiplier


@dataclass
class PieceIdentifier:
    """
    Rich identifier for garment pieces.
    
    Supports industry naming conventions with multiple levels of identification.
    
    Attributes:
        piece_name: Name of the piece (e.g., "Front Panel", "Sleeve", "Collar")
        style_name: Style/design name (e.g., "Oxford Shirt", "Polo")
        style_number: Style reference number (e.g., "STY-2024-001")
        size: Size identifier (e.g., "M", "32", "10-12")
        size_range: Size range if graded (e.g., "S-XL", "8-16")
        color: Color/colorway identifier (e.g., "Navy", "White")
        ply: Number of fabric layers (usually 1 or 2)
        custom_id: Any additional custom identifier
        
    Example:
        >>> id = PieceIdentifier(
        ...     piece_name="Front Panel",
        ...     style_name="Oxford Shirt",
        ...     style_number="OX-2024-M",
        ...     size="M"
        ... )
        >>> id.full_id
        'Front_Panel_OX-2024-M_M'
    """
    piece_name: str
    style_name: Optional[str] = None
    style_number: Optional[str] = None
    size: Optional[str] = None
    size_range: Optional[str] = None
    color: Optional[str] = None
    ply: int = 1
    custom_id: Optional[str] = None
    
    @property
    def full_id(self) -> str:
        """
        Generate a unique identifier string.
        
        Format: {piece_name}_{style_number}_{size}_{custom_id}
        (only non-None parts are included)
        """
        parts = [self.piece_name.replace(" ", "_")]
        if self.style_number:
            parts.append(self.style_number)
        if self.size:
            parts.append(self.size)
        if self.custom_id:
            parts.append(self.custom_id)
        return "_".join(parts)
    
    @property
    def display_name(self) -> str:
        """Human-readable display name."""
        parts = [self.piece_name]
        if self.size:
            parts.append(f"({self.size})")
        return " ".join(parts)
    
    def __str__(self) -> str:
        return self.full_id


@dataclass
class Piece:
    """
    Complete piece definition for nesting.
    
    A Piece represents a single pattern piece that can be placed in a nesting
    solution. It includes:
    - Geometry: The polygon outline (vertices in mm)
    - Identification: Name, style, size, etc.
    - Constraints: Grain direction, allowed orientations
    - Geometric features: Fold line (from DXF Layer 6), notches, drill holes
    
    IMPORTANT - Understanding fold_line vs flip:
    
    The 'fold_line' attribute stores a geometric feature FROM THE PATTERN FILE.
    It's a line segment (two points) from DXF Layer 6 that indicates where
    the fabric is folded during cutting. This is REFERENCE DATA - the nesting
    engine doesn't use it directly for placement decisions.
    
    The 'orientation.allow_flip' attribute controls whether the NESTING ENGINE
    can flip/reflect the piece during placement. This is a PLACEMENT DECISION
    separate from the fold_line geometry.
    
    Attributes:
        vertices: Polygon vertices in mm (closed, CCW winding)
        identifier: Piece identification metadata
        grain: Grain direction constraints
        orientation: Allowed rotations and flip settings for nesting
        fold_line: GEOMETRIC fold line from DXF Layer 6 (NOT a nesting setting).
                  This is a line segment (start, end) marking the symmetry axis
                  for pieces that are "cut on the fold".
        notches: List of notch positions (from DXF Layer 4)
        drill_holes: List of drill hole positions (from DXF Layer 13)
        internal_lines: List of internal line segments (from DXF Layer 8)
        
    Example:
        >>> piece = Piece(
        ...     vertices=[(0, 0), (100, 0), (100, 150), (0, 150), (0, 0)],
        ...     identifier=PieceIdentifier(piece_name="Front Panel", size="M"),
        ...     grain=GrainConstraint(direction=GrainDirection.LENGTHWISE),
        ...     orientation=OrientationConstraint(
        ...         allowed_rotations=[0, 180],
        ...         allow_flip=False
        ...     )
        ... )
        >>> piece.area
        15000.0
    """
    # Core geometry (in mm, internal unit)
    vertices: List[Coordinate]
    
    # Identification
    identifier: PieceIdentifier
    
    # Constraints
    grain: GrainConstraint = field(default_factory=GrainConstraint)
    orientation: OrientationConstraint = field(default_factory=OrientationConstraint)
    
    # Geometric features from DXF (reference data, NOT nesting controls)
    # ========================================================================
    # FOLD LINE: Geometric feature from DXF Layer 6
    # This is a LINE SEGMENT inside the pattern marking the fold axis.
    # It is NOT the same as "flipping" during nesting.
    # A piece "cut on the fold" has this line along one edge.
    # ========================================================================
    fold_line: Optional[Tuple[Coordinate, Coordinate]] = None
    
    # Other geometric features from DXF
    notches: List[Coordinate] = field(default_factory=list)
    drill_holes: List[Coordinate] = field(default_factory=list)
    internal_lines: List[Tuple[Coordinate, Coordinate]] = field(default_factory=list)
    
    # Cached computed properties
    _polygon: Optional[Polygon] = field(default=None, repr=False, compare=False)
    
    def __post_init__(self):
        """Validate piece after creation."""
        if len(self.vertices) < 4:
            raise ValueError(
                f"Piece must have at least 3 vertices (plus closing vertex). "
                f"Got {len(self.vertices)}"
            )
    
    @property
    def id(self) -> str:
        """Unique identifier string."""
        return self.identifier.full_id
    
    @property
    def name(self) -> str:
        """Piece name."""
        return self.identifier.piece_name
    
    @property
    def polygon(self) -> Polygon:
        """Get the piece geometry as a Polygon object."""
        if self._polygon is None:
            self._polygon = Polygon(self.vertices)
        return self._polygon
    
    @property
    def area(self) -> float:
        """Area of the piece in mm²."""
        return self.polygon.area
    
    @property
    def bounding_box(self) -> BoundingBox:
        """Axis-aligned bounding box."""
        return self.polygon.bounding_box
    
    @property
    def width(self) -> float:
        """Width of bounding box in mm."""
        return self.polygon.width
    
    @property
    def height(self) -> float:
        """Height of bounding box in mm."""
        return self.polygon.height
    
    @property
    def centroid(self) -> Point:
        """Center of mass."""
        return self.polygon.centroid
    
    @property
    def perimeter(self) -> float:
        """Perimeter (edge length) in mm."""
        return self.polygon.perimeter
    
    @property
    def has_fold_line(self) -> bool:
        """
        Check if this piece has a fold line (DXF Layer 6 feature).
        
        A fold line indicates the piece is meant to be "cut on the fold",
        where the fabric is folded and only half the pattern is cut.
        
        This is a GEOMETRIC FEATURE, not a nesting control.
        """
        return self.fold_line is not None
    
    @property
    def can_be_flipped(self) -> bool:
        """
        Check if this piece can be flipped during NESTING.
        
        This is a NESTING CONTROL - whether the engine can reflect
        the piece for left/right pairs or better utilization.
        
        This is SEPARATE from the fold_line geometric feature.
        """
        return self.orientation.allow_flip
    
    def normalize_to_origin(self) -> "Piece":
        """
        Return a new Piece with bounding box starting at (0, 0).
        
        Useful for standardizing pieces before storage or comparison.
        
        Returns:
            New Piece with translated geometry
        """
        bb = self.bounding_box
        offset_x, offset_y = bb.min_x, bb.min_y
        
        # Translate vertices
        new_vertices = [(x - offset_x, y - offset_y) for x, y in self.vertices]
        
        # Translate fold line if present
        new_fold_line = None
        if self.fold_line:
            new_fold_line = (
                (self.fold_line[0][0] - offset_x, self.fold_line[0][1] - offset_y),
                (self.fold_line[1][0] - offset_x, self.fold_line[1][1] - offset_y)
            )
        
        # Translate notches
        new_notches = [(x - offset_x, y - offset_y) for x, y in self.notches]
        
        # Translate drill holes
        new_drill_holes = [(x - offset_x, y - offset_y) for x, y in self.drill_holes]
        
        # Translate internal lines
        new_internal_lines = [
            ((s[0] - offset_x, s[1] - offset_y), (e[0] - offset_x, e[1] - offset_y))
            for s, e in self.internal_lines
        ]
        
        # Translate grain line if present
        new_grain = GrainConstraint(
            direction=self.grain.direction,
            tolerance_degrees=self.grain.tolerance_degrees
        )
        if self.grain.has_grain_line:
            new_grain.grain_line_start = (
                self.grain.grain_line_start[0] - offset_x,
                self.grain.grain_line_start[1] - offset_y
            )
            new_grain.grain_line_end = (
                self.grain.grain_line_end[0] - offset_x,
                self.grain.grain_line_end[1] - offset_y
            )
        
        return Piece(
            vertices=new_vertices,
            identifier=self.identifier,
            grain=new_grain,
            orientation=self.orientation,
            fold_line=new_fold_line,
            notches=new_notches,
            drill_holes=new_drill_holes,
            internal_lines=new_internal_lines
        )
    
    def get_transformed_polygon(
        self, 
        rotation: float = 0.0, 
        flipped: bool = False,
        translation: Optional[Coordinate] = None
    ) -> Polygon:
        """
        Get the piece polygon with transformations applied.
        
        This is used by the nesting engine to get the actual geometry
        for a specific placement.
        
        Args:
            rotation: Rotation angle in degrees (CCW positive)
            flipped: Whether to flip the piece (NESTING flip, not fold_line)
            translation: (x, y) translation to apply
            
        Returns:
            Transformed Polygon
            
        Note:
            The 'flipped' parameter is a NESTING PLACEMENT decision.
            It is SEPARATE from the fold_line geometric feature.
        """
        poly = self.polygon
        
        # Apply flip first (around centroid)
        if flipped:
            poly = poly.flip_horizontal()
        
        # Apply rotation (around centroid)
        if rotation != 0:
            poly = poly.rotate(rotation)
        
        # Apply translation
        if translation:
            poly = poly.translate(translation[0], translation[1])
        
        return poly
    
    def to_spyrrow_format(self) -> List[Tuple[float, float]]:
        """
        Convert vertices to spyrrow format.
        
        Spyrrow expects a list of (x, y) tuples with the polygon closed.
        
        Returns:
            List of coordinate tuples ready for spyrrow.Item
        """
        # Ensure polygon is closed
        verts = list(self.vertices)
        if verts[0] != verts[-1]:
            verts.append(verts[0])
        return verts
    
    def to_spyrrow_orientations(self) -> List[float]:
        """
        Convert orientation constraints to spyrrow format.
        
        Spyrrow uses allowed_orientations as a list of angles in degrees.
        For flipped pieces, we add the flip as 180° offset variations.
        
        Returns:
            List of allowed rotation angles for spyrrow
            
        Note:
            Spyrrow handles flip implicitly through allowed orientations.
            If allow_flip is True, we include both normal and flipped variants.
        """
        # For now, just return the rotations
        # Spyrrow may need special handling for flip - to be refined
        return list(self.orientation.allowed_rotations)
    
    def __str__(self) -> str:
        return f"Piece({self.identifier.display_name}, area={self.area:.1f}mm²)"
    
    def __repr__(self) -> str:
        return (
            f"Piece(id='{self.id}', vertices={len(self.vertices)}, "
            f"area={self.area:.1f}, has_fold_line={self.has_fold_line}, "
            f"can_flip={self.can_be_flipped})"
        )
