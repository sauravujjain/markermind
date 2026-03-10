#!/usr/bin/env python3
"""
GPU Improved Placement Experiment.

Tests multiple placement strategies beyond dual-sort BLF to close
the gap with Spyrrow CPU nesting. Each strategy modifies how pieces
are ordered before BLF placement.

Strategies tested:
1. width_desc (baseline)
2. area_desc (baseline)
3. height_desc
4. perimeter_desc (pieces with larger perimeter = more complex shape)
5. fill_ratio_asc (least-rectangular pieces first — gives them best positions)
6. interleave_large_small (alternate between largest and smallest)
7. random_permutation × N (Monte Carlo starts)

Then applies coordinate descent post-refinement to the best result.

Usage:
    conda activate nester
    python scripts/gpu_improved_placement.py
"""

import sys
import time
import random
import numpy as np
import importlib
import importlib.util
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

_spec = importlib.util.spec_from_file_location(
    "gpu_nesting_runner",
    PROJECT_ROOT / "backend/backend/services/gpu_nesting_runner.py",
)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

load_pieces_for_material = _mod.load_pieces_for_material
GPUPacker = _mod.GPUPacker

_spec_cd = importlib.util.spec_from_file_location(
    "gpu_coordinate_descent_experiment",
    PROJECT_ROOT / "scripts/gpu_coordinate_descent_experiment.py",
)
_mod_cd = importlib.util.module_from_spec(_spec_cd)
_spec_cd.loader.exec_module(_mod_cd)
_mod_cd.init()

place_with_positions = _mod_cd.place_with_positions
rebuild_container = _mod_cd.rebuild_container
coordinate_descent = _mod_cd.coordinate_descent

OUTPUT_DIR = PROJECT_ROOT / "experiment_results" / "gpu_vs_cpu_v2"

PATTERNS = {
    'A': {
        'name': 'Order2 / style 1',
        'dxf': str(PROJECT_ROOT / "uploads/patterns/275d4d77-d17e-4686-8ceb-d53cd67b83a4/style 1.dxf"),
        'rul': str(PROJECT_ROOT / "uploads/patterns/275d4d77-d17e-4686-8ceb-d53cd67b83a4/style 1.rul"),
        'file_type': 'aama', 'material': 'SO1',
        'sizes': ['46', '48', '50', '52', '54', '56', '58'],
        'width_inches': 60.0, 'swap_axes': True,
    },
    'B': {
        'name': 'C2509-0360 (3) / vt 201',
        'dxf': str(PROJECT_ROOT / "uploads/patterns/9ccb01fd-32b7-49a3-a0e6-628030a810bb/vt 201 (2).dxf"),
        'rul': None, 'file_type': 'vt_dxf', 'material': '201',
        'sizes': ['XS', 'S', 'M', 'L', 'XL', '2XL', '3XL'],
        'width_inches': 59.75,
    },
    'C': {
        'name': '25138 / check_count',
        'dxf': str(PROJECT_ROOT / "uploads/patterns/31bdd406-8314-42c5-8de9-26d837fc91ac/check_count.dxf"),
        'rul': str(PROJECT_ROOT / "uploads/patterns/31bdd406-8314-42c5-8de9-26d837fc91ac/check_count.rul"),
        'file_type': 'aama', 'material': 'SHELL',
        'sizes': ['XS', 'S', 'M', 'L', 'XL', '1X', '2X', '3X'],
        'width_inches': 54.25,
    },
}

TEST_MARKERS = [
    {'pattern': 'A', 'label': 'A-bc1',  'ratio': {'50': 1}},
    {'pattern': 'A', 'label': 'A-bc2',  'ratio': {'50': 1, '54': 1}},
    {'pattern': 'A', 'label': 'A-bc4',  'ratio': {'50': 2, '54': 1, '56': 1}},
    {'pattern': 'A', 'label': 'A-bc6',  'ratio': {'50': 5, '54': 1}},
    {'pattern': 'B', 'label': 'B-bc3',  'ratio': {'XS': 1, 'M': 1, 'L': 1}},
    {'pattern': 'B', 'label': 'B-bc5',  'ratio': {'XL': 1, '2XL': 1, '3XL': 3}},
    {'pattern': 'C', 'label': 'C-bc1',  'ratio': {'XS': 1}},
    {'pattern': 'C', 'label': 'C-bc2',  'ratio': {'M': 1, '3X': 1}},
    {'pattern': 'C', 'label': 'C-bc4',  'ratio': {'XL': 2, '2X': 1, '3X': 1}},
]

# Surface 10m results as benchmark target
SURF_10M = {
    'A-bc1': 75.45, 'A-bc2': 87.09, 'A-bc4': 87.55, 'A-bc6': 87.36,
    'B-bc1': 40.09, 'B-bc3': 93.96, 'B-bc5': 92.37,
    'C-bc1': 85.35, 'C-bc2': 87.70, 'C-bc4': 87.31,
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


def compute_perimeter_px(raster):
    """Approximate perimeter from raster (count edge pixels)."""
    h, w = raster.shape
    if h < 2 or w < 2:
        return 2 * (h + w)
    interior = raster[1:-1, 1:-1]
    padded = np.pad(raster, 1, mode='constant', constant_values=0)
    # Count transitions
    horiz = np.abs(np.diff(padded, axis=1))
    vert = np.abs(np.diff(padded, axis=0))
    return float(np.sum(horiz) + np.sum(vert))


def interleave_sort(pieces_list):
    """Sort pieces alternating between largest and smallest area."""
    by_area = sorted(pieces_list, key=lambda p: -p['area'])
    result = []
    left, right = 0, len(by_area) - 1
    toggle = True
    while left <= right:
        if toggle:
            result.append(by_area[left])
            left += 1
        else:
            result.append(by_area[right])
            right -= 1
        toggle = not toggle
    return result


# Named sort strategies
SORT_STRATEGIES = {
    'width_desc': lambda p: -p['raster'].shape[0],
    'area_desc': lambda p: -p['area'],
    'height_desc': lambda p: -p['raster'].shape[1],
    'perimeter_desc': lambda p: -compute_perimeter_px(p['raster']),
    'fill_asc': lambda p: p['area'] / max(1, p['raster'].shape[0] * p['raster'].shape[1]),
    'width_area': lambda p: (-p['raster'].shape[0], -p['area']),
}

N_RANDOM = 8  # Random permutations to try
CD_MAX_PASSES = 20  # More passes for refinement (not screening)


def main():
    GPU_SCALE = 0.30
    gpu_pieces = {}
    packers = {}

    for key, pat in PATTERNS.items():
        print(f"Loading {key}: {pat['name']}")
        pbs = load_pieces_for_material(
            pat['dxf'], pat['rul'], pat['material'], pat['sizes'],
            GPU_SCALE, file_type=pat['file_type'],
        )
        if pat.get('swap_axes'):
            swap_gpu_piece_axes(pbs)
        gpu_pieces[key] = pbs

        fabric_mm = pat['width_inches'] * 25.4
        sw_px = int(fabric_mm * GPU_SCALE)
        max_area = max(
            sum(p['area'] * p['demand'] for p in pbs.get(s, []))
            for s in pat['sizes'] if s in pbs
        )
        max_len = int((6 * max_area * 2) / sw_px) + 500
        packers[key] = (GPUPacker(sw_px, max_len), sw_px)

    print(f"\n{'='*130}")
    print(f"IMPROVED PLACEMENT: {len(SORT_STRATEGIES)} sort strategies + {N_RANDOM} random + CD({CD_MAX_PASSES} passes)")
    print(f"{'='*130}")

    results = []

    for tm in TEST_MARKERS:
        key = tm['pattern']
        pat = PATTERNS[key]
        label = tm['label']
        ratio = tm['ratio']
        bc = sum(ratio.values())
        packer, sw_px = packers[key]
        pbs = gpu_pieces[key]

        pieces_list = []
        for size, count in ratio.items():
            if count <= 0 or size not in pbs:
                continue
            for _ in range(count):
                for p in pbs[size]:
                    for _ in range(p['demand']):
                        pieces_list.append(p)

        print(f"\n--- {label}: bc={bc}, {len(pieces_list)} pieces ---")

        # Try all named sort strategies
        best_eff = 0
        best_sort = None
        best_placements = None
        best_area = 0
        best_cl = 0
        strategy_results = {}

        t0_all = time.time()

        for name, sort_fn in SORT_STRATEGIES.items():
            eff, ly, pl, area, cl = place_with_positions(
                pieces_list, packer, sw_px, GPU_SCALE, sort_key=sort_fn,
            )
            strategy_results[name] = eff
            if eff > best_eff:
                best_eff = eff
                best_sort = name
                best_placements = pl
                best_area = area
                best_cl = cl

        # Interleave strategy (custom sort, not key-based)
        interleaved = interleave_sort(pieces_list)
        packer.reset()
        # Place in interleave order (no sort_key, use pre-sorted list)
        eff_il, ly_il, pl_il, area_il, cl_il = place_with_positions(
            interleaved, packer, sw_px, GPU_SCALE, sort_key=lambda p: 0,  # preserve order
        )
        strategy_results['interleave'] = eff_il
        if eff_il > best_eff:
            best_eff = eff_il
            best_sort = 'interleave'
            best_placements = pl_il
            best_area = area_il
            best_cl = cl_il

        # Random permutations
        random.seed(42)
        best_rand_eff = 0
        for ri in range(N_RANDOM):
            shuffled = pieces_list.copy()
            random.shuffle(shuffled)
            eff_r, _, pl_r, area_r, cl_r = place_with_positions(
                shuffled, packer, sw_px, GPU_SCALE, sort_key=lambda p: 0,
            )
            if eff_r > best_rand_eff:
                best_rand_eff = eff_r
            if eff_r > best_eff:
                best_eff = eff_r
                best_sort = f'random_{ri}'
                best_placements = pl_r
                best_area = area_r
                best_cl = cl_r
        strategy_results['best_random'] = best_rand_eff

        t_sort = time.time() - t0_all

        # Report per-strategy results
        strat_str = ' | '.join(f"{k}={v*100:.1f}" for k, v in sorted(strategy_results.items(), key=lambda x: -x[1]))
        print(f"  Strategies: {strat_str}")
        print(f"  Best: {best_sort} = {best_eff*100:.2f}% ({t_sort*1000:.0f}ms total)")

        # Coordinate descent on best result
        rebuild_container(best_placements, packer)
        t0_cd = time.time()
        pl_cd, cl_cd, moves, passes = coordinate_descent(
            best_placements, packer, sw_px, best_area, max_passes=CD_MAX_PASSES,
        )
        t_cd = time.time() - t0_cd

        eff_cd = best_area / (sw_px * cl_cd) if cl_cd > 0 else 0
        delta_sort = (best_eff - strategy_results.get('width_desc', best_eff)) * 100
        delta_cd = (eff_cd - best_eff) * 100
        total_gain = delta_sort + delta_cd

        surf = SURF_10M.get(label, 0)
        gap = surf - eff_cd * 100

        print(f"  CD({passes}p, {moves}m): {eff_cd*100:.2f}% ({t_cd*1000:.0f}ms)")
        print(f"  Sort gain: {delta_sort:+.2f}%, CD gain: {delta_cd:+.2f}%, Total: {total_gain:+.2f}%")
        print(f"  vs Surface 10m: {gap:+.2f}% gap")

        # Save PNG
        png_bytes = packer.get_container_png(cl_cd)
        png_path = OUTPUT_DIR / f"{label}_gpu_improved.png"
        png_path.write_bytes(png_bytes)

        results.append({
            'label': label, 'bc': bc,
            'baseline': strategy_results.get('width_desc', 0) * 100,
            'best_sort': best_sort,
            'best_sort_eff': best_eff * 100,
            'cd_eff': eff_cd * 100,
            'sort_gain': delta_sort,
            'cd_gain': delta_cd,
            'total_gain': total_gain,
            'surf10m': surf,
            'gap': gap,
            'time_sort_ms': t_sort * 1000,
            'time_cd_ms': t_cd * 1000,
        })

    # Summary
    print(f"\n{'='*140}")
    print(f"SUMMARY: Improved Placement + CD vs Surface 10m")
    print(f"{'='*140}")
    print(f"{'Label':<10} {'BC':>3} {'Baseline':>9} {'BestSort':>12} {'SortEff':>8} "
          f"{'CD_Eff':>7} {'SortGain':>9} {'CDGain':>7} {'Total':>7} {'Surf10m':>8} {'Gap':>7}")
    print(f"{'-'*140}")

    for r in results:
        print(f"{r['label']:<10} {r['bc']:>3} {r['baseline']:>8.2f}% {r['best_sort']:>12} "
              f"{r['best_sort_eff']:>7.2f}% {r['cd_eff']:>6.2f}% "
              f"{r['sort_gain']:>+8.2f}% {r['cd_gain']:>+6.2f}% {r['total_gain']:>+6.2f}% "
              f"{r['surf10m']:>7.2f}% {r['gap']:>+6.2f}%")

    if results:
        avg_total = sum(r['total_gain'] for r in results) / len(results)
        avg_gap = sum(r['gap'] for r in results) / len(results)
        print(f"\nAvg total improvement over width_desc baseline: {avg_total:+.2f}%")
        print(f"Avg remaining gap vs Surface 10m: {avg_gap:+.2f}%")


if __name__ == '__main__':
    main()
