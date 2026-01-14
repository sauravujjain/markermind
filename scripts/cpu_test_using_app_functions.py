#!/usr/bin/env python3
"""
CPU Nesting Test - Using EXACT same functions as working Streamlit app.

This script imports and uses the EXACT same functions that work in the Streamlit app,
ensuring consistent results without reimplementing any logic.

Usage:
    PYTHONPATH=. python scripts/cpu_test_using_app_functions.py
"""

import sys
import json
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "apps"))

# Import the WORKING functions from the Streamlit app
from app import (
    group_pieces_by_type,
    build_bundle_pieces,
    run_nesting,
    plot_solution_with_bundles,
    STANDARD_SIZES,
)

# Import DXF loading from nesting_engine (same as app.py does)
from nesting_engine.io import load_pieces_from_dxf

import matplotlib.pyplot as plt

# Configuration
DXF_PATH = Path("data/24_2506_7_S-AN1000.DXF")
OUTPUT_DIR = Path("experiment_results/cpu_test_v4")
FABRIC_WIDTH_MM = 1524.0  # 60 inches
TIME_LIMIT = 30
PIECE_BUFFER = 2.0
EDGE_BUFFER = 5.0

TEST_COMBINATIONS = [
    {"M": 1},
    {"S": 2},
    {"XS": 1, "XXL": 1},
    {"M": 2, "L": 1},
    {"XS": 2, "S": 1, "M": 1},
    {"S": 1, "M": 1, "L": 1, "XL": 1},
    {"M": 1, "S": 2, "XS": 2, "XXL": 1},
    {"L": 2, "XL": 2, "XXL": 1},
    {"XS": 3, "S": 2, "M": 1},
    {"S": 2, "M": 2, "L": 1, "XL": 1},
]


def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # Load pieces using the SAME function as Streamlit app
    print(f"Loading pieces from {DXF_PATH}...")
    pieces, parse_result = load_pieces_from_dxf(
        str(DXF_PATH),
        rotations=[0, 180],
        allow_flip=True
    )
    print(f"  Loaded {len(pieces)} pieces")

    # Group pieces - SAME as Streamlit
    grouped = group_pieces_by_type(pieces)
    print(f"  Piece types: {list(grouped.keys())}")

    # Default piece config - SAME as Streamlit
    piece_type_config = {}
    for ptype in grouped.keys():
        if ptype == "SL":
            piece_type_config[ptype] = {'demand': 2, 'flipped': True}
        else:
            piece_type_config[ptype] = {'demand': 1, 'flipped': False}

    print(f"\nRunning {len(TEST_COMBINATIONS)} test combinations...")
    print("=" * 80)

    results = []

    for i, combo in enumerate(TEST_COMBINATIONS):
        combo_str = ", ".join(f"{s}:{n}" for s, n in sorted(combo.items()))

        # Build size quantities dict - SAME format as Streamlit
        size_quantities = {s: 0 for s in STANDARD_SIZES}
        for size, count in combo.items():
            size_quantities[size] = count

        # Build bundle pieces - EXACT SAME as Streamlit
        bundle_pieces = build_bundle_pieces(grouped, piece_type_config, size_quantities)

        if not bundle_pieces:
            print(f"  [{i}] {combo_str}: No pieces!")
            continue

        # Run nesting - EXACT SAME as Streamlit
        solution = run_nesting(
            bundle_pieces,
            FABRIC_WIDTH_MM,
            PIECE_BUFFER,
            EDGE_BUFFER,
            TIME_LIMIT,
            allowed_rotations=[0, 180]
        )

        print(f"  [{i}] {combo_str:<30} -> {solution.utilization_percent:5.1f}% | "
              f"{len(solution.placements)} pieces | {solution.strip_length:.0f}mm")

        # Visualize - EXACT SAME as Streamlit
        fig = plot_solution_with_bundles(solution, bundle_pieces, show_labels=True)

        # Add combo info to title
        ax = fig.axes[0]
        ax.set_title(f"Combo {i}: {combo_str}\n"
                     f"Utilization: {solution.utilization_percent:.1f}% | "
                     f"Strip: {solution.strip_length:.0f}mm | "
                     f"{len(solution.placements)} pieces")

        # Save PNG
        png_path = OUTPUT_DIR / f"cpu_combo_{i:02d}.png"
        fig.savefig(png_path, dpi=150, bbox_inches='tight', facecolor='white')
        plt.close(fig)

        results.append({
            "combo_id": i,
            "combo": combo,
            "utilization": solution.utilization_percent,
            "strip_length_mm": solution.strip_length,
            "pieces_placed": len(solution.placements),
            "png": str(png_path)
        })

    # Summary
    print("\n" + "=" * 80)
    print("SUMMARY")
    print("=" * 80)

    for r in results:
        combo_str = ", ".join(f"{s}:{n}" for s, n in sorted(r["combo"].items()))
        print(f"  Combo {r['combo_id']}: {combo_str:<30} "
              f"{r['utilization']:5.1f}% | {r['strip_length_mm']:.0f}mm")

    # Save results JSON
    with open(OUTPUT_DIR / "results.json", "w") as f:
        json.dump(results, f, indent=2)

    print(f"\nResults saved to {OUTPUT_DIR}/")


if __name__ == "__main__":
    main()
