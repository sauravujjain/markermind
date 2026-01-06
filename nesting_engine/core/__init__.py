"""
Core data structures for the nesting engine.

This module provides the fundamental building blocks:
- units: Length unit conversion system
- geometry: Point, polygon, and transformation primitives
- piece: Piece definition with garment industry metadata
- instance: Nesting problem definition
- solution: Nesting solution representation

NAMING CONVENTION - Two "Mirror" Concepts (IMPORTANT):

1. FOLD LINE (Piece.fold_line):
   - A geometric feature INSIDE the pattern (from DXF Layer 6)
   - Represents a symmetry axis for "cut on fold" pieces
   - Stored as reference geometry, not used by nesting engine directly

2. FLIP (flipped, allow_flip, FlipMode):
   - A NESTING PLACEMENT decision
   - Controls whether pieces are reflected during placement
   - Used for left/right pairs (sleeves, shirt fronts, etc.)

These concepts are COMPLETELY SEPARATE and should not be confused.
"""

from nesting_engine.core.units import LengthUnit, UnitConverter, UnitContext
from nesting_engine.core.geometry import Point, BoundingBox, Polygon
from nesting_engine.core.piece import (
    Piece,
    PieceIdentifier,
    GrainDirection,
    GrainConstraint,
    OrientationConstraint,
)
from nesting_engine.core.instance import (
    Container,
    NestingItem,
    NestingInstance,
    FlipMode,
)
from nesting_engine.core.solution import (
    PlacedPiece,
    NestingSolution,
)

__all__ = [
    # Units
    "LengthUnit",
    "UnitConverter",
    "UnitContext",
    # Geometry
    "Point",
    "BoundingBox",
    "Polygon",
    # Pieces
    "Piece",
    "PieceIdentifier",
    "GrainDirection",
    "GrainConstraint",
    "OrientationConstraint",
    # Instance
    "Container",
    "NestingItem",
    "NestingInstance",
    "FlipMode",
    # Solution
    "PlacedPiece",
    "NestingSolution",
]
