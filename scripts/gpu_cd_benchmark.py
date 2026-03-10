#!/usr/bin/env python3
"""
GPU Coordinate Descent Benchmark.

Applies coordinate descent post-refinement to the same 10 markers
from gpu_vs_cpu_benchmark_v2.py and measures improvement over BLF.

Usage:
    conda activate nester
    python scripts/gpu_cd_benchmark.py
"""

import sys
import time
import numpy as np
import importlib
import importlib.util
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# GPU nesting runner
_spec = importlib.util.spec_from_file_location(
    "gpu_nesting_runner",
    PROJECT_ROOT / "backend" / "backend" / "services" / "gpu_nesting_runner.py",
)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

load_pieces_for_material = _mod.load_pieces_for_material
GPUPacker = _mod.GPUPacker
_init_gpu = _mod._init_gpu

# Import CD functions from existing experiment
_spec_cd = importlib.util.spec_from_file_location(
    "gpu_coordinate_descent_experiment",
    PROJECT_ROOT / "scripts" / "gpu_coordinate_descent_experiment.py",
)
_mod_cd = importlib.util.module_from_spec(_spec_cd)
_spec_cd.loader.exec_module(_mod_cd)

_mod_cd.init()  # Initialize CuPy references in CD module
dual_sort_with_positions = _mod_cd.dual_sort_with_positions
rebuild_container = _mod_cd.rebuild_container
coordinate_descent = _mod_cd.coordinate_descent
compute_strip_length = _mod_cd.compute_strip_length

OUTPUT_DIR = PROJECT_ROOT / "experiment_results" / "gpu_vs_cpu_v2"

# Same patterns and markers as v2 benchmark
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
    {'pattern': 'A', 'label': 'A-bc1',  'ratio': {'50': 1}},
    {'pattern': 'A', 'label': 'A-bc2',  'ratio': {'50': 1, '54': 1}},
    {'pattern': 'A', 'label': 'A-bc4',  'ratio': {'50': 2, '54': 1, '56': 1}},
    {'pattern': 'A', 'label': 'A-bc6',  'ratio': {'50': 5, '54': 1}},
    {'pattern': 'B', 'label': 'B-bc1',  'ratio': {'M': 1}},
    {'pattern': 'B', 'label': 'B-bc3',  'ratio': {'XS': 1, 'M': 1, 'L': 1}},
    {'pattern': 'B', 'label': 'B-bc5',  'ratio': {'XL': 1, '2XL': 1, '3XL': 3}},
    {'pattern': 'C', 'label': 'C-bc1',  'ratio': {'XS': 1}},
    {'pattern': 'C', 'label': 'C-bc2',  'ratio': {'M': 1, '3X': 1}},
    {'pattern': 'C', 'label': 'C-bc4',  'ratio': {'XL': 2, '2X': 1, '3X': 1}},
]

# CPU 30s results from v2 benchmark (for reference)
CPU_30S = {
    'A-bc1': 75.44, 'A-bc2': 84.95, 'A-bc4': 85.82, 'A-bc6': 85.47,
    'B-bc1': 40.09, 'B-bc3': 92.50, 'B-bc5': 92.20,
    'C-bc1': 85.35, 'C-bc2': 86.46, 'C-bc4': 85.95,
}


def swap_gpu_piece_axes(pieces_by_size):
    import cupy as _cp
    for size, pieces in pieces_by_size.items():
        for p in pieces:
            p['vertices_mm'] = [(y, x) for x, y in p['vertices_mm']]
            p['raster'] = p['raster'].T.copy()
            p['raster_gpu'] = _cp.asarray(p['raster'])
            p['raster_180'] = np.rot90(p['raster'], 2).copy()
            p['raster_180_gpu'] = _cp.asarray(p['raster_180'])


def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    GPU_SCALE = 0.30  # Fine resolution, same as v2

    # Load pieces
    gpu_pieces = {}
    packers = {}

    for key, pat in PATTERNS.items():
        print(f"Loading {key}: {pat['name']}")
        pbs = load_pieces_for_material(
            dxf_path=pat['dxf'], rul_path=pat['rul'],
            material=pat['material'], sizes=pat['sizes'],
            gpu_scale=GPU_SCALE, file_type=pat['file_type'],
        )
        if pat.get('swap_axes'):
            swap_gpu_piece_axes(pbs)
        gpu_pieces[key] = pbs

        fabric_width_mm = pat['width_inches'] * 25.4
        strip_width_px = int(fabric_width_mm * GPU_SCALE)
        max_area = max(
            sum(p['area'] * p['demand'] for p in pbs.get(s, []))
            for s in pat['sizes'] if s in pbs
        )
        max_length = int((6 * max_area * 2) / strip_width_px) + 500
        packers[key] = (GPUPacker(strip_width_px, max_length), strip_width_px)

    # Run BLF + CD on each marker
    print(f"\n{'='*110}")
    print(f"COORDINATE DESCENT EXPERIMENT: {len(TEST_MARKERS)} markers")
    print(f"{'='*110}")

    results = []
    for tm in TEST_MARKERS:
        key = tm['pattern']
        pat = PATTERNS[key]
        label = tm['label']
        ratio = tm['ratio']
        bc = sum(ratio.values())
        fabric_width_mm = pat['width_inches'] * 25.4

        packer, strip_width_px = packers[key]
        pbs = gpu_pieces[key]

        # Build pieces list
        pieces_list = []
        for size, count in ratio.items():
            if count <= 0 or size not in pbs:
                continue
            for _ in range(count):
                for p in pbs[size]:
                    for _ in range(p['demand']):
                        pieces_list.append(p)

        print(f"\n--- {label}: bc={bc}, {len(pieces_list)} pieces ---")

        # Step 1: Dual-sort BLF
        t0 = time.time()
        eff_blf, len_blf, placements, placed_area, cl_blf, sort_used = \
            dual_sort_with_positions(pieces_list, packer, strip_width_px, GPU_SCALE)
        t_blf = time.time() - t0

        print(f"  BLF ({sort_used}): {eff_blf*100:.2f}%, {len_blf:.3f}yd, {t_blf*1000:.0f}ms")

        if not placements:
            print("  Skipped (no placements)")
            continue

        # Step 2: Coordinate descent
        rebuild_container(placements, packer)
        t0 = time.time()
        placements_cd, cl_cd, total_moves, passes = coordinate_descent(
            placements, packer, strip_width_px, placed_area, max_passes=10,
        )
        t_cd = time.time() - t0

        eff_cd = placed_area / (strip_width_px * cl_cd) if cl_cd > 0 else 0
        len_cd = cl_cd / GPU_SCALE / 25.4 / 36
        delta = (eff_cd - eff_blf) * 100

        print(f"  CD:               {eff_cd*100:.2f}%, {len_cd:.3f}yd, {t_cd*1000:.0f}ms, "
              f"{total_moves} moves, {passes} passes")
        print(f"  Improvement:      {delta:+.2f}%")

        # Save CD PNG (packer container state after CD)
        png_bytes = packer.get_container_png(cl_cd)
        png_path = OUTPUT_DIR / f"{label}_gpu_cd.png"
        png_path.write_bytes(png_bytes)

        cpu_ref = CPU_30S.get(label, 0)
        gap_blf = cpu_ref - eff_blf * 100
        gap_cd = cpu_ref - eff_cd * 100

        results.append({
            'label': label, 'bc': bc,
            'blf': eff_blf * 100, 'cd': eff_cd * 100,
            'delta': delta, 'cpu30s': cpu_ref,
            'gap_blf': gap_blf, 'gap_cd': gap_cd,
            'moves': total_moves, 'passes': passes,
            'ms_blf': t_blf * 1000, 'ms_cd': t_cd * 1000,
        })

    # Summary
    print(f"\n{'='*120}")
    print(f"SUMMARY: BLF vs BLF+CD vs CPU(30s)")
    print(f"{'='*120}")
    print(f"{'Label':<12} {'BC':>3} {'BLF%':>7} {'CD%':>7} {'CD+':>6} "
          f"{'CPU30s':>7} {'GapBLF':>7} {'GapCD':>7} {'Moves':>6} {'BLFms':>6} {'CDms':>6}")
    print(f"{'-'*120}")

    for r in results:
        print(f"{r['label']:<12} {r['bc']:>3} {r['blf']:>7.2f} {r['cd']:>7.2f} "
              f"{r['delta']:>+5.2f}% {r['cpu30s']:>7.2f} "
              f"{r['gap_blf']:>+6.2f}% {r['gap_cd']:>+6.2f}% "
              f"{r['moves']:>6} {r['ms_blf']:>5.0f}ms {r['ms_cd']:>5.0f}ms")

    if results:
        avg_blf = sum(r['blf'] for r in results) / len(results)
        avg_cd = sum(r['cd'] for r in results) / len(results)
        avg_delta = sum(r['delta'] for r in results) / len(results)
        avg_gap_blf = sum(r['gap_blf'] for r in results) / len(results)
        avg_gap_cd = sum(r['gap_cd'] for r in results) / len(results)
        print(f"\nAvg BLF: {avg_blf:.2f}%  Avg CD: {avg_cd:.2f}%  "
              f"Avg CD gain: {avg_delta:+.2f}%")
        print(f"Avg gap vs CPU30s:  BLF={avg_gap_blf:+.2f}%  CD={avg_gap_cd:+.2f}%")
        print(f"\nCD SVGs/PNGs saved to: {OUTPUT_DIR}/*_gpu_cd.*")


if __name__ == '__main__':
    main()
