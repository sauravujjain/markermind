"""
File I/O for the nesting engine.

Modules:
- dxf_parser: Parse DXF pattern files (Gerber, AAMA/ASTM formats)
- aama_parser: Parse AAMA/ASTM DXF+RUL grading files
"""

from nesting_engine.io.dxf_parser import (
    DXFParser,
    DXFParseResult,
    ParsedPiece,
    load_pieces_from_dxf,
)

from nesting_engine.io.aama_parser import (
    AAMARuleParser,
    AAMADXFParser,
    AAMAGrader,
    GradingRules,
    GradingRuleHeader,
    GradingRule,
    GradePoint,
    AAMAPiece,
    GradedPiece,
    load_aama_pattern,
    grade_to_nesting_pieces,
    print_aama_summary,
)

__all__ = [
    # Existing DXF parser
    "DXFParser",
    "DXFParseResult",
    "ParsedPiece",
    "load_pieces_from_dxf",
    # AAMA parser
    "AAMARuleParser",
    "AAMADXFParser",
    "AAMAGrader",
    "GradingRules",
    "GradingRuleHeader",
    "GradingRule",
    "GradePoint",
    "AAMAPiece",
    "GradedPiece",
    "load_aama_pattern",
    "grade_to_nesting_pieces",
    "print_aama_summary",
]
