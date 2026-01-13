"""
Streamlit App for Garment Nesting Engine

Run with:
    streamlit run app.py

Features:
- Upload DXF pattern files
- Size selector with quantity multipliers
- Piece type configuration with preview, demand, and flip options
- Bundle tracking (all pieces of one garment share color)
- Orientation modes: Free, Nap-Safe, Garment-Linked
- Run nesting optimization
- Display results with utilization metrics
- Export to DXF and SVG
"""

import sys
import os
from pathlib import Path
import re
import io
import base64
from dataclasses import dataclass, field
from typing import List, Optional, Tuple, Dict, Set
import tempfile
import time
import math
import colorsys

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent))

import streamlit as st
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
from matplotlib.collections import PatchCollection
import numpy as np

# Import our nesting engine
from nesting_engine.core import (
    Piece, PieceIdentifier, OrientationConstraint, GrainConstraint, GrainDirection,
    Container, NestingItem, NestingInstance, FlipMode,
    NestingSolution, PlacedPiece,
)
from nesting_engine.engine import SpyrrowEngine, SpyrrowConfig, check_spyrrow_available
from nesting_engine.io import DXFParser, load_pieces_from_dxf, DXFParseResult
from nesting_engine.io import (
    load_aama_pattern, grade_to_nesting_pieces, AAMAGrader,
    AAMAPiece, GradingRules
)


# Page configuration
st.set_page_config(
    page_title="Garment Nesting Engine",
    page_icon="✂️",
    layout="wide",
    initial_sidebar_state="expanded"
)

# Standard sizes in order
STANDARD_SIZES = ['XXS', 'XS', 'S', 'M', 'L', 'XL', 'XXL', '2XL', '3XL']

# Size aliases (map variations to standard)
SIZE_ALIASES = {
    'XXXS': 'XXS',
    'XXXXL': '3XL',
    'XXXL': '3XL',
}

# Orientation mode definitions
ORIENTATION_MODES = {
    "Free": {
        "desc": "Each piece rotates independently (0° or 180°) for best efficiency",
        "rotations": [0, 180],
        "linked": False
    },
    "Nap-Safe": {
        "desc": "All pieces face same direction (for napped/directional fabrics)",
        "rotations": [0],
        "linked": False
    },
    "Garment-Linked": {
        "desc": "Pieces of same garment rotate together (tries both orientations)",
        "rotations": [0, 180],
        "linked": True
    }
}


@dataclass
class BundlePiece:
    """A piece with bundle tracking information."""
    piece: Piece
    bundle_id: str      # e.g., "M_1", "M_2" for different garments of size M
    size: str
    piece_type: str
    is_flipped: bool = False
    instance_idx: int = 0


def normalize_size(size: str) -> str:
    """Normalize size string to standard format."""
    if not size:
        return ""
    size_upper = size.upper().strip()
    if '/T' in size_upper:
        return size_upper
    return SIZE_ALIASES.get(size_upper, size_upper)


def extract_piece_type(piece_name: str) -> str:
    """Extract piece type from name like '24-0391-P2-BKX1' -> 'BK'"""
    parts = piece_name.upper().split('-')
    if parts:
        last_part = parts[-1]
        # Extract letters, stop before X followed by number or just numbers
        match = re.match(r'^([A-Z]+?)(?:X[0-9]|[0-9]|_)', last_part)
        if match:
            return match.group(1)
        match = re.match(r'^([A-Z]+)', last_part)
        if match:
            code = match.group(1)
            if code.endswith('X') and len(code) > 2:
                return code[:-1]
            return code
    
    known_types = ['BK', 'FR', 'FRT', 'SL', 'NK', 'PK', 'WB', 'CF']
    for t in known_types:
        if t in piece_name.upper():
            return t
    
    return "OTHER"


def get_piece_type_full_name(piece_type: str) -> str:
    """Get full name for piece type code."""
    names = {
        'BK': 'Back', 'BKX': 'Back',
        'FR': 'Front', 'FRT': 'Front', 'FRX': 'Front',
        'SL': 'Sleeve', 'SLX': 'Sleeve',
        'NK': 'Neck', 'NKX': 'Neck',
        'PK': 'Pocket', 'PKX': 'Pocket',
        'WB': 'Waistband', 'WBX': 'Waistband',
        'CF': 'Cuff', 'CFX': 'Cuff',
        'HAT': 'Hood',
    }
    return names.get(piece_type, piece_type)


def create_piece_thumbnail(piece: Piece, size: Tuple[int, int] = (80, 80)) -> str:
    """Create a small thumbnail image of a piece and return as base64."""
    fig, ax = plt.subplots(figsize=(1.2, 1.2), dpi=80)
    
    verts = piece.vertices
    if verts[0] != verts[-1]:
        verts = verts + [verts[0]]
    
    xs = [v[0] for v in verts]
    ys = [v[1] for v in verts]
    
    ax.fill(xs, ys, alpha=0.4, color='steelblue')
    ax.plot(xs, ys, 'b-', linewidth=1)
    ax.set_aspect('equal')
    ax.axis('off')
    
    ax.set_xlim(min(xs) - 5, max(xs) + 5)
    ax.set_ylim(min(ys) - 5, max(ys) + 5)
    
    plt.tight_layout(pad=0.1)
    
    buf = io.BytesIO()
    fig.savefig(buf, format='png', bbox_inches='tight', pad_inches=0.05, 
                facecolor='white', edgecolor='none', dpi=80)
    plt.close(fig)
    buf.seek(0)
    
    img_base64 = base64.b64encode(buf.read()).decode()
    return f"data:image/png;base64,{img_base64}"


def group_pieces_by_type(pieces: List[Piece]) -> Dict[str, Dict[str, List[Piece]]]:
    """Group pieces by type and size. Returns: {piece_type: {size: [pieces]}}"""
    grouped = {}
    for p in pieces:
        ptype = extract_piece_type(p.name)
        size = p.identifier.size or ""
        if ptype not in grouped:
            grouped[ptype] = {}
        if size not in grouped[ptype]:
            grouped[ptype][size] = []
        grouped[ptype][size].append(p)
    return grouped


def get_representative_piece(pieces_by_size: Dict[str, List[Piece]], preferred_size: str = 'M') -> Optional[Piece]:
    """Get a representative piece, preferring the specified size."""
    if preferred_size in pieces_by_size and pieces_by_size[preferred_size]:
        return pieces_by_size[preferred_size][0]
    for size in ['M', 'L', 'S', 'XL', 'XS', 'XXL', 'XXS']:
        if size in pieces_by_size and pieces_by_size[size]:
            return pieces_by_size[size][0]
    for size, piece_list in pieces_by_size.items():
        if piece_list:
            return piece_list[0]
    return None


def generate_bundle_colors(num_bundles: int) -> List[Tuple[float, float, float, float]]:
    """Generate distinct colors for bundles using HSV color space."""
    colors = []
    for i in range(num_bundles):
        hue = (i * 0.618033988749895) % 1.0  # Golden ratio for good distribution
        saturation = 0.5 + (i % 3) * 0.15    # Vary saturation slightly
        value = 0.85 + (i % 2) * 0.1         # Vary brightness slightly
        r, g, b = colorsys.hsv_to_rgb(hue, saturation, value)
        colors.append((r, g, b, 1.0))
    return colors


def _create_piece_copy(piece: Piece, suffix: str) -> Piece:
    """Create a copy of a piece with a unique ID suffix."""
    new_id = PieceIdentifier(
        piece_name=piece.identifier.piece_name + suffix,
        style_name=piece.identifier.style_name,
        size=piece.identifier.size
    )
    return Piece(
        vertices=piece.vertices,
        identifier=new_id,
        orientation=piece.orientation,
        grain=piece.grain,
        fold_line=piece.fold_line
    )


def create_flipped_piece(piece: Piece) -> Piece:
    """Create a flipped version of a piece (mirrored along grain/Y direction)."""
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
        fold_line=piece.fold_line
    )


def build_bundle_pieces(
    grouped: Dict[str, Dict[str, List[Piece]]],
    piece_type_config: Dict,
    size_quantities: Dict
) -> List[BundlePiece]:
    """Build list of BundlePiece objects with proper bundle tracking."""
    bundle_pieces = []
    active_sizes = {size: qty for size, qty in size_quantities.items() if qty > 0}
    
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
                config = piece_type_config.get(ptype, {'demand': 1, 'flipped': False})
                demand = config.get('demand', 1)
                is_flipped_type = config.get('flipped', False)
                
                if is_flipped_type:
                    normal_count = demand // 2
                    flipped_count = demand // 2
                    
                    for i in range(normal_count):
                        unique_piece = _create_piece_copy(base_piece, f"_{bundle_id}_n{i}")
                        bundle_pieces.append(BundlePiece(
                            piece=unique_piece,
                            bundle_id=bundle_id,
                            size=size,
                            piece_type=ptype,
                            is_flipped=False,
                            instance_idx=i
                        ))
                    
                    for i in range(flipped_count):
                        flipped_piece = create_flipped_piece(base_piece)
                        unique_piece = _create_piece_copy(flipped_piece, f"_{bundle_id}_f{i}")
                        bundle_pieces.append(BundlePiece(
                            piece=unique_piece,
                            bundle_id=bundle_id,
                            size=size,
                            piece_type=ptype,
                            is_flipped=True,
                            instance_idx=i
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
                            instance_idx=i
                        ))
    
    return bundle_pieces


def plot_solution_with_bundles(
    solution: NestingSolution, 
    bundle_pieces: List[BundlePiece],
    show_labels: bool = True
) -> plt.Figure:
    """Create a matplotlib figure showing the nesting solution with bundle coloring."""
    
    # Create lookups
    piece_map = {bp.piece.id: bp for bp in bundle_pieces}
    
    # Get unique bundles and assign colors
    unique_bundles = sorted(set(bp.bundle_id for bp in bundle_pieces))
    bundle_colors = generate_bundle_colors(len(unique_bundles))
    color_map = dict(zip(unique_bundles, bundle_colors))
    
    # Figure size
    width_mm = solution.container_width
    length_mm = solution.strip_length
    aspect = length_mm / width_mm if width_mm > 0 else 1
    fig_width = min(18, max(12, aspect * 6))
    fig_height = 7
    
    fig, ax = plt.subplots(figsize=(fig_width, fig_height))
    
    # Container outline
    container_rect = plt.Rectangle(
        (0, 0), length_mm, width_mm,
        fill=False, edgecolor='black', linewidth=2
    )
    ax.add_patch(container_rect)
    
    # Draw pieces
    for placement in solution.placements:
        bp = piece_map.get(placement.piece_id)
        if bp is None:
            continue
        
        verts = list(bp.piece.vertices)
        is_flipped = bp.is_flipped or '_f' in placement.piece_id
        
        # Apply rotation
        if placement.rotation != 0:
            angle_rad = np.radians(placement.rotation)
            cos_a, sin_a = np.cos(angle_rad), np.sin(angle_rad)
            verts = [(x * cos_a - y * sin_a, x * sin_a + y * cos_a) for x, y in verts]
        
        # Translate
        verts = [(x + placement.x, y + placement.y) for x, y in verts]
        
        # Close polygon
        if verts[0] != verts[-1]:
            verts = verts + [verts[0]]
        
        xs = [v[0] for v in verts]
        ys = [v[1] for v in verts]
        
        # Color by bundle
        base_color = color_map.get(bp.bundle_id, (0.7, 0.7, 0.7, 1.0))
        alpha = 0.5 if is_flipped else 0.7
        edge_color = 'darkred' if is_flipped else 'black'
        edge_width = 0.8 if is_flipped else 0.5
        
        ax.fill(xs, ys, alpha=alpha, color=base_color[:3], edgecolor=edge_color, linewidth=edge_width)
        
        # Label: TYPE-SIZE
        if show_labels:
            cx = np.mean(xs[:-1])
            cy = np.mean(ys[:-1])
            label = f"{bp.piece_type}-{bp.size}" if bp.size else bp.piece_type
            if is_flipped:
                label += "[F]"
            
            # Font size based on piece area
            piece_area = abs(sum((xs[i] - xs[i-1]) * (ys[i] + ys[i-1]) / 2 for i in range(len(xs))))
            font_size = max(4, min(7, piece_area / 40000))
            
            ax.text(cx, cy, label, fontsize=font_size, ha='center', va='center',
                   fontweight='bold', color='black')
    
    ax.set_xlim(-10, length_mm + 10)
    ax.set_ylim(-10, width_mm + 10)
    ax.set_aspect('equal')
    ax.set_xlabel('Length (mm)', fontsize=10)
    ax.set_ylabel('Width (mm)', fontsize=10)
    
    title = f"Nesting Solution - {solution.utilization_percent:.1f}% Utilization"
    ax.set_title(title, fontsize=14, fontweight='bold')
    
    # Legend for bundles (limited)
    if len(unique_bundles) <= 16:
        legend_patches = []
        for bundle_id in unique_bundles[:12]:
            color = color_map[bundle_id]
            patch = mpatches.Patch(color=color[:3], alpha=0.7, label=bundle_id)
            legend_patches.append(patch)
        if legend_patches:
            ax.legend(handles=legend_patches, loc='upper right', fontsize=6, 
                     title="Bundles", title_fontsize=7, ncol=2)
    
    plt.tight_layout()
    return fig


def export_to_svg(solution: NestingSolution, bundle_pieces: List[BundlePiece]) -> str:
    """Export nesting solution to SVG format."""
    width_mm = solution.strip_length
    height_mm = solution.container_width
    
    piece_map = {bp.piece.id: bp for bp in bundle_pieces}
    unique_bundles = sorted(set(bp.bundle_id for bp in bundle_pieces))
    bundle_colors = generate_bundle_colors(len(unique_bundles))
    color_map = dict(zip(unique_bundles, bundle_colors))
    
    svg_parts = [
        f'<?xml version="1.0" encoding="UTF-8"?>',
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width_mm}mm" height="{height_mm}mm" viewBox="0 0 {width_mm} {height_mm}">',
        f'  <rect x="0" y="0" width="{width_mm}" height="{height_mm}" fill="white" stroke="black" stroke-width="1"/>',
    ]
    
    for placement in solution.placements:
        bp = piece_map.get(placement.piece_id)
        if bp is None:
            continue
        
        verts = list(bp.piece.vertices)
        is_flipped = bp.is_flipped
        
        if placement.rotation != 0:
            angle_rad = math.radians(placement.rotation)
            cos_a, sin_a = math.cos(angle_rad), math.sin(angle_rad)
            verts = [(x * cos_a - y * sin_a, x * sin_a + y * cos_a) for x, y in verts]
        
        verts = [(x + placement.x, y + placement.y) for x, y in verts]
        verts = [(x, height_mm - y) for x, y in verts]  # Flip Y for SVG
        
        points_str = " ".join(f"{x:.2f},{y:.2f}" for x, y in verts)
        
        color = color_map.get(bp.bundle_id, (0.7, 0.7, 0.7, 1.0))
        hex_color = "#{:02x}{:02x}{:02x}".format(
            int(color[0] * 255), int(color[1] * 255), int(color[2] * 255)
        )
        stroke_color = "#8B0000" if is_flipped else "#000000"
        
        svg_parts.append(
            f'  <polygon points="{points_str}" fill="{hex_color}" '
            f'fill-opacity="0.7" stroke="{stroke_color}" stroke-width="0.5"/>'
        )
        
        # Label
        cx = sum(v[0] for v in verts) / len(verts)
        cy = sum(v[1] for v in verts) / len(verts)
        label = f"{bp.piece_type}-{bp.size}"
        if is_flipped:
            label += "[F]"
        
        svg_parts.append(
            f'  <text x="{cx:.1f}" y="{cy:.1f}" font-size="6" font-weight="bold" '
            f'text-anchor="middle" dominant-baseline="middle">{label}</text>'
        )
    
    svg_parts.append('</svg>')
    return '\n'.join(svg_parts)


def export_to_dxf(solution: NestingSolution, bundle_pieces: List[BundlePiece]) -> str:
    """Export nesting solution to DXF format."""
    piece_map = {bp.piece.id: bp for bp in bundle_pieces}
    
    dxf_lines = [
        "0", "SECTION", "2", "HEADER",
        "9", "$INSUNITS", "70", "4",
        "0", "ENDSEC",
        "0", "SECTION", "2", "ENTITIES",
    ]
    
    for placement in solution.placements:
        bp = piece_map.get(placement.piece_id)
        if bp is None:
            continue
        
        verts = list(bp.piece.vertices)
        
        if placement.rotation != 0:
            angle_rad = math.radians(placement.rotation)
            cos_a, sin_a = math.cos(angle_rad), math.sin(angle_rad)
            verts = [(x * cos_a - y * sin_a, x * sin_a + y * cos_a) for x, y in verts]
        
        verts = [(x + placement.x, y + placement.y) for x, y in verts]
        
        layer_name = f"{bp.bundle_id}_{bp.piece_type}"
        
        dxf_lines.extend([
            "0", "LWPOLYLINE",
            "8", layer_name,
            "90", str(len(verts)),
            "70", "1",
        ])
        
        for x, y in verts:
            dxf_lines.extend(["10", f"{x:.6f}", "20", f"{y:.6f}"])
    
    dxf_lines.extend(["0", "ENDSEC", "0", "EOF"])
    return '\n'.join(dxf_lines)


def run_nesting(
    bundle_pieces: List[BundlePiece],
    fabric_width_mm: float,
    piece_buffer: float,
    edge_buffer: float,
    time_limit: int,
    allowed_rotations: List[int]
) -> NestingSolution:
    """Run the nesting solver."""
    nest_pieces = [bp.piece for bp in bundle_pieces]
    
    container = Container(width=fabric_width_mm, height=None)
    
    items = [
        NestingItem(piece=p, demand=1, flip_mode=FlipMode.NONE)
        for p in nest_pieces
    ]
    
    instance = NestingInstance.create(
        name="Streamlit Marker",
        container=container,
        items=items,
        piece_buffer=piece_buffer,
        edge_buffer=edge_buffer
    )
    
    engine = SpyrrowEngine()
    config = SpyrrowConfig(
        time_limit=time_limit,
        num_workers=None,
        seed=42
    )
    
    return engine.solve(instance, config=config)


def main():
    st.title("✂️ Garment Nesting Engine")
    st.markdown("*State-of-the-art 2D irregular nesting for garment manufacturing*")
    
    if not check_spyrrow_available():
        st.error("⚠️ Spyrrow engine not installed! Please run: `pip install spyrrow`")
        st.stop()
    
    # Sidebar
    st.sidebar.header("⚙️ Configuration")
    
    st.sidebar.subheader("Fabric")
    fabric_width_inches = st.sidebar.number_input(
        "Fabric Width (inches)", 
        min_value=10.0, max_value=200.0, value=60.0, step=1.0
    )
    fabric_width_mm = fabric_width_inches * 25.4
    
    st.sidebar.subheader("Nesting")
    time_limit = st.sidebar.slider("Solve Time (seconds)", 5, 120, 30, 5)
    piece_buffer = st.sidebar.number_input("Piece Gap (mm)", 0.0, 20.0, 2.0, 0.5)
    edge_buffer = st.sidebar.number_input("Edge Buffer (mm)", 0.0, 50.0, 5.0, 1.0)
    
    st.sidebar.subheader("Orientation")
    orientation_mode = st.sidebar.selectbox(
        "Rotation Mode",
        list(ORIENTATION_MODES.keys()),
        index=0
    )
    st.sidebar.caption(ORIENTATION_MODES[orientation_mode]["desc"])
    
    mode_config = ORIENTATION_MODES[orientation_mode]
    allowed_rotations = mode_config["rotations"]
    is_linked_mode = mode_config["linked"]
    
    # Session state
    if 'pieces' not in st.session_state:
        st.session_state.pieces = []
    if 'parse_result' not in st.session_state:
        st.session_state.parse_result = None
    if 'solution' not in st.session_state:
        st.session_state.solution = None
    if 'size_quantities' not in st.session_state:
        st.session_state.size_quantities = {s: 0 for s in STANDARD_SIZES}
    if 'piece_type_config' not in st.session_state:
        st.session_state.piece_type_config = {}
    if 'bundle_pieces' not in st.session_state:
        st.session_state.bundle_pieces = []
    if 'aama_grader' not in st.session_state:
        st.session_state.aama_grader = None
    if 'aama_available_sizes' not in st.session_state:
        st.session_state.aama_available_sizes = []
    
    # Tabs
    tab1, tab2, tab3 = st.tabs(["📁 Upload", "🔧 Configure", "📊 Results"])
    
    # ==================== TAB 1: UPLOAD ====================
    with tab1:
        st.header("Upload Pattern Files")

        # Pattern type selector
        pattern_type = st.radio(
            "Pattern Type",
            ["Standard DXF", "AAMA/ASTM Graded (DXF + RUL)"],
            horizontal=True,
            help="Standard DXF: Pre-sized patterns. AAMA: Base pattern + grading rules for size generation."
        )

        if pattern_type == "Standard DXF":
            # Original DXF upload flow
            uploaded_files = st.file_uploader(
                "Upload DXF pattern files",
                type=['dxf'],
                accept_multiple_files=True,
                key="standard_dxf"
            )

            if uploaded_files:
                all_pieces = []
                all_results = []

                for uploaded_file in uploaded_files:
                    with tempfile.NamedTemporaryFile(delete=False, suffix='.dxf') as tmp:
                        tmp.write(uploaded_file.getbuffer())
                        tmp_path = tmp.name

                    try:
                        pieces, result = load_pieces_from_dxf(
                            tmp_path,
                            rotations=allowed_rotations,
                            allow_flip=True
                        )
                        all_pieces.extend(pieces)
                        all_results.append(result)

                        with st.expander(f"📄 {uploaded_file.name}", expanded=True):
                            col1, col2, col3 = st.columns(3)
                            col1.metric("Pieces", len(pieces))
                            if result.unit:
                                col2.metric("Units", result.unit.value)
                            if result.marker_info and 'utilization_percent' in result.marker_info:
                                col3.metric("Original Utilization",
                                           f"{result.marker_info['utilization_percent']:.1f}%")
                            for err in result.errors:
                                st.error(err)
                            for warn in result.warnings:
                                st.warning(warn)
                    finally:
                        os.unlink(tmp_path)

                st.session_state.pieces = all_pieces
                st.session_state.parse_result = all_results[0] if all_results else None
                st.session_state.aama_grader = None  # Clear AAMA state
                st.session_state.aama_available_sizes = []

                grouped = group_pieces_by_type(all_pieces)
                for ptype in grouped.keys():
                    if ptype not in st.session_state.piece_type_config:
                        st.session_state.piece_type_config[ptype] = {'demand': 1, 'flipped': False}

                available_sizes = set(p.identifier.size for p in all_pieces if p.identifier.size)
                st.success(f"✅ Loaded {len(all_pieces)} pieces from {len(uploaded_files)} file(s)")
                st.info(f"📏 Detected sizes: {', '.join(sorted(available_sizes))}")

        else:
            # AAMA/ASTM Graded pattern flow
            st.markdown("**Upload AAMA/ASTM pattern files:**")
            st.caption("AAMA format: DXF contains base size pattern with grade points, RUL contains grading rules for generating all sizes.")

            col_dxf, col_rul = st.columns(2)

            with col_dxf:
                dxf_file = st.file_uploader(
                    "DXF file (base pattern)",
                    type=['dxf'],
                    key="aama_dxf"
                )

            with col_rul:
                rul_file = st.file_uploader(
                    "RUL file (grading rules)",
                    type=['rul'],
                    key="aama_rul"
                )

            if dxf_file and rul_file:
                # Save uploaded files temporarily
                with tempfile.NamedTemporaryFile(delete=False, suffix='.dxf') as tmp_dxf:
                    tmp_dxf.write(dxf_file.getbuffer())
                    dxf_path = tmp_dxf.name

                with tempfile.NamedTemporaryFile(delete=False, suffix='.rul') as tmp_rul:
                    tmp_rul.write(rul_file.getbuffer())
                    rul_path = tmp_rul.name

                try:
                    # Load AAMA pattern
                    aama_pieces, rules = load_aama_pattern(dxf_path, rul_path)
                    grader = AAMAGrader(aama_pieces, rules)

                    st.session_state.aama_grader = grader
                    st.session_state.aama_available_sizes = rules.header.size_list

                    # Show pattern info
                    with st.expander("📊 Pattern Information", expanded=True):
                        col1, col2, col3 = st.columns(3)
                        col1.metric("Pieces", len(aama_pieces))
                        col2.metric("Available Sizes", len(rules.header.size_list))
                        col3.metric("Sample Size", rules.header.sample_size)

                        st.markdown(f"**Size range:** {' → '.join(rules.header.size_list)}")
                        st.markdown(f"**Units:** {rules.header.units}")
                        st.markdown(f"**Grading Rules:** {rules.num_rules}")

                        # Show piece summary
                        st.markdown("---")
                        st.markdown("**Pieces in pattern:**")
                        piece_info = []
                        for p in aama_pieces[:10]:
                            piece_info.append({
                                "Name": p.name,
                                "Vertices": p.num_vertices,
                                "Grade Points": p.num_grade_points,
                                "Material": p.material or "-"
                            })
                        st.dataframe(piece_info, use_container_width=True)
                        if len(aama_pieces) > 10:
                            st.caption(f"... and {len(aama_pieces) - 10} more pieces")

                    # Size selection for grading
                    st.markdown("---")
                    st.subheader("🎯 Select Sizes to Generate")
                    st.caption("Choose which sizes to generate from the grading rules")

                    # All sizes selector
                    selected_sizes = st.multiselect(
                        "Select sizes",
                        options=rules.header.size_list,
                        default=[rules.header.sample_size],
                        key="aama_size_select"
                    )

                    # Quick buttons
                    btn_col1, btn_col2, btn_col3 = st.columns(3)
                    with btn_col1:
                        if st.button("Select All", use_container_width=True, key="aama_all"):
                            selected_sizes = rules.header.size_list
                            st.rerun()
                    with btn_col2:
                        if st.button("Select Sample Only", use_container_width=True, key="aama_sample"):
                            selected_sizes = [rules.header.sample_size]
                            st.rerun()
                    with btn_col3:
                        if st.button("Clear", use_container_width=True, key="aama_clear"):
                            selected_sizes = []
                            st.rerun()

                    if selected_sizes:
                        # Generate pieces for selected sizes
                        if st.button("🔄 Generate Pieces", type="primary", use_container_width=True):
                            with st.spinner(f"Generating pieces for {len(selected_sizes)} sizes..."):
                                nesting_pieces = grade_to_nesting_pieces(
                                    dxf_path,
                                    rul_path,
                                    target_sizes=selected_sizes,
                                    rotations=allowed_rotations,
                                    allow_flip=True
                                )

                            st.session_state.pieces = nesting_pieces
                            st.session_state.parse_result = None

                            # Configure piece types
                            grouped = group_pieces_by_type(nesting_pieces)
                            for ptype in grouped.keys():
                                if ptype not in st.session_state.piece_type_config:
                                    st.session_state.piece_type_config[ptype] = {'demand': 1, 'flipped': False}

                            # Update size quantities to 1 for generated sizes
                            for size in selected_sizes:
                                st.session_state.size_quantities[size] = 1

                            st.success(f"✅ Generated {len(nesting_pieces)} pieces for sizes: {', '.join(selected_sizes)}")
                            st.info("👉 Go to the **Configure** tab to set quantities and run nesting")
                    else:
                        st.warning("⚠️ Please select at least one size to generate")

                except Exception as e:
                    st.error(f"❌ Error loading AAMA files: {str(e)}")
                    import traceback
                    with st.expander("Error details"):
                        st.code(traceback.format_exc())
                finally:
                    os.unlink(dxf_path)
                    os.unlink(rul_path)

            elif dxf_file or rul_file:
                st.info("📁 Please upload both DXF and RUL files")
    
    # ==================== TAB 2: CONFIGURE ====================
    with tab2:
        st.header("Configure Nesting")
        
        pieces = st.session_state.pieces
        
        if not pieces:
            st.info("👆 Upload DXF files in the Upload tab first")
        else:
            grouped = group_pieces_by_type(pieces)
            
            available_sizes_set = set()
            for ptype, pieces_by_size in grouped.items():
                for size in pieces_by_size.keys():
                    if size:
                        available_sizes_set.add(size)
            
            # Size selector
            st.subheader("📏 Size Selector")
            st.caption("Set quantity (number of garments) for each size")
            
            display_sizes = [s for s in STANDARD_SIZES if s in available_sizes_set]
            extra_sizes = sorted(available_sizes_set - set(STANDARD_SIZES))
            display_sizes.extend(extra_sizes)
            
            if display_sizes:
                num_cols = min(len(display_sizes), 9)
                size_cols = st.columns(num_cols)
                
                for idx, size in enumerate(display_sizes[:num_cols]):
                    with size_cols[idx]:
                        st.markdown(f"**{size}**")
                        qty = st.number_input(
                            f"qty_{size}", min_value=0, max_value=100,
                            value=st.session_state.size_quantities.get(size, 0),
                            key=f"size_qty_{size}", label_visibility="collapsed"
                        )
                        st.session_state.size_quantities[size] = qty
                
                if len(display_sizes) > num_cols:
                    size_cols2 = st.columns(min(len(display_sizes) - num_cols, 9))
                    for idx, size in enumerate(display_sizes[num_cols:]):
                        with size_cols2[idx]:
                            st.markdown(f"**{size}**")
                            qty = st.number_input(
                                f"qty_{size}", min_value=0, max_value=100,
                                value=st.session_state.size_quantities.get(size, 0),
                                key=f"size_qty_{size}", label_visibility="collapsed"
                            )
                            st.session_state.size_quantities[size] = qty
            
            # Presets
            st.caption("Quick presets:")
            p1, p2, p3, p4 = st.columns(4)
            with p1:
                if st.button("All = 1", use_container_width=True):
                    for s in display_sizes:
                        st.session_state.size_quantities[s] = 1
                    st.rerun()
            with p2:
                if st.button("All = 0", use_container_width=True):
                    for s in display_sizes:
                        st.session_state.size_quantities[s] = 0
                    st.rerun()
            with p3:
                if st.button("S-XL = 1", use_container_width=True):
                    for s in display_sizes:
                        st.session_state.size_quantities[s] = 1 if s in ['S', 'M', 'L', 'XL'] else 0
                    st.rerun()
            with p4:
                if st.button("Size Run", use_container_width=True):
                    preset = {'XXS': 1, 'XS': 2, 'S': 3, 'M': 3, 'L': 3, 'XL': 2, 'XXL': 1}
                    for s in display_sizes:
                        st.session_state.size_quantities[s] = preset.get(s, 0)
                    st.rerun()
            
            st.markdown("---")
            
            # Piece type config
            st.subheader("🧩 Piece Configuration")
            st.caption("Configure demand (pieces per garment) and flip option")
            
            header_cols = st.columns([1.2, 1.8, 1, 1, 2])
            header_cols[0].markdown("**Piece Type**")
            header_cols[1].markdown("**Preview**")
            header_cols[2].markdown("**Demand**")
            header_cols[3].markdown("**Flipped**")
            header_cols[4].markdown("**Dimensions**")
            st.markdown("---")
            
            for ptype in sorted(grouped.keys()):
                pieces_by_size = grouped[ptype]
                rep_piece = get_representative_piece(pieces_by_size, 'M')
                if rep_piece is None:
                    continue
                
                if ptype not in st.session_state.piece_type_config:
                    st.session_state.piece_type_config[ptype] = {'demand': 1, 'flipped': False}
                config = st.session_state.piece_type_config[ptype]
                
                cols = st.columns([1.2, 1.8, 1, 1, 2])
                
                with cols[0]:
                    st.markdown(f"**{ptype}**")
                    st.caption(get_piece_type_full_name(ptype))
                
                with cols[1]:
                    try:
                        img_data = create_piece_thumbnail(rep_piece)
                        st.markdown(f'<img src="{img_data}" style="max-width:70px; max-height:70px;">', 
                                   unsafe_allow_html=True)
                    except:
                        st.caption("N/A")
                
                with cols[2]:
                    is_flipped = config.get('flipped', False)
                    min_d, step = (2, 2) if is_flipped else (1, 1)
                    current_d = config.get('demand', 1)
                    if is_flipped and current_d % 2 != 0:
                        current_d = max(2, current_d + 1)
                    new_d = st.number_input(f"d_{ptype}", min_value=min_d, max_value=100, 
                                           value=current_d, step=step, key=f"demand_{ptype}",
                                           label_visibility="collapsed")
                    st.session_state.piece_type_config[ptype]['demand'] = new_d
                
                with cols[3]:
                    new_f = st.checkbox(f"f_{ptype}", value=config.get('flipped', False),
                                       key=f"flipped_{ptype}", label_visibility="collapsed")
                    if new_f != config.get('flipped', False):
                        st.session_state.piece_type_config[ptype]['flipped'] = new_f
                        if new_f and st.session_state.piece_type_config[ptype]['demand'] % 2 != 0:
                            st.session_state.piece_type_config[ptype]['demand'] = max(2, st.session_state.piece_type_config[ptype]['demand'] + 1)
                        st.rerun()
                
                with cols[4]:
                    st.text(f"{rep_piece.width:.1f} × {rep_piece.height:.1f} mm")
                    st.caption(f"Area: {rep_piece.area/100:.0f} cm²")
                
                st.markdown("<hr style='margin: 5px 0;'>", unsafe_allow_html=True)
            
            # Summary
            st.markdown("---")
            st.subheader("📊 Summary")
            
            bundle_pieces = build_bundle_pieces(
                grouped, st.session_state.piece_type_config, st.session_state.size_quantities
            )
            
            num_bundles = len(set(bp.bundle_id for bp in bundle_pieces))
            total_area = sum(bp.piece.area for bp in bundle_pieces)
            
            s1, s2, s3, s4 = st.columns(4)
            s1.metric("Total Pieces", len(bundle_pieces))
            s2.metric("Garments", num_bundles)
            s3.metric("Total Area", f"{total_area/1000000:.2f} m²")
            if total_area > 0:
                s4.metric("Min Length", f"{(total_area / fabric_width_mm)/1000:.2f} m")
            else:
                s4.metric("Min Length", "-")
    
    # ==================== TAB 3: RESULTS ====================
    with tab3:
        st.header("Nesting Results")
        
        pieces = st.session_state.pieces
        
        if not pieces:
            st.info("👆 Upload DXF files in the Upload tab first")
        else:
            grouped = group_pieces_by_type(pieces)
            col1, col2 = st.columns([1, 2])
            
            with col1:
                st.subheader("Run Nesting")
                
                bundle_pieces = build_bundle_pieces(
                    grouped, st.session_state.piece_type_config, st.session_state.size_quantities
                )
                
                num_bundles = len(set(bp.bundle_id for bp in bundle_pieces))
                flipped_count = sum(1 for bp in bundle_pieces if bp.is_flipped)
                
                st.markdown(f"**Pieces:** {len(bundle_pieces)}")
                st.markdown(f"**Garments:** {num_bundles}")
                if flipped_count > 0:
                    st.caption(f"Including {flipped_count} flipped")
                st.markdown(f"**Mode:** {orientation_mode}")
                
                st.markdown("---")
                
                if not bundle_pieces:
                    st.warning("⚠️ No pieces! Set size quantities > 0.")
                else:
                    if st.button("🚀 Run Nesting", type="primary", use_container_width=True):
                        
                        if is_linked_mode:
                            # Garment-Linked: try both 0° and 180°, pick best
                            with st.spinner(f"Nesting with Garment-Linked mode (trying both orientations)..."):
                                start_time = time.time()
                                
                                # Try 0° only
                                sol_0 = run_nesting(bundle_pieces, fabric_width_mm, piece_buffer, 
                                                   edge_buffer, time_limit // 2, [0])
                                
                                # Try 180° only
                                sol_180 = run_nesting(bundle_pieces, fabric_width_mm, piece_buffer, 
                                                     edge_buffer, time_limit // 2, [180])
                                
                                # Pick better solution
                                if sol_0.utilization_percent >= sol_180.utilization_percent:
                                    solution = sol_0
                                    chosen = "0°"
                                else:
                                    solution = sol_180
                                    chosen = "180°"
                                
                                elapsed = time.time() - start_time
                                
                            st.success(f"✅ Complete in {elapsed:.1f}s (Best: {chosen})")
                        else:
                            with st.spinner(f"Nesting {len(bundle_pieces)} pieces..."):
                                start_time = time.time()
                                solution = run_nesting(bundle_pieces, fabric_width_mm, piece_buffer, 
                                                      edge_buffer, time_limit, allowed_rotations)
                                elapsed = time.time() - start_time
                            st.success(f"✅ Complete in {elapsed:.1f}s")
                        
                        st.session_state.solution = solution
                        st.session_state.bundle_pieces = bundle_pieces
                        st.rerun()
            
            with col2:
                solution = st.session_state.solution
                bundle_pieces = st.session_state.bundle_pieces
                
                if solution and bundle_pieces:
                    st.subheader("Results")
                    
                    m1, m2, m3, m4 = st.columns(4)
                    m1.metric("Utilization", f"{solution.utilization_percent:.1f}%")
                    m2.metric("Waste", f"{solution.waste_percent:.1f}%")
                    m3.metric("Strip Length", f"{solution.strip_length/1000:.2f} m")
                    m4.metric("Placements", solution.num_placements)
                    
                    # Compare with original
                    if st.session_state.parse_result and st.session_state.parse_result.marker_info:
                        orig_util = st.session_state.parse_result.marker_info.get('utilization_percent')
                        if orig_util:
                            delta = solution.utilization_percent - orig_util
                            if delta > 0:
                                st.success(f"📈 {delta:.1f}% better than original ({orig_util:.1f}%)")
                            elif delta < 0:
                                st.warning(f"📉 {abs(delta):.1f}% worse than original ({orig_util:.1f}%)")
                    
                    num_bundles = len(set(bp.bundle_id for bp in bundle_pieces))
                    flipped_count = sum(1 for bp in bundle_pieces if bp.is_flipped)
                    st.info(f"🎯 {num_bundles} garments" + (f" | 🔄 {flipped_count} flipped" if flipped_count else ""))
                    
                    # Visualization
                    st.subheader("Nesting Layout")
                    show_labels = st.checkbox("Show labels (Type-Size)", value=True)
                    
                    fig = plot_solution_with_bundles(solution, bundle_pieces, show_labels)
                    st.pyplot(fig)

                    # Save PNG before closing figure
                    png_buf = io.BytesIO()
                    fig.savefig(png_buf, format='png', dpi=150, bbox_inches='tight',
                               facecolor='white', edgecolor='none')
                    png_buf.seek(0)
                    plt.close()

                    # Export
                    st.subheader("📥 Export")
                    exp1, exp2, exp3 = st.columns(3)

                    with exp1:
                        svg_content = export_to_svg(solution, bundle_pieces)
                        st.download_button("⬇️ Download SVG", data=svg_content,
                                          file_name="nesting_result.svg", mime="image/svg+xml",
                                          use_container_width=True)

                    with exp2:
                        dxf_content = export_to_dxf(solution, bundle_pieces)
                        st.download_button("⬇️ Download DXF", data=dxf_content,
                                          file_name="nesting_result.dxf", mime="application/dxf",
                                          use_container_width=True)

                    with exp3:
                        # PNG Export with size ratio in filename
                        size_parts = []
                        for size in STANDARD_SIZES:
                            qty = st.session_state.size_quantities.get(size, 0)
                            if qty > 0:
                                size_parts.append(f"{size}{qty}")
                        ratio_str = "_".join(size_parts) if size_parts else "marker"
                        png_filename = f"nest_{ratio_str}_{solution.utilization_percent:.0f}pct.png"

                        st.download_button("📷 Download PNG", data=png_buf.getvalue(),
                                          file_name=png_filename, mime="image/png",
                                          use_container_width=True)
                    
                    # Placement details
                    with st.expander("📋 Placement Details"):
                        piece_map = {bp.piece.id: bp for bp in bundle_pieces}
                        placement_data = []
                        for p in solution.placements:
                            bp = piece_map.get(p.piece_id)
                            placement_data.append({
                                "Bundle": bp.bundle_id if bp else "-",
                                "Type": bp.piece_type if bp else "-",
                                "Size": bp.size if bp else "-",
                                "X (mm)": f"{p.x:.1f}",
                                "Y (mm)": f"{p.y:.1f}",
                                "Rotation": f"{p.rotation}°",
                                "Flipped": "✓" if (bp and bp.is_flipped) else ""
                            })
                        st.dataframe(placement_data, use_container_width=True)
                else:
                    st.info("👈 Configure pieces, then click 'Run Nesting'")


if __name__ == "__main__":
    main()
