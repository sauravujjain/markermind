#!/usr/bin/env python
"""
Test script to verify spyrrow installation and integration.

Run this in your conda environment:
    conda activate nester
    python tests/test_spyrrow_integration.py

This script:
1. Tests raw spyrrow API
2. Tests our SpyrrowEngine wrapper
3. Tests paired/flipped pieces
"""

import sys
import os

# Add src to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

def test_raw_spyrrow():
    """Test raw spyrrow API works."""
    print("=" * 60)
    print("TEST 1: Raw spyrrow API")
    print("=" * 60)
    
    try:
        import spyrrow
        print(f"✓ spyrrow imported successfully")
    except ImportError as e:
        print(f"✗ Failed to import spyrrow: {e}")
        return False
    
    # Create simple test items
    rectangle = spyrrow.Item(
        "rectangle",
        [(0, 0), (100, 0), (100, 50), (0, 50), (0, 0)],
        demand=4,
        allowed_orientations=[0, 180]
    )
    
    triangle = spyrrow.Item(
        "triangle",
        [(0, 0), (80, 0), (40, 60), (0, 0)],
        demand=3,
        allowed_orientations=[0, 90, 180, 270]
    )
    
    print(f"✓ Created 2 test items")
    
    # Create instance
    instance = spyrrow.StripPackingInstance(
        "test_instance",
        strip_height=200.0,  # Container width
        items=[rectangle, triangle]
    )
    print(f"✓ Created instance with strip_height=200")
    
    # Configure and solve
    # Note: num_wokers is a typo in the spyrrow API
    config = spyrrow.StripPackingConfig(
        early_termination=False,
        total_computation_time=10,  # 10 seconds
        num_workers=2,  # API typo: num_wokers not num_workers
        seed=42
    )
    
    print(f"  Solving (10 second limit)...")
    solution = instance.solve(config)
    
    print(f"✓ Solution found!")
    print(f"  Strip width (length): {solution.width:.2f}")
    print(f"  Density: {solution.density:.4f} ({solution.density * 100:.2f}%)")
    print(f"  Placed items: {len(solution.placed_items)}")
    
    print("\n  Placements:")
    for pi in solution.placed_items:
        print(f"    - {pi.id}: pos=({pi.translation[0]:.1f}, {pi.translation[1]:.1f}), rot={pi.rotation}°")
    
    return True


def test_engine_wrapper():
    """Test our SpyrrowEngine wrapper."""
    print("\n" + "=" * 60)
    print("TEST 2: SpyrrowEngine Wrapper")
    print("=" * 60)
    
    from nesting_engine.core import (
        Piece, PieceIdentifier, OrientationConstraint, GrainConstraint,
        Container, NestingItem, NestingInstance, FlipMode
    )
    from nesting_engine.engine.spyrrow_engine import SpyrrowEngine, SpyrrowConfig
    
    # Create pieces
    front_panel = Piece(
        vertices=[(0, 0), (150, 0), (150, 200), (0, 200), (0, 0)],
        identifier=PieceIdentifier(piece_name="Front Panel", size="M"),
        orientation=OrientationConstraint(allowed_rotations=[0, 180], allow_flip=False)
    )
    
    back_panel = Piece(
        vertices=[(0, 0), (150, 0), (150, 220), (0, 220), (0, 0)],
        identifier=PieceIdentifier(piece_name="Back Panel", size="M"),
        orientation=OrientationConstraint(allowed_rotations=[0, 180], allow_flip=False)
    )
    
    # L-shaped piece (asymmetric, good for flip testing)
    pocket = Piece(
        vertices=[(0, 0), (60, 0), (60, 30), (30, 30), (30, 50), (0, 50), (0, 0)],
        identifier=PieceIdentifier(piece_name="Pocket", size="M"),
        orientation=OrientationConstraint(allowed_rotations=[0, 180], allow_flip=False)
    )
    
    print(f"✓ Created 3 test pieces")
    print(f"  Front Panel: {front_panel.area:.0f} mm²")
    print(f"  Back Panel: {back_panel.area:.0f} mm²")
    print(f"  Pocket: {pocket.area:.0f} mm²")
    
    # Create nesting instance
    container = Container(width=500, height=None)  # Strip packing
    items = [
        NestingItem(front_panel, demand=2, flip_mode=FlipMode.NONE),
        NestingItem(back_panel, demand=1, flip_mode=FlipMode.NONE),
        NestingItem(pocket, demand=4, flip_mode=FlipMode.NONE),
    ]
    
    instance = NestingInstance.create(
        name="Test Marker",
        container=container,
        items=items,
        piece_buffer=0.0,
        edge_buffer=0.0
    )
    
    print(f"✓ Created nesting instance")
    print(f"  Total pieces: {instance.total_piece_count}")
    print(f"  Total area: {instance.total_piece_area:.0f} mm²")
    
    # Solve
    engine = SpyrrowEngine()
    config = SpyrrowConfig(time_limit=15, num_workers=2, seed=42)
    
    print(f"\n  Solving with SpyrrowEngine (15 second limit)...")
    solution = engine.solve(instance, config=config)
    
    print(f"✓ Solution found!")
    print(f"  Strip length: {solution.strip_length:.2f} mm")
    print(f"  Utilization: {solution.utilization_percent:.2f}%")
    print(f"  Computation time: {solution.computation_time_ms:.1f} ms")
    print(f"  Placements: {solution.num_placements}")
    
    # Validate
    is_valid, errors = solution.validate(instance)
    if is_valid:
        print(f"✓ Solution validation passed")
    else:
        print(f"✗ Solution validation failed:")
        for err in errors:
            print(f"    - {err}")
    
    return is_valid


def test_paired_flip():
    """Test paired/flipped pieces (left/right sleeves)."""
    print("\n" + "=" * 60)
    print("TEST 3: Paired/Flipped Pieces (Left/Right)")
    print("=" * 60)
    
    from nesting_engine.core import (
        Piece, PieceIdentifier, OrientationConstraint,
        Container, NestingItem, NestingInstance, FlipMode
    )
    from nesting_engine.engine.spyrrow_engine import SpyrrowEngine, SpyrrowConfig
    
    # Create asymmetric sleeve piece
    # This L-shape will look different when flipped
    sleeve = Piece(
        vertices=[
            (0, 0), (100, 0), (100, 30), (70, 30),
            (70, 150), (0, 150), (0, 0)
        ],
        identifier=PieceIdentifier(piece_name="Sleeve", size="M"),
        orientation=OrientationConstraint(
            allowed_rotations=[0, 180],
            allow_flip=True  # Can be flipped for left/right
        )
    )
    
    print(f"✓ Created asymmetric sleeve piece")
    print(f"  Area: {sleeve.area:.0f} mm²")
    print(f"  Can be flipped: {sleeve.can_be_flipped}")
    
    # Create instance with PAIRED flip mode
    container = Container(width=300, height=None)
    items = [
        NestingItem(
            sleeve, 
            demand=4,  # Need 4 sleeves total
            flip_mode=FlipMode.PAIRED  # 2 normal (right) + 2 flipped (left)
        ),
    ]
    
    instance = NestingInstance.create(
        name="Sleeve Pair Test",
        container=container,
        items=items
    )
    
    # Check breakdown
    breakdown = items[0].get_placement_breakdown()
    print(f"  Placement breakdown: {breakdown}")
    # Should be [(False, 2), (True, 2)] for 2 right + 2 left
    
    # Solve
    engine = SpyrrowEngine()
    config = SpyrrowConfig(time_limit=15, num_workers=2, seed=42)
    
    print(f"\n  Solving paired sleeve nesting...")
    solution = engine.solve(instance, config=config)
    
    print(f"✓ Solution found!")
    print(f"  Strip length: {solution.strip_length:.2f} mm")
    print(f"  Utilization: {solution.utilization_percent:.2f}%")
    
    # Check flip summary
    flip_summary = solution.flip_summary
    print(f"\n  Flip summary (NESTING decisions):")
    print(f"    Non-flipped (right sleeves): {flip_summary['not_flipped']}")
    print(f"    Flipped (left sleeves): {flip_summary['flipped']}")
    
    print("\n  Placements:")
    for p in solution.placements:
        flip_str = " [LEFT/flipped]" if p.flipped else " [RIGHT]"
        print(f"    - {p.piece_id}[{p.instance_index}]: "
              f"({p.x:.1f}, {p.y:.1f}), rot={p.rotation}°{flip_str}")
    
    # Verify we have correct flip distribution
    expected_flipped = 2
    expected_not_flipped = 2
    
    if (flip_summary['flipped'] == expected_flipped and 
        flip_summary['not_flipped'] == expected_not_flipped):
        print(f"\n✓ Correct flip distribution: {expected_not_flipped} right + {expected_flipped} left")
        return True
    else:
        print(f"\n✗ Unexpected flip distribution")
        return False


def main():
    """Run all tests."""
    print("\nSpyrrow Integration Tests")
    print("=" * 60)
    
    results = []
    
    # Test 1: Raw spyrrow
    try:
        results.append(("Raw spyrrow API", test_raw_spyrrow()))
    except Exception as e:
        print(f"✗ Test failed with exception: {e}")
        results.append(("Raw spyrrow API", False))
    
    # Test 2: Engine wrapper
    try:
        results.append(("SpyrrowEngine wrapper", test_engine_wrapper()))
    except Exception as e:
        print(f"✗ Test failed with exception: {e}")
        import traceback
        traceback.print_exc()
        results.append(("SpyrrowEngine wrapper", False))
    
    # Test 3: Paired flip
    try:
        results.append(("Paired/flipped pieces", test_paired_flip()))
    except Exception as e:
        print(f"✗ Test failed with exception: {e}")
        import traceback
        traceback.print_exc()
        results.append(("Paired/flipped pieces", False))
    
    # Summary
    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    
    all_passed = True
    for name, passed in results:
        status = "✓ PASSED" if passed else "✗ FAILED"
        print(f"  {name}: {status}")
        if not passed:
            all_passed = False
    
    print("\n" + "=" * 60)
    if all_passed:
        print("All tests passed! Spyrrow integration is working.")
    else:
        print("Some tests failed. Check the output above.")
    print("=" * 60)
    
    return 0 if all_passed else 1


if __name__ == "__main__":
    sys.exit(main())
