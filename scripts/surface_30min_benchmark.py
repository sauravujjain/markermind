#!/usr/bin/env python3
"""
Surface PC 30-Minute CPU Benchmark v2.

Same 10 markers as gpu_vs_cpu_benchmark_v2.py, nested on Surface PC
for 10 minutes each via SSH pipe.

Params: time=600s, buffers=0, qt_depth=3, early_term=False,
        default exploration/compression, rotations=0/180, seed=42

Usage:
    python scripts/surface_30min_benchmark_v2.py
"""

import sys
import time
import logging
from pathlib import Path
from typing import Dict

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import importlib
import importlib.util

_spec_cpu = importlib.util.spec_from_file_location(
    "spyrrow_nesting_runner",
    PROJECT_ROOT / "backend" / "backend" / "services" / "spyrrow_nesting_runner.py",
)
_mod_cpu = importlib.util.module_from_spec(_spec_cpu)
_spec_cpu.loader.exec_module(_mod_cpu)

load_pieces_for_spyrrow = _mod_cpu.load_pieces_for_spyrrow
nest_single_marker_surface = _mod_cpu.nest_single_marker_surface
export_marker_svg = _mod_cpu.export_marker_svg

logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
logger = logging.getLogger(__name__)

OUTPUT_DIR = PROJECT_ROOT / "experiment_results" / "gpu_vs_cpu_v2"

# ── Same patterns and markers as gpu_vs_cpu_benchmark_v2.py ──────────

PATTERNS = {
    'A': {
        'name': 'Order2 / style 1',
        'dxf': str(PROJECT_ROOT / "uploads/patterns/275d4d77-d17e-4686-8ceb-d53cd67b83a4/style 1.dxf"),
        'rul': str(PROJECT_ROOT / "uploads/patterns/275d4d77-d17e-4686-8ceb-d53cd67b83a4/style 1.rul"),
        'file_type': 'aama',
        'material': 'SO1',
        'sizes': ['46', '48', '50', '52', '54', '56', '58'],
        'width_inches': 60.0,
        'swap_axes': True,
    },
    'B': {
        'name': 'C2509-0360 (3) / vt 201',
        'dxf': str(PROJECT_ROOT / "uploads/patterns/9ccb01fd-32b7-49a3-a0e6-628030a810bb/vt 201 (2).dxf"),
        'rul': None,
        'file_type': 'vt_dxf',
        'material': '201',
        'sizes': ['XS', 'S', 'M', 'L', 'XL', '2XL', '3XL'],
        'width_inches': 59.75,
    },
    'C': {
        'name': '25138 / check_count',
        'dxf': str(PROJECT_ROOT / "uploads/patterns/31bdd406-8314-42c5-8de9-26d837fc91ac/check_count.dxf"),
        'rul': str(PROJECT_ROOT / "uploads/patterns/31bdd406-8314-42c5-8de9-26d837fc91ac/check_count.rul"),
        'file_type': 'aama',
        'material': 'SHELL',
        'sizes': ['XS', 'S', 'M', 'L', 'XL', '1X', '2X', '3X'],
        'width_inches': 54.25,
    },
}

TEST_MARKERS = [
    {'pattern': 'A', 'label': 'A-bc2',  'ratio': {'50': 1, '54': 1}},
    {'pattern': 'A', 'label': 'A-bc4',  'ratio': {'50': 2, '54': 1, '56': 1}},
    {'pattern': 'B', 'label': 'B-bc3',  'ratio': {'XS': 1, 'M': 1, 'L': 1}},
    {'pattern': 'B', 'label': 'B-bc5',  'ratio': {'XL': 1, '2XL': 1, '3XL': 3}},
    {'pattern': 'C', 'label': 'C-bc1',  'ratio': {'XS': 1}},
    {'pattern': 'C', 'label': 'C-bc4',  'ratio': {'XL': 2, '2X': 1, '3X': 1}},
]

TIME_LIMIT = 1800  # 30 minutes per marker

# Monkey-patch SSH timeout buffer for long runs (30s buffer is too short)
import subprocess as _subprocess
_orig_run = _subprocess.run
def _patched_run(*args, **kwargs):
    if 'timeout' in kwargs and kwargs['timeout'] > 600:
        kwargs['timeout'] = kwargs['timeout'] + 120  # Add extra 120s buffer
    return _orig_run(*args, **kwargs)
_subprocess.run = _patched_run


def swap_cpu_piece_axes(nesting_pieces):
    """Swap X/Y axes for Spyrrow pieces."""
    for p in nesting_pieces:
        p.vertices = [(y, x) for x, y in p.vertices]
    return nesting_pieces


def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # Load pieces for each pattern (once)
    cpu_pieces = {}
    for key, pat in PATTERNS.items():
        print(f"Loading Pattern {key}: {pat['name']} ({pat['file_type']})")
        nesting_pieces, piece_config = load_pieces_for_spyrrow(
            dxf_path=pat['dxf'],
            rul_path=pat['rul'],
            material=pat['material'],
            sizes=pat['sizes'],
            allowed_rotations=[0, 180],
            file_type=pat['file_type'],
        )
        if pat.get('swap_axes'):
            swap_cpu_piece_axes(nesting_pieces)
            print(f"  Applied X/Y axis swap")
        print(f"  {len(nesting_pieces)} pieces loaded")
        cpu_pieces[key] = (nesting_pieces, piece_config)

    # Run 10-min nests on Surface
    total_est = len(TEST_MARKERS) * TIME_LIMIT / 60
    print(f"\n{'='*100}")
    print(f"SURFACE 30-MIN BENCHMARK: {len(TEST_MARKERS)} markers × {TIME_LIMIT}s = ~{total_est:.0f} min")
    print(f"Params: qt_depth=3, early_term=False, buffers=0, seed=42")
    print(f"{'='*100}")

    results = []
    t0_total = time.time()

    for i, tm in enumerate(TEST_MARKERS):
        key = tm['pattern']
        pat = PATTERNS[key]
        label = tm['label']
        ratio = tm['ratio']
        bc = sum(ratio.values())
        fabric_width_mm = pat['width_inches'] * 25.4

        nesting_pieces, piece_config = cpu_pieces[key]

        print(f"\n[{i+1}/{len(TEST_MARKERS)}] {label}: bc={bc}, ratio={ratio}")
        print(f"  Sending to Surface ({TIME_LIMIT}s)...", flush=True)

        t0 = time.time()
        try:
            result = nest_single_marker_surface(
                ratio=ratio,
                nesting_pieces=nesting_pieces,
                piece_config=piece_config,
                fabric_width_mm=fabric_width_mm,
                piece_buffer_mm=0.0,
                edge_buffer_mm=0.0,
                time_limit=float(TIME_LIMIT),
                rotation_mode='free',
                quadtree_depth=3,
                early_termination=False,
                seed=42,
            )
            elapsed = time.time() - t0
            eff = result['utilization'] * 100
            len_yd = result['length_yards']
            comp_time = result['computation_time_s']

            # Save SVG
            svg_saved = False
            if result.get('solution'):
                svg = export_marker_svg(result, fabric_width_mm)
                svg_path = OUTPUT_DIR / f"{label}_surface10m.svg"
                svg_path.write_text(svg, encoding='utf-8')
                svg_saved = True

            print(f"  Done: {eff:.2f}%, {len_yd:.3f}yd, solve={comp_time:.0f}s, wall={elapsed:.0f}s"
                  f"{' → ' + svg_path.name if svg_saved else ''}")

            results.append({
                'label': label, 'pattern': key, 'bc': bc,
                'eff': eff, 'len_yd': len_yd,
                'comp_time': comp_time, 'wall_time': elapsed,
            })
        except Exception as e:
            elapsed = time.time() - t0
            print(f"  FAILED after {elapsed:.0f}s: {e}")
            results.append({
                'label': label, 'pattern': key, 'bc': bc,
                'eff': 0, 'len_yd': 0, 'comp_time': 0, 'wall_time': elapsed,
            })

    total_elapsed = time.time() - t0_total

    # Summary
    print(f"\n{'='*100}")
    print(f"SURFACE 30-MIN RESULTS (total wall time: {total_elapsed/60:.1f} min)")
    print(f"{'='*100}")
    print(f"{'Label':<12} {'Pat':>3} {'BC':>3} {'Surf%':>8} {'Surf_yd':>9} {'Solve_s':>8} {'Wall_s':>7}")
    print(f"{'-'*100}")

    for r in results:
        print(f"{r['label']:<12} {r['pattern']:>3} {r['bc']:>3} "
              f"{r['eff']:>8.2f} {r['len_yd']:>9.3f} {r['comp_time']:>7.0f}s {r['wall_time']:>6.0f}s")

    print(f"\nSVGs saved to: {OUTPUT_DIR}/*_surface10m.svg")


if __name__ == '__main__':
    main()
