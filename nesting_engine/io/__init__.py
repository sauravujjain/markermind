"""
File I/O for the nesting engine.

Modules:
- dxf_parser: Parse DXF pattern files (Gerber, AAMA/ASTM formats)
"""

from nesting_engine.io.dxf_parser import (
    DXFParser,
    DXFParseResult,
    ParsedPiece,
    load_pieces_from_dxf,
)

__all__ = [
    "DXFParser",
    "DXFParseResult",
    "ParsedPiece",
    "load_pieces_from_dxf",
]
