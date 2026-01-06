"""
Nesting solution representation.

This module provides:
- PlacedPiece: A single piece placement in the solution
- NestingSolution: Complete solution with all placements and metrics

NAMING CONVENTION REMINDER:

PlacedPiece.flipped indicates whether the piece was FLIPPED during NESTING.
This is a PLACEMENT DECISION made by the nesting engine.

It is COMPLETELY SEPARATE from Piece.fold_line, which is a geometric
feature inside the pattern (from DXF Layer 6).

Example:
    >>> from nesting_engine.core.solution import NestingSolution, PlacedPiece
    
    # Check a placed piece
    >>> placement = solution.placements[0]
    >>> placement.flipped  # Was this piece flipped during NESTING?
    True
    >>> placement.piece.has_fold_line  # Does the pattern have a fold line?
    False  # These are independent!
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Tuple, Dict, Any
from datetime import datetime

from nesting_engine.core.geometry import Polygon, BoundingBox, Coordinate
from nesting_engine.core.piece import Piece
from nesting_engine.core.instance import NestingInstance


@dataclass
class PlacedPiece:
    """
    A single piece placement in a nesting solution.
    
    Represents where and how a piece is placed:
    - Position (x, y) of the piece reference point
    - Rotation angle applied
    - Whether the piece was FLIPPED during NESTING
    
    IMPORTANT - flipped attribute:
    
    The 'flipped' attribute indicates a NESTING PLACEMENT decision.
    It means the piece was reflected/mirrored when placed.
    
    This is COMPLETELY SEPARATE from Piece.fold_line:
    - fold_line: A geometric feature INSIDE the pattern (from DXF Layer 6)
    - flipped: A PLACEMENT decision made by the nesting engine
    
    A piece can be:
    - flipped=True with no fold_line: Regular piece placed as mirror (left/right pair)
    - flipped=False with fold_line: Cut-on-fold piece placed normally
    - Any combination - they are independent concepts!
    
    Attributes:
        piece_id: ID of the placed piece
        instance_index: Which copy of the piece (0-based) for multi-demand items
        x: X position of placement (mm, from container origin)
        y: Y position of placement (mm, from container origin)
        rotation: Rotation angle in degrees (CCW positive)
        flipped: True if piece was FLIPPED during NESTING placement.
                 This is a NESTING decision, NOT related to fold_line.
                 
    Example:
        >>> placement = PlacedPiece(
        ...     piece_id="sleeve_001",
        ...     instance_index=1,  # Second copy of this piece
        ...     x=150.5,
        ...     y=200.3,
        ...     rotation=180.0,
        ...     flipped=True  # This is a left sleeve (flipped from right)
        ... )
    """
    piece_id: str
    instance_index: int
    x: float  # mm
    y: float  # mm
    rotation: float = 0.0  # degrees, CCW positive
    flipped: bool = False  # NESTING flip decision (NOT fold_line!)
    
    @property
    def position(self) -> Coordinate:
        """Placement position as (x, y) tuple."""
        return (self.x, self.y)
    
    @property
    def placement_id(self) -> str:
        """Unique identifier for this specific placement."""
        return f"{self.piece_id}_{self.instance_index}"
    
    def get_transform_description(self) -> str:
        """Human-readable description of the transformation."""
        parts = []
        if self.rotation != 0:
            parts.append(f"rotated {self.rotation}°")
        if self.flipped:
            parts.append("flipped")  # NESTING flip
        if not parts:
            return "no transform"
        return ", ".join(parts)
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            "piece_id": self.piece_id,
            "instance_index": self.instance_index,
            "x": self.x,
            "y": self.y,
            "rotation": self.rotation,
            "flipped": self.flipped,  # NESTING flip state
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "PlacedPiece":
        """Create from dictionary."""
        return cls(
            piece_id=data["piece_id"],
            instance_index=data["instance_index"],
            x=data["x"],
            y=data["y"],
            rotation=data.get("rotation", 0.0),
            flipped=data.get("flipped", False),
        )
    
    def __str__(self) -> str:
        flip_str = " [flipped]" if self.flipped else ""
        return (
            f"PlacedPiece({self.piece_id}[{self.instance_index}] "
            f"at ({self.x:.1f}, {self.y:.1f}), rot={self.rotation}°{flip_str})"
        )


@dataclass
class NestingSolution:
    """
    Complete nesting solution with placements and metrics.
    
    A NestingSolution contains:
    - All piece placements
    - Performance metrics (utilization, strip length, etc.)
    - Metadata (engine used, computation time, etc.)
    
    Attributes:
        instance_id: ID of the NestingInstance this solves
        placements: List of all PlacedPiece objects
        strip_length: Length of the used strip (mm) for strip packing
        container_width: Width of the container (mm)
        container_height: Height used (mm) - equals strip_length for strip packing
        computation_time_ms: Time to compute solution (milliseconds)
        engine_name: Name of the nesting engine used
        engine_version: Version of the engine
        created_at: Solution creation timestamp
        metadata: Additional metadata
        
    Example:
        >>> solution = NestingSolution(
        ...     instance_id="marker_001",
        ...     placements=placements,
        ...     strip_length=1850.5,
        ...     container_width=1524.0,
        ...     computation_time_ms=2500,
        ...     engine_name="spyrrow",
        ...     engine_version="0.8.0"
        ... )
        >>> solution.utilization
        0.8645  # 86.45% utilization
    """
    instance_id: str
    placements: List[PlacedPiece]
    strip_length: float  # mm
    container_width: float  # mm
    container_height: Optional[float] = None  # mm, None for strip packing
    computation_time_ms: float = 0.0
    engine_name: str = "unknown"
    engine_version: str = "0.0.0"
    created_at: datetime = field(default_factory=datetime.now)
    metadata: dict = field(default_factory=dict)
    
    # Cached values
    _piece_areas: Optional[Dict[str, float]] = field(default=None, repr=False, compare=False)
    
    @property
    def num_placements(self) -> int:
        """Total number of placed pieces."""
        return len(self.placements)
    
    @property
    def used_area(self) -> float:
        """
        Total container area used (mm²).
        
        For strip packing: width × strip_length
        For fixed bin: width × height
        """
        height = self.container_height if self.container_height else self.strip_length
        return self.container_width * height
    
    @property
    def utilization(self) -> float:
        """
        Material utilization ratio (0.0 to 1.0).
        
        Calculated as: total_piece_area / used_container_area
        
        Returns:
            Utilization as a float (e.g., 0.85 = 85%)
        """
        if self.used_area == 0:
            return 0.0
        return self.total_piece_area / self.used_area
    
    @property
    def utilization_percent(self) -> float:
        """Material utilization as percentage (0.0 to 100.0)."""
        return self.utilization * 100
    
    @property
    def waste_percent(self) -> float:
        """Material waste as percentage (0.0 to 100.0)."""
        return (1 - self.utilization) * 100
    
    @property
    def total_piece_area(self) -> float:
        """
        Total area of all placed pieces (mm²).
        
        Note: This requires piece_areas to be set via set_piece_areas()
        or computed during validation.
        """
        if self._piece_areas is None:
            return 0.0
        
        total = 0.0
        for placement in self.placements:
            area = self._piece_areas.get(placement.piece_id, 0.0)
            total += area
        return total
    
    def set_piece_areas(self, piece_areas: Dict[str, float]) -> None:
        """
        Set piece areas for utilization calculation.
        
        Args:
            piece_areas: Dict mapping piece_id to area in mm²
        """
        self._piece_areas = piece_areas
    
    def get_placements_for_piece(self, piece_id: str) -> List[PlacedPiece]:
        """Get all placements for a specific piece ID."""
        return [p for p in self.placements if p.piece_id == piece_id]
    
    def get_flipped_placements(self) -> List[PlacedPiece]:
        """
        Get all placements that were FLIPPED during NESTING.
        
        Returns:
            List of PlacedPiece objects where flipped=True
            
        Note:
            This returns pieces that were flipped during NESTING placement.
            This is SEPARATE from pieces that have a fold_line (geometry).
        """
        return [p for p in self.placements if p.flipped]
    
    def get_non_flipped_placements(self) -> List[PlacedPiece]:
        """Get all placements that were NOT flipped during nesting."""
        return [p for p in self.placements if not p.flipped]
    
    @property
    def flip_summary(self) -> Dict[str, int]:
        """
        Summary of NESTING flip decisions.
        
        Returns:
            Dict with 'flipped' and 'not_flipped' counts
            
        Note:
            This summarizes NESTING PLACEMENT decisions,
            not the fold_line geometric feature.
        """
        flipped_count = sum(1 for p in self.placements if p.flipped)
        return {
            "flipped": flipped_count,
            "not_flipped": len(self.placements) - flipped_count
        }
    
    def get_bounding_box(self) -> BoundingBox:
        """Get bounding box of all placements."""
        # For now, return container bounds
        # Full implementation would compute from actual piece polygons
        height = self.container_height if self.container_height else self.strip_length
        return BoundingBox(0, 0, self.container_width, height)
    
    def validate(self, instance: NestingInstance) -> Tuple[bool, List[str]]:
        """
        Validate solution against the instance.
        
        Checks:
        - All pieces from instance are placed
        - No pieces exceed demand
        - Placements are within container bounds
        - No overlaps (TODO: implement with geometry)
        
        Args:
            instance: The NestingInstance this solution is for
            
        Returns:
            (is_valid, list_of_error_messages)
        """
        errors = []
        
        # Check instance ID matches
        if self.instance_id != instance.id:
            errors.append(
                f"Instance ID mismatch: solution has '{self.instance_id}', "
                f"expected '{instance.id}'"
            )
        
        # Count placements per piece
        placement_counts: Dict[str, int] = {}
        for p in self.placements:
            placement_counts[p.piece_id] = placement_counts.get(p.piece_id, 0) + 1
        
        # Check against demand
        for item in instance.items:
            piece_id = item.piece_id
            placed = placement_counts.get(piece_id, 0)
            
            if placed < item.demand:
                errors.append(
                    f"Piece '{piece_id}': placed {placed}, required {item.demand}"
                )
            elif placed > item.demand:
                errors.append(
                    f"Piece '{piece_id}': placed {placed}, exceeds demand {item.demand}"
                )
        
        # Check for unexpected pieces
        expected_ids = {item.piece_id for item in instance.items}
        for piece_id in placement_counts:
            if piece_id not in expected_ids:
                errors.append(f"Unexpected piece in solution: '{piece_id}'")
        
        # Check placements are within bounds
        effective_height = self.strip_length  # For strip packing
        if instance.container.height:
            effective_height = instance.container.height
        
        for p in self.placements:
            if p.x < 0 or p.y < 0:
                errors.append(
                    f"Placement '{p.placement_id}' has negative coordinates: "
                    f"({p.x}, {p.y})"
                )
            # Note: Full bounds checking requires piece geometry
        
        # Set piece areas for utilization calculation
        piece_areas = {item.piece_id: item.piece.area for item in instance.items}
        self.set_piece_areas(piece_areas)
        
        return len(errors) == 0, errors
    
    def summary(self) -> str:
        """Generate a summary string for this solution."""
        flip_info = self.flip_summary
        lines = [
            f"NestingSolution for {self.instance_id}",
            f"  Placements: {self.num_placements}",
            f"  Strip length: {self.strip_length:.1f} mm",
            f"  Container: {self.container_width:.1f} × {self.strip_length:.1f} mm",
            f"  Utilization: {self.utilization_percent:.2f}%",
            f"  Waste: {self.waste_percent:.2f}%",
            f"  Flipped pieces: {flip_info['flipped']} (nesting decision)",
            f"  Computation time: {self.computation_time_ms:.1f} ms",
            f"  Engine: {self.engine_name} v{self.engine_version}",
        ]
        return "\n".join(lines)
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            "instance_id": self.instance_id,
            "placements": [p.to_dict() for p in self.placements],
            "strip_length": self.strip_length,
            "container_width": self.container_width,
            "container_height": self.container_height,
            "computation_time_ms": self.computation_time_ms,
            "engine_name": self.engine_name,
            "engine_version": self.engine_version,
            "created_at": self.created_at.isoformat(),
            "metadata": self.metadata,
            "metrics": {
                "utilization": self.utilization,
                "utilization_percent": self.utilization_percent,
                "waste_percent": self.waste_percent,
                "total_piece_area": self.total_piece_area,
                "used_area": self.used_area,
            }
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "NestingSolution":
        """Create from dictionary."""
        placements = [PlacedPiece.from_dict(p) for p in data["placements"]]
        
        return cls(
            instance_id=data["instance_id"],
            placements=placements,
            strip_length=data["strip_length"],
            container_width=data["container_width"],
            container_height=data.get("container_height"),
            computation_time_ms=data.get("computation_time_ms", 0.0),
            engine_name=data.get("engine_name", "unknown"),
            engine_version=data.get("engine_version", "0.0.0"),
            created_at=datetime.fromisoformat(data["created_at"]) if "created_at" in data else datetime.now(),
            metadata=data.get("metadata", {})
        )
    
    def __str__(self) -> str:
        return (
            f"NestingSolution({self.instance_id}: {self.num_placements} pieces, "
            f"{self.utilization_percent:.1f}% utilization)"
        )
