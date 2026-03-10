#!/usr/bin/env python3
"""
GPU Fast ILS: Rapid perturbation without intermediate CD.

Previous ILS was bottlenecked by CD (3 iters/10s). This version:
1. Removes K pieces, re-inserts by area desc (no CD between)
2. Tracks best strip length seen
3. 1000+ iterations in 60 seconds
4. Final CD only at the very end

Also tests "ruin-and-recreate" (destroy-rebuild):
- Remove ALL pieces right of a cut point
- Rebuild from that point with different ordering

Usage:
    conda activate nester
    python scripts/gpu_fast_ils.py
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
compute_strip_length = _mod_cd.compute_strip_length
find_best_position_for_piece = _mod_cd.find_best_position_for_piece

import cupy as cp

OUTPUT_DIR = PROJECT_ROOT / "experiment_results" / "gpu_fast_ils"

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

SURF_10M = {
    'A-bc1': 75.45, 'A-bc2': 87.09, 'A-bc4': 87.55, 'A-bc6': 87.36,
    'B-bc1': 40.09, 'B-bc3': 93.96, 'B-bc5': 92.37,
    'C-bc1': 85.35, 'C-bc2': 87.70, 'C-bc4': 87.31,
}

SORT_STRATEGIES = {
    'width_desc': lambda p: -p['raster'].shape[0],
    'area_desc': lambda p: -p['area'],
    'height_desc': lambda p: -p['raster'].shape[1],
    'perimeter_desc': lambda p: -(np.sum(np.abs(np.diff(np.pad(p['raster'], 1, 'constant'), axis=1))) +
                                   np.sum(np.abs(np.diff(np.pad(p['raster'], 1, 'constant'), axis=0)))),
}


def swap_gpu_piece_axes(pieces_by_size):
    for size, pieces in pieces_by_size.items():
        for p in pieces:
            p['vertices_mm'] = [(y, x) for x, y in p['vertices_mm']]
            p['raster'] = p['raster'].T.copy()
            p['raster_gpu'] = cp.asarray(p['raster'])
            p['raster_180'] = np.rot90(p['raster'], 2).copy()
            p['raster_180_gpu'] = cp.asarray(p['raster_180'])


def fast_ils(placements, packer, strip_width_px, placed_area,
             time_limit=60.0, perturb_frac=0.33):
    """
    Fast ILS: perturbation without intermediate CD.

    Each iteration:
    1. Copy best solution
    2. Remove K random pieces (sorted by x descending = rightmost first)
    3. Re-insert removed pieces sorted by area desc (largest first get best spots)
    4. If strip is shorter, accept

    No CD between iterations — pure perturbation search.
    """
    n = len(placements)
    k = max(2, int(n * perturb_frac))

    best_pl = [dict(p) for p in placements]
    best_length = compute_strip_length(best_pl)

    iterations = 0
    improvements = 0
    t0 = time.time()

    while time.time() - t0 < time_limit:
        iterations += 1

        # Start from best
        current = [dict(p) for p in best_pl]
        packer.reset()
        for pl in current:
            packer.place(pl['raster'], pl['x'], pl['y'])

        # Select K pieces to remove (biased toward rightmost = most wasteful)
        # Mix of strategies: 50% random, 50% rightmost
        if random.random() < 0.5:
            remove_idx = random.sample(range(n), k)
        else:
            # Remove pieces from the right half (most room for improvement)
            sorted_by_right = sorted(range(n),
                key=lambda i: -(current[i]['x'] + current[i]['raster'].shape[1]))
            # Pick from the rightmost 60% with some randomness
            pool = sorted_by_right[:max(k, int(n * 0.6))]
            remove_idx = random.sample(pool, k)

        # Remove selected pieces from container
        for idx in remove_idx:
            pl = current[idx]
            x, y = pl['x'], pl['y']
            r = pl['raster']
            ph, pw = r.shape
            packer.container[y:y+ph, x:x+pw] -= r
        packer.container = cp.maximum(packer.container, 0)

        # Current length of remaining pieces
        remaining = set(range(n)) - set(remove_idx)
        current_length = 0
        for i in remaining:
            pl = current[i]
            current_length = max(current_length, pl['x'] + pl['raster'].shape[1])

        # Re-insert: sort removed pieces by area desc (largest first)
        removed_sorted = sorted(remove_idx, key=lambda i: -current[i]['piece']['area'])

        for idx in removed_sorted:
            piece = current[idx]['piece']
            best_pos = None
            best_raster = None
            best_right = float('inf')

            for try_raster in [piece['raster_gpu'], piece['raster_180_gpu']]:
                pos = find_best_position_for_piece(
                    packer, try_raster, strip_width_px, current_length
                )
                if pos is None:
                    continue
                x, y = pos
                right = x + try_raster.shape[1]
                if right < best_right:
                    best_right = right
                    best_pos = (x, y)
                    best_raster = try_raster

            if best_pos is not None:
                x, y = best_pos
                packer.place(best_raster, x, y)
                current[idx] = {
                    'piece': piece,
                    'x': x, 'y': y,
                    'raster': best_raster,
                    'rotated': not cp.array_equal(best_raster, piece['raster_gpu']),
                }
                current_length = max(current_length, x + best_raster.shape[1])
            else:
                # Can't place — rebuild from scratch
                packer.place(current[idx]['raster'], current[idx]['x'], current[idx]['y'])
                current_length = max(current_length,
                    current[idx]['x'] + current[idx]['raster'].shape[1])

        # Check if better
        new_length = compute_strip_length(current)
        if new_length < best_length:
            best_length = new_length
            best_pl = [dict(p) for p in current]
            improvements += 1

    elapsed = time.time() - t0
    best_eff = placed_area / (strip_width_px * best_length) if best_length > 0 else 0
    return best_pl, best_length, best_eff, iterations, improvements, elapsed


def ruin_and_recreate(pieces_list, packer, strip_width_px, gpu_scale,
                      time_limit=60.0, n_sort_strategies=4):
    """
    Ruin-and-recreate: full restarts with different orderings.

    Unlike multi-start which uses purely random orderings, this uses
    "guided random" orderings: mostly sorted by a key, with some pieces
    randomly swapped.

    Each restart: BLF placement (no CD). Final CD at the end.
    """
    best_eff = 0
    best_pl = None
    best_area = 0
    best_cl = 0

    sort_fns = list(SORT_STRATEGIES.values())

    iterations = 0
    t0 = time.time()

    while time.time() - t0 < time_limit:
        iterations += 1

        # Pick a base sort strategy
        base_sort = random.choice(sort_fns)

        # Sort, then apply random swaps (controlled perturbation)
        sorted_pieces = sorted(pieces_list, key=base_sort)

        # Swap 10-30% of adjacent pairs
        n_swaps = random.randint(len(sorted_pieces) // 10, len(sorted_pieces) // 3)
        for _ in range(n_swaps):
            i = random.randint(0, len(sorted_pieces) - 2)
            sorted_pieces[i], sorted_pieces[i+1] = sorted_pieces[i+1], sorted_pieces[i]

        # BLF placement (no sort_key since we pre-sorted)
        eff, _, pl, area, cl = place_with_positions(
            sorted_pieces, packer, strip_width_px, gpu_scale,
            sort_key=lambda p: 0,  # preserve pre-sorted order
        )

        if eff > best_eff:
            best_eff = eff
            best_pl = pl
            best_area = area
            best_cl = cl

    elapsed = time.time() - t0
    return best_pl, best_cl, best_eff, best_area, iterations, elapsed


def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
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

    print(f"\n{'='*140}")
    print(f"FAST ILS + RUIN-AND-RECREATE: 60s per marker")
    print(f"{'='*140}")

    results = []

    for tm in TEST_MARKERS:
        key = tm['pattern']
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

        n = len(pieces_list)
        print(f"\n--- {label}: bc={bc}, {n} pieces ---")

        # Baseline: best sort + CD
        best_init_eff = 0
        best_init_pl = None
        best_init_area = 0
        best_init_sort = ''

        for name, sort_fn in SORT_STRATEGIES.items():
            eff, _, pl, area, cl = place_with_positions(
                pieces_list, packer, sw_px, GPU_SCALE, sort_key=sort_fn,
            )
            if eff > best_init_eff:
                best_init_eff = eff
                best_init_sort = name
                best_init_pl = pl
                best_init_area = area

        rebuild_container(best_init_pl, packer)
        pl_cd, cl_cd, _, _ = coordinate_descent(
            best_init_pl, packer, sw_px, best_init_area, max_passes=20,
        )
        eff_cd = best_init_area / (sw_px * cl_cd) if cl_cd > 0 else 0
        print(f"  Baseline: {eff_cd*100:.2f}% ({best_init_sort}+CD)")

        # Track best across all approaches
        overall_best_eff = eff_cd
        overall_best_pl = [dict(p) for p in pl_cd]
        overall_best_area = best_init_area

        # Approach 1: Fast ILS (perturbation without CD)
        random.seed(42)
        rebuild_container(pl_cd, packer)
        pl_ils, _, eff_ils, iters_ils, impr_ils, t_ils = fast_ils(
            [dict(p) for p in pl_cd], packer, sw_px, best_init_area,
            time_limit=30.0, perturb_frac=0.33,
        )
        print(f"  FastILS(30s,k=33%): {eff_ils*100:.2f}% ({iters_ils} iters, {impr_ils} impr)")

        if eff_ils > overall_best_eff:
            overall_best_eff = eff_ils
            overall_best_pl = pl_ils
            overall_best_area = best_init_area

        # Approach 2: Ruin-and-recreate (guided random restarts)
        random.seed(42)
        pl_rnr, cl_rnr, eff_rnr, area_rnr, iters_rnr, t_rnr = ruin_and_recreate(
            pieces_list, packer, sw_px, GPU_SCALE, time_limit=30.0,
        )
        print(f"  Ruin&Recreate(30s): {eff_rnr*100:.2f}% ({iters_rnr} restarts)")

        if eff_rnr > overall_best_eff:
            overall_best_eff = eff_rnr
            overall_best_pl = pl_rnr
            overall_best_area = area_rnr

        # Final: CD on overall best
        rebuild_container(overall_best_pl, packer)
        pl_final, cl_final, moves_f, passes_f = coordinate_descent(
            overall_best_pl, packer, sw_px, overall_best_area, max_passes=50,
        )
        eff_final = overall_best_area / (sw_px * cl_final) if cl_final > 0 else 0

        surf = SURF_10M.get(label, 0)
        gap_base = surf - eff_cd * 100
        gap_final = surf - eff_final * 100
        improvement = eff_final * 100 - eff_cd * 100

        print(f"  Final+CD(50): {eff_final*100:.2f}% (gap={gap_final:+.2f}%, Δ={improvement:+.2f}%)")

        png_bytes = packer.get_container_png(cl_final)
        (OUTPUT_DIR / f"{label}_fast_ils.png").write_bytes(png_bytes)

        results.append({
            'label': label, 'bc': bc, 'n': n,
            'baseline': eff_cd * 100,
            'fast_ils': eff_ils * 100,
            'rnr': eff_rnr * 100,
            'final': eff_final * 100,
            'surf': surf,
            'gap_base': gap_base,
            'gap_final': gap_final,
            'improvement': improvement,
            'iters_ils': iters_ils,
            'iters_rnr': iters_rnr,
        })

    # Summary
    print(f"\n{'='*150}")
    print(f"SUMMARY: Fast ILS + Ruin&Recreate vs Baseline")
    print(f"{'='*150}")
    print(f"{'Label':<10} {'BC':>3} {'N':>4} {'Base':>8} {'F-ILS':>8} {'R&R':>8} "
          f"{'Final':>8} {'Surf':>8} {'Gap_B':>7} {'Gap_F':>7} {'Δ':>7} "
          f"{'#ILS':>6} {'#R&R':>6}")
    print(f"{'-'*150}")

    for r in results:
        print(f"{r['label']:<10} {r['bc']:>3} {r['n']:>4} "
              f"{r['baseline']:>7.2f}% {r['fast_ils']:>7.2f}% {r['rnr']:>7.2f}% "
              f"{r['final']:>7.2f}% {r['surf']:>7.2f}% "
              f"{r['gap_base']:>+6.2f}% {r['gap_final']:>+6.2f}% "
              f"{r['improvement']:>+6.2f}% "
              f"{r['iters_ils']:>6} {r['iters_rnr']:>6}")

    if results:
        avg_base = sum(r['gap_base'] for r in results) / len(results)
        avg_final = sum(r['gap_final'] for r in results) / len(results)
        avg_impr = sum(r['improvement'] for r in results) / len(results)
        print(f"\nAvg gap baseline: {avg_base:+.2f}%  |  Avg gap final: {avg_final:+.2f}%")
        print(f"Avg improvement: {avg_impr:+.2f}%")


if __name__ == '__main__':
    main()
