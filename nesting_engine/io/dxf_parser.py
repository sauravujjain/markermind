"""
DXF Parser orchestrator for garment pattern files.

This module provides a unified interface that tries multiple DXF parsing
strategies and returns the best result. It delegates to:

- dxf_text_parser: Text-label based parsing (Gerber-style nested markers)
- dxf_block_parser: Block-based parsing (production DXFs with INSERT references)

All public names are re-exported here for backward compatibility, so existing
imports like ``from nesting_engine.io.dxf_parser import DXFParser`` continue to work.

See docs/parser_index.md for the full format guide.
"""

from __future__ import annotations

import logging
from typing import List, Dict, Optional, Tuple

from nesting_engine.core.piece import Piece

# Re-export everything from sub-parsers for backward compatibility
from nesting_engine.io.dxf_text_parser import (
    DXFParser,
    DXFParseResult,
    ParsedPiece,
    DXF_UNIT_MAP,
    load_pieces_from_dxf,
)
from nesting_engine.io.dxf_block_parser import parse_block_dxf

logger = logging.getLogger(__name__)

# Keep the old private name as an alias so any internal references still work
_parse_block_dxf = parse_block_dxf


def load_dxf_pieces_by_size(
    dxf_path: str,
    target_sizes: Optional[List[str]] = None,
    rotations: List[float] = [0, 180],
    allow_flip: bool = True,
    size_names: Optional[List[str]] = None,
) -> Tuple[List[Piece], Dict[str, dict], List[str]]:
    """
    Load pieces from a DXF-only pattern (no RUL grading).

    Tries the standard DXFParser (text-label) first. If that returns
    no pieces, falls back to block-based parsing for production DXF files
    that use INSERT references to named blocks.

    Args:
        dxf_path: Path to DXF file
        target_sizes: If given, only return pieces matching these sizes
        rotations: Allowed rotation angles in degrees
        allow_flip: Whether pieces can be flipped during nesting
        size_names: Optional list of size labels (e.g. ["S","M","L",...]).
                    Passed to block-based parser to replace auto-generated SIZE_N labels.

    Returns:
        (nesting_pieces, piece_config, all_sizes)
        - nesting_pieces: List[Piece] filtered to target_sizes
        - piece_config: {piece_name: {demand: 1, flipped: False}}
        - all_sizes: all sizes found in the DXF
    """
    # Try standard text-label parser first
    parser = DXFParser(dxf_path)
    result = parser.parse()
    all_pieces = parser.to_nesting_pieces(result, rotations, allow_flip)

    # If standard parser found nothing, try block-based parser
    if not all_pieces:
        logger.info(f"Standard DXF parser found 0 pieces, trying block-based parser")
        all_pieces, all_sizes_from_blocks = parse_block_dxf(
            dxf_path, size_names=size_names, rotations=rotations, allow_flip=allow_flip,
        )
        if all_pieces:
            all_sizes = all_sizes_from_blocks
        else:
            return [], {}, []
    else:
        all_sizes = sorted(set(
            p.identifier.size for p in all_pieces if p.identifier.size
        ))

    # Filter to target sizes if specified
    if target_sizes:
        target_set = set(target_sizes)
        pieces = [p for p in all_pieces if p.identifier.size in target_set]
    else:
        pieces = all_pieces

    # Build piece_config: demand=1, flipped=False for every piece
    # (each DXF polyline is a unique instance, no L/R auto-detection)
    piece_config: Dict[str, dict] = {}
    for p in pieces:
        piece_name = p.identifier.piece_name
        if piece_name not in piece_config:
            piece_config[piece_name] = {'demand': 1, 'flipped': False}

    return pieces, piece_config, all_sizes
