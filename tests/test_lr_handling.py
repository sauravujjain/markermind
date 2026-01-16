"""
Tests for L/R (Left/Right) piece handling in AAMA parser.

Tests the LRType enum, detect_lr_type() function, NestingQueueItem dataclass,
and generate_nesting_queue() function.
"""

import sys
import os

# Add src to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import pytest
from nesting_engine.io.aama_parser import (
    LRType,
    detect_lr_type,
    NestingQueueItem,
    generate_nesting_queue,
    PieceQuantity,
    parse_annotation,
    AAMAPiece,
    GradePoint,
)


class TestLRType:
    """Tests for LRType enum."""

    def test_enum_values(self):
        """Test LRType enum has expected values."""
        assert LRType.NONE.value == "none"
        assert LRType.SEPARATE_LEFT.value == "left"
        assert LRType.SEPARATE_RIGHT.value == "right"
        assert LRType.FLIP_FOR_LR.value == "flip"

    def test_enum_members(self):
        """Test all LRType members exist."""
        assert hasattr(LRType, 'NONE')
        assert hasattr(LRType, 'SEPARATE_LEFT')
        assert hasattr(LRType, 'SEPARATE_RIGHT')
        assert hasattr(LRType, 'FLIP_FOR_LR')


class TestDetectLRType:
    """Tests for detect_lr_type function."""

    def test_pattern_b_flip_for_lr(self):
        """Test Pattern B: annotation has L/R quantities -> FLIP_FOR_LR."""
        qty = parse_annotation("SHELL(L*1-R*1)")
        result = detect_lr_type("FRONT", qty)
        assert result == LRType.FLIP_FOR_LR

    def test_pattern_b_flip_for_lr_il(self):
        """Test Pattern B with IL material."""
        qty = parse_annotation("IL(L*2-R*2)")
        result = detect_lr_type("COLLAR", qty)
        assert result == LRType.FLIP_FOR_LR

    def test_pattern_a_separate_left(self):
        """Test Pattern A: name contains 'LEFT' -> SEPARATE_LEFT."""
        qty = PieceQuantity.default()
        result = detect_lr_type("FRONT LEFT", qty)
        assert result == LRType.SEPARATE_LEFT

    def test_pattern_a_separate_left_case_insensitive(self):
        """Test case insensitivity for LEFT detection."""
        qty = PieceQuantity.default()
        assert detect_lr_type("Front Left", qty) == LRType.SEPARATE_LEFT
        assert detect_lr_type("front left", qty) == LRType.SEPARATE_LEFT
        assert detect_lr_type("FRONT LEFT", qty) == LRType.SEPARATE_LEFT

    def test_pattern_a_separate_right(self):
        """Test Pattern A: name contains 'RIGHT' -> SEPARATE_RIGHT."""
        qty = PieceQuantity.default()
        result = detect_lr_type("FRONT RIGHT", qty)
        assert result == LRType.SEPARATE_RIGHT

    def test_pattern_a_separate_right_case_insensitive(self):
        """Test case insensitivity for RIGHT detection."""
        qty = PieceQuantity.default()
        assert detect_lr_type("Front Right", qty) == LRType.SEPARATE_RIGHT
        assert detect_lr_type("front right", qty) == LRType.SEPARATE_RIGHT

    def test_pattern_c_center_piece(self):
        """Test Pattern C: no L/R indicators -> NONE (center piece)."""
        qty = PieceQuantity.default()
        result = detect_lr_type("BACK", qty)
        assert result == LRType.NONE

    def test_pattern_c_collar(self):
        """Test center piece: COLLAR."""
        qty = PieceQuantity.default()
        result = detect_lr_type("COLLAR", qty)
        assert result == LRType.NONE

    def test_annotation_takes_precedence_over_name(self):
        """Test that annotation with L/R takes precedence over name pattern."""
        # Even if name says "LEFT", if annotation has L*1-R*1, it's FLIP_FOR_LR
        qty = parse_annotation("SHELL(L*1-R*1)")
        result = detect_lr_type("SOME LEFT PIECE", qty)
        assert result == LRType.FLIP_FOR_LR

    def test_simple_quantity_is_not_lr(self):
        """Test that simple quantity (no L/R) doesn't trigger FLIP_FOR_LR."""
        qty = parse_annotation("SHELL*2")
        result = detect_lr_type("FRONT", qty)
        assert result == LRType.NONE


class TestNestingQueueItem:
    """Tests for NestingQueueItem dataclass."""

    def _create_test_piece(
        self,
        name: str,
        material: str = None,
        annotation: str = None
    ) -> AAMAPiece:
        """Create a minimal AAMAPiece for testing."""
        qty = parse_annotation(annotation) if annotation else PieceQuantity.default()
        lr_type = detect_lr_type(name, qty)
        return AAMAPiece(
            name=name,
            block_name=f"{name}-32",
            size="32",
            vertices=[(0, 0), (1, 0), (1, 1), (0, 1)],
            grade_points=[],
            material=material,
            annotation=annotation,
            quantity=qty,
            lr_type=lr_type
        )

    def test_create_queue_item(self):
        """Test creating a NestingQueueItem."""
        piece = self._create_test_piece("FRONT", "SHELL")
        item = NestingQueueItem(
            piece=piece,
            graded_piece=None,
            display_name="FRONT",
            quantity=2,
            flip=False,
            material="SHELL"
        )
        assert item.piece == piece
        assert item.display_name == "FRONT"
        assert item.quantity == 2
        assert item.flip is False
        assert item.material == "SHELL"

    def test_queue_item_with_flip(self):
        """Test NestingQueueItem with flip=True."""
        piece = self._create_test_piece("SLEEVE", "SHELL", "SHELL(L*1-R*1)")
        item = NestingQueueItem(
            piece=piece,
            graded_piece=None,
            display_name="SLEEVE (Flip)",
            quantity=1,
            flip=True,
            material="SHELL"
        )
        assert item.flip is True


class TestGenerateNestingQueue:
    """Tests for generate_nesting_queue function."""

    def _create_test_piece(
        self,
        name: str,
        material: str = None,
        annotation: str = None
    ) -> AAMAPiece:
        """Create a minimal AAMAPiece for testing."""
        qty = parse_annotation(annotation) if annotation else PieceQuantity.default()
        lr_type = detect_lr_type(name, qty)
        return AAMAPiece(
            name=name,
            block_name=f"{name}-32",
            size="32",
            vertices=[(0, 0), (1, 0), (1, 1), (0, 1)],
            grade_points=[],
            material=material,
            annotation=annotation,
            quantity=qty,
            lr_type=lr_type
        )

    def test_empty_list(self):
        """Test with empty list."""
        result = generate_nesting_queue([])
        assert result == []

    def test_center_piece_no_flip(self):
        """Test center piece (BACK) - no flip needed."""
        pieces = [self._create_test_piece("BACK", "SHELL")]
        queue = generate_nesting_queue(pieces)

        assert len(queue) == 1
        assert queue[0].display_name == "BACK"
        assert queue[0].flip is False
        assert queue[0].quantity == 1

    def test_separate_left_piece_no_flip(self):
        """Test separate LEFT piece - no flip needed."""
        pieces = [self._create_test_piece("FRONT LEFT", "SHELL")]
        queue = generate_nesting_queue(pieces)

        assert len(queue) == 1
        assert queue[0].flip is False
        assert queue[0].piece.lr_type == LRType.SEPARATE_LEFT

    def test_separate_right_piece_no_flip(self):
        """Test separate RIGHT piece - no flip needed."""
        pieces = [self._create_test_piece("FRONT RIGHT", "SHELL")]
        queue = generate_nesting_queue(pieces)

        assert len(queue) == 1
        assert queue[0].flip is False
        assert queue[0].piece.lr_type == LRType.SEPARATE_RIGHT

    def test_flip_for_lr_creates_two_entries(self):
        """Test FLIP_FOR_LR creates normal + flipped entries."""
        pieces = [self._create_test_piece("SLEEVE", "SHELL", "SHELL(L*1-R*1)")]
        queue = generate_nesting_queue(pieces)

        # Should have 2 entries: one normal (qty=1), one flipped (qty=1)
        assert len(queue) == 2

        normal = [q for q in queue if not q.flip]
        flipped = [q for q in queue if q.flip]

        assert len(normal) == 1
        assert len(flipped) == 1
        assert normal[0].quantity == 1
        assert flipped[0].quantity == 1

    def test_flip_for_lr_with_larger_quantities(self):
        """Test FLIP_FOR_LR with L*2-R*2."""
        pieces = [self._create_test_piece("SLEEVE", "SHELL", "SHELL(L*2-R*2)")]
        queue = generate_nesting_queue(pieces)

        assert len(queue) == 2

        normal = [q for q in queue if not q.flip]
        flipped = [q for q in queue if q.flip]

        assert normal[0].quantity == 2
        assert flipped[0].quantity == 2

    def test_material_filter(self):
        """Test filtering by material."""
        pieces = [
            self._create_test_piece("FRONT", "SHELL"),
            self._create_test_piece("BACK", "SHELL"),
            self._create_test_piece("COLLAR", "IL"),
            self._create_test_piece("FACING", "FINISH"),
        ]

        shell_queue = generate_nesting_queue(pieces, material_filter="SHELL")
        assert len(shell_queue) == 2
        assert all(q.material == "SHELL" for q in shell_queue)

        il_queue = generate_nesting_queue(pieces, material_filter="IL")
        assert len(il_queue) == 1
        assert il_queue[0].material == "IL"

    def test_material_filter_none_returns_all(self):
        """Test that None material_filter returns all pieces."""
        pieces = [
            self._create_test_piece("FRONT", "SHELL"),
            self._create_test_piece("COLLAR", "IL"),
        ]
        queue = generate_nesting_queue(pieces, material_filter=None)
        assert len(queue) == 2

    def test_material_filter_case_insensitive(self):
        """Test material filter is case insensitive."""
        pieces = [self._create_test_piece("FRONT", "SHELL")]

        queue1 = generate_nesting_queue(pieces, material_filter="shell")
        queue2 = generate_nesting_queue(pieces, material_filter="SHELL")
        queue3 = generate_nesting_queue(pieces, material_filter="Shell")

        assert len(queue1) == len(queue2) == len(queue3) == 1

    def test_mixed_lr_types(self):
        """Test queue with mixed L/R types."""
        pieces = [
            self._create_test_piece("BACK", "SHELL"),                       # NONE
            self._create_test_piece("SLEEVE LEFT", "SHELL"),                # SEPARATE_LEFT
            self._create_test_piece("SLEEVE RIGHT", "SHELL"),               # SEPARATE_RIGHT
            self._create_test_piece("POCKET", "SHELL", "SHELL(L*1-R*1)"),   # FLIP_FOR_LR
        ]
        queue = generate_nesting_queue(pieces)

        # BACK: 1 entry, no flip
        # SLEEVE LEFT: 1 entry, no flip
        # SLEEVE RIGHT: 1 entry, no flip
        # POCKET: 2 entries (normal + flip)
        assert len(queue) == 5

        flip_count = sum(1 for q in queue if q.flip)
        assert flip_count == 1


class TestAAMAPieceDisplayName:
    """Test AAMAPiece.display_name property."""

    def test_display_name_none_lr(self):
        """Test display_name for NONE lr_type."""
        qty = PieceQuantity.default()
        piece = AAMAPiece(
            name="BACK",
            block_name="BACK-32",
            size="32",
            vertices=[(0, 0), (1, 0), (1, 1), (0, 1)],
            grade_points=[],
            lr_type=LRType.NONE,
            quantity=qty
        )
        assert piece.display_name == "BACK"

    def test_display_name_separate_left(self):
        """Test display_name for SEPARATE_LEFT."""
        qty = PieceQuantity.default()
        piece = AAMAPiece(
            name="FRONT LEFT",
            block_name="FRONT LEFT-32",
            size="32",
            vertices=[(0, 0), (1, 0), (1, 1), (0, 1)],
            grade_points=[],
            lr_type=LRType.SEPARATE_LEFT,
            quantity=qty
        )
        # Should keep the name as-is (already indicates L/R)
        assert piece.display_name == "FRONT LEFT"

    def test_display_name_flip_for_lr(self):
        """Test display_name for FLIP_FOR_LR."""
        qty = parse_annotation("SHELL(L*1-R*1)")
        piece = AAMAPiece(
            name="SLEEVE",
            block_name="SLEEVE-32",
            size="32",
            vertices=[(0, 0), (1, 0), (1, 1), (0, 1)],
            grade_points=[],
            lr_type=LRType.FLIP_FOR_LR,
            quantity=qty
        )
        # Should indicate it's a flip piece
        assert "SLEEVE" in piece.display_name


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
