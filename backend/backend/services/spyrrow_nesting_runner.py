"""
Spyrrow CPU Nesting Runner - Final marker refinement using Spyrrow solver.

This module replicates the exact same pipeline as the working Streamlit app
(apps/app.py):

  1. grade_material_to_nesting_pieces() → Piece objects
  2. Pre-expand pieces into individual BundlePiece objects (demand=1 each)
  3. L/R pairs: pre-flip geometry via create_flipped_piece() + FlipMode.NONE
  4. SpyrrowEngine.solve() with demand=1 per item
  5. Render using bp.piece.vertices directly + rotate around origin + translate

NO separate piece registry.  NO FlipMode.PAIRED.  NO manual normalization.
"""

import io
import logging
import math
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Callable, Tuple

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# Vertex cleaning — each parser owns its own clean_vertices_for_spyrrow().
# The runner imports from the parser, never defines parser-specific logic.
# Only _dedup_vertices is kept here for _create_piece_copy / create_flipped_piece
# (safety dedup on already-cleaned pieces during bundle expansion).
# --------------------------------------------------------------------------

def _pts_equal(
    a: Tuple[float, float],
    b: Tuple[float, float],
    tol: float = 0.01,
) -> bool:
    """Check if two points are equal within tolerance."""
    return abs(a[0] - b[0]) < tol and abs(a[1] - b[1]) < tol


def _dedup_vertices(
    vertices: List[Tuple[float, float]],
    tolerance: float = 0.01,
) -> List[Tuple[float, float]]:
    """
    Remove duplicate vertices (both consecutive AND non-consecutive) from a
    polygon, then re-close it.

    Safety dedup for _create_piece_copy / create_flipped_piece.
    These operate on pieces that were already cleaned by their parser's
    clean_vertices_for_spyrrow(). This catches any duplicates introduced
    by the copy/flip operations themselves.

    jagua-rs (the Rust collision engine inside Spyrrow) requires simple
    polygons with NO duplicate vertices at all — not just consecutive ones.
    Non-consecutive duplicates can slip through after coordinate swaps,
    flipping, or grading interpolation.

    Algorithm:
        1. Strip closing vertex if polygon is closed
        2. Walk the vertex list, keeping only the first occurrence of each
           point (within tolerance)
        3. Remove consecutive duplicates that might remain
        4. Re-close the polygon
    """
    if len(vertices) < 3:
        return vertices

    # Remove closing vertex if present
    verts = list(vertices)
    if len(verts) > 1 and _pts_equal(verts[0], verts[-1], tolerance):
        verts = verts[:-1]

    # Remove non-consecutive duplicate vertices (keep first occurrence)
    seen: List[Tuple[float, float]] = []
    for v in verts:
        if not any(_pts_equal(v, s, tolerance) for s in seen):
            seen.append(v)

    # Remove consecutive duplicates that might remain
    cleaned: List[Tuple[float, float]] = [seen[0]] if seen else []
    for v in seen[1:]:
        if not _pts_equal(v, cleaned[-1], tolerance):
            cleaned.append(v)

    if len(cleaned) < 3:
        logger.warning(
            f"Polygon reduced to {len(cleaned)} vertices after dedup — "
            f"original had {len(vertices)}"
        )
        return vertices  # Return original; let downstream handle it

    # Re-close
    cleaned.append(cleaned[0])
    return cleaned


import ezdxf

# Add MarkerMind project root to path so nesting_engine is importable
PROJECT_ROOT = Path(__file__).parent.parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from nesting_engine.io.aama_parser import (
    load_aama_pattern, AAMAGrader,
    grade_material_to_nesting_pieces, get_pieces_by_material,
    clean_vertices_for_spyrrow as clean_aama_vertices,
)
from nesting_engine.io.dxf_block_parser import (
    clean_vertices_for_spyrrow as clean_block_dxf_vertices,
)
from nesting_engine.io.vt_dxf_parser import (
    clean_vertices_for_spyrrow as clean_vt_dxf_vertices,
)
from nesting_engine.io.gerber_accumark_parser import (
    clean_vertices_for_spyrrow as clean_accumark_vertices,
)
from nesting_engine.io.gerber_aama_parser import (
    clean_vertices_for_spyrrow as clean_gerber_aama_vertices,
)
from nesting_engine.engine.spyrrow_engine import SpyrrowEngine, SpyrrowConfig
from nesting_engine.core.instance import Container, NestingItem, NestingInstance, FlipMode
from nesting_engine.core.piece import Piece, PieceIdentifier, OrientationConstraint


def _compute_perimeter_mm(vertices_mm: List[Tuple[float, float]]) -> float:
    """Sum of edge lengths for a closed polygon. Returns mm."""
    if len(vertices_mm) < 2:
        return 0.0
    perim = 0.0
    for i in range(len(vertices_mm) - 1):
        x1, y1 = vertices_mm[i]
        x2, y2 = vertices_mm[i + 1]
        perim += math.sqrt((x2 - x1) ** 2 + (y2 - y1) ** 2)
    # Close if not already closed
    if vertices_mm[0] != vertices_mm[-1]:
        x1, y1 = vertices_mm[-1]
        x2, y2 = vertices_mm[0]
        perim += math.sqrt((x2 - x1) ** 2 + (y2 - y1) ** 2)
    return perim


# --------------------------------------------------------------------------
# BundlePiece (same dataclass as Streamlit app)
# --------------------------------------------------------------------------

@dataclass
class BundlePiece:
    """A piece with bundle tracking information (mirrors Streamlit app)."""
    piece: Piece
    bundle_id: str       # e.g., "M_1", "M_2"
    size: str
    piece_type: str
    is_flipped: bool = False
    instance_idx: int = 0


# --------------------------------------------------------------------------
# Piece copy / flip helpers (exact copies from Streamlit app)
# --------------------------------------------------------------------------

def _create_piece_copy(piece: Piece, suffix: str) -> Piece:
    """Create a copy of a piece with a unique ID suffix.
    Also cleans vertices to prevent jagua-rs panics on duplicate vertices."""
    new_id = PieceIdentifier(
        piece_name=piece.identifier.piece_name + suffix,
        style_name=piece.identifier.style_name,
        size=piece.identifier.size,
    )
    return Piece(
        vertices=_dedup_vertices(list(piece.vertices)),
        identifier=new_id,
        orientation=piece.orientation,
        grain=piece.grain,
        fold_line=piece.fold_line,
    )


def create_flipped_piece(piece: Piece) -> Piece:
    """Create a flipped version of a piece (mirrored along X center).
    Also cleans vertices to prevent jagua-rs panics on duplicate vertices."""
    verts = piece.vertices
    xs = [v[0] for v in verts]
    center_x = (min(xs) + max(xs)) / 2
    flipped_verts = [(2 * center_x - x, y) for x, y in verts]
    flipped_verts = flipped_verts[::-1]

    # Clean after flipping — the mirror + reverse can produce duplicates
    flipped_verts = _dedup_vertices(flipped_verts)

    new_id = PieceIdentifier(
        piece_name=piece.identifier.piece_name + "_f",
        style_name=piece.identifier.style_name,
        size=piece.identifier.size,
    )
    return Piece(
        vertices=flipped_verts,
        identifier=new_id,
        orientation=piece.orientation,
        grain=piece.grain,
        fold_line=piece.fold_line,
    )


# --------------------------------------------------------------------------
# Piece loading (uses grade_material_to_nesting_pieces — same as Streamlit)
# --------------------------------------------------------------------------

def load_pieces_for_spyrrow(
    dxf_path: str,
    rul_path: Optional[str],
    material: str,
    sizes: List[str],
    allowed_rotations: List[int] = [0, 180],
    file_type: Optional[str] = None,
    material_sources: Optional[List[str]] = None,
) -> Tuple[List[Piece], Dict[str, dict]]:
    """
    Load graded pieces via grade_material_to_nesting_pieces().

    For DXF-only patterns (rul_path is None), uses load_dxf_pieces_by_size()
    instead. For VT DXF patterns, uses parse_vt_dxf().

    Returns:
        (nesting_pieces, piece_config)
        - nesting_pieces: List[Piece] — one per piece-name × size
        - piece_config: {piece_name: {demand: int, flipped: bool}}
          Mirrors the Streamlit app's piece_type_config.
    """
    # VT DXF path: Optitex Graded Nest format
    if file_type == "vt_dxf":
        return _load_pieces_vt_dxf_for_spyrrow(dxf_path, sizes, allowed_rotations)

    # Gerber AccuMark path: pre-graded DXF with rich metadata
    if file_type == "gerber_accumark":
        return _load_pieces_gerber_accumark_for_spyrrow(
            dxf_path, material, sizes, allowed_rotations)

    # OptiTex AAMA path: DXF+RUL with packed multi-pair delta lines
    if file_type == "optitex_aama":
        return _load_pieces_optitex_aama_for_spyrrow(
            dxf_path, rul_path, material, sizes, allowed_rotations)

    # Gerber AAMA path: DXF+RUL with Gerber AccuMark grading
    if file_type == "gerber_aama":
        return _load_pieces_gerber_aama_for_spyrrow(
            dxf_path, rul_path, material, sizes, allowed_rotations,
            material_sources=material_sources)

    # DXF-only path: no RUL grading, pieces already sized in DXF
    if rul_path is None or not Path(rul_path).exists():
        from nesting_engine.io.dxf_parser import load_dxf_pieces_by_size
        nesting_pieces, piece_config, _ = load_dxf_pieces_by_size(
            dxf_path, sizes, rotations=allowed_rotations,
            size_names=sizes,  # Map SIZE_1..N labels to actual size names
        )
        logger.info(f"Loaded {len(nesting_pieces)} DXF-only pieces for sizes={sizes}")

        # Block parser vertex cleaning (dedup only)
        cleaned_count = 0
        for p in nesting_pieces:
            original_len = len(p.vertices)
            p.vertices = clean_block_dxf_vertices(list(p.vertices))
            if len(p.vertices) != original_len:
                cleaned_count += 1
        if cleaned_count:
            logger.info(f"Cleaned/simplified vertices for {cleaned_count}/{len(nesting_pieces)} DXF-only pieces")

        return nesting_pieces, piece_config

    # AAMA path (existing logic)
    # For merged materials, load pieces for each source material and combine
    source_mats = material_sources if material_sources else [material]
    nesting_pieces = []
    for src_mat in source_mats:
        mat_pieces = grade_material_to_nesting_pieces(
            dxf_path, rul_path,
            material=src_mat,
            target_sizes=sizes,
            rotations=allowed_rotations,
            allow_flip=True,
        )
        nesting_pieces.extend(mat_pieces)
    logger.info(f"Loaded {len(nesting_pieces)} graded pieces for material={material} (sources={source_mats}), sizes={sizes}")

    # AAMA vertex cleaning (dedup only — no simplification)
    # Uses parser-owned cleaning function per CLAUDE.md architecture rules
    cleaned_count = 0
    for p in nesting_pieces:
        original_len = len(p.vertices)
        p.vertices = clean_aama_vertices(list(p.vertices))
        if len(p.vertices) != original_len:
            cleaned_count += 1
    if cleaned_count:
        logger.info(f"Cleaned duplicate vertices from {cleaned_count}/{len(nesting_pieces)} AAMA pieces")

    # Build piece_config from AAMA annotation (L/R detection)
    aama_pieces, rules = load_aama_pattern(dxf_path, rul_path)
    # Filter to source materials for accurate L/R lookup
    accept_materials = set(m.upper() for m in source_mats)
    aama_lookup = {
        ap.name: ap for ap in aama_pieces
        if (ap.material or "").upper() in accept_materials
    }

    piece_config: Dict[str, dict] = {}
    for p in nesting_pieces:
        piece_name = p.identifier.piece_name
        if piece_name in piece_config:
            continue  # Already configured (same name across sizes)
        aama_piece = aama_lookup.get(piece_name)
        if aama_piece and aama_piece.quantity.has_left_right:
            demand = aama_piece.quantity.left_qty
            flipped = True
        else:
            demand = aama_piece.quantity.total if aama_piece else 1
            flipped = False
        piece_config[piece_name] = {'demand': demand, 'flipped': flipped}

    return nesting_pieces, piece_config


def _load_pieces_vt_dxf_for_spyrrow(
    dxf_path: str,
    sizes: List[str],
    allowed_rotations: List[int] = [0, 180],
) -> Tuple[List[Piece], Dict[str, dict]]:
    """Load pieces from a VT DXF (Optitex Graded Nest) for Spyrrow nesting."""
    from nesting_engine.io.vt_dxf_parser import parse_vt_dxf

    all_pieces, all_sizes, piece_quantities, _material = parse_vt_dxf(
        dxf_path, rotations=allowed_rotations,
    )

    # Filter to requested sizes
    target_set = set(sizes)
    nesting_pieces = [p for p in all_pieces if p.identifier.size in target_set]

    logger.info(f"Loaded {len(nesting_pieces)} VT DXF pieces for sizes={sizes}")

    # VT DXF vertex cleaning (dedup only — no simplification)
    cleaned_count = 0
    for p in nesting_pieces:
        original_len = len(p.vertices)
        p.vertices = clean_vt_dxf_vertices(list(p.vertices))
        if len(p.vertices) != original_len:
            cleaned_count += 1
    if cleaned_count:
        logger.info(f"Cleaned duplicate vertices from {cleaned_count}/{len(nesting_pieces)} VT DXF pieces")

    # Build piece_config from quantities
    # qty=2 means L/R pair -> demand=1 per side, flipped=True
    # qty=1 means single piece -> demand=1, flipped=False
    piece_config: Dict[str, dict] = {}
    for p in nesting_pieces:
        piece_name = p.identifier.piece_name
        if piece_name in piece_config:
            continue
        qty = piece_quantities.get(piece_name, 1)
        if qty >= 2:
            piece_config[piece_name] = {'demand': qty // 2, 'flipped': True}
        else:
            piece_config[piece_name] = {'demand': qty, 'flipped': False}

    return nesting_pieces, piece_config


def _load_pieces_optitex_aama_for_spyrrow(
    dxf_path: str,
    rul_path: Optional[str],
    material: str,
    sizes: List[str],
    allowed_rotations: List[int] = [0, 180],
) -> Tuple[List[Piece], Dict[str, dict]]:
    """Load pieces from OptiTex AAMA DXF+RUL for Spyrrow nesting.

    Uses the optitex_aama_parser which handles OptiTex's packed multi-pair
    delta lines in the RUL file.
    """
    from nesting_engine.io.optitex_kpr_parser import (
        grade_material_to_nesting_pieces as optitex_grade,
        load_aama_pattern as optitex_load,
        clean_vertices_for_spyrrow as clean_optitex_vertices,
    )

    nesting_pieces = optitex_grade(
        dxf_path, rul_path,
        material=material,
        target_sizes=sizes,
        rotations=allowed_rotations,
        allow_flip=True,
    )
    logger.info(f"Loaded {len(nesting_pieces)} OptiTex AAMA pieces for material={material}, sizes={sizes}")

    # OptiTex AAMA vertex cleaning (parser-owned per CLAUDE.md)
    cleaned_count = 0
    for p in nesting_pieces:
        original_len = len(p.vertices)
        p.vertices = clean_optitex_vertices(list(p.vertices))
        if len(p.vertices) != original_len:
            cleaned_count += 1
    if cleaned_count:
        logger.info(f"Cleaned duplicate vertices from {cleaned_count}/{len(nesting_pieces)} OptiTex AAMA pieces")

    # Build piece_config from AAMA annotation (L/R detection)
    aama_pieces, rules = optitex_load(dxf_path, rul_path)
    aama_lookup = {ap.name: ap for ap in aama_pieces}

    piece_config: Dict[str, dict] = {}
    for p in nesting_pieces:
        piece_name = p.identifier.piece_name
        if piece_name in piece_config:
            continue
        aama_piece = aama_lookup.get(piece_name)
        if aama_piece and aama_piece.quantity.has_left_right:
            demand = aama_piece.quantity.left_qty
            flipped = True
        else:
            demand = aama_piece.quantity.total if aama_piece else 1
            flipped = False
        piece_config[piece_name] = {'demand': demand, 'flipped': flipped}

    return nesting_pieces, piece_config


def _load_pieces_gerber_accumark_for_spyrrow(
    dxf_path: str,
    material: str,
    sizes: List[str],
    allowed_rotations: List[int] = [0, 180],
) -> Tuple[List[Piece], Dict[str, dict]]:
    """Load pieces from a Gerber AccuMark DXF for Spyrrow nesting.

    Filters pieces by material code (derived from ANNOTATION).
    """
    from nesting_engine.io.gerber_accumark_parser import parse_gerber_accumark_dxf

    all_pieces, all_sizes, mat_map, piece_cfg = parse_gerber_accumark_dxf(
        dxf_path, size_names=sizes,
    )

    # Filter to requested sizes and material
    target_set = set(sizes)
    nesting_pieces = [
        p for p in all_pieces
        if p.identifier.size in target_set
        and mat_map.get(p.identifier.piece_name) == material
    ]

    logger.info(
        f"Loaded {len(nesting_pieces)} AccuMark pieces for "
        f"material={material}, sizes={sizes}"
    )

    # AccuMark vertex cleaning (dedup only — no simplification)
    cleaned_count = 0
    for p in nesting_pieces:
        original_len = len(p.vertices)
        p.vertices = clean_accumark_vertices(list(p.vertices))
        if len(p.vertices) != original_len:
            cleaned_count += 1
    if cleaned_count:
        logger.info(
            f"Cleaned duplicate vertices from "
            f"{cleaned_count}/{len(nesting_pieces)} AccuMark pieces"
        )

    # Build piece_config from parser's piece_cfg (already has demand + flipped)
    piece_config: Dict[str, dict] = {}
    for p in nesting_pieces:
        pname = p.identifier.piece_name
        if pname in piece_config:
            continue
        cfg = piece_cfg.get(pname, {'demand': 1, 'flipped': False})
        piece_config[pname] = cfg

    return nesting_pieces, piece_config


def _load_pieces_gerber_aama_for_spyrrow(
    dxf_path: str,
    rul_path: Optional[str],
    material: str,
    sizes: List[str],
    allowed_rotations: List[int] = [0, 180],
    material_sources: Optional[List[str]] = None,
) -> Tuple[List[Piece], Dict[str, dict]]:
    """Load pieces from Gerber AAMA DXF+RUL for Spyrrow nesting.

    Uses the gerber_aama_parser which handles Gerber AccuMark-specific
    AAMA format differences (chained L1 polylines, etc.).
    """
    from nesting_engine.io.gerber_aama_parser import (
        grade_material_to_nesting_pieces as gerber_aama_grade,
        parse_gerber_aama,
    )

    # For merged materials, load pieces for each source material and combine
    source_mats = material_sources if material_sources else [material]
    nesting_pieces = []
    for src_mat in source_mats:
        mat_pieces = gerber_aama_grade(
            dxf_path, rul_path,
            material=src_mat,
            target_sizes=sizes,
            rotations=allowed_rotations,
            allow_flip=True,
        )
        nesting_pieces.extend(mat_pieces)
    logger.info(f"Loaded {len(nesting_pieces)} Gerber AAMA pieces for material={material} (sources={source_mats}), sizes={sizes}")

    # Gerber AAMA vertex cleaning (parser-owned per CLAUDE.md)
    cleaned_count = 0
    for p in nesting_pieces:
        original_len = len(p.vertices)
        p.vertices = clean_gerber_aama_vertices(list(p.vertices))
        if len(p.vertices) != original_len:
            cleaned_count += 1
    if cleaned_count:
        logger.info(f"Cleaned duplicate vertices from {cleaned_count}/{len(nesting_pieces)} Gerber AAMA pieces")

    # Build piece_config from parsed pieces (L/R detection)
    # GerberAAMAPiece has quantity as plain int, no L/R info at parser level
    pieces_raw, rules = parse_gerber_aama(dxf_path, rul_path)
    # Filter to source materials for accurate lookup
    accept_materials = set(m.upper() for m in source_mats)
    aama_lookup = {
        ap.name: ap for ap in pieces_raw
        if (ap.material or "").upper() in accept_materials
    }

    piece_config: Dict[str, dict] = {}
    for p in nesting_pieces:
        piece_name = p.identifier.piece_name
        if piece_name in piece_config:
            continue  # Already configured (same name across sizes)
        aama_piece = aama_lookup.get(piece_name)
        # Gerber AAMA quantity is a plain int, no L/R distinction
        demand = aama_piece.quantity if aama_piece else 1
        piece_config[piece_name] = {'demand': demand, 'flipped': False}

    return nesting_pieces, piece_config


# --------------------------------------------------------------------------
# Build bundle pieces (exact replication of Streamlit build_bundle_pieces)
# --------------------------------------------------------------------------

def _group_pieces_by_name(pieces: List[Piece]) -> Dict[str, Dict[str, List[Piece]]]:
    """Group pieces by piece_name and size. Returns {piece_name: {size: [pieces]}}"""
    grouped: Dict[str, Dict[str, List[Piece]]] = {}
    for p in pieces:
        piece_name = p.identifier.piece_name or p.name
        size = p.identifier.size or ""
        if piece_name not in grouped:
            grouped[piece_name] = {}
        if size not in grouped[piece_name]:
            grouped[piece_name][size] = []
        grouped[piece_name][size].append(p)
    return grouped


def build_bundle_pieces(
    grouped: Dict[str, Dict[str, List[Piece]]],
    piece_config: Dict[str, dict],
    size_quantities: Dict[str, int],
) -> List[BundlePiece]:
    """
    Pre-expand pieces into individual BundlePiece objects.

    This is the exact same logic as the Streamlit app's build_bundle_pieces():
    - Each physical piece instance gets a unique Piece with unique ID
    - L/R pieces are pre-flipped (create_flipped_piece) — NOT FlipMode.PAIRED
    - Every BundlePiece maps to demand=1, FlipMode.NONE in the solver
    """
    bundle_pieces: List[BundlePiece] = []
    active_sizes = {s: q for s, q in size_quantities.items() if q > 0}

    for size, num_garments in active_sizes.items():
        for garment_idx in range(num_garments):
            bundle_id = f"{size}_{garment_idx + 1}"

            for ptype, pieces_by_size in grouped.items():
                if size not in pieces_by_size:
                    continue
                piece_list = pieces_by_size[size]
                if not piece_list:
                    continue

                base_piece = piece_list[0]
                config = piece_config.get(ptype, {'demand': 1, 'flipped': False})
                demand = config.get('demand', 1)
                is_flipped_type = config.get('flipped', False)

                if is_flipped_type:
                    # L/R piece: create 'demand' normal + 'demand' flipped
                    for i in range(demand):
                        unique_piece = _create_piece_copy(base_piece, f"_{bundle_id}_n{i}")
                        bundle_pieces.append(BundlePiece(
                            piece=unique_piece,
                            bundle_id=bundle_id,
                            size=size,
                            piece_type=ptype,
                            is_flipped=False,
                            instance_idx=i,
                        ))

                    for i in range(demand):
                        flipped_piece = create_flipped_piece(base_piece)
                        unique_piece = _create_piece_copy(flipped_piece, f"_{bundle_id}_f{i}")
                        bundle_pieces.append(BundlePiece(
                            piece=unique_piece,
                            bundle_id=bundle_id,
                            size=size,
                            piece_type=ptype,
                            is_flipped=True,
                            instance_idx=i,
                        ))
                else:
                    for i in range(demand):
                        unique_piece = _create_piece_copy(base_piece, f"_{bundle_id}_n{i}")
                        bundle_pieces.append(BundlePiece(
                            piece=unique_piece,
                            bundle_id=bundle_id,
                            size=size,
                            piece_type=ptype,
                            is_flipped=False,
                            instance_idx=i,
                        ))

    return bundle_pieces


# --------------------------------------------------------------------------
# Nesting (exact replication of Streamlit run_nesting)
# --------------------------------------------------------------------------

def run_nesting(
    bundle_pieces: List[BundlePiece],
    fabric_width_mm: float,
    piece_buffer: float,
    edge_buffer: float,
    time_limit: float,
    quadtree_depth: int = 4,
    early_termination: bool = True,
    exploration_time: Optional[int] = None,
    compression_time: Optional[int] = None,
    seed: int = 42,
) -> 'NestingSolution':
    """Run the nesting solver — identical to Streamlit run_nesting()."""
    nest_pieces = [bp.piece for bp in bundle_pieces]

    container = Container(width=fabric_width_mm, height=None)

    items = [
        NestingItem(piece=p, demand=1, flip_mode=FlipMode.NONE)
        for p in nest_pieces
    ]

    instance = NestingInstance.create(
        name="FinalMarker",
        container=container,
        items=items,
        piece_buffer=piece_buffer,
        edge_buffer=edge_buffer,
    )

    engine = SpyrrowEngine()
    config = SpyrrowConfig(
        time_limit=time_limit,
        num_workers=None,
        seed=seed,
        early_termination=early_termination,
        quadtree_depth=quadtree_depth,
        exploration_time=exploration_time,
        compression_time=compression_time,
    )

    return engine.solve(instance, config=config)


# --------------------------------------------------------------------------
# Single marker nesting (high-level: load → expand → solve → package)
# --------------------------------------------------------------------------

def nest_single_marker(
    ratio: Dict[str, int],
    nesting_pieces: List[Piece],
    piece_config: Dict[str, dict],
    fabric_width_mm: float,
    piece_buffer_mm: float = 0.0,
    edge_buffer_mm: float = 0.0,
    time_limit: float = 20.0,
    rotation_mode: str = "free",
    quadtree_depth: int = 4,
    early_termination: bool = True,
    exploration_time: Optional[int] = None,
    compression_time: Optional[int] = None,
    seed: int = 42,
    seed_screening: bool = False,
) -> Dict:
    """
    Run Spyrrow on a single marker ratio.

    Uses the exact Streamlit pipeline:
    1. Group pieces by name
    2. build_bundle_pieces() with the ratio as size_quantities
    3. run_nesting() with demand=1 per piece
    4. Package solution + bundle_pieces for rendering

    Returns dict with: utilization, strip_length_mm, length_yards,
        solution, bundle_pieces, computation_time_s
    """
    grouped = _group_pieces_by_name(nesting_pieces)

    # ratio is {size: count} — this IS the size_quantities dict
    bundle_pieces = build_bundle_pieces(grouped, piece_config, ratio)

    if not bundle_pieces:
        return {
            'utilization': 0.0,
            'strip_length_mm': 0.0,
            'length_yards': 0.0,
            'solution': None,
            'bundle_pieces': [],
            'computation_time_s': 0.0,
        }

    logger.info(f"Nesting {len(bundle_pieces)} pieces for ratio {ratio}")

    # Seed screening: run 6 random seeds for 10s each, pick the best
    if seed_screening:
        import random
        screen_seeds = [random.randint(1, 9999) for _ in range(6)]
        best_seed = seed
        best_util = 0.0
        for s in screen_seeds:
            quick_sol = run_nesting(
                bundle_pieces, fabric_width_mm, piece_buffer_mm, edge_buffer_mm,
                time_limit=10, quadtree_depth=quadtree_depth,
                early_termination=False, seed=s,
            )
            if quick_sol.utilization_percent > best_util:
                best_util = quick_sol.utilization_percent
                best_seed = s
        seed = best_seed
        logger.info(f"Seed screening: best={best_seed} ({best_util:.2f}%) from {screen_seeds}")

    start = time.time()
    solution = run_nesting(
        bundle_pieces,
        fabric_width_mm,
        piece_buffer_mm,
        edge_buffer_mm,
        time_limit,
        quadtree_depth=quadtree_depth,
        early_termination=early_termination,
        exploration_time=exploration_time,
        compression_time=compression_time,
        seed=seed,
    )
    elapsed = time.time() - start
    logger.info(f"Spyrrow solve completed in {elapsed:.1f}s (limit={time_limit}s, early_term={early_termination}, qt_depth={quadtree_depth}, seed={seed})")

    length_yards = solution.strip_length / 914.4  # 1 yard = 914.4 mm

    # Compute total perimeter from placed pieces (vertices already in mm)
    piece_map = {bp.piece.id: bp for bp in bundle_pieces}
    total_perimeter_mm = 0.0
    for placement in solution.placements:
        bp = piece_map.get(placement.piece_id)
        if bp:
            total_perimeter_mm += _compute_perimeter_mm(list(bp.piece.vertices))
    perimeter_cm = total_perimeter_mm / 10.0

    return {
        'utilization': solution.utilization_percent / 100.0,
        'strip_length_mm': solution.strip_length,
        'length_yards': length_yards,
        'perimeter_cm': perimeter_cm,
        'solution': solution,
        'bundle_pieces': bundle_pieces,
        'computation_time_s': elapsed,
        'seed_used': seed,
    }


# --------------------------------------------------------------------------
# SVG Export (exact replication of Streamlit export_to_svg)
# --------------------------------------------------------------------------

def export_marker_svg(
    solution_data: Dict,
    fabric_width_mm: float,
    max_width_px: int = 1200,
) -> str:
    """
    Export a single marker solution to SVG string for web preview.

    Uses the exact same transform as Streamlit:
    - verts = bp.piece.vertices
    - rotate around origin (0, 0)
    - translate by (placement.x, placement.y)
    - flip Y for SVG coordinate system
    """
    solution = solution_data.get('solution')
    bundle_pieces = solution_data.get('bundle_pieces', [])

    if solution is None or solution.strip_length <= 0:
        return '<svg xmlns="http://www.w3.org/2000/svg" width="100" height="40"><text x="10" y="20" font-size="12">No solution</text></svg>'

    strip_length = solution.strip_length

    # Build piece_map: piece.id → BundlePiece (same as Streamlit)
    piece_map = {bp.piece.id: bp for bp in bundle_pieces}

    # Color palette — one color per size for easy identification
    colors = [
        '#4CAF50', '#2196F3', '#FF9800', '#9C27B0', '#F44336',
        '#00BCD4', '#795548', '#607D8B', '#E91E63', '#3F51B5',
    ]
    size_color_map: Dict[str, str] = {}
    color_idx = 0

    # SVG header
    scale = max_width_px / strip_length
    svg_width = max_width_px
    svg_height = int(fabric_width_mm * scale)

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'viewBox="0 0 {strip_length:.1f} {fabric_width_mm:.1f}" '
        f'preserveAspectRatio="xMinYMin meet" '
        f'style="background:#f5f5f5;border:1px solid #ddd;border-radius:4px;width:100%;height:auto;max-height:200px">',
    ]

    # Container outline
    sw = max(strip_length, fabric_width_mm) * 0.002
    parts.append(
        f'<rect x="0" y="0" width="{strip_length:.1f}" height="{fabric_width_mm:.1f}" '
        f'fill="none" stroke="#999" stroke-width="{sw:.2f}"/>'
    )

    rendered = 0
    skipped = 0

    for placement in solution.placements:
        bp = piece_map.get(placement.piece_id)
        if bp is None:
            skipped += 1
            continue

        rendered += 1

        # Transform: exact same as Streamlit app (lines 478-487)
        verts = list(bp.piece.vertices)

        if placement.rotation != 0:
            angle_rad = math.radians(placement.rotation)
            cos_a = math.cos(angle_rad)
            sin_a = math.sin(angle_rad)
            verts = [(x * cos_a - y * sin_a, x * sin_a + y * cos_a) for x, y in verts]

        verts = [(x + placement.x, y + placement.y) for x, y in verts]

        # Flip Y for SVG coordinate system (Streamlit line 487)
        verts = [(x, fabric_width_mm - y) for x, y in verts]

        # Color by size
        size = bp.size
        if size not in size_color_map:
            size_color_map[size] = colors[color_idx % len(colors)]
            color_idx += 1
        color = size_color_map[size]

        points_str = ' '.join(f'{x:.1f},{y:.1f}' for x, y in verts)
        stroke_w = max(strip_length, fabric_width_mm) * 0.001
        opacity = "0.3" if bp.is_flipped else "0.5"
        stroke_color = "#8B0000" if bp.is_flipped else color

        parts.append(
            f'<polygon points="{points_str}" fill="{color}" fill-opacity="{opacity}" '
            f'stroke="{stroke_color}" stroke-width="{stroke_w:.2f}"/>'
        )

        # Size label at centroid
        cx = sum(x for x, y in verts) / len(verts)
        cy = sum(y for x, y in verts) / len(verts)
        font_size = max(strip_length, fabric_width_mm) * 0.022
        parts.append(
            f'<text x="{cx:.1f}" y="{cy:.1f}" font-size="{font_size:.1f}" '
            f'fill="#333" text-anchor="middle" dominant-baseline="middle" '
            f'font-family="sans-serif" font-weight="600" opacity="0.8">{size}</text>'
        )

    logger.info(f"SVG export: {rendered} pieces rendered, {skipped} skipped")
    parts.append('</svg>')
    return '\n'.join(parts)


# --------------------------------------------------------------------------
# DXF Export (using ezdxf, same transform as Streamlit export_to_dxf)
# --------------------------------------------------------------------------

def export_marker_dxf(
    solution_data: Dict,
    fabric_width_mm: float,
    marker_label: str,
) -> bytes:
    """
    Export a single marker solution to DXF bytes using ezdxf.

    Uses the exact same transform as Streamlit:
    - verts = bp.piece.vertices
    - rotate around origin (0, 0)
    - translate by (placement.x, placement.y)
    """
    solution = solution_data.get('solution')
    bundle_pieces = solution_data.get('bundle_pieces', [])

    doc = ezdxf.new('R2010')
    msp = doc.modelspace()

    doc.layers.add("CONTAINER", color=7)
    doc.layers.add("INFO", color=7)

    # Container outline
    strip_length = solution.strip_length if solution else 0
    container_pts = [
        (0, 0), (strip_length, 0),
        (strip_length, fabric_width_mm), (0, fabric_width_mm), (0, 0),
    ]
    msp.add_lwpolyline(container_pts, dxfattribs={"layer": "CONTAINER"})

    if solution is None:
        stream = io.StringIO()
        doc.write(stream)
        stream.seek(0)
        return stream.getvalue().encode('utf-8')

    piece_map = {bp.piece.id: bp for bp in bundle_pieces}

    # Color cycle for layers
    color_cycle = [1, 2, 3, 4, 5, 6, 8, 9]
    type_layers: Dict[str, str] = {}
    color_idx = 0

    for placement in solution.placements:
        bp = piece_map.get(placement.piece_id)
        if bp is None:
            continue

        # Same transform as Streamlit (lines 534-541)
        verts = list(bp.piece.vertices)

        if placement.rotation != 0:
            angle_rad = math.radians(placement.rotation)
            cos_a = math.cos(angle_rad)
            sin_a = math.sin(angle_rad)
            verts = [(x * cos_a - y * sin_a, x * sin_a + y * cos_a) for x, y in verts]

        verts = [(x + placement.x, y + placement.y) for x, y in verts]

        # Ensure closed
        if verts[0] != verts[-1]:
            verts.append(verts[0])

        # Layer per bundle+type (same as Streamlit line 543)
        layer_name = f"{bp.bundle_id}_{bp.piece_type}"
        if layer_name not in type_layers:
            color = color_cycle[color_idx % len(color_cycle)]
            color_idx += 1
            doc.layers.add(layer_name, color=color)
            type_layers[layer_name] = layer_name

        msp.add_lwpolyline(verts, dxfattribs={"layer": layer_name})

    # Info text
    util_pct = solution_data.get('utilization', 0) * 100
    yards = solution_data.get('length_yards', 0)
    info_text = f"{marker_label} | Util: {util_pct:.1f}% | Length: {strip_length:.1f}mm ({yards:.2f}yd)"
    msp.add_text(
        info_text,
        dxfattribs={
            "layer": "INFO",
            "height": 30,
            "insert": (10, fabric_width_mm + 50),
        },
    )

    stream = io.StringIO()
    doc.write(stream)
    stream.seek(0)
    return stream.getvalue().encode('utf-8')


# --------------------------------------------------------------------------
# Remote nesting via Surface SSH
# --------------------------------------------------------------------------

def nest_single_marker_surface(
    ratio: Dict[str, int],
    nesting_pieces: List[Piece],
    piece_config: Dict[str, dict],
    fabric_width_mm: float,
    piece_buffer_mm: float = 0.0,
    edge_buffer_mm: float = 0.0,
    time_limit: float = 20.0,
    rotation_mode: str = "free",
    quadtree_depth: int = 4,
    early_termination: bool = True,
    exploration_time: Optional[int] = None,
    compression_time: Optional[int] = None,
    seed: int = 42,
    seed_screening: bool = False,
) -> Dict:
    """
    Run nesting on the Surface worker via SSH pipe.

    Same interface as nest_single_marker() — builds bundle_pieces locally,
    sends anonymous vertices to Surface, maps placements back.
    """
    import subprocess
    import json
    from nesting_engine.core.solution import PlacedPiece

    grouped = _group_pieces_by_name(nesting_pieces)
    bundle_pieces = build_bundle_pieces(grouped, piece_config, ratio)

    if not bundle_pieces:
        return {
            'utilization': 0.0,
            'strip_length_mm': 0.0,
            'length_yards': 0.0,
            'solution': None,
            'bundle_pieces': [],
            'computation_time_s': 0.0,
        }

    effective_width = fabric_width_mm - 2 * edge_buffer_mm
    orientation = "free" if rotation_mode == "free" else "nap_one_way"

    # Build job payload
    remote_pieces = []
    for bp in bundle_pieces:
        verts = [list(v) for v in bp.piece.vertices]
        if verts and verts[0] != verts[-1]:
            verts.append(verts[0])
        remote_pieces.append({
            "vertices": verts,
            "demand": 1,
            "allowed_orientations": [0.0, 180.0] if orientation == "free" else [0.0],
        })

    remote_config = {
        "quadtree_depth": quadtree_depth,
        "early_termination": early_termination,
        "seed": seed,
        "num_workers": 0,
        "min_items_separation": piece_buffer_mm if piece_buffer_mm > 0 else None,
    }
    if exploration_time is not None and compression_time is not None:
        remote_config["exploration_time"] = exploration_time
        remote_config["compression_time"] = compression_time
    else:
        remote_config["time_limit_s"] = int(time_limit)

    job_payload = json.dumps({
        "pieces": remote_pieces,
        "strip_width_mm": effective_width,
        "config": remote_config,
        "label": "-".join(str(ratio.get(s, 0)) for s in ratio),
    })

    ssh_cmd = [
        "ssh",
        "-o", "ConnectTimeout=5",
        "-o", "ServerAliveInterval=30",
        "-o", "ServerAliveCountMax=240",
        "surface",
        "source ~/nester/bin/activate && python3 ~/surface_nesting_worker.py --stdin",
    ]
    logger.info(f"Surface nest: {len(remote_pieces)} pieces, width={effective_width:.0f}mm")
    proc = subprocess.run(
        ssh_cmd,
        input=job_payload,
        capture_output=True,
        text=True,
        timeout=int(time_limit * 2) + 600,  # generous: Spyrrow on slow CPUs overruns heavily
    )
    if proc.returncode != 0:
        raise RuntimeError(f"Surface nesting failed: {proc.stderr.strip()[-500:]}")

    result = json.loads(proc.stdout)

    # Map placements back to local bundle_pieces
    placements = []
    for cp in result["placements"]:
        idx = cp["piece_index"]
        if idx < len(bundle_pieces):
            bp = bundle_pieces[idx]
            placements.append(PlacedPiece(
                piece_id=bp.piece.id,
                instance_index=0,
                x=cp["x"],
                y=cp["y"],
                rotation=cp["rotation"],
                flipped=False,
            ))

    class _RemoteSolution:
        def __init__(self, strip_length, util_pct, placed):
            self.strip_length = strip_length
            self.utilization_percent = util_pct * 100
            self.placements = placed

    mock_solution = _RemoteSolution(
        result["strip_length_mm"],
        result["utilization"],
        placements,
    )

    return {
        'utilization': result["utilization"],
        'strip_length_mm': result["strip_length_mm"],
        'length_yards': result["strip_length_mm"] / 914.4,
        'solution': mock_solution,
        'bundle_pieces': bundle_pieces,
        'computation_time_s': result.get("computation_time_s", 0),
    }


# --------------------------------------------------------------------------
# Full Cutplan Refinement
# --------------------------------------------------------------------------

def refine_cutplan_markers(
    dxf_path: str,
    rul_path: str,
    material: str,
    sizes: List[str],
    markers: List[Dict],
    fabric_width_mm: float,
    piece_buffer_mm: float = 0.0,
    edge_buffer_mm: float = 0.0,
    time_limit: float = 20.0,
    rotation_mode: str = "free",
    progress_callback: Optional[Callable] = None,
    cancel_check: Optional[Callable] = None,
    file_type: Optional[str] = None,
    quadtree_depth: int = 4,
    early_termination: bool = True,
    exploration_time: Optional[int] = None,
    compression_time: Optional[int] = None,
    seed_screening: bool = False,
    use_surface: bool = False,
    material_sources: Optional[List[str]] = None,
) -> List[Dict]:
    """
    Refine all markers in a cutplan sequentially with Spyrrow.

    Args:
        markers: List of dicts, each with at least 'ratio_str' key
        progress_callback: Called as (marker_idx, total, result_dict) after each marker
        cancel_check: Returns True if job should be cancelled
        material_sources: For merged materials, original DXF material names

    Returns:
        List of result dicts, one per marker, with:
            ratio_str, utilization, strip_length_mm, length_yards,
            computation_time_s, svg_preview, dxf_bytes
    """
    allowed_rotations = [0, 180] if rotation_mode == "free" else [0]

    # Load pieces once for all markers
    nesting_pieces, piece_config = load_pieces_for_spyrrow(
        dxf_path, rul_path, material, sizes, allowed_rotations,
        file_type=file_type,
        material_sources=material_sources,
    )

    total = len(markers)
    results = []

    for idx, marker_info in enumerate(markers):
        if cancel_check and cancel_check():
            break

        ratio_str = marker_info['ratio_str']

        # Parse ratio_str (e.g. "0-3-1-1-1-0-0") into {size: count}
        parts = ratio_str.split('-')
        ratio = {}
        for i, size in enumerate(sizes):
            if i < len(parts):
                ratio[size] = int(parts[i])
            else:
                ratio[size] = 0

        marker_label = f"M{idx + 1}"

        # Run Spyrrow (local or Surface via global queue)
        if use_surface:
            from backend.services.surface_queue import SurfaceNestingQueue

            # Pre-build the data the queue worker needs
            grouped = _group_pieces_by_name(nesting_pieces)
            bundle_pieces = build_bundle_pieces(grouped, piece_config, ratio)
            effective_width = fabric_width_mm - 2 * edge_buffer_mm
            orientation = "free" if rotation_mode == "free" else "nap_one_way"

            remote_pieces = []
            for bp in bundle_pieces:
                verts = [list(v) for v in bp.piece.vertices]
                if verts and verts[0] != verts[-1]:
                    verts.append(verts[0])
                remote_pieces.append({
                    "vertices": verts,
                    "demand": 1,
                    "allowed_orientations": [0.0, 180.0] if orientation == "free" else [0.0],
                })

            remote_config = {
                "quadtree_depth": quadtree_depth,
                "early_termination": early_termination,
                "seed": 42,
                "num_workers": 0,
                "min_items_separation": piece_buffer_mm if piece_buffer_mm > 0 else None,
            }
            if exploration_time is not None and compression_time is not None:
                remote_config["exploration_time"] = exploration_time
                remote_config["compression_time"] = compression_time
            else:
                remote_config["time_limit_s"] = int(time_limit)

            cutplan_id = marker_info.get('cutplan_id', 'unknown')
            solution_data = SurfaceNestingQueue.get().submit(
                params={
                    "bundle_pieces": bundle_pieces,
                    "effective_width": effective_width,
                    "remote_pieces": remote_pieces,
                    "remote_config": remote_config,
                    "label": ratio_str,
                },
                marker_label=marker_label,
                cutplan_id=cutplan_id,
            )
        else:
            solution_data = nest_single_marker(
                ratio=ratio,
                nesting_pieces=nesting_pieces,
                piece_config=piece_config,
                fabric_width_mm=fabric_width_mm,
                piece_buffer_mm=piece_buffer_mm,
                edge_buffer_mm=edge_buffer_mm,
                time_limit=time_limit,
                rotation_mode=rotation_mode,
                quadtree_depth=quadtree_depth,
                early_termination=early_termination,
                exploration_time=exploration_time,
                compression_time=compression_time,
                seed_screening=seed_screening,
            )

        # Generate exports
        svg_preview = export_marker_svg(solution_data, fabric_width_mm)
        dxf_bytes = export_marker_dxf(solution_data, fabric_width_mm, marker_label)

        result = {
            'marker_idx': idx,
            'marker_label': marker_label,
            'ratio_str': ratio_str,
            'utilization': solution_data['utilization'],
            'strip_length_mm': solution_data['strip_length_mm'],
            'length_yards': solution_data['length_yards'],
            'perimeter_cm': solution_data.get('perimeter_cm', 0.0),
            'computation_time_s': solution_data['computation_time_s'],
            'svg_preview': svg_preview,
            'dxf_bytes': dxf_bytes,
        }
        results.append(result)

        if progress_callback:
            progress_callback(idx, total, result)

    return results


# --------------------------------------------------------------------------
# Quick CPU Preview Nesting (parallel, short time limit)
# --------------------------------------------------------------------------

def quick_nest_markers(
    dxf_path: str,
    rul_path: Optional[str],
    material: str,
    sizes: List[str],
    ratio_strs: List[str],
    fabric_width_mm: float,
    time_limit: float = 20.0,
    rotation_mode: str = "free",
    file_type: Optional[str] = None,
    progress_callback: Optional[Callable] = None,
    material_sources: Optional[List[str]] = None,
) -> Dict[str, Dict]:
    """
    Nest multiple markers in parallel using ThreadPoolExecutor for quick CPU preview.

    Loads pieces ONCE, then dispatches each ratio to a separate thread.
    Spyrrow is Rust and releases the GIL, so ThreadPoolExecutor works well.

    Args:
        ratio_strs: List of ratio strings (e.g. ["0-3-1-1-1-0-0", "1-0-0-1-0-0-0"])
        progress_callback: Optional (done_count, total, ratio_str) callback
        time_limit: CPU time per marker (default 20s)

    Returns:
        {ratio_str: {utilization, length_yards, length_mm, computation_time_s}}
    """
    import concurrent.futures
    import os

    allowed_rotations = [0, 180] if rotation_mode == "free" else [0]

    # Load pieces once for all markers
    nesting_pieces, piece_config = load_pieces_for_spyrrow(
        dxf_path, rul_path, material, sizes, allowed_rotations,
        file_type=file_type, material_sources=material_sources,
    )

    # Limit parallelism: concurrent threads contend for CPU and degrade Spyrrow
    # quality by ~0.7pp per worker. Cap at half cores (max 3) for balance.
    max_workers = min(len(ratio_strs), max(1, (os.cpu_count() or 2) // 4), 3)
    total = len(ratio_strs)
    results: Dict[str, Dict] = {}
    done_count = 0

    def _nest_one(ratio_str: str) -> tuple:
        """Nest a single ratio and return (ratio_str, result_dict)."""
        parts = ratio_str.split('-')
        ratio = {}
        for i, size in enumerate(sizes):
            ratio[size] = int(parts[i]) if i < len(parts) else 0

        solution_data = nest_single_marker(
            ratio=ratio,
            nesting_pieces=nesting_pieces,
            piece_config=piece_config,
            fabric_width_mm=fabric_width_mm,
            piece_buffer_mm=0.0,
            edge_buffer_mm=0.0,
            time_limit=time_limit,
            rotation_mode=rotation_mode,
            quadtree_depth=4,
            early_termination=True,
        )

        # Generate SVG preview from the solution
        svg_preview = None
        if solution_data.get('solution') and solution_data.get('bundle_pieces'):
            try:
                svg_preview = export_marker_svg(solution_data, fabric_width_mm)
            except Exception:
                pass  # SVG generation is best-effort

        return (ratio_str, {
            'utilization': solution_data['utilization'],
            'length_yards': solution_data['length_yards'],
            'length_mm': solution_data['strip_length_mm'],
            'computation_time_s': solution_data['computation_time_s'],
            'svg_preview': svg_preview,
        })

    logger.info(f"[CPU Preview] Nesting {total} markers with {max_workers} workers, {time_limit}s each")

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(_nest_one, rs): rs for rs in ratio_strs}
        for future in concurrent.futures.as_completed(futures):
            ratio_str = futures[future]
            try:
                rs, result = future.result()
                results[rs] = result
                done_count += 1
                logger.info(
                    f"[CPU Preview] {done_count}/{total} — {rs}: "
                    f"{result['utilization']*100:.1f}% / {result['length_yards']:.2f}yd "
                    f"({result['computation_time_s']:.1f}s)"
                )
                if progress_callback:
                    progress_callback(done_count, total, rs)
            except Exception as e:
                logger.error(f"[CPU Preview] Failed to nest {ratio_str}: {e}")
                done_count += 1
                if progress_callback:
                    progress_callback(done_count, total, ratio_str)

    logger.info(f"[CPU Preview] Complete: {len(results)}/{total} markers nested")
    return results
