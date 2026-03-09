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

    def parse_pattern(self, db: Session, pattern: Pattern, size_names: Optional[str] = None) -> Dict[str, Any]:
        """Parse pattern file and extract metadata."""
        try:
            from ..config import resolve_path
            dxf_path = resolve_path(pattern.dxf_file_path)

            # DXF-only branch: no RUL grading, pieces already sized
            if pattern.file_type == "dxf_only":
                return self._parse_dxf_only(db, pattern, dxf_path, size_names=size_names)

            # VT DXF branch: Optitex Graded Nest (one DXF per material, sizes in blocks)
            if pattern.file_type == "vt_dxf":
                return self._parse_vt_dxf(db, pattern, dxf_path)

            # Import the parser functions
            from nesting_engine.io.aama_parser import load_aama_pattern, AAMAGrader

            # Resolve paths - they may be relative
            rul_path = resolve_path(pattern.rul_file_path) if pattern.rul_file_path else None

            # Load and parse the pattern
            pieces, grading_rules = load_aama_pattern(dxf_path, rul_path)

            # Extract unique sizes and materials from pieces
            materials = set()
            piece_details = []

            for piece in pieces:
                if piece.material:
                    materials.add(piece.material)

                # Calculate bounding box
                if piece.vertices and len(piece.vertices) >= 2:
                    xs = [v[0] for v in piece.vertices]
                    ys = [v[1] for v in piece.vertices]
                    bbox = {
                        "min_x": min(xs),
                        "max_x": max(xs),
                        "min_y": min(ys),
                        "max_y": max(ys),
                        "width": max(xs) - min(xs),
                        "height": max(ys) - min(ys),
                    }
                else:
                    bbox = None

                # Get quantity info
                qty = piece.quantity
                has_lr = qty.has_left_right if qty else False
                total_qty = qty.total if qty else 1

                # Simplify vertices for preview (reduce point count for performance)
                simplified_vertices = None
                if piece.vertices and len(piece.vertices) >= 3:
                    verts = piece.vertices
                    # If too many points, simplify by taking every Nth point
                    if len(verts) > 50:
                        step = len(verts) // 50
                        verts = verts[::step]
                    # Normalize vertices relative to bounding box for easier SVG rendering
                    if bbox:
                        simplified_vertices = [
                            [v[0] - bbox["min_x"], v[1] - bbox["min_y"]]
                            for v in verts
                        ]

                piece_details.append({
                    "name": piece.name,
                    "material": piece.material,
                    "quantity": total_qty,
                    "has_left_right": has_lr,
                    "left_qty": qty.left_qty if qty and has_lr else 0,
                    "right_qty": qty.right_qty if qty and has_lr else 0,
                    "has_grain_line": bool(piece.grain_line) if hasattr(piece, 'grain_line') else False,
                    "bbox": bbox,
                    "vertices": simplified_vertices,
                })

            # Get sizes from grading rules
            sizes = grading_rules.header.size_list if grading_rules and grading_rules.header else []
            piece_count = len(pieces)

            # Group pieces by material
            pieces_by_material = {}
            for pd in piece_details:
                mat = pd.get("material", "UNKNOWN")
                if mat not in pieces_by_material:
                    pieces_by_material[mat] = []
                pieces_by_material[mat].append(pd)

            # Compute perimeter_by_size for cost calculations
            # Result: { material: { size: total_perimeter_cm } }
            perimeter_by_size = {}
            if grading_rules and sizes:
                try:
                    import math
                    grader = AAMAGrader(pieces, grading_rules)
                    # Unit conversion: ENGLISH=inches, METRIC=mm → convert to cm
                    units = grading_rules.header.units if grading_rules.header else 'METRIC'
                    if units == 'ENGLISH':
                        to_cm = 2.54  # inches to cm
                    else:
                        to_cm = 0.1   # mm to cm

                    for material in materials:
                        perimeter_by_size[material] = {}
                        for target_size in sizes:
                            try:
                                graded = grader.grade(target_size)
                            except ValueError:
                                continue
                            total_perimeter = 0.0
                            for gp in graded:
                                orig_piece = next(
                                    (p for p in pieces if p.name == gp.source_piece), None
                                )
                                if orig_piece is None or orig_piece.material != material:
                                    continue
                                # Calculate perimeter from graded vertices
                                verts = gp.vertices
                                if len(verts) >= 3:
                                    perim = 0.0
                                    for i in range(len(verts)):
                                        x1, y1 = verts[i]
                                        x2, y2 = verts[(i + 1) % len(verts)]
                                        perim += math.sqrt((x2 - x1)**2 + (y2 - y1)**2)
                                    perimeter_native = perim
                                else:
                                    perimeter_native = 0.0
                                perimeter_cm = perimeter_native * to_cm
                                # Multiply by demand (total pieces per bundle)
                                demand = orig_piece.quantity.total
                                if orig_piece.quantity.has_left_right:
                                    demand = orig_piece.quantity.left_qty + orig_piece.quantity.right_qty
                                total_perimeter += perimeter_cm * demand
                            perimeter_by_size[material][target_size] = round(total_perimeter, 2)
                except Exception as e:
                    import traceback
                    print(f"[PatternService] Warning: perimeter computation failed: {e}")
                    print(traceback.format_exc())
                    perimeter_by_size = {}

            # Update pattern record
            pattern.is_parsed = True
            pattern.available_sizes = list(sizes)
            pattern.available_materials = sorted(list(materials))
            pattern.parse_metadata = {
                "piece_count": piece_count,
                "sizes": list(sizes),
                "materials": sorted(list(materials)),
                "pieces": piece_details,
                "pieces_by_material": pieces_by_material,
                "perimeter_by_size": perimeter_by_size,
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

    def _parse_dxf_only(self, db: Session, pattern: Pattern, dxf_path: str, size_names: Optional[str] = None) -> Dict[str, Any]:
        """Parse a DXF-only pattern (no RUL grading file)."""
        import math
        from nesting_engine.io.dxf_parser import load_dxf_pieces_by_size

        size_name_list = [s.strip() for s in size_names.split(',')] if size_names else None
        pieces, piece_config, sizes = load_dxf_pieces_by_size(dxf_path, size_names=size_name_list)

        # Material = "MAIN" (single fabric, user maps later)
        materials = ["MAIN"]

        # Build piece details for UI (same structure as AAMA path)
        piece_details = []
        for piece in pieces:
            verts = list(piece.vertices)
            if verts:
                xs = [v[0] for v in verts]
                ys = [v[1] for v in verts]
                bbox = {
                    "min_x": min(xs), "max_x": max(xs),
                    "min_y": min(ys), "max_y": max(ys),
                    "width": max(xs) - min(xs),
                    "height": max(ys) - min(ys),
                }
                # Simplified vertices for preview
                preview_verts = verts
                if len(preview_verts) > 50:
                    step = len(preview_verts) // 50
                    preview_verts = preview_verts[::step]
                simplified_vertices = [
                    [v[0] - bbox["min_x"], v[1] - bbox["min_y"]]
                    for v in preview_verts
                ]
            else:
                bbox = None
                simplified_vertices = None

            piece_details.append({
                "name": piece.identifier.piece_name,
                "material": "MAIN",
                "quantity": 1,
                "has_left_right": False,
                "left_qty": 0,
                "right_qty": 0,
                "has_grain_line": False,
                "bbox": bbox,
                "vertices": simplified_vertices,
            })

        # Group pieces by material (all MAIN for DXF-only)
        pieces_by_material = {"MAIN": piece_details}

        # Compute perimeter_by_size from actual piece vertices (already in mm)
        perimeter_by_size = {"MAIN": {}}
        for size in sizes:
            total_perimeter = 0.0
            for piece in pieces:
                if piece.identifier.size != size:
                    continue
                verts = list(piece.vertices)
                if len(verts) >= 3:
                    perim = 0.0
                    for i in range(len(verts)):
                        x1, y1 = verts[i]
                        x2, y2 = verts[(i + 1) % len(verts)]
                        perim += math.sqrt((x2 - x1)**2 + (y2 - y1)**2)
                    # Convert mm to cm
                    total_perimeter += perim * 0.1
            perimeter_by_size["MAIN"][size] = round(total_perimeter, 2)

        # Update pattern record
        pattern.is_parsed = True
        pattern.available_sizes = sizes
        pattern.available_materials = materials
        pattern.parse_metadata = {
            "piece_count": len(pieces),
            "sizes": sizes,
            "materials": materials,
            "pieces": piece_details,
            "pieces_by_material": pieces_by_material,
            "perimeter_by_size": perimeter_by_size,
        }

        # Create fabric mapping for MAIN
        existing = db.query(PatternFabricMapping).filter(
            PatternFabricMapping.pattern_id == pattern.id,
            PatternFabricMapping.material_name == "MAIN"
        ).first()
        if not existing:
            mapping = PatternFabricMapping(
                pattern_id=pattern.id,
                material_name="MAIN",
            )
            db.add(mapping)

        db.commit()
        db.refresh(pattern)

        return {
            "success": True,
            "sizes": sizes,
            "materials": materials,
            "piece_count": len(pieces),
            "metadata": pattern.parse_metadata,
        }

    def _parse_vt_dxf(self, db: Session, pattern: Pattern, dxf_path: str) -> Dict[str, Any]:
        """Parse an Optitex Graded Nest DXF (VT format)."""
        import math
        from nesting_engine.io.vt_dxf_parser import parse_vt_dxf

        pieces, sizes, piece_quantities, material = parse_vt_dxf(dxf_path)
        materials = [material]

        # Build piece details for UI (same structure as AAMA/DXF-only paths)
        piece_details = []
        for piece in pieces:
            verts = list(piece.vertices)
            piece_name = piece.identifier.piece_name
            size = piece.identifier.size
            qty = piece_quantities.get(piece_name, 1)

            if verts:
                xs = [v[0] for v in verts]
                ys = [v[1] for v in verts]
                bbox = {
                    "min_x": min(xs), "max_x": max(xs),
                    "min_y": min(ys), "max_y": max(ys),
                    "width": max(xs) - min(xs),
                    "height": max(ys) - min(ys),
                }
                preview_verts = verts
                if len(preview_verts) > 50:
                    step = len(preview_verts) // 50
                    preview_verts = preview_verts[::step]
                simplified_vertices = [
                    [v[0] - bbox["min_x"], v[1] - bbox["min_y"]]
                    for v in preview_verts
                ]
            else:
                bbox = None
                simplified_vertices = None

            piece_details.append({
                "name": f"{piece_name}_{size}",
                "material": material,
                "quantity": qty,
                "has_left_right": qty >= 2,
                "left_qty": qty // 2 if qty >= 2 else 0,
                "right_qty": qty // 2 if qty >= 2 else 0,
                "has_grain_line": piece.grain is not None,
                "bbox": bbox,
                "vertices": simplified_vertices,
            })

        pieces_by_material = {material: piece_details}

        # Compute perimeter_by_size (vertices already in mm)
        perimeter_by_size = {material: {}}
        for size in sizes:
            total_perimeter = 0.0
            for piece in pieces:
                if piece.identifier.size != size:
                    continue
                verts = list(piece.vertices)
                piece_name = piece.identifier.piece_name
                qty = piece_quantities.get(piece_name, 1)
                if len(verts) >= 3:
                    perim = 0.0
                    for i in range(len(verts)):
                        x1, y1 = verts[i]
                        x2, y2 = verts[(i + 1) % len(verts)]
                        perim += math.sqrt((x2 - x1)**2 + (y2 - y1)**2)
                    # Convert mm to cm, multiply by demand
                    total_perimeter += perim * 0.1 * qty
            perimeter_by_size[material][size] = round(total_perimeter, 2)

        # Update pattern record
        pattern.is_parsed = True
        pattern.available_sizes = sizes
        pattern.available_materials = materials
        pattern.parse_metadata = {
            "piece_count": len(pieces),
            "sizes": sizes,
            "materials": materials,
            "pieces": piece_details,
            "pieces_by_material": pieces_by_material,
            "perimeter_by_size": perimeter_by_size,
            "piece_quantities": piece_quantities,
        }

        # Create fabric mapping
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
            "sizes": sizes,
            "materials": materials,
            "piece_count": len(pieces),
            "metadata": pattern.parse_metadata,
        }

    def get_pattern_pieces(self, pattern: Pattern) -> List[Dict[str, Any]]:
        """Get list of pieces from a parsed pattern."""
        if not pattern.is_parsed or not pattern.parse_metadata:
            return []
        return pattern.parse_metadata.get("pieces", [])

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
