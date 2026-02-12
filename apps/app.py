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
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
import multiprocessing

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
    AAMAPiece, GradingRules, get_pieces_by_material, get_available_materials,
    grade_material_to_nesting_pieces, generate_nesting_queue, LRType
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


def group_pieces_by_name(pieces: List[Piece]) -> Dict[str, Dict[str, List[Piece]]]:
    """Group pieces by actual piece name and size. Returns: {piece_name: {size: [pieces]}}

    This is better for AAMA patterns where each piece has a unique name like
    FRONT, BACK, SLEEVE, etc. rather than type codes like BK, FR, SL.
    """
    grouped = {}
    for p in pieces:
        # Use the piece name directly (strip size suffix if present)
        piece_name = p.identifier.piece_name or p.name
        # Remove size suffix if the name ends with it
        size = p.identifier.size or ""
        if size and piece_name.endswith(f"-{size}"):
            piece_name = piece_name[:-len(size)-1]

        if piece_name not in grouped:
            grouped[piece_name] = {}
        if size not in grouped[piece_name]:
            grouped[piece_name][size] = []
        grouped[piece_name][size].append(p)
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
                    # When flip is checked, create 'demand' normal + 'demand' flipped pieces
                    # This matches L/R behavior: L*1-R*1 means 1 left + 1 right
                    normal_count = demand
                    flipped_count = demand

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


def mm_to_yards(mm: float) -> float:
    """Convert millimeters to yards."""
    return mm / 914.4  # 1 yard = 914.4 mm


@dataclass
class MarkerResult:
    """Result of a single marker nesting."""
    name: str
    ratio_str: str
    size_quantities: Dict[str, int]
    solution: Optional[NestingSolution]
    bundle_pieces: List[BundlePiece]
    elapsed_time: float
    error: Optional[str] = None

    @property
    def length_mm(self) -> float:
        return self.solution.strip_length if self.solution else 0

    @property
    def length_yards(self) -> float:
        return mm_to_yards(self.length_mm)

    @property
    def utilization(self) -> float:
        return self.solution.utilization_percent if self.solution else 0


def run_single_marker(
    marker_name: str,
    size_quantities: Dict[str, int],
    grouped: Dict[str, Dict[str, List[Piece]]],
    piece_type_config: Dict,
    fabric_width_mm: float,
    piece_buffer: float,
    edge_buffer: float,
    time_limit: int,
    allowed_rotations: List[int]
) -> MarkerResult:
    """Run nesting for a single marker configuration."""
    # Create ratio string
    all_sizes = sorted(size_quantities.keys())
    ratio_str = "-".join(str(size_quantities.get(s, 0)) for s in all_sizes)

    start_time = time.time()
    error = None
    solution = None

    try:
        # Build bundle pieces for this marker
        bundle_pieces = build_bundle_pieces(grouped, piece_type_config, size_quantities)

        if not bundle_pieces:
            error = "No pieces generated"
        else:
            solution = run_nesting(
                bundle_pieces, fabric_width_mm, piece_buffer,
                edge_buffer, time_limit, allowed_rotations
            )
    except Exception as e:
        error = str(e)
        bundle_pieces = []

    elapsed = time.time() - start_time

    return MarkerResult(
        name=marker_name,
        ratio_str=ratio_str,
        size_quantities=size_quantities.copy(),
        solution=solution,
        bundle_pieces=bundle_pieces,
        elapsed_time=elapsed,
        error=error
    )


def run_marker_queue(
    markers: List[Tuple[str, Dict[str, int]]],  # List of (name, size_quantities)
    grouped: Dict[str, Dict[str, List[Piece]]],
    piece_type_config: Dict,
    fabric_width_mm: float,
    piece_buffer: float,
    edge_buffer: float,
    time_limit: int,
    allowed_rotations: List[int],
    progress_callback=None
) -> List[MarkerResult]:
    """
    Run nesting for multiple markers sequentially.
    Returns results as they complete.
    """
    results = []

    for i, (marker_name, size_quantities) in enumerate(markers):
        if progress_callback:
            progress_callback(i, len(markers), marker_name)

        result = run_single_marker(
            marker_name, size_quantities, grouped, piece_type_config,
            fabric_width_mm, piece_buffer, edge_buffer, time_limit, allowed_rotations
        )
        results.append(result)

    return results


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
    if 'aama_pieces' not in st.session_state:
        st.session_state.aama_pieces = []  # Store AAMAPiece objects for L/R detection
    if 'marker_queue' not in st.session_state:
        st.session_state.marker_queue = []  # List of (name, size_quantities) tuples
    if 'marker_results' not in st.session_state:
        st.session_state.marker_results = []  # List of MarkerResult objects
    if 'selected_result_idx' not in st.session_state:
        st.session_state.selected_result_idx = 0

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
                st.session_state.aama_pieces = []  # Clear AAMA pieces

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
                    st.session_state.aama_pieces = aama_pieces  # Store for L/R detection

                    # Get available materials
                    available_materials = get_available_materials(aama_pieces)
                    pieces_by_material = get_pieces_by_material(aama_pieces)

                    # Show pattern info
                    with st.expander("📊 Pattern Information", expanded=True):
                        col1, col2, col3, col4 = st.columns(4)
                        col1.metric("Total Pieces", len(aama_pieces))
                        col2.metric("Available Sizes", len(rules.header.size_list))
                        col3.metric("Sample Size", rules.header.sample_size)
                        col4.metric("Materials", len(available_materials))

                        st.markdown(f"**Size range:** {' → '.join(rules.header.size_list)}")
                        st.markdown(f"**Units:** {rules.header.units}")
                        st.markdown(f"**Grading Rules:** {rules.num_rules}")

                        # Show materials breakdown
                        if len(available_materials) > 1:
                            st.markdown("---")
                            st.markdown("**Pieces by Material:**")
                            material_cols = st.columns(min(len(available_materials), 4))
                            for idx, mat in enumerate(available_materials[:4]):
                                with material_cols[idx]:
                                    count = len(pieces_by_material.get(mat, []))
                                    st.metric(mat, f"{count} pieces")

                        # Show piece summary with L/R detection
                        st.markdown("---")
                        st.markdown("**Pieces in pattern:**")
                        piece_info = []
                        for p in aama_pieces[:10]:
                            lr_display = {
                                LRType.NONE: "Center",
                                LRType.SEPARATE_LEFT: "Left",
                                LRType.SEPARATE_RIGHT: "Right",
                                LRType.FLIP_FOR_LR: "Flip L/R"
                            }.get(p.lr_type, "-")
                            piece_info.append({
                                "Name": p.display_name,
                                "Vertices": p.num_vertices,
                                "Grade Points": p.num_grade_points,
                                "Material": p.material or "-",
                                "L/R Type": lr_display
                            })
                        st.dataframe(piece_info, use_container_width=True)
                        if len(aama_pieces) > 10:
                            st.caption(f"... and {len(aama_pieces) - 10} more pieces")

                    # Material selection (if multiple materials)
                    st.markdown("---")
                    if len(available_materials) > 1:
                        st.subheader("🧵 Select Material")
                        st.caption("Different materials are cut separately on different fabric rolls")

                        # Material selector with piece counts
                        material_options = [f"{mat} ({len(pieces_by_material.get(mat, []))} pieces)"
                                           for mat in available_materials]
                        selected_material_idx = st.selectbox(
                            "Material to nest",
                            range(len(available_materials)),
                            format_func=lambda x: material_options[x],
                            key="aama_material_select"
                        )
                        selected_material = available_materials[selected_material_idx]

                        st.info(f"📌 Will generate pieces for **{selected_material}** material only. "
                               "Run nesting separately for other materials.")
                    else:
                        selected_material = available_materials[0] if available_materials else None
                        if selected_material and selected_material != "UNKNOWN":
                            st.info(f"📌 All pieces are **{selected_material}** material")

                    # Show nesting queue for selected material
                    if selected_material:
                        queue = generate_nesting_queue(aama_pieces, material_filter=selected_material)
                        if queue:
                            with st.expander("📋 Nesting Queue", expanded=False):
                                st.caption("Preview of how pieces will be nested with L/R handling")
                                queue_data = []
                                for item in queue:
                                    lr_display = {
                                        LRType.NONE: "Center",
                                        LRType.SEPARATE_LEFT: "Left",
                                        LRType.SEPARATE_RIGHT: "Right",
                                        LRType.FLIP_FOR_LR: "Flip L/R"
                                    }.get(item.piece.lr_type, "-")
                                    queue_data.append({
                                        "Piece": item.display_name,
                                        "Material": item.material,
                                        "Qty": item.quantity,
                                        "Flip": "✅" if item.flip else "",
                                        "L/R Type": lr_display
                                    })
                                st.dataframe(queue_data, use_container_width=True)

                                # Summary stats
                                total_pieces = sum(item.quantity for item in queue)
                                flip_pieces = sum(item.quantity for item in queue if item.flip)
                                st.caption(f"Total: {total_pieces} pieces | Flip required: {flip_pieces} pieces")

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
                        generate_label = f"🔄 Generate {selected_material} Pieces" if len(available_materials) > 1 else "🔄 Generate Pieces"
                        if st.button(generate_label, type="primary", use_container_width=True):
                            with st.spinner(f"Generating {selected_material} pieces for {len(selected_sizes)} sizes..."):
                                if len(available_materials) > 1 and selected_material:
                                    # Use material-filtered function
                                    nesting_pieces = grade_material_to_nesting_pieces(
                                        dxf_path,
                                        rul_path,
                                        material=selected_material,
                                        target_sizes=selected_sizes,
                                        rotations=allowed_rotations,
                                        allow_flip=True
                                    )
                                else:
                                    # Use regular function (all materials)
                                    nesting_pieces = grade_to_nesting_pieces(
                                        dxf_path,
                                        rul_path,
                                        target_sizes=selected_sizes,
                                        rotations=allowed_rotations,
                                        allow_flip=True
                                    )

                            st.session_state.pieces = nesting_pieces
                            st.session_state.parse_result = None

                            # Configure piece types with L/R auto-flip detection
                            # Use group_pieces_by_name for AAMA (same as Configure tab)
                            grouped = group_pieces_by_name(nesting_pieces)

                            # Build lookup of aama_pieces by name for L/R detection
                            aama_pieces_lookup = {}
                            for ap in st.session_state.aama_pieces:
                                aama_pieces_lookup[ap.name] = ap

                            for piece_name in grouped.keys():
                                if piece_name not in st.session_state.piece_type_config:
                                    # Check if this piece has L/R annotation
                                    aama_piece = aama_pieces_lookup.get(piece_name)
                                    if aama_piece and aama_piece.quantity.has_left_right:
                                        # L/R piece: set demand to left_qty, auto-check flip
                                        demand = aama_piece.quantity.left_qty
                                        flipped = True
                                    else:
                                        # Regular piece: use total, no flip
                                        demand = aama_piece.quantity.total if aama_piece else 1
                                        flipped = False
                                    st.session_state.piece_type_config[piece_name] = {
                                        'demand': demand,
                                        'flipped': flipped
                                    }

                            # Update size quantities to 1 for generated sizes
                            for size in selected_sizes:
                                st.session_state.size_quantities[size] = 1

                            material_note = f" ({selected_material})" if len(available_materials) > 1 else ""
                            st.success(f"✅ Generated {len(nesting_pieces)} pieces{material_note} for sizes: {', '.join(selected_sizes)}")
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
            # Use group_pieces_by_name for AAMA patterns (shows all pieces individually)
            # Use group_pieces_by_type for standard DXF (groups by type code)
            is_aama = st.session_state.aama_grader is not None
            grouped = group_pieces_by_name(pieces) if is_aama else group_pieces_by_type(pieces)
            
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

            # === ADD MARKER SECTION (right after size selector) ===
            st.markdown("---")
            st.subheader("➕ Add Marker to Queue")

            # Current ratio display
            current_ratio = "-".join(str(st.session_state.size_quantities.get(s, 0)) for s in display_sizes)
            current_bundles = sum(st.session_state.size_quantities.get(s, 0) for s in display_sizes)

            # Build bundle pieces for piece count
            bundle_pieces = build_bundle_pieces(
                grouped, st.session_state.piece_type_config, st.session_state.size_quantities
            )

            add_col1, add_col2 = st.columns([3, 1])
            with add_col1:
                st.markdown(f"**Current:** `{current_ratio}` ({current_bundles} bundles, {len(bundle_pieces)} pieces)")
            with add_col2:
                if current_bundles > 0:
                    if st.button("➕ Add to Queue", use_container_width=True, type="primary"):
                        marker_name = f"M{len(st.session_state.marker_queue) + 1}"
                        st.session_state.marker_queue.append(
                            (marker_name, {s: st.session_state.size_quantities.get(s, 0) for s in display_sizes})
                        )
                        st.rerun()
                else:
                    st.button("➕ Add to Queue", use_container_width=True, disabled=True)

            # Bulk add section
            sizes_header = "-".join(display_sizes)
            st.caption(f"Or paste multiple ratios (format: `{sizes_header}`, one per line):")
            ratio_text = st.text_area(
                "Bulk ratios",
                placeholder="-".join(["0"] * len(display_sizes)) + "\n" + "-".join(["1"] * len(display_sizes)),
                height=80,
                key="bulk_add_ratios",
                label_visibility="collapsed"
            )
            if st.button("➕ Add All Ratios", use_container_width=True):
                lines = [l.strip() for l in ratio_text.strip().split('\n') if l.strip()]
                added = 0
                for line in lines:
                    parts = line.replace(' ', '').split('-')
                    if len(parts) == len(display_sizes):
                        try:
                            sq = {s: int(parts[i]) for i, s in enumerate(display_sizes)}
                            if sum(sq.values()) > 0:
                                marker_name = f"M{len(st.session_state.marker_queue) + 1}"
                                st.session_state.marker_queue.append((marker_name, sq))
                                added += 1
                        except ValueError:
                            pass
                if added > 0:
                    st.success(f"Added {added} marker(s)")
                    st.rerun()
                elif ratio_text.strip():
                    st.warning(f"No valid ratios found. Expected {len(display_sizes)} values per line (e.g., `{sizes_header}`)")

            # === QUEUE DISPLAY ===
            st.markdown("---")
            st.subheader("📋 Marker Queue")
            if st.session_state.marker_queue:
                st.markdown(f"**{len(st.session_state.marker_queue)} markers queued:**")
                queue_df_data = []
                for i, (name, sq) in enumerate(st.session_state.marker_queue):
                    ratio = "-".join(str(sq.get(s, 0)) for s in display_sizes)
                    total_b = sum(sq.values())
                    queue_df_data.append({
                        "#": i + 1,
                        "Ratio": ratio,
                        "Bundles": total_b
                    })
                st.dataframe(queue_df_data, use_container_width=True, hide_index=True,
                            height=min(250, 35 * len(queue_df_data) + 38))

                col_clear, col_go = st.columns(2)
                with col_clear:
                    if st.button("🗑️ Clear Queue", use_container_width=True):
                        st.session_state.marker_queue = []
                        st.rerun()
                with col_go:
                    st.info(f"👉 **Results** tab to run")
            else:
                st.info("Queue empty. Set sizes above and click 'Add to Queue'")

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
                    # Allow 0 demand (to exclude pieces from nesting)
                    # Demand represents count per side:
                    # - Non-flipped: demand pieces total
                    # - Flipped: demand normal + demand flipped = 2×demand pieces total
                    min_d = 0
                    step = 1
                    current_d = config.get('demand', 1)
                    new_d = st.number_input(f"d_{ptype}", min_value=min_d, max_value=100,
                                           value=current_d, step=step, key=f"demand_{ptype}",
                                           label_visibility="collapsed")
                    st.session_state.piece_type_config[ptype]['demand'] = new_d
                
                with cols[3]:
                    new_f = st.checkbox(f"f_{ptype}", value=config.get('flipped', False),
                                       key=f"flipped_{ptype}", label_visibility="collapsed")
                    if new_f != config.get('flipped', False):
                        st.session_state.piece_type_config[ptype]['flipped'] = new_f
                        st.rerun()
                
                with cols[4]:
                    st.text(f"{rep_piece.width:.1f} × {rep_piece.height:.1f} mm")
                    st.caption(f"Area: {rep_piece.area/100:.0f} cm²")
                
                st.markdown("<hr style='margin: 5px 0;'>", unsafe_allow_html=True)
            
            # Summary
            st.markdown("---")
            st.subheader("📊 Summary")

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
            # Use same grouping as Configure tab
            is_aama = st.session_state.aama_grader is not None
            grouped = group_pieces_by_name(pieces) if is_aama else group_pieces_by_type(pieces)

            # Get available sizes
            available_sizes_set = set()
            for ptype, pieces_by_size in grouped.items():
                for size in pieces_by_size.keys():
                    if size:
                        available_sizes_set.add(size)
            display_sizes = [s for s in STANDARD_SIZES if s in available_sizes_set]
            extra_sizes = sorted(available_sizes_set - set(STANDARD_SIZES))
            display_sizes.extend(extra_sizes)

            # === QUEUE PREVIEW & RUN SECTION ===
            col_queue, col_params = st.columns([2, 1])

            with col_queue:
                st.subheader("📋 Marker Queue")
                if st.session_state.marker_queue:
                    st.markdown(f"**{len(st.session_state.marker_queue)} markers ready to nest:**")
                    queue_df_data = []
                    for i, (name, sq) in enumerate(st.session_state.marker_queue):
                        ratio = "-".join(str(sq.get(s, 0)) for s in display_sizes)
                        total_b = sum(sq.values())
                        queue_df_data.append({
                            "#": i + 1,
                            "Ratio": ratio,
                            "Bundles": total_b
                        })
                    st.dataframe(queue_df_data, use_container_width=True, hide_index=True,
                                height=min(300, 35 * len(queue_df_data) + 38))
                else:
                    st.warning("No markers in queue. Go to **Configure** tab to add markers.")

            with col_params:
                st.subheader("⚙️ Nesting Params")
                st.markdown(f"**Fabric Width:** {fabric_width_inches}\" ({fabric_width_mm:.0f}mm)")
                st.markdown(f"**Piece Gap:** {piece_buffer}mm")
                st.markdown(f"**Edge Buffer:** {edge_buffer}mm")
                st.markdown(f"**Rotation Mode:** {orientation_mode}")
                st.markdown(f"**Time/Marker:** {time_limit}s")

            st.markdown("---")

            # === RUN BUTTON ===
            if st.session_state.marker_queue:
                # Count unique ratios for display
                unique_ratios = set("-".join(str(sq.get(s, 0)) for s in display_sizes)
                                   for _, sq in st.session_state.marker_queue)
                num_unique = len(unique_ratios)
                num_total = len(st.session_state.marker_queue)
                cache_note = f" ({num_unique} unique)" if num_unique < num_total else ""

                if st.button(f"🚀 Run Nesting ({num_total} markers{cache_note})", type="primary", use_container_width=True):
                    # Run markers sequentially with progress, caching duplicates
                    progress_bar = st.progress(0)
                    status_text = st.empty()
                    results_container = st.empty()

                    results = []
                    result_cache = {}  # Cache by ratio string
                    total_markers = len(st.session_state.marker_queue)
                    cached_count = 0

                    for i, (marker_name, sq) in enumerate(st.session_state.marker_queue):
                        ratio_str = "-".join(str(sq.get(s, 0)) for s in display_sizes)
                        progress_bar.progress(i / total_markers)

                        # Check cache first
                        if ratio_str in result_cache:
                            # Reuse cached result with new marker name
                            cached_result = result_cache[ratio_str]
                            result = MarkerResult(
                                name=marker_name,
                                ratio_str=cached_result.ratio_str,
                                size_quantities=cached_result.size_quantities.copy(),
                                solution=cached_result.solution,
                                bundle_pieces=cached_result.bundle_pieces,
                                elapsed_time=0.0,  # No time spent
                                error=cached_result.error
                            )
                            cached_count += 1
                            status_text.markdown(f"**Marker {i+1}/{total_markers}:** `{ratio_str}` *(cached)*")
                        else:
                            # Run nesting for new ratio
                            status_text.markdown(f"**Nesting marker {i+1}/{total_markers}:** `{ratio_str}`")
                            result = run_single_marker(
                                marker_name, sq, grouped, st.session_state.piece_type_config,
                                fabric_width_mm, piece_buffer, edge_buffer, time_limit, allowed_rotations
                            )
                            result_cache[ratio_str] = result

                        results.append(result)

                        # Show intermediate results table
                        with results_container.container():
                            interim_data = []
                            for r in results:
                                interim_data.append({
                                    "Ratio": r.ratio_str,
                                    "Length (m)": f"{r.length_mm/1000:.2f}" if not r.error else "ERR",
                                    "Eff %": f"{r.utilization:.1f}" if not r.error else "-",
                                    "Status": "✅" if not r.error else "❌",
                                    "Cached": "♻️" if r.elapsed_time == 0 else ""
                                })
                            st.dataframe(interim_data, use_container_width=True, hide_index=True)

                    progress_bar.progress(1.0)
                    cache_msg = f" ({cached_count} from cache)" if cached_count > 0 else ""
                    status_text.markdown(f"**✅ All {total_markers} markers complete!{cache_msg}**")

                    st.session_state.marker_results = results
                    st.session_state.selected_result_idx = 0
                    time.sleep(0.5)
                    st.rerun()
            else:
                st.button("🚀 Run Nesting (no markers)", disabled=True, use_container_width=True)

            st.markdown("---")

            # === RESULTS SECTION ===
            st.subheader("📊 Results")

            marker_results = st.session_state.marker_results

            if marker_results:
                # Build results table for display and export
                results_table_data = []
                for r in marker_results:
                    results_table_data.append({
                        "Ratio": r.ratio_str,
                        "Length (m)": f"{r.length_mm/1000:.2f}" if not r.error else "-",
                        "Length (yds)": f"{r.length_yards:.2f}" if not r.error else "-",
                        "Eff %": f"{r.utilization:.1f}" if not r.error else "-",
                        "Pieces": len(r.bundle_pieces),
                        "Bundles": sum(r.size_quantities.values()),
                        "Time (s)": f"{r.elapsed_time:.1f}"
                    })

                st.dataframe(results_table_data, use_container_width=True, hide_index=True)

                # CSV Export
                st.markdown("**Export Results:**")
                csv_lines = [f"Ratio,Length(m),Length(yds),Eff%,Pieces,Bundles"]
                for r in marker_results:
                    if not r.error:
                        csv_lines.append(f"{r.ratio_str},{r.length_mm/1000:.2f},{r.length_yards:.2f},{r.utilization:.1f},{len(r.bundle_pieces)},{sum(r.size_quantities.values())}")
                csv_content = "\n".join(csv_lines)

                exp_col1, exp_col2 = st.columns(2)
                with exp_col1:
                    st.download_button(
                        "📥 Download Results CSV",
                        data=csv_content,
                        file_name="nesting_results.csv",
                        mime="text/csv",
                        use_container_width=True
                    )
                with exp_col2:
                    # Summary stats
                    valid_results = [r for r in marker_results if not r.error]
                    if valid_results:
                        avg_eff = sum(r.utilization for r in valid_results) / len(valid_results)
                        total_length = sum(r.length_mm for r in valid_results) / 1000
                        st.metric("Avg Efficiency", f"{avg_eff:.1f}%")

                st.markdown("---")

                # === INDIVIDUAL MARKER PREVIEW ===
                st.subheader("🔍 Marker Preview")

                marker_options = [f"{i+1}. {r.ratio_str} ({r.utilization:.1f}%)" if not r.error else f"{i+1}. {r.ratio_str} (ERROR)"
                                 for i, r in enumerate(marker_results)]
                selected_idx = st.selectbox(
                    "Select marker to view",
                    range(len(marker_results)),
                    format_func=lambda x: marker_options[x],
                    index=min(st.session_state.selected_result_idx, len(marker_results)-1),
                    key="result_selector"
                )
                st.session_state.selected_result_idx = selected_idx

                selected_result = marker_results[selected_idx]

                if selected_result.error:
                    st.error(f"Error: {selected_result.error}")
                elif selected_result.solution:
                    solution = selected_result.solution
                    bundle_pieces_sel = selected_result.bundle_pieces

                    # Metrics row
                    m1, m2, m3, m4 = st.columns(4)
                    m1.metric("Utilization", f"{solution.utilization_percent:.1f}%")
                    m2.metric("Length", f"{selected_result.length_yards:.2f} yds")
                    m3.metric("Length", f"{selected_result.length_mm/1000:.2f} m")
                    m4.metric("Pieces", solution.num_placements)

                    # Layout visualization
                    show_labels = st.checkbox("Show piece labels", value=True, key="show_labels_preview")
                    fig = plot_solution_with_bundles(solution, bundle_pieces_sel, show_labels)
                    st.pyplot(fig)

                    # Save PNG
                    png_buf = io.BytesIO()
                    fig.savefig(png_buf, format='png', dpi=150, bbox_inches='tight',
                               facecolor='white', edgecolor='none')
                    png_buf.seek(0)
                    plt.close()

                    # Export buttons for individual marker
                    exp1, exp2, exp3 = st.columns(3)
                    with exp1:
                        svg_content = export_to_svg(solution, bundle_pieces_sel)
                        st.download_button("⬇️ SVG", data=svg_content,
                                          file_name=f"marker_{selected_result.ratio_str.replace('-','_')}.svg",
                                          mime="image/svg+xml", use_container_width=True)
                    with exp2:
                        dxf_content = export_to_dxf(solution, bundle_pieces_sel)
                        st.download_button("⬇️ DXF", data=dxf_content,
                                          file_name=f"marker_{selected_result.ratio_str.replace('-','_')}.dxf",
                                          mime="application/dxf", use_container_width=True)
                    with exp3:
                        st.download_button("📷 PNG", data=png_buf.getvalue(),
                                          file_name=f"marker_{selected_result.ratio_str.replace('-','_')}_{solution.utilization_percent:.0f}pct.png",
                                          mime="image/png", use_container_width=True)
            else:
                st.info("No results yet. Add markers in the **Configure** tab and click **Run Nesting**.")


if __name__ == "__main__":
    main()
