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

            # Gerber AccuMark branch: pre-graded DXF with rich metadata
            if pattern.file_type == "gerber_accumark":
                return self._parse_gerber_accumark(db, pattern, dxf_path)

            # DXF-only branch: no RUL grading, pieces already sized
            if pattern.file_type == "dxf_only":
                return self._parse_dxf_only(db, pattern, dxf_path, size_names=size_names)

            # VT DXF branch: Optitex Graded Nest (one DXF per material, sizes in blocks)
            if pattern.file_type == "vt_dxf":
                return self._parse_vt_dxf(db, pattern, dxf_path)

            # Import the parser functions — each AAMA variant uses its own parser
            if pattern.file_type == "gerber_aama":
                from nesting_engine.io.gerber_aama_parser import parse_gerber_aama as load_aama_pattern, GerberAAMAGrader as AAMAGrader
            elif pattern.file_type == "optitex_aama":
                from nesting_engine.io.optitex_kpr_parser import load_aama_pattern, AAMAGrader
            else:
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

                # Get quantity info (PieceQuantity object for AAMA/OptiTex, plain int for Gerber AAMA)
                qty = piece.quantity
                if isinstance(qty, int):
                    has_lr = False
                    total_qty = qty
                else:
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
                    "left_qty": qty.left_qty if not isinstance(qty, int) and qty and has_lr else 0,
                    "right_qty": qty.right_qty if not isinstance(qty, int) and qty and has_lr else 0,
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
                                oq = orig_piece.quantity
                                if isinstance(oq, int):
                                    demand = oq
                                else:
                                    demand = oq.total
                                    if oq.has_left_right:
                                        demand = oq.left_qty + oq.right_qty
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

    def _parse_gerber_accumark(self, db: Session, pattern: Pattern, dxf_path: str) -> Dict[str, Any]:
        """Parse a Gerber AccuMark 'Expanded Shapes' DXF."""
        import math
        from nesting_engine.io.gerber_accumark_parser import parse_gerber_accumark_dxf

        pieces, sizes, mat_map, piece_config = parse_gerber_accumark_dxf(dxf_path)

        # Collect unique materials
        materials = sorted(set(mat_map.values()))

        # Build piece details for UI (same structure as other paths)
        piece_details = []
        for piece in pieces:
            verts = list(piece.vertices)
            pname = piece.identifier.piece_name
            size = piece.identifier.size
            mat = mat_map.get(pname, "UNKNOWN")
            cfg = piece_config.get(pname, {'demand': 1, 'flipped': False})

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

            has_lr = cfg.get('flipped', False)
            demand = cfg.get('demand', 1)

            piece_details.append({
                "name": f"{pname}_{size}",
                "material": mat,
                "quantity": (demand * 2) if has_lr else demand,
                "has_left_right": has_lr,
                "left_qty": demand if has_lr else 0,
                "right_qty": demand if has_lr else 0,
                "has_grain_line": False,
                "bbox": bbox,
                "vertices": simplified_vertices,
            })

        # Group by material
        pieces_by_material: Dict[str, list] = {}
        for pd in piece_details:
            mat = pd.get("material", "UNKNOWN")
            if mat not in pieces_by_material:
                pieces_by_material[mat] = []
            pieces_by_material[mat].append(pd)

        # Compute perimeter_by_size (vertices already in mm from parser)
        perimeter_by_size: Dict[str, Dict[str, float]] = {}
        for mat in materials:
            perimeter_by_size[mat] = {}
            for size in sizes:
                total_perimeter = 0.0
                for piece in pieces:
                    if piece.identifier.size != size:
                        continue
                    pname = piece.identifier.piece_name
                    if mat_map.get(pname) != mat:
                        continue
                    verts = list(piece.vertices)
                    cfg = piece_config.get(pname, {'demand': 1, 'flipped': False})
                    demand = cfg.get('demand', 1)
                    if cfg.get('flipped', False):
                        demand = demand * 2  # L+R
                    if len(verts) >= 3:
                        perim = 0.0
                        for i in range(len(verts)):
                            x1, y1 = verts[i]
                            x2, y2 = verts[(i + 1) % len(verts)]
                            perim += math.sqrt((x2 - x1)**2 + (y2 - y1)**2)
                        # Convert mm to cm, multiply by demand
                        total_perimeter += perim * 0.1 * demand
                perimeter_by_size[mat][size] = round(total_perimeter, 2)

        # Update pattern record
        pattern.is_parsed = True
        pattern.available_sizes = sizes
        pattern.available_materials = materials
        pattern.parse_metadata = {
            "piece_count": len(set(p.identifier.piece_name for p in pieces)),
            "sizes": sizes,
            "materials": materials,
            "pieces": piece_details,
            "pieces_by_material": pieces_by_material,
            "perimeter_by_size": perimeter_by_size,
            "piece_config": {k: v for k, v in piece_config.items()},
        }

        # Create fabric mappings for each material
        for material in materials:
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
            "piece_count": len(set(p.identifier.piece_name for p in pieces)),
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

    def merge_materials(
        self,
        db: Session,
        pattern: Pattern,
        source_materials: List[str],
        target_name: str,
    ) -> Dict[str, Any]:
        """
        Merge multiple pattern materials into one.

        Rewrites parse_metadata and PatternFabricMapping so that all
        downstream code (nesting, cutplan, refine) sees a single material.

        For AAMA patterns that re-parse the DXF at nesting time, a
        material_merge_map is stored so the runners know to load pieces
        from all source materials.
        """
        meta = dict(pattern.parse_metadata or {})
        available = list(pattern.available_materials or [])

        # Validate
        for mat in source_materials:
            if mat not in available:
                return {"success": False, "error": f"Material '{mat}' not found in pattern"}

        source_set = set(source_materials)

        # 1. Update pieces list — rename material on each piece
        pieces = meta.get("pieces", [])
        for piece in pieces:
            if piece.get("material") in source_set:
                piece["material"] = target_name

        # 2. Rebuild pieces_by_material
        pieces_by_material = meta.get("pieces_by_material", {})
        merged_pieces = []
        for mat in source_materials:
            merged_pieces.extend(pieces_by_material.pop(mat, []))
        # Update material field on the moved piece dicts
        for p in merged_pieces:
            p["material"] = target_name
        # Add to existing target group if it already exists
        if target_name in pieces_by_material:
            pieces_by_material[target_name].extend(merged_pieces)
        else:
            pieces_by_material[target_name] = merged_pieces

        # 3. Merge perimeter_by_size — sum across source materials per size
        perim = meta.get("perimeter_by_size", {})
        merged_perim: Dict[str, float] = {}
        for mat in source_materials:
            mat_perim = perim.pop(mat, {})
            for size, val in mat_perim.items():
                merged_perim[size] = merged_perim.get(size, 0) + val
        # Add to existing target if present
        if target_name in perim:
            for size, val in merged_perim.items():
                perim[target_name][size] = perim[target_name].get(size, 0) + val
        else:
            perim[target_name] = merged_perim

        # 4. Update materials list
        new_materials = [m for m in available if m not in source_set]
        if target_name not in new_materials:
            new_materials.append(target_name)
        new_materials.sort()

        meta["materials"] = new_materials
        meta["pieces"] = pieces
        meta["pieces_by_material"] = pieces_by_material
        meta["perimeter_by_size"] = perim

        # 5. Store merge map for AAMA nesting runners
        merge_map = meta.get("material_merge_map", {})
        # Flatten: if target already has sources, extend
        existing_sources = merge_map.get(target_name, [])
        all_sources = list(existing_sources)
        for mat in source_materials:
            # If this source was itself a merge target, pull its sources
            if mat in merge_map:
                all_sources.extend(merge_map.pop(mat))
            else:
                all_sources.append(mat)
        merge_map[target_name] = sorted(set(all_sources))
        meta["material_merge_map"] = merge_map

        # 6. Update pattern record
        pattern.available_materials = new_materials
        pattern.parse_metadata = meta
        # Force SQLAlchemy to detect the JSON change
        from sqlalchemy.orm.attributes import flag_modified
        flag_modified(pattern, "parse_metadata")

        # 7. Merge PatternFabricMapping records
        existing_fabric_id = None
        mappings_to_delete = []
        for mat in source_materials:
            mapping = db.query(PatternFabricMapping).filter(
                PatternFabricMapping.pattern_id == pattern.id,
                PatternFabricMapping.material_name == mat,
            ).first()
            if mapping:
                if mapping.fabric_id and not existing_fabric_id:
                    existing_fabric_id = mapping.fabric_id
                mappings_to_delete.append(mapping)

        for m in mappings_to_delete:
            db.delete(m)
        db.flush()

        # Create or update target mapping
        target_mapping = db.query(PatternFabricMapping).filter(
            PatternFabricMapping.pattern_id == pattern.id,
            PatternFabricMapping.material_name == target_name,
        ).first()
        if not target_mapping:
            target_mapping = PatternFabricMapping(
                pattern_id=pattern.id,
                material_name=target_name,
                fabric_id=existing_fabric_id,
            )
            db.add(target_mapping)
        elif existing_fabric_id and not target_mapping.fabric_id:
            target_mapping.fabric_id = existing_fabric_id

        db.commit()
        db.refresh(pattern)

        return {
            "success": True,
            "materials": new_materials,
            "merge_map": merge_map,
        }

    def get_pattern_svg(self, pattern: Pattern) -> Optional[str]:
        """Generate SVG preview of pattern pieces."""
        # TODO: Implement SVG generation
        return None
