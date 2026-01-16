"""
Tests for AAMA parser multi-material workflow support.

Tests the PieceQuantity dataclass, parse_annotation function,
and material grouping/filtering functions.
"""

import sys
import os

# Add src to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import pytest
from nesting_engine.io.aama_parser import (
    PieceQuantity,
    parse_annotation,
    get_pieces_by_material,
    get_available_materials,
    AAMAPiece,
    GradePoint,
)


class TestPieceQuantity:
    """Tests for PieceQuantity dataclass."""

    def test_default_quantity(self):
        """Test default PieceQuantity creation."""
        qty = PieceQuantity.default()
        assert qty.total == 1
        assert qty.has_left_right is False
        assert qty.left_qty == 0
        assert qty.right_qty == 0
        assert qty.material is None
        assert qty.raw is None

    def test_simple_quantity(self):
        """Test creating PieceQuantity with simple values."""
        qty = PieceQuantity(
            total=2,
            has_left_right=False,
            left_qty=0,
            right_qty=0,
            material="SHELL",
            raw="SHELL*2"
        )
        assert qty.total == 2
        assert qty.material == "SHELL"
        assert qty.raw == "SHELL*2"

    def test_left_right_quantity(self):
        """Test creating PieceQuantity with L/R info."""
        qty = PieceQuantity(
            total=4,
            has_left_right=True,
            left_qty=2,
            right_qty=2,
            material="IL",
            raw="IL(L*2-R*2)"
        )
        assert qty.total == 4
        assert qty.has_left_right is True
        assert qty.left_qty == 2
        assert qty.right_qty == 2


class TestParseAnnotation:
    """Tests for parse_annotation function."""

    def test_none_input(self):
        """Test None input returns default."""
        result = parse_annotation(None)
        assert result.total == 1
        assert result.has_left_right is False
        assert result.material is None

    def test_empty_string(self):
        """Test empty string returns default."""
        result = parse_annotation("")
        assert result.total == 1
        assert result.has_left_right is False

    def test_whitespace_only(self):
        """Test whitespace-only string returns default."""
        result = parse_annotation("   ")
        assert result.total == 1
        assert result.has_left_right is False

    def test_material_with_quantity(self):
        """Test 'SHELL*2' format."""
        result = parse_annotation("SHELL*2")
        assert result.total == 2
        assert result.has_left_right is False
        assert result.left_qty == 0
        assert result.right_qty == 0
        assert result.material == "SHELL"
        assert result.raw == "SHELL*2"

    def test_material_with_quantity_il(self):
        """Test 'IL*4' format."""
        result = parse_annotation("IL*4")
        assert result.total == 4
        assert result.material == "IL"

    def test_left_right_format_equal(self):
        """Test 'IL(L*1-R*1)' format with equal quantities."""
        result = parse_annotation("IL(L*1-R*1)")
        assert result.total == 2
        assert result.has_left_right is True
        assert result.left_qty == 1
        assert result.right_qty == 1
        assert result.material == "IL"

    def test_left_right_format_unequal(self):
        """Test 'SHELL(L*2-R*3)' format with unequal quantities."""
        result = parse_annotation("SHELL(L*2-R*3)")
        assert result.total == 5
        assert result.has_left_right is True
        assert result.left_qty == 2
        assert result.right_qty == 3
        assert result.material == "SHELL"

    def test_left_right_larger_quantities(self):
        """Test 'SHELL(L*10-R*10)' format with larger quantities."""
        result = parse_annotation("SHELL(L*10-R*10)")
        assert result.total == 20
        assert result.left_qty == 10
        assert result.right_qty == 10

    def test_material_only(self):
        """Test 'FINISH' format (material only, implies qty 1)."""
        result = parse_annotation("FINISH")
        assert result.total == 1
        assert result.material == "FINISH"
        assert result.has_left_right is False
        assert result.raw == "FINISH"

    def test_case_insensitive_material_qty(self):
        """Test that parsing is case-insensitive for MATERIAL*N."""
        result1 = parse_annotation("shell*2")
        result2 = parse_annotation("SHELL*2")
        result3 = parse_annotation("Shell*2")
        assert result1.material == "SHELL"
        assert result2.material == "SHELL"
        assert result3.material == "SHELL"
        assert result1.total == result2.total == result3.total == 2

    def test_case_insensitive_left_right(self):
        """Test case insensitivity for L/R format."""
        result = parse_annotation("shell(L*1-R*1)")
        assert result.material == "SHELL"
        assert result.has_left_right is True

    def test_case_insensitive_material_only(self):
        """Test case insensitivity for material-only format."""
        result = parse_annotation("finish")
        assert result.material == "FINISH"

    def test_unknown_format_returns_default(self):
        """Test unknown format returns default with raw preserved."""
        result = parse_annotation("WEIRD FORMAT 123")
        assert result.total == 1
        assert result.has_left_right is False
        assert result.raw == "WEIRD FORMAT 123"

    def test_unknown_format_with_numbers(self):
        """Test format with numbers that doesn't match patterns."""
        result = parse_annotation("SHELL-2")
        assert result.total == 1
        assert result.raw == "SHELL-2"

    def test_trim_material(self):
        """Test 'TRIM' material name."""
        result = parse_annotation("TRIM")
        assert result.material == "TRIM"
        assert result.total == 1

    def test_fusing_material(self):
        """Test 'FUSING*2' material."""
        result = parse_annotation("FUSING*2")
        assert result.material == "FUSING"
        assert result.total == 2

    def test_alphanumeric_material_lr(self):
        """Test 'SO1(L*1-R*1)' format with alphanumeric material."""
        result = parse_annotation("SO1(L*1-R*1)")
        assert result.material == "SO1"
        assert result.total == 2
        assert result.has_left_right is True
        assert result.left_qty == 1
        assert result.right_qty == 1

    def test_alphanumeric_material_qty(self):
        """Test 'FO1*2' format with alphanumeric material."""
        result = parse_annotation("FO1*2")
        assert result.material == "FO1"
        assert result.total == 2
        assert result.has_left_right is False

    def test_alphanumeric_material_only(self):
        """Test 'WO2' format (alphanumeric material only)."""
        result = parse_annotation("WO2")
        assert result.material == "WO2"
        assert result.total == 1

    def test_alphanumeric_lr_larger_qty(self):
        """Test 'SO1(L*2-R*3)' format with larger quantities."""
        result = parse_annotation("SO1(L*2-R*3)")
        assert result.material == "SO1"
        assert result.total == 5
        assert result.has_left_right is True
        assert result.left_qty == 2
        assert result.right_qty == 3


class TestGetPiecesByMaterial:
    """Tests for get_pieces_by_material function."""

    def _create_test_piece(
        self,
        name: str,
        material: str = None,
        annotation: str = None
    ) -> AAMAPiece:
        """Create a minimal AAMAPiece for testing."""
        qty = parse_annotation(annotation) if annotation else PieceQuantity.default()
        return AAMAPiece(
            name=name,
            block_name=f"{name}-32",
            size="32",
            vertices=[(0, 0), (1, 0), (1, 1), (0, 1)],
            grade_points=[],
            material=material,
            annotation=annotation,
            quantity=qty
        )

    def test_empty_list(self):
        """Test with empty list."""
        result = get_pieces_by_material([])
        assert result == {}

    def test_single_material(self):
        """Test with pieces of single material."""
        pieces = [
            self._create_test_piece("Front", material="SHELL"),
            self._create_test_piece("Back", material="SHELL"),
        ]
        result = get_pieces_by_material(pieces)
        assert len(result) == 1
        assert "SHELL" in result
        assert len(result["SHELL"]) == 2

    def test_multiple_materials(self):
        """Test with pieces of multiple materials."""
        pieces = [
            self._create_test_piece("Front", material="SHELL"),
            self._create_test_piece("Back", material="SHELL"),
            self._create_test_piece("Collar", material="IL"),
            self._create_test_piece("Cuff", material="IL"),
            self._create_test_piece("Facing", material="FINISH"),
        ]
        result = get_pieces_by_material(pieces)
        assert len(result) == 3
        assert "SHELL" in result
        assert "IL" in result
        assert "FINISH" in result
        assert len(result["SHELL"]) == 2
        assert len(result["IL"]) == 2
        assert len(result["FINISH"]) == 1

    def test_materials_sorted_alphabetically(self):
        """Test that materials are sorted alphabetically."""
        pieces = [
            self._create_test_piece("P1", material="SHELL"),
            self._create_test_piece("P2", material="FINISH"),
            self._create_test_piece("P3", material="IL"),
        ]
        result = get_pieces_by_material(pieces)
        keys = list(result.keys())
        assert keys == ["FINISH", "IL", "SHELL"]

    def test_falls_back_to_annotation_material(self):
        """Test that material is extracted from annotation if not set."""
        pieces = [
            self._create_test_piece("P1", material=None, annotation="FINISH*2"),
        ]
        result = get_pieces_by_material(pieces)
        assert "FINISH" in result
        assert len(result["FINISH"]) == 1

    def test_material_takes_precedence_over_annotation(self):
        """Test that explicit material field takes precedence."""
        # Create piece with material="SHELL" but annotation contains "IL"
        piece = self._create_test_piece("P1", material="SHELL", annotation="IL*2")
        result = get_pieces_by_material([piece])
        assert "SHELL" in result
        assert "IL" not in result

    def test_unknown_material_for_missing(self):
        """Test that UNKNOWN is used when no material info available."""
        pieces = [
            self._create_test_piece("P1", material=None, annotation=None),
        ]
        result = get_pieces_by_material(pieces)
        assert "UNKNOWN" in result
        assert len(result["UNKNOWN"]) == 1

    def test_case_normalization(self):
        """Test that materials are normalized to uppercase."""
        pieces = [
            self._create_test_piece("P1", material="shell"),
            self._create_test_piece("P2", material="SHELL"),
            self._create_test_piece("P3", material="Shell"),
        ]
        result = get_pieces_by_material(pieces)
        assert len(result) == 1
        assert "SHELL" in result
        assert len(result["SHELL"]) == 3


class TestGetAvailableMaterials:
    """Tests for get_available_materials function."""

    def _create_test_piece(
        self,
        name: str,
        material: str = None
    ) -> AAMAPiece:
        """Create a minimal AAMAPiece for testing."""
        return AAMAPiece(
            name=name,
            block_name=f"{name}-32",
            size="32",
            vertices=[(0, 0), (1, 0), (1, 1), (0, 1)],
            grade_points=[],
            material=material
        )

    def test_empty_list(self):
        """Test with empty list."""
        result = get_available_materials([])
        assert result == []

    def test_returns_sorted_list(self):
        """Test that materials are returned sorted."""
        pieces = [
            self._create_test_piece("P1", material="SHELL"),
            self._create_test_piece("P2", material="FINISH"),
            self._create_test_piece("P3", material="IL"),
        ]
        result = get_available_materials(pieces)
        assert result == ["FINISH", "IL", "SHELL"]

    def test_unique_materials(self):
        """Test that duplicate materials are not included."""
        pieces = [
            self._create_test_piece("P1", material="SHELL"),
            self._create_test_piece("P2", material="SHELL"),
            self._create_test_piece("P3", material="IL"),
        ]
        result = get_available_materials(pieces)
        assert result == ["IL", "SHELL"]

    def test_includes_unknown_when_missing(self):
        """Test that UNKNOWN is included for pieces without material."""
        pieces = [
            self._create_test_piece("P1", material="SHELL"),
            self._create_test_piece("P2", material=None),
        ]
        result = get_available_materials(pieces)
        assert "UNKNOWN" in result
        assert "SHELL" in result


class TestAAMAPieceQuantityIntegration:
    """Test that AAMAPiece correctly uses PieceQuantity type."""

    def test_default_quantity_type(self):
        """Test that default quantity is PieceQuantity."""
        piece = AAMAPiece(
            name="Test",
            block_name="Test-32",
            size="32",
            vertices=[(0, 0), (1, 0), (1, 1), (0, 1)],
            grade_points=[]
        )
        assert isinstance(piece.quantity, PieceQuantity)
        assert piece.quantity.total == 1
        assert piece.quantity.has_left_right is False

    def test_custom_quantity(self):
        """Test assigning custom PieceQuantity."""
        qty = PieceQuantity(
            total=4,
            has_left_right=True,
            left_qty=2,
            right_qty=2,
            material="SHELL",
            raw="SHELL(L*2-R*2)"
        )
        piece = AAMAPiece(
            name="Test",
            block_name="Test-32",
            size="32",
            vertices=[(0, 0), (1, 0), (1, 1), (0, 1)],
            grade_points=[],
            quantity=qty
        )
        assert piece.quantity.total == 4
        assert piece.quantity.has_left_right is True
        assert piece.quantity.left_qty == 2
        assert piece.quantity.right_qty == 2


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
