"""
Spyrrow CPU Nesting Runner - Final marker refinement using Spyrrow solver.

This module replicates the exact same pipeline as the working Streamlit app
(garment-nester/apps/app.py):

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

import ezdxf

# Add garment-nester to path
GARMENT_NESTER_PATH = Path(__file__).parent.parent.parent.parent.parent / "garment-nester"
sys.path.insert(0, str(GARMENT_NESTER_PATH))

from nesting_engine.io.aama_parser import (
    load_aama_pattern, AAMAGrader,
    grade_material_to_nesting_pieces, get_pieces_by_material,
)
from nesting_engine.engine.spyrrow_engine import SpyrrowEngine, SpyrrowConfig
from nesting_engine.core.instance import Container, NestingItem, NestingInstance, FlipMode
from nesting_engine.core.piece import Piece, PieceIdentifier, OrientationConstraint


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
    """Create a copy of a piece with a unique ID suffix."""
    new_id = PieceIdentifier(
        piece_name=piece.identifier.piece_name + suffix,
        style_name=piece.identifier.style_name,
        size=piece.identifier.size,
    )
    return Piece(
        vertices=piece.vertices,
        identifier=new_id,
        orientation=piece.orientation,
        grain=piece.grain,
        fold_line=piece.fold_line,
    )


def create_flipped_piece(piece: Piece) -> Piece:
    """Create a flipped version of a piece (mirrored along X center)."""
    verts = piece.vertices
    xs = [v[0] for v in verts]
    center_x = (min(xs) + max(xs)) / 2
    flipped_verts = [(2 * center_x - x, y) for x, y in verts]
    flipped_verts = flipped_verts[::-1]

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
    rul_path: str,
    material: str,
    sizes: List[str],
    allowed_rotations: List[int] = [0, 180],
) -> Tuple[List[Piece], Dict[str, dict]]:
    """
    Load graded pieces via grade_material_to_nesting_pieces().

    Returns:
        (nesting_pieces, piece_config)
        - nesting_pieces: List[Piece] — one per piece-name × size
        - piece_config: {piece_name: {demand: int, flipped: bool}}
          Mirrors the Streamlit app's piece_type_config.
    """
    nesting_pieces = grade_material_to_nesting_pieces(
        dxf_path, rul_path,
        material=material,
        target_sizes=sizes,
        rotations=allowed_rotations,
        allow_flip=True,
    )
    logger.info(f"Loaded {len(nesting_pieces)} graded pieces for material={material}, sizes={sizes}")

    # Build piece_config from AAMA annotation (L/R detection)
    # Same logic as Streamlit app lines 1036-1055
    aama_pieces, rules = load_aama_pattern(dxf_path, rul_path)
    aama_lookup = {ap.name: ap for ap in aama_pieces}

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
        seed=42,
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
    piece_buffer_mm: float = 2.0,
    edge_buffer_mm: float = 5.0,
    time_limit: float = 20.0,
    rotation_mode: str = "free",
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

    start = time.time()
    solution = run_nesting(
        bundle_pieces,
        fabric_width_mm,
        piece_buffer_mm,
        edge_buffer_mm,
        time_limit,
    )
    elapsed = time.time() - start

    length_yards = solution.strip_length / 914.4  # 1 yard = 914.4 mm

    return {
        'utilization': solution.utilization_percent / 100.0,
        'strip_length_mm': solution.strip_length,
        'length_yards': length_yards,
        'solution': solution,
        'bundle_pieces': bundle_pieces,
        'computation_time_s': elapsed,
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
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{svg_width}" height="{svg_height + 2}" '
        f'viewBox="0 0 {strip_length:.1f} {fabric_width_mm:.1f}" '
        f'style="background:#f5f5f5;border:1px solid #ddd;border-radius:4px">',
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
# Full Cutplan Refinement
# --------------------------------------------------------------------------

def refine_cutplan_markers(
    dxf_path: str,
    rul_path: str,
    material: str,
    sizes: List[str],
    markers: List[Dict],
    fabric_width_mm: float,
    piece_buffer_mm: float = 2.0,
    edge_buffer_mm: float = 5.0,
    time_limit: float = 20.0,
    rotation_mode: str = "free",
    progress_callback: Optional[Callable] = None,
    cancel_check: Optional[Callable] = None,
) -> List[Dict]:
    """
    Refine all markers in a cutplan sequentially with Spyrrow.

    Args:
        markers: List of dicts, each with at least 'ratio_str' key
        progress_callback: Called as (marker_idx, total, result_dict) after each marker
        cancel_check: Returns True if job should be cancelled

    Returns:
        List of result dicts, one per marker, with:
            ratio_str, utilization, strip_length_mm, length_yards,
            computation_time_s, svg_preview, dxf_bytes
    """
    allowed_rotations = [0, 180] if rotation_mode == "free" else [0]

    # Load pieces once for all markers
    nesting_pieces, piece_config = load_pieces_for_spyrrow(
        dxf_path, rul_path, material, sizes, allowed_rotations,
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

        # Run Spyrrow
        solution_data = nest_single_marker(
            ratio=ratio,
            nesting_pieces=nesting_pieces,
            piece_config=piece_config,
            fabric_width_mm=fabric_width_mm,
            piece_buffer_mm=piece_buffer_mm,
            edge_buffer_mm=edge_buffer_mm,
            time_limit=time_limit,
            rotation_mode=rotation_mode,
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
            'computation_time_s': solution_data['computation_time_s'],
            'svg_preview': svg_preview,
            'dxf_bytes': dxf_bytes,
        }
        results.append(result)

        if progress_callback:
            progress_callback(idx, total, result)

    return results
