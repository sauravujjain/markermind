#!/usr/bin/env python3
"""
Diagnostic script to verify Spyrrow/CPU nesting is working correctly.

Checks:
1. Piece loading from DXF
2. Piece counts per combination
3. Spyrrow solution validity
4. Placement data structure
5. DXF export coordinate transforms

Run from garment-nester directory:
    PYTHONPATH=. python verify_spyrrow_nesting.py
"""

import sys
from pathlib import Path
from collections import defaultdict

# Configuration - adjust path if needed
DXF_PATH = Path("data/24_2506_7_S-AN1000.DXF")
CONTAINER_WIDTH_MM = 1524.0

SIZES = ["XS", "S", "M", "L", "XL", "XXL"]
PIECE_CONFIG = {
    "BK": {"demand": 1, "flip": False},
    "FRT": {"demand": 1, "flip": False},
    "SL": {"demand": 2, "flip": True},
}

def extract_piece_type(piece_name: str):
    name_upper = piece_name.upper()
    for piece_type in PIECE_CONFIG.keys():
        if piece_type in name_upper:
            return piece_type
    return None


def main():
    print("=" * 80)
    print("SPYRROW NESTING VERIFICATION")
    print("=" * 80)
    
    # Check DXF exists
    if not DXF_PATH.exists():
        print(f"\n❌ ERROR: DXF not found at {DXF_PATH}")
        print("   Run this script from the garment-nester directory")
        sys.exit(1)
    
    # =========================================================================
    # Step 1: Load and verify pieces from DXF
    # =========================================================================
    print("\n" + "-" * 80)
    print("STEP 1: Loading pieces from DXF")
    print("-" * 80)
    
    from nesting_engine.io.dxf_parser import DXFParser
    
    parser = DXFParser(str(DXF_PATH))
    result = parser.parse()
    
    print(f"  Raw pieces found: {len(result.pieces)}")
    
    # Organize by size and type
    pieces_by_size = defaultdict(dict)
    all_piece_names = []
    
    for parsed in result.pieces:
        size = parsed.size
        piece_name = parsed.piece_name or ""
        piece_type = extract_piece_type(piece_name)
        
        all_piece_names.append(f"{size}_{piece_name}")
        
        if size not in SIZES:
            continue
        if piece_type is None:
            continue
        if piece_type in pieces_by_size[size]:
            continue  # Skip duplicates
            
        # Convert to mm
        vertices_mm = [(x * 25.4, y * 25.4) for x, y in parsed.vertices]
        pieces_by_size[size][piece_type] = vertices_mm
    
    print(f"\n  Organized pieces by size:")
    for size in SIZES:
        types = list(pieces_by_size.get(size, {}).keys())
        print(f"    {size}: {types}")
    
    # Verify we have all expected pieces
    missing = []
    for size in SIZES:
        for ptype in PIECE_CONFIG.keys():
            if ptype not in pieces_by_size.get(size, {}):
                missing.append(f"{size}_{ptype}")
    
    if missing:
        print(f"\n  ⚠️  WARNING: Missing pieces: {missing}")
    else:
        print(f"\n  ✓ All {len(SIZES) * len(PIECE_CONFIG)} expected piece types found")
    
    # =========================================================================
    # Step 2: Run a test nest and check piece counts
    # =========================================================================
    print("\n" + "-" * 80)
    print("STEP 2: Running test nest (M:1, S:2, XS:2, XXL:1 = 6 garments)")
    print("-" * 80)
    
    from nesting_engine.engine.spyrrow_engine import SpyrrowEngine, SpyrrowConfig
    from nesting_engine.core.instance import Container, NestingItem, NestingInstance, FlipMode
    from nesting_engine.core.piece import Piece, PieceIdentifier, OrientationConstraint
    
    test_combo = {"M": 1, "S": 2, "XS": 2, "XXL": 1}  # 6 garments total
    
    print(f"\n  Building items for: {test_combo}")
    
    items = []
    expected_pieces = 0
    
    for size, garment_count in test_combo.items():
        if garment_count == 0:
            continue
            
        for piece_type, config in PIECE_CONFIG.items():
            if piece_type not in pieces_by_size.get(size, {}):
                print(f"    ⚠️  Missing: {size}_{piece_type}")
                continue
                
            vertices = pieces_by_size[size][piece_type]
            
            identifier = PieceIdentifier(
                piece_name=f"{piece_type}_{size}",
                size=size
            )
            
            orientation = OrientationConstraint(
                allowed_rotations=[0, 180],
                allow_flip=config["flip"]
            )
            
            piece = Piece(
                vertices=vertices,
                identifier=identifier,
                orientation=orientation
            )
            
            total_demand = config["demand"] * garment_count
            flip_mode = FlipMode.PAIRED if config["flip"] else FlipMode.NONE
            
            item = NestingItem(
                piece=piece,
                demand=total_demand,
                flip_mode=flip_mode
            )
            items.append(item)
            
            # Calculate expected piece instances
            if flip_mode == FlipMode.PAIRED:
                expected_pieces += total_demand * 2  # Paired creates 2 per demand
            else:
                expected_pieces += total_demand
            
            print(f"    {size}_{piece_type}: demand={total_demand}, flip={flip_mode.name}")
    
    print(f"\n  Total NestingItems created: {len(items)}")
    print(f"  Expected piece instances in solution: {expected_pieces}")
    
    # Actually expected:
    # BK: 1+2+2+1 = 6 pieces
    # FRT: 1+2+2+1 = 6 pieces  
    # SL: (1+2+2+1) * 2 = 12 pieces (paired)
    # Total: 24 pieces
    
    manual_expected = sum(test_combo.values()) * (1 + 1 + 2)  # BK + FRT + 2*SL per garment
    print(f"  Manual calculation: {sum(test_combo.values())} garments × 4 pieces = {manual_expected}")
    
    # Create container and instance
    container = Container(width=CONTAINER_WIDTH_MM, height=None)
    
    instance = NestingInstance.create(
        name="VerificationTest",
        container=container,
        items=items,
        piece_buffer=2.0,
        edge_buffer=5.0
    )
    
    print(f"\n  Running Spyrrow (10s limit)...")
    
    engine = SpyrrowEngine()
    config = SpyrrowConfig(time_limit=10)
    solution = engine.solve(instance, config=config)
    
    # =========================================================================
    # Step 3: Verify solution
    # =========================================================================
    print("\n" + "-" * 80)
    print("STEP 3: Verifying solution")
    print("-" * 80)
    
    print(f"\n  Solution summary:")
    print(f"    Utilization: {solution.utilization_percent:.2f}%")
    print(f"    Strip length: {solution.strip_length:.2f} mm")
    print(f"    Placements count: {len(solution.placements)}")
    
    if len(solution.placements) != expected_pieces:
        print(f"\n  ❌ MISMATCH: Expected {expected_pieces} placements, got {len(solution.placements)}")
    else:
        print(f"\n  ✓ Piece count matches expected ({expected_pieces})")
    
    # Analyze placements
    print(f"\n  Placement details (first 10):")
    placement_by_type = defaultdict(int)
    
    for i, p in enumerate(solution.placements):
        placement_by_type[p.piece_id.split('_')[0]] += 1
        if i < 10:
            print(f"    [{i}] id={p.piece_id}, pos=({p.x:.1f}, {p.y:.1f}), rot={p.rotation}°, flip={p.flipped}")
    
    print(f"\n  Placements by piece type:")
    for ptype, count in sorted(placement_by_type.items()):
        expected = sum(test_combo.values()) * (2 if ptype == "SL" else 1)
        status = "✓" if count == expected else "❌"
        print(f"    {ptype}: {count} (expected {expected}) {status}")
    
    # =========================================================================
    # Step 4: Check coordinate bounds
    # =========================================================================
    print("\n" + "-" * 80)
    print("STEP 4: Checking coordinate bounds")
    print("-" * 80)
    
    x_coords = [p.x for p in solution.placements]
    y_coords = [p.y for p in solution.placements]
    
    print(f"\n  Placement coordinate ranges:")
    print(f"    X: {min(x_coords):.1f} to {max(x_coords):.1f} mm")
    print(f"    Y: {min(y_coords):.1f} to {max(y_coords):.1f} mm")
    print(f"    Container width: {CONTAINER_WIDTH_MM} mm")
    print(f"    Strip length: {solution.strip_length:.1f} mm")
    
    if max(y_coords) > CONTAINER_WIDTH_MM:
        print(f"\n  ⚠️  WARNING: Some Y coordinates exceed container width!")
    
    if min(x_coords) < 0 or min(y_coords) < 0:
        print(f"\n  ⚠️  WARNING: Negative coordinates detected!")
    
    # =========================================================================
    # Step 5: Test DXF export transform
    # =========================================================================
    print("\n" + "-" * 80)
    print("STEP 5: Testing coordinate transform for DXF export")
    print("-" * 80)
    
    import math
    
    def transform_vertices(vertices, x, y, rotation, flipped=False):
        """Transform as done in export_cpu_markers_dxf.py"""
        min_x = min(v[0] for v in vertices)
        min_y = min(v[1] for v in vertices)
        normalized = [(vx - min_x, vy - min_y) for vx, vy in vertices]
        
        cx = sum(v[0] for v in normalized) / len(normalized)
        cy = sum(v[1] for v in normalized) / len(normalized)
        
        result = []
        angle_rad = math.radians(rotation)
        cos_a = math.cos(angle_rad)
        sin_a = math.sin(angle_rad)
        
        for vx, vy in normalized:
            px = vx - cx
            py = vy - cy
            
            if flipped:
                px = -px
            
            rx = px * cos_a - py * sin_a
            ry = px * sin_a + py * cos_a
            
            final_x = rx + x
            final_y = ry + y
            result.append((final_x, final_y))
        
        return result
    
    # Test with first placement
    p = solution.placements[0]
    piece_type = p.piece_id.split('_')[0]
    size = p.piece_id.split('_')[1] if '_' in p.piece_id else "M"
    
    if size in pieces_by_size and piece_type in pieces_by_size[size]:
        original_verts = pieces_by_size[size][piece_type]
        transformed = transform_vertices(original_verts, p.x, p.y, p.rotation, p.flipped)
        
        print(f"\n  Testing transform for: {p.piece_id}")
        print(f"    Original bounds: X=[{min(v[0] for v in original_verts):.1f}, {max(v[0] for v in original_verts):.1f}]")
        print(f"    Original bounds: Y=[{min(v[1] for v in original_verts):.1f}, {max(v[1] for v in original_verts):.1f}]")
        print(f"    Placement: ({p.x:.1f}, {p.y:.1f}), rot={p.rotation}°, flip={p.flipped}")
        print(f"    Transformed bounds: X=[{min(v[0] for v in transformed):.1f}, {max(v[0] for v in transformed):.1f}]")
        print(f"    Transformed bounds: Y=[{min(v[1] for v in transformed):.1f}, {max(v[1] for v in transformed):.1f}]")
        
        # Check if transformed piece is within expected area
        if min(v[0] for v in transformed) < -100 or min(v[1] for v in transformed) < -100:
            print(f"\n  ⚠️  WARNING: Transform produces negative coordinates!")
    
    # =========================================================================
    # Summary
    # =========================================================================
    print("\n" + "=" * 80)
    print("VERIFICATION SUMMARY")
    print("=" * 80)
    
    issues = []
    
    if missing:
        issues.append(f"Missing pieces: {missing}")
    
    if len(solution.placements) != expected_pieces:
        issues.append(f"Piece count mismatch: {len(solution.placements)} vs {expected_pieces}")
    
    if solution.utilization_percent < 50:
        issues.append(f"Suspiciously low utilization: {solution.utilization_percent:.1f}%")
    
    if issues:
        print("\n❌ Issues found:")
        for issue in issues:
            print(f"   - {issue}")
    else:
        print("\n✓ All checks passed!")
        print(f"  - Pieces loaded correctly from DXF")
        print(f"  - Spyrrow produces expected piece count ({expected_pieces})")
        print(f"  - Utilization reasonable ({solution.utilization_percent:.1f}%)")
    
    print("\n" + "=" * 80)


if __name__ == "__main__":
    main()
