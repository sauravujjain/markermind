import os
import sys
from typing import Optional, List, Dict, Any
from sqlalchemy.orm import Session

from ..config import settings
from ..models import Pattern, PatternFabricMapping

# Add nesting_engine to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', '..'))


class PatternService:
    """Service for pattern file management and parsing."""

    def create_pattern(
        self,
        db: Session,
        customer_id: str,
        name: str,
        file_type: str = "aama"
    ) -> Pattern:
        """Create a new pattern record."""
        pattern = Pattern(
            customer_id=customer_id,
            name=name,
            file_type=file_type.lower(),  # Ensure lowercase for enum
        )
        db.add(pattern)
        db.commit()
        db.refresh(pattern)
        return pattern

    def save_uploaded_files(
        self,
        pattern: Pattern,
        dxf_content: bytes,
        rul_content: Optional[bytes] = None
    ) -> tuple[str, Optional[str]]:
        """Save uploaded DXF and RUL files."""
        upload_dir = os.path.join(settings.upload_dir, "patterns", pattern.id)
        os.makedirs(upload_dir, exist_ok=True)

        # Save DXF file
        dxf_path = os.path.join(upload_dir, f"{pattern.name}.dxf")
        with open(dxf_path, "wb") as f:
            f.write(dxf_content)

        # Save RUL file if provided
        rul_path = None
        if rul_content:
            rul_path = os.path.join(upload_dir, f"{pattern.name}.rul")
            with open(rul_path, "wb") as f:
                f.write(rul_content)

        return dxf_path, rul_path

    def parse_pattern(self, db: Session, pattern: Pattern) -> Dict[str, Any]:
        """Parse pattern file and extract metadata."""
        try:
            # Import the parser functions
            from nesting_engine.io.aama_parser import load_aama_pattern

            # Resolve paths - they may be relative
            dxf_path = pattern.dxf_file_path
            rul_path = pattern.rul_file_path

            # If relative, resolve from backend directory
            if dxf_path.startswith("../"):
                base_dir = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
                dxf_path = os.path.normpath(os.path.join(base_dir, dxf_path))
                if rul_path:
                    rul_path = os.path.normpath(os.path.join(base_dir, rul_path))

            # Load and parse the pattern
            pieces, grading_rules = load_aama_pattern(dxf_path, rul_path)

            # Extract unique sizes and materials from pieces
            materials = set()
            for piece in pieces:
                if piece.material:
                    materials.add(piece.material)

            # Get sizes from grading rules
            sizes = grading_rules.header.size_list if grading_rules and grading_rules.header else []
            piece_count = len(pieces)

            # Update pattern record
            pattern.is_parsed = True
            pattern.available_sizes = list(sizes)
            pattern.available_materials = sorted(list(materials))
            pattern.parse_metadata = {
                "piece_count": piece_count,
                "sizes": list(sizes),
                "materials": sorted(list(materials)),
            }

            # Create fabric mappings for each material
            for material in materials:
                # Check if mapping already exists
                existing = db.query(PatternFabricMapping).filter(
                    PatternFabricMapping.pattern_id == pattern.id,
                    PatternFabricMapping.material_name == material
                ).first()
                if not existing:
                    mapping = PatternFabricMapping(
                        pattern_id=pattern.id,
                        material_name=material,
                    )
                    db.add(mapping)

            db.commit()
            db.refresh(pattern)

            return {
                "success": True,
                "sizes": list(sizes),
                "materials": sorted(list(materials)),
                "piece_count": piece_count,
                "metadata": pattern.parse_metadata,
            }

        except Exception as e:
            import traceback
            return {
                "success": False,
                "error": str(e),
                "traceback": traceback.format_exc(),
                "sizes": [],
                "materials": [],
                "piece_count": 0,
                "metadata": {},
            }

    def update_fabric_mapping(
        self,
        db: Session,
        pattern_id: str,
        material_name: str,
        fabric_id: str
    ) -> PatternFabricMapping:
        """Update fabric mapping for a pattern material."""
        mapping = db.query(PatternFabricMapping).filter(
            PatternFabricMapping.pattern_id == pattern_id,
            PatternFabricMapping.material_name == material_name
        ).first()

        if mapping:
            mapping.fabric_id = fabric_id
        else:
            mapping = PatternFabricMapping(
                pattern_id=pattern_id,
                material_name=material_name,
                fabric_id=fabric_id,
            )
            db.add(mapping)

        db.commit()
        db.refresh(mapping)
        return mapping

    def get_pattern_svg(self, pattern: Pattern) -> Optional[str]:
        """Generate SVG preview of pattern pieces."""
        # TODO: Implement SVG generation
        return None
