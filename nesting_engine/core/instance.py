"""
Nesting instance (problem definition) for the nesting engine.

This module provides:
- Container: The sheet/bin to nest pieces into
- NestingItem: A piece with demand quantity and nesting parameters
- FlipMode: How flipping should be handled during nesting
- NestingInstance: Complete problem definition

NAMING CONVENTION REMINDER:

This module deals with NESTING decisions, not pattern geometry.

- FlipMode: Controls how pieces are FLIPPED during NESTING placement
- NestingItem.flip_mode: Specifies if/how this item can be flipped when placed

These are NESTING CONTROLS, completely separate from:
- Piece.fold_line: A geometric feature inside the pattern (DXF Layer 6)

Example:
    >>> from nesting_engine.core.instance import NestingInstance, NestingItem, Container
    >>> from nesting_engine.core.piece import Piece, PieceIdentifier
    
    # Create a simple nesting problem
    >>> container = Container(width=1500, height=None)  # Strip packing
    >>> items = [
    ...     NestingItem(piece=front_panel, demand=2, flip_mode=FlipMode.NONE),
    ...     NestingItem(piece=sleeve, demand=2, flip_mode=FlipMode.PAIRED),
    ... ]
    >>> instance = NestingInstance(
    ...     id="job_001",
    ...     name="Men's Shirt Marker",
    ...     container=container,
    ...     items=items
    ... )
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional, Tuple
from datetime import datetime
import uuid

from nesting_engine.core.units import LengthUnit, UnitContext, UnitConverter
from nesting_engine.core.piece import Piece
from nesting_engine.core.geometry import Coordinate


class FlipMode(Enum):
    """
    Specifies how a piece should be FLIPPED during NESTING placement.
    
    This is a NESTING CONTROL - it tells the nesting engine how to handle
    flip/reflection when placing pieces.
    
    IMPORTANT: This is COMPLETELY SEPARATE from Piece.fold_line, which is
    a geometric feature inside the pattern (from DXF Layer 6).
    
    Values:
        NONE: Never flip. All copies placed in original orientation.
              Use for: Most pieces where orientation matters.
              
        PAIRED: Half the copies are flipped to create left/right pairs.
                Use for: Paired pieces like left/right sleeves, shirt fronts.
                Example: demand=4 with PAIRED → 2 normal + 2 flipped placements.
                
        ANY: Each copy can be flipped independently for better utilization.
             The engine decides per-placement whether to flip.
             Use for: Symmetric pieces or when orientation doesn't matter.
    
    Example:
        # Sleeves - need left/right pairs
        >>> NestingItem(piece=sleeve, demand=4, flip_mode=FlipMode.PAIRED)
        # This produces: 2 normal (right sleeves) + 2 flipped (left sleeves)
        
        # Collar - symmetric, flip for better fit
        >>> NestingItem(piece=collar, demand=2, flip_mode=FlipMode.ANY)
        # Engine decides per-placement whether to flip
        
        # Back panel - no flipping allowed
        >>> NestingItem(piece=back_panel, demand=1, flip_mode=FlipMode.NONE)
    """
    NONE = "none"      # Never flip - all copies same orientation
    PAIRED = "paired"  # Half normal, half flipped (for left/right pairs)
    ANY = "any"        # Engine decides per-placement (for better utilization)
    
    def __str__(self) -> str:
        return self.value


@dataclass
class Container:
    """
    The sheet/bin/container for nesting.
    
    For strip packing (common in garment manufacturing), set height=None.
    The nesting engine will minimize the used length (strip_length).
    
    Attributes:
        width: Container width in mm (internal unit)
        height: Container height in mm, or None for strip packing
        original_width: Original width value before conversion (for display)
        original_height: Original height value before conversion (for display)
        original_unit: Original unit before conversion (for display)
        
    Example:
        # Strip packing with 60-inch wide fabric
        >>> container = Container.from_inches(width=60, height=None)
        >>> container.width
        1524.0  # mm
        
        # Fixed bin
        >>> container = Container(width=1000, height=800)
    """
    width: float  # mm (internal)
    height: Optional[float] = None  # mm, None = strip packing
    
    # Original values for display/export
    original_width: Optional[float] = None
    original_height: Optional[float] = None
    original_unit: Optional[LengthUnit] = None
    
    def __post_init__(self):
        if self.width <= 0:
            raise ValueError(f"Container width must be positive, got {self.width}")
        if self.height is not None and self.height <= 0:
            raise ValueError(f"Container height must be positive, got {self.height}")
        
        # Store original values if not set
        if self.original_width is None:
            self.original_width = self.width
            self.original_unit = LengthUnit.MILLIMETER
    
    @property
    def is_strip_packing(self) -> bool:
        """True if this is a strip packing problem (open-ended height)."""
        return self.height is None
    
    @property
    def area(self) -> Optional[float]:
        """
        Container area in mm², or None for strip packing.
        
        For strip packing, area depends on the solution's strip_length.
        """
        if self.height is None:
            return None
        return self.width * self.height
    
    def display_dimensions(self, unit: Optional[LengthUnit] = None) -> str:
        """
        Format dimensions for display.
        
        Args:
            unit: Unit for display (default: original unit)
            
        Returns:
            Formatted string like "60.00 in × open" or "1000.00 mm × 800.00 mm"
        """
        if unit is None:
            unit = self.original_unit or LengthUnit.MILLIMETER
        
        width_display = UnitConverter.from_mm(self.width, unit)
        
        if self.height is None:
            return f"{width_display:.2f} {unit.value} × open (strip)"
        else:
            height_display = UnitConverter.from_mm(self.height, unit)
            return f"{width_display:.2f} {unit.value} × {height_display:.2f} {unit.value}"
    
    @classmethod
    def from_inches(cls, width: float, height: Optional[float] = None) -> "Container":
        """Create container with dimensions in inches."""
        width_mm = UnitConverter.to_mm(width, LengthUnit.INCH)
        height_mm = UnitConverter.to_mm(height, LengthUnit.INCH) if height else None
        return cls(
            width=width_mm,
            height=height_mm,
            original_width=width,
            original_height=height,
            original_unit=LengthUnit.INCH
        )
    
    @classmethod
    def from_cm(cls, width: float, height: Optional[float] = None) -> "Container":
        """Create container with dimensions in centimeters."""
        width_mm = UnitConverter.to_mm(width, LengthUnit.CENTIMETER)
        height_mm = UnitConverter.to_mm(height, LengthUnit.CENTIMETER) if height else None
        return cls(
            width=width_mm,
            height=height_mm,
            original_width=width,
            original_height=height,
            original_unit=LengthUnit.CENTIMETER
        )


@dataclass
class NestingItem:
    """
    A piece to be nested with quantity and NESTING constraints.
    
    This wraps a Piece with nesting-specific parameters:
    - demand: How many copies to place
    - flip_mode: How flipping should be handled during NESTING
    - priority: Placement priority (higher = placed first)
    
    IMPORTANT - flip_mode vs Piece.fold_line:
    
    flip_mode is a NESTING CONTROL that tells the engine how to flip
    pieces during placement. It is COMPLETELY SEPARATE from Piece.fold_line,
    which is a geometric feature inside the pattern.
    
    A piece might have:
    - fold_line (geometric) + flip_mode=NONE: Cut-on-fold piece, no nesting flip
    - No fold_line + flip_mode=PAIRED: Regular piece, create left/right pairs
    - No fold_line + flip_mode=NONE: Regular piece, fixed orientation
    
    Attributes:
        piece: The Piece to nest
        demand: Number of copies to place
        flip_mode: How to handle flipping during NESTING (not geometry!)
        priority: Placement priority (higher values placed first)
        
    Example:
        # Sleeve pair - 2 left + 2 right
        >>> NestingItem(
        ...     piece=sleeve_piece,
        ...     demand=4,
        ...     flip_mode=FlipMode.PAIRED  # Creates 2 normal + 2 flipped
        ... )
    """
    piece: Piece
    demand: int = 1
    flip_mode: FlipMode = FlipMode.NONE
    priority: int = 0
    
    def __post_init__(self):
        if self.demand < 1:
            raise ValueError(f"Demand must be at least 1, got {self.demand}")
        
        # Validate flip_mode against piece constraints
        if self.flip_mode != FlipMode.NONE and not self.piece.can_be_flipped:
            # Warning: flip_mode set but piece doesn't allow flipping
            # This could be intentional override or a mistake
            pass  # TODO: Add logging warning
    
    @property
    def total_area(self) -> float:
        """Total area needed for all copies of this item (mm²)."""
        return self.piece.area * self.demand
    
    @property
    def piece_id(self) -> str:
        """ID of the wrapped piece."""
        return self.piece.id
    
    def get_placement_breakdown(self) -> List[Tuple[bool, int]]:
        """
        Get breakdown of how many pieces need which orientation.
        
        Returns:
            List of (is_flipped, count) tuples for NESTING placement.
            
        Example:
            >>> item = NestingItem(piece=sleeve, demand=4, flip_mode=FlipMode.PAIRED)
            >>> item.get_placement_breakdown()
            [(False, 2), (True, 2)]  # 2 normal + 2 flipped
        """
        if self.flip_mode == FlipMode.NONE:
            return [(False, self.demand)]
        
        elif self.flip_mode == FlipMode.PAIRED:
            # Half and half (extra goes to non-flipped)
            half = self.demand // 2
            remainder = self.demand % 2
            result = []
            if half + remainder > 0:
                result.append((False, half + remainder))
            if half > 0:
                result.append((True, half))
            return result
        
        else:  # FlipMode.ANY
            # Engine decides per-placement, return as flexible
            return [(False, self.demand)]  # All flexible
    
    def __str__(self) -> str:
        flip_str = f", flip={self.flip_mode.value}" if self.flip_mode != FlipMode.NONE else ""
        return f"NestingItem({self.piece.name} ×{self.demand}{flip_str})"


@dataclass
class NestingInstance:
    """
    Complete nesting problem definition.
    
    A NestingInstance contains everything needed to solve a nesting problem:
    - Container (sheet/bin dimensions)
    - Items (pieces with quantities)
    - Parameters (buffers, unit context)
    
    This is the input to the nesting engine.
    
    Attributes:
        id: Unique identifier for this instance
        name: Human-readable name
        container: The container/sheet to nest into
        items: List of NestingItems (pieces with quantities)
        piece_buffer: Minimum distance between pieces (mm)
        edge_buffer: Minimum distance from container edges (mm)
        unit_context: Unit configuration for this job
        created_at: Creation timestamp
        metadata: Additional metadata (style info, customer, etc.)
        
    Example:
        >>> instance = NestingInstance(
        ...     id="marker_001",
        ...     name="Men's Dress Shirt - Size M",
        ...     container=Container.from_inches(60, None),
        ...     items=[
        ...         NestingItem(front_panel, demand=2),
        ...         NestingItem(back_panel, demand=1),
        ...         NestingItem(sleeve, demand=2, flip_mode=FlipMode.PAIRED),
        ...         NestingItem(collar, demand=1),
        ...     ],
        ...     piece_buffer=2.0,  # 2mm between pieces
        ...     edge_buffer=5.0,   # 5mm from edges
        ... )
    """
    id: str
    name: str
    container: Container
    items: List[NestingItem]
    
    # Nesting parameters
    piece_buffer: float = 0.0  # mm - minimum distance between pieces
    edge_buffer: float = 0.0   # mm - minimum distance from container edges
    
    # Unit context
    unit_context: UnitContext = field(default_factory=UnitContext)
    
    # Metadata
    created_at: datetime = field(default_factory=datetime.now)
    metadata: dict = field(default_factory=dict)
    
    def __post_init__(self):
        if not self.items:
            raise ValueError("NestingInstance must have at least one item")
    
    @property
    def total_piece_count(self) -> int:
        """Total number of pieces to place."""
        return sum(item.demand for item in self.items)
    
    @property
    def total_piece_area(self) -> float:
        """Total area of all pieces to place (mm²)."""
        return sum(item.total_area for item in self.items)
    
    @property
    def unique_piece_count(self) -> int:
        """Number of unique piece types."""
        return len(self.items)
    
    @property
    def is_strip_packing(self) -> bool:
        """True if this is a strip packing problem."""
        return self.container.is_strip_packing
    
    def get_effective_container_width(self) -> float:
        """Container width minus edge buffers."""
        return self.container.width - 2 * self.edge_buffer
    
    def get_effective_container_height(self) -> Optional[float]:
        """Container height minus edge buffers, or None for strip packing."""
        if self.container.height is None:
            return None
        return self.container.height - 2 * self.edge_buffer
    
    def get_theoretical_min_length(self) -> float:
        """
        Calculate theoretical minimum strip length (lower bound).
        
        This is the total piece area divided by container width.
        Actual length will always be >= this value.
        
        Returns:
            Minimum possible strip length in mm
        """
        effective_width = self.get_effective_container_width()
        return self.total_piece_area / effective_width
    
    def summary(self) -> str:
        """Generate a summary string for this instance."""
        lines = [
            f"NestingInstance: {self.name} ({self.id})",
            f"  Container: {self.container.display_dimensions()}",
            f"  Pieces: {self.total_piece_count} total ({self.unique_piece_count} unique)",
            f"  Total area: {self.total_piece_area:.1f} mm²",
            f"  Buffers: piece={self.piece_buffer}mm, edge={self.edge_buffer}mm",
        ]
        if self.is_strip_packing:
            min_length = self.get_theoretical_min_length()
            lines.append(f"  Theoretical min length: {min_length:.1f} mm")
        return "\n".join(lines)
    
    @classmethod
    def create(
        cls,
        name: str,
        container: Container,
        items: List[NestingItem],
        piece_buffer: float = 0.0,
        edge_buffer: float = 0.0,
        **metadata
    ) -> "NestingInstance":
        """
        Factory method to create a NestingInstance with auto-generated ID.
        
        Args:
            name: Human-readable name
            container: Container/sheet definition
            items: List of NestingItems
            piece_buffer: Minimum distance between pieces (mm)
            edge_buffer: Minimum distance from edges (mm)
            **metadata: Additional metadata fields
            
        Returns:
            New NestingInstance
        """
        instance_id = f"inst_{uuid.uuid4().hex[:8]}"
        return cls(
            id=instance_id,
            name=name,
            container=container,
            items=items,
            piece_buffer=piece_buffer,
            edge_buffer=edge_buffer,
            metadata=metadata
        )
    
    def __str__(self) -> str:
        return f"NestingInstance({self.name}: {self.total_piece_count} pieces)"
