"""
Tests for core data structures.

Run with: pytest tests/test_core.py -v
"""

import pytest
import math
import sys
import os

# Add src to path for testing
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from nesting_engine.core.units import (
    LengthUnit, UnitConverter, UnitContext,
    mm_to_inches, inches_to_mm
)
from nesting_engine.core.geometry import Point, BoundingBox, Polygon
from nesting_engine.core.piece import (
    Piece, PieceIdentifier, GrainDirection, GrainConstraint, OrientationConstraint
)
from nesting_engine.core.instance import (
    Container, NestingItem, NestingInstance, FlipMode
)
from nesting_engine.core.solution import PlacedPiece, NestingSolution


class TestUnits:
    """Tests for unit conversion system."""
    
    def test_to_mm_from_inches(self):
        """Test converting inches to mm."""
        result = UnitConverter.to_mm(1.0, LengthUnit.INCH)
        assert result == 25.4
    
    def test_to_mm_from_cm(self):
        """Test converting cm to mm."""
        result = UnitConverter.to_mm(10.0, LengthUnit.CENTIMETER)
        assert result == 100.0
    
    def test_from_mm_to_inches(self):
        """Test converting mm to inches."""
        result = UnitConverter.from_mm(25.4, LengthUnit.INCH)
        assert result == 1.0
    
    def test_convert_yards_to_meters(self):
        """Test converting yards to meters."""
        result = UnitConverter.convert(1.0, LengthUnit.YARD, LengthUnit.METER)
        assert abs(result - 0.9144) < 0.0001
    
    def test_pixel_conversion_with_dpi(self):
        """Test pixel conversion with DPI."""
        # At 96 DPI, 96 pixels = 1 inch = 25.4 mm
        result = UnitConverter.to_mm(96.0, LengthUnit.PIXEL, dpi=96.0)
        assert abs(result - 25.4) < 0.0001
    
    def test_unit_from_string(self):
        """Test parsing unit strings."""
        assert LengthUnit.from_string("mm") == LengthUnit.MILLIMETER
        assert LengthUnit.from_string("inches") == LengthUnit.INCH
        assert LengthUnit.from_string("CM") == LengthUnit.CENTIMETER
    
    def test_unit_context_conversions(self):
        """Test UnitContext helper methods."""
        ctx = UnitContext(
            piece_unit=LengthUnit.CENTIMETER,
            container_unit=LengthUnit.INCH
        )
        
        # 10 cm → 100 mm
        assert ctx.piece_to_internal(10.0) == 100.0
        
        # 1 inch → 25.4 mm
        assert ctx.container_to_internal(1.0) == 25.4


class TestGeometry:
    """Tests for geometry primitives."""
    
    def test_point_operations(self):
        """Test Point arithmetic."""
        p1 = Point(10, 20)
        p2 = Point(5, 5)
        
        result = p1 + p2
        assert result == Point(15, 25)
        
        result = p1 - p2
        assert result == Point(5, 15)
    
    def test_point_distance(self):
        """Test distance calculation."""
        p1 = Point(0, 0)
        p2 = Point(3, 4)
        
        assert p1.distance_to(p2) == 5.0  # 3-4-5 triangle
    
    def test_point_rotate(self):
        """Test point rotation."""
        p = Point(1, 0)
        rotated = p.rotate(90)  # 90° CCW
        
        assert abs(rotated.x - 0) < 0.0001
        assert abs(rotated.y - 1) < 0.0001
    
    def test_bounding_box(self):
        """Test BoundingBox creation and properties."""
        bb = BoundingBox(10, 20, 110, 70)
        
        assert bb.width == 100
        assert bb.height == 50
        assert bb.area == 5000
        assert bb.center == Point(60, 45)
    
    def test_bounding_box_from_points(self):
        """Test BoundingBox.from_points."""
        points = [(0, 0), (100, 50), (50, 100)]
        bb = BoundingBox.from_points(points)
        
        assert bb.min_x == 0
        assert bb.min_y == 0
        assert bb.max_x == 100
        assert bb.max_y == 100
    
    def test_polygon_area(self):
        """Test polygon area calculation."""
        # 100x50 rectangle
        vertices = [(0, 0), (100, 0), (100, 50), (0, 50), (0, 0)]
        poly = Polygon(vertices)
        
        assert poly.area == 5000.0
    
    def test_polygon_centroid(self):
        """Test polygon centroid."""
        vertices = [(0, 0), (100, 0), (100, 50), (0, 50), (0, 0)]
        poly = Polygon(vertices)
        
        centroid = poly.centroid
        assert centroid.x == 50.0
        assert centroid.y == 25.0
    
    def test_polygon_translate(self):
        """Test polygon translation."""
        vertices = [(0, 0), (100, 0), (100, 50), (0, 50), (0, 0)]
        poly = Polygon(vertices)
        
        translated = poly.translate(10, 20)
        bb = translated.bounding_box
        
        assert bb.min_x == 10
        assert bb.min_y == 20
    
    def test_polygon_flip_horizontal(self):
        """Test polygon horizontal flip (NESTING flip)."""
        # L-shaped polygon
        vertices = [(0, 0), (100, 0), (100, 50), (50, 50), (50, 100), (0, 100), (0, 0)]
        poly = Polygon(vertices)
        
        flipped = poly.flip_horizontal()
        
        # Area should be preserved
        assert abs(flipped.area - poly.area) < 0.01
        
        # Should be mirrored
        assert flipped.bounding_box.width == poly.bounding_box.width
    
    def test_polygon_rectangle_factory(self):
        """Test rectangle factory method."""
        rect = Polygon.rectangle(100, 50)
        
        assert rect.width == 100
        assert rect.height == 50
        assert rect.area == 5000


class TestPiece:
    """Tests for Piece and related classes."""
    
    def test_piece_identifier(self):
        """Test PieceIdentifier properties."""
        identifier = PieceIdentifier(
            piece_name="Front Panel",
            style_number="OX-2024",
            size="M"
        )
        
        assert identifier.full_id == "Front_Panel_OX-2024_M"
        assert identifier.display_name == "Front Panel (M)"
    
    def test_grain_constraint(self):
        """Test GrainConstraint properties."""
        grain = GrainConstraint(
            direction=GrainDirection.LENGTHWISE,
            grain_line_start=(50, 0),
            grain_line_end=(50, 100)
        )
        
        assert grain.has_grain_line
        assert abs(grain.grain_line_angle - 90.0) < 0.001  # Vertical
    
    def test_orientation_constraint_get_all_orientations(self):
        """Test generating all valid orientations."""
        # No flip
        orient1 = OrientationConstraint(allowed_rotations=[0, 180], allow_flip=False)
        orientations1 = orient1.get_all_orientations()
        assert len(orientations1) == 2
        assert (0, False) in orientations1
        assert (180, False) in orientations1
        
        # With flip
        orient2 = OrientationConstraint(allowed_rotations=[0, 180], allow_flip=True)
        orientations2 = orient2.get_all_orientations()
        assert len(orientations2) == 4
        assert (0, False) in orientations2
        assert (0, True) in orientations2
        assert (180, False) in orientations2
        assert (180, True) in orientations2
    
    def test_piece_creation(self):
        """Test basic Piece creation."""
        vertices = [(0, 0), (100, 0), (100, 150), (0, 150), (0, 0)]
        identifier = PieceIdentifier(piece_name="Test Piece")
        
        piece = Piece(vertices=vertices, identifier=identifier)
        
        assert piece.area == 15000.0
        assert piece.width == 100
        assert piece.height == 150
        assert piece.id == "Test_Piece"
    
    def test_piece_fold_line_vs_flip_distinction(self):
        """
        CRITICAL TEST: Verify fold_line and flip are independent.
        
        fold_line = geometric feature from DXF Layer 6
        can_be_flipped = nesting placement control
        
        These must be independent!
        """
        vertices = [(0, 0), (100, 0), (100, 150), (0, 150), (0, 0)]
        identifier = PieceIdentifier(piece_name="Test Piece")
        
        # Case 1: Has fold_line, no flip allowed
        piece1 = Piece(
            vertices=vertices,
            identifier=identifier,
            fold_line=((0, 0), (0, 150)),  # Geometric feature
            orientation=OrientationConstraint(allow_flip=False)
        )
        assert piece1.has_fold_line == True
        assert piece1.can_be_flipped == False
        
        # Case 2: No fold_line, flip allowed
        piece2 = Piece(
            vertices=vertices,
            identifier=identifier,
            fold_line=None,
            orientation=OrientationConstraint(allow_flip=True)
        )
        assert piece2.has_fold_line == False
        assert piece2.can_be_flipped == True
        
        # Case 3: Has fold_line AND flip allowed (rare but valid)
        piece3 = Piece(
            vertices=vertices,
            identifier=identifier,
            fold_line=((0, 0), (0, 150)),
            orientation=OrientationConstraint(allow_flip=True)
        )
        assert piece3.has_fold_line == True
        assert piece3.can_be_flipped == True
    
    def test_piece_normalize_to_origin(self):
        """Test normalizing piece to origin."""
        vertices = [(50, 50), (150, 50), (150, 100), (50, 100), (50, 50)]
        identifier = PieceIdentifier(piece_name="Test")
        piece = Piece(vertices=vertices, identifier=identifier)
        
        normalized = piece.normalize_to_origin()
        bb = normalized.bounding_box
        
        assert bb.min_x == 0
        assert bb.min_y == 0


class TestInstance:
    """Tests for NestingInstance and related classes."""
    
    def test_container_strip_packing(self):
        """Test strip packing container."""
        container = Container(width=1500, height=None)
        
        assert container.is_strip_packing
        assert container.area is None
    
    def test_container_from_inches(self):
        """Test creating container from inches."""
        container = Container.from_inches(60, None)
        
        assert abs(container.width - 1524.0) < 0.1  # 60 * 25.4
        assert container.is_strip_packing
        assert container.original_unit == LengthUnit.INCH
    
    def test_flip_mode_enum(self):
        """Test FlipMode values - NESTING control."""
        assert FlipMode.NONE.value == "none"
        assert FlipMode.PAIRED.value == "paired"
        assert FlipMode.ANY.value == "any"
    
    def test_nesting_item_placement_breakdown_none(self):
        """Test placement breakdown with no flip."""
        vertices = [(0, 0), (100, 0), (100, 50), (0, 50), (0, 0)]
        piece = Piece(
            vertices=vertices,
            identifier=PieceIdentifier(piece_name="Test")
        )
        
        item = NestingItem(piece=piece, demand=4, flip_mode=FlipMode.NONE)
        breakdown = item.get_placement_breakdown()
        
        assert breakdown == [(False, 4)]  # All non-flipped
    
    def test_nesting_item_placement_breakdown_paired(self):
        """Test placement breakdown with paired flip."""
        vertices = [(0, 0), (100, 0), (100, 50), (0, 50), (0, 0)]
        piece = Piece(
            vertices=vertices,
            identifier=PieceIdentifier(piece_name="Sleeve"),
            orientation=OrientationConstraint(allow_flip=True)
        )
        
        item = NestingItem(piece=piece, demand=4, flip_mode=FlipMode.PAIRED)
        breakdown = item.get_placement_breakdown()
        
        # Should be 2 normal + 2 flipped
        assert (False, 2) in breakdown
        assert (True, 2) in breakdown
    
    def test_nesting_instance_creation(self):
        """Test creating a NestingInstance."""
        vertices = [(0, 0), (100, 0), (100, 50), (0, 50), (0, 0)]
        piece = Piece(
            vertices=vertices,
            identifier=PieceIdentifier(piece_name="Panel")
        )
        
        container = Container(width=1500, height=None)
        items = [NestingItem(piece=piece, demand=3)]
        
        instance = NestingInstance(
            id="test_001",
            name="Test Marker",
            container=container,
            items=items,
            piece_buffer=2.0,
            edge_buffer=5.0
        )
        
        assert instance.total_piece_count == 3
        assert instance.unique_piece_count == 1
        assert instance.total_piece_area == 5000.0 * 3
        assert instance.is_strip_packing


class TestSolution:
    """Tests for NestingSolution and PlacedPiece."""
    
    def test_placed_piece_flipped_attribute(self):
        """
        Test PlacedPiece.flipped attribute - NESTING placement decision.
        
        This is separate from Piece.fold_line (geometric feature).
        """
        # Non-flipped placement
        p1 = PlacedPiece(
            piece_id="sleeve_001",
            instance_index=0,
            x=100, y=200,
            rotation=0,
            flipped=False
        )
        assert p1.flipped == False
        
        # Flipped placement (NESTING decision)
        p2 = PlacedPiece(
            piece_id="sleeve_001",
            instance_index=1,
            x=300, y=200,
            rotation=0,
            flipped=True  # Left sleeve (flipped from right)
        )
        assert p2.flipped == True
        assert "flipped" in p2.get_transform_description()
    
    def test_solution_flip_summary(self):
        """Test solution flip summary - NESTING decisions."""
        placements = [
            PlacedPiece("p1", 0, 0, 0, 0, flipped=False),
            PlacedPiece("p1", 1, 100, 0, 0, flipped=True),
            PlacedPiece("p2", 0, 0, 100, 0, flipped=False),
            PlacedPiece("p2", 1, 100, 100, 0, flipped=True),
        ]
        
        solution = NestingSolution(
            instance_id="test",
            placements=placements,
            strip_length=200,
            container_width=200
        )
        
        summary = solution.flip_summary
        assert summary["flipped"] == 2
        assert summary["not_flipped"] == 2
    
    def test_solution_utilization(self):
        """Test utilization calculation."""
        placements = [
            PlacedPiece("p1", 0, 0, 0, 0, flipped=False),
        ]
        
        solution = NestingSolution(
            instance_id="test",
            placements=placements,
            strip_length=100,
            container_width=100
        )
        
        # Set piece area
        solution.set_piece_areas({"p1": 8000})  # 80% of 10000
        
        assert solution.used_area == 10000  # 100 * 100
        assert solution.total_piece_area == 8000
        assert solution.utilization == 0.8
        assert solution.utilization_percent == 80.0
    
    def test_solution_serialization(self):
        """Test solution to_dict and from_dict."""
        placements = [
            PlacedPiece("p1", 0, 10, 20, 90, flipped=True),
        ]
        
        solution = NestingSolution(
            instance_id="test_001",
            placements=placements,
            strip_length=500,
            container_width=1000,
            engine_name="test_engine"
        )
        
        # Serialize
        data = solution.to_dict()
        
        assert data["instance_id"] == "test_001"
        assert len(data["placements"]) == 1
        assert data["placements"][0]["flipped"] == True
        
        # Deserialize
        restored = NestingSolution.from_dict(data)
        
        assert restored.instance_id == "test_001"
        assert restored.placements[0].flipped == True


class TestNamingConventionClarity:
    """
    Dedicated tests to ensure naming convention is clear.
    
    These tests document the distinction between:
    - fold_line: Geometric feature from DXF Layer 6
    - flipped/allow_flip: NESTING placement decision
    """
    
    def test_fold_line_is_geometry_not_nesting(self):
        """fold_line is a geometric feature, not a nesting control."""
        vertices = [(0, 0), (100, 0), (100, 150), (0, 150), (0, 0)]
        
        # Create piece with fold_line
        piece = Piece(
            vertices=vertices,
            identifier=PieceIdentifier(piece_name="Cut On Fold Bodice"),
            fold_line=((0, 0), (0, 150)),  # This is GEOMETRY from DXF Layer 6
            orientation=OrientationConstraint(allow_flip=False)
        )
        
        # fold_line is stored geometry
        assert piece.fold_line is not None
        assert piece.fold_line == ((0, 0), (0, 150))
        
        # has_fold_line checks for geometric feature
        assert piece.has_fold_line == True
        
        # This does NOT mean the piece can be flipped during nesting
        assert piece.can_be_flipped == False
    
    def test_flip_is_nesting_not_geometry(self):
        """flip/allow_flip is a nesting decision, not geometry."""
        vertices = [(0, 0), (100, 0), (100, 150), (0, 150), (0, 0)]
        
        # Create piece that CAN be flipped during nesting
        piece = Piece(
            vertices=vertices,
            identifier=PieceIdentifier(piece_name="Sleeve"),
            fold_line=None,  # No geometric fold line
            orientation=OrientationConstraint(allow_flip=True)  # NESTING control
        )
        
        # No fold_line geometry
        assert piece.has_fold_line == False
        
        # Can be flipped during NESTING
        assert piece.can_be_flipped == True
        
        # Create NestingItem with FlipMode
        item = NestingItem(
            piece=piece,
            demand=4,
            flip_mode=FlipMode.PAIRED  # NESTING control: make left/right pairs
        )
        
        # This creates paired placements
        breakdown = item.get_placement_breakdown()
        flipped_count = sum(count for is_flipped, count in breakdown if is_flipped)
        non_flipped_count = sum(count for is_flipped, count in breakdown if not is_flipped)
        
        assert flipped_count == 2  # Left sleeves
        assert non_flipped_count == 2  # Right sleeves
    
    def test_placed_piece_flipped_is_nesting_decision(self):
        """PlacedPiece.flipped records a NESTING decision."""
        # When the nesting engine places a piece, it records whether
        # it flipped the piece to achieve better utilization or create pairs
        
        placement = PlacedPiece(
            piece_id="sleeve_001",
            instance_index=1,
            x=100, y=200,
            rotation=0,
            flipped=True  # This is a NESTING PLACEMENT decision
        )
        
        # This means the nesting engine flipped this piece when placing it
        assert placement.flipped == True
        
        # The description mentions "flipped" as a transform
        desc = placement.get_transform_description()
        assert "flipped" in desc


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
