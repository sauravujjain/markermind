#!/usr/bin/env python3
"""
GPU Deep Refinement Experiment.

Tests three approaches to close the GPU-CPU nesting gap:

1. Resolution sweep: 0.30 → 0.40 → 0.50 px/mm
   - Higher resolution reduces rasterization error, pieces fit tighter
   - More VRAM and slower, but OK for finetune step

2. Multi-start CD: 100 random orderings + 6 named sorts → CD(50 passes) on top 5
   - BLF is deterministic for a given ordering
   - Different orderings produce very different packings
   - CD from 5 diverse starts explores more of the solution space

3. SA-CD: Simulated Annealing Coordinate Descent
   - Standard CD only accepts strict improvements → gets stuck in local optima
   - SA-CD accepts uphill moves with Boltzmann probability → explores more
   - Temperature decays over passes

Usage:
    conda activate nester
    python scripts/gpu_deep_refinement.py
"""

import sys
import time
import random
import math
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

import cupy as _cp

OUTPUT_DIR = PROJECT_ROOT / "experiment_results" / "gpu_deep_refinement"

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

# Focus on markers with largest gaps
TEST_MARKERS = [
    {'pattern': 'A', 'label': 'A-bc2',  'ratio': {'50': 1, '54': 1}},
    {'pattern': 'A', 'label': 'A-bc4',  'ratio': {'50': 2, '54': 1, '56': 1}},
    {'pattern': 'B', 'label': 'B-bc3',  'ratio': {'XS': 1, 'M': 1, 'L': 1}},
    {'pattern': 'C', 'label': 'C-bc1',  'ratio': {'XS': 1}},
    {'pattern': 'C', 'label': 'C-bc4',  'ratio': {'XL': 2, '2X': 1, '3X': 1}},
]

SURF_10M = {
    'A-bc1': 75.45, 'A-bc2': 87.09, 'A-bc4': 87.55, 'A-bc6': 87.36,
    'B-bc3': 93.96, 'B-bc5': 92.37,
    'C-bc1': 85.35, 'C-bc2': 87.70, 'C-bc4': 87.31,
}

SORT_STRATEGIES = {
    'width_desc': lambda p: -p['raster'].shape[0],
    'area_desc': lambda p: -p['area'],
    'height_desc': lambda p: -p['raster'].shape[1],
    'perimeter_desc': lambda p: -(np.sum(np.abs(np.diff(np.pad(p['raster'], 1, 'constant'), axis=1))) +
                                   np.sum(np.abs(np.diff(np.pad(p['raster'], 1, 'constant'), axis=0)))),
    'fill_asc': lambda p: p['area'] / max(1, p['raster'].shape[0] * p['raster'].shape[1]),
    'width_area': lambda p: (-p['raster'].shape[0], -p['area']),
}


def swap_gpu_piece_axes(pieces_by_size):
    for size, pieces in pieces_by_size.items():
        for p in pieces:
            p['vertices_mm'] = [(y, x) for x, y in p['vertices_mm']]
            p['raster'] = p['raster'].T.copy()
            p['raster_gpu'] = _cp.asarray(p['raster'])
            p['raster_180'] = np.rot90(p['raster'], 2).copy()
            p['raster_180_gpu'] = _cp.asarray(p['raster_180'])


def sa_coordinate_descent(placements, packer, strip_width_px, placed_area,
                           max_passes=50, t_start=5.0, t_end=0.1):
    """
    Simulated Annealing variant of Coordinate Descent.

    Differences from standard CD:
    - Accepts worse moves with probability exp(-delta/T)
    - Temperature decays linearly over passes
    - Tries random piece order (not just right-to-left)
    """
    total_moves = 0
    passes = 0
    current_length = compute_strip_length(placements)
    best_length = current_length
    best_placements = [dict(pl) for pl in placements]

    for pass_num in range(max_passes):
        passes += 1
        t = t_start + (t_end - t_start) * pass_num / max(1, max_passes - 1)

        # Randomized piece order
        order = list(range(len(placements)))
        random.shuffle(order)

        moves = 0
        for idx in order:
            pl = placements[idx]
            old_x, old_y = pl['x'], pl['y']
            old_raster = pl['raster']
            ph, pw = old_raster.shape

            # Remove piece
            packer.container[old_y:old_y+ph, old_x:old_x+pw] -= old_raster
            packer.container = _cp.maximum(packer.container, 0)

            piece = pl['piece']
            best_pos = None
            best_raster_try = None
            best_right_edge = float('inf')

            for try_raster in [piece['raster_gpu'], piece['raster_180_gpu']]:
                pos = find_best_position_for_piece(
                    packer, try_raster, strip_width_px, current_length
                )
                if pos is None:
                    continue
                tx, ty = pos
                right_edge = tx + try_raster.shape[1]
                if right_edge < best_right_edge:
                    best_right_edge = right_edge
                    best_pos = (tx, ty)
                    best_raster_try = try_raster

            if best_pos is not None:
                nx, ny = best_pos
                new_right = nx + best_raster_try.shape[1]

                # Compute strip length without this piece
                other_length = 0
                for j, pl2 in enumerate(placements):
                    if j == idx:
                        continue
                    other_length = max(other_length, pl2['x'] + pl2['raster'].shape[1])

                new_length_candidate = max(other_length, new_right)
                delta = new_length_candidate - current_length  # positive = worse

                # SA acceptance: always accept improvements, probabilistically accept worse
                accept = False
                if delta <= 0:
                    accept = True
                elif t > 0:
                    prob = math.exp(-delta / t)
                    accept = random.random() < prob

                if accept:
                    packer.place(best_raster_try, nx, ny)
                    placements[idx] = {
                        'piece': piece,
                        'x': nx, 'y': ny,
                        'raster': best_raster_try,
                        'rotated': not _cp.array_equal(best_raster_try, piece['raster_gpu']),
                    }
                    current_length = new_length_candidate
                    if nx != old_x or ny != old_y or best_raster_try is not old_raster:
                        moves += 1
                else:
                    # Reject — put back
                    packer.place(old_raster, old_x, old_y)
            else:
                packer.place(old_raster, old_x, old_y)

        total_moves += moves

        # Track best ever (SA may go uphill temporarily)
        if current_length < best_length:
            best_length = current_length
            best_placements = [dict(pl) for pl in placements]

    # Restore best solution
    placements[:] = best_placements
    rebuild_container(placements, packer)
    current_length = best_length

    return placements, current_length, total_moves, passes


def experiment_1_resolution(patterns_data, test_markers):
    """Test impact of higher rasterization resolution."""
    print(f"\n{'='*130}")
    print("EXPERIMENT 1: RESOLUTION SWEEP")
    print(f"{'='*130}")

    resolutions = [0.30, 0.40, 0.50]
    results = {}

    for gpu_scale in resolutions:
        print(f"\n--- Resolution: {gpu_scale} px/mm ---")
        # Load pieces at this resolution
        pieces_cache = {}
        packers = {}

        for key, pat in PATTERNS.items():
            pbs = load_pieces_for_material(
                pat['dxf'], pat['rul'], pat['material'], pat['sizes'],
                gpu_scale, file_type=pat['file_type'],
            )
            if pat.get('swap_axes'):
                swap_gpu_piece_axes(pbs)
            pieces_cache[key] = pbs

            fabric_mm = pat['width_inches'] * 25.4
            sw_px = int(fabric_mm * gpu_scale)
            max_area = max(
                sum(p['area'] * p['demand'] for p in pbs.get(s, []))
                for s in pat['sizes'] if s in pbs
            )
            max_len = int((6 * max_area * 2) / sw_px) + 500
            packers[key] = (GPUPacker(sw_px, max_len), sw_px)

        for tm in test_markers:
            key = tm['pattern']
            label = tm['label']
            ratio = tm['ratio']
            packer, sw_px = packers[key]
            pbs = pieces_cache[key]

            pieces_list = []
            for size, count in ratio.items():
                if count <= 0 or size not in pbs:
                    continue
                for _ in range(count):
                    for p in pbs[size]:
                        for _ in range(p['demand']):
                            pieces_list.append(p)

            # Best of named sorts
            best_eff = 0
            best_placements = None
            best_area = 0
            best_sort = ''

            for name, sort_fn in SORT_STRATEGIES.items():
                eff, _, pl, area, cl = place_with_positions(
                    pieces_list, packer, sw_px, gpu_scale, sort_key=sort_fn,
                )
                if eff > best_eff:
                    best_eff = eff
                    best_placements = pl
                    best_area = area
                    best_sort = name

            # CD on best
            if best_placements:
                rebuild_container(best_placements, packer)
                pl_cd, cl_cd, moves, passes = coordinate_descent(
                    best_placements, packer, sw_px, best_area, max_passes=20,
                )
                eff_cd = best_area / (sw_px * cl_cd) if cl_cd > 0 else 0
            else:
                eff_cd = best_eff

            surf = SURF_10M.get(label, 0)
            gap = surf - eff_cd * 100
            print(f"  {label}: {eff_cd*100:.2f}% (sort={best_sort}, gap={gap:+.2f}%)")

            results.setdefault(label, {})[gpu_scale] = {
                'eff': eff_cd * 100, 'gap': gap, 'sort': best_sort,
            }

    # Summary
    print(f"\n{'Label':<10}", end='')
    for s in resolutions:
        print(f"  {s:.2f}px/mm", end='')
    print(f"  {'Surf10m':>8}")

    for label in [tm['label'] for tm in test_markers]:
        print(f"{label:<10}", end='')
        for s in resolutions:
            r = results.get(label, {}).get(s, {})
            print(f"  {r.get('eff', 0):>8.2f}%", end='')
        print(f"  {SURF_10M.get(label, 0):>7.2f}%")

    return results


def experiment_2_multistart(patterns_data, test_markers):
    """Test multi-start: 100 random orderings + CD on top 5."""
    print(f"\n{'='*130}")
    print("EXPERIMENT 2: MULTI-START (100 random + 6 sorts → CD(50) on top 5)")
    print(f"{'='*130}")

    GPU_SCALE = 0.30
    N_RANDOM = 100
    TOP_N_CD = 5
    CD_PASSES = 50

    pieces_cache = {}
    packers = {}

    for key, pat in PATTERNS.items():
        pbs = load_pieces_for_material(
            pat['dxf'], pat['rul'], pat['material'], pat['sizes'],
            GPU_SCALE, file_type=pat['file_type'],
        )
        if pat.get('swap_axes'):
            swap_gpu_piece_axes(pbs)
        pieces_cache[key] = pbs

        fabric_mm = pat['width_inches'] * 25.4
        sw_px = int(fabric_mm * GPU_SCALE)
        max_area = max(
            sum(p['area'] * p['demand'] for p in pbs.get(s, []))
            for s in pat['sizes'] if s in pbs
        )
        max_len = int((6 * max_area * 2) / sw_px) + 500
        packers[key] = (GPUPacker(sw_px, max_len), sw_px)

    results = {}

    for tm in test_markers:
        key = tm['pattern']
        label = tm['label']
        ratio = tm['ratio']
        packer, sw_px = packers[key]
        pbs = pieces_cache[key]

        pieces_list = []
        for size, count in ratio.items():
            if count <= 0 or size not in pbs:
                continue
            for _ in range(count):
                for p in pbs[size]:
                    for _ in range(p['demand']):
                        pieces_list.append(p)

        print(f"\n--- {label}: {len(pieces_list)} pieces ---")
        t0 = time.time()

        # Phase 1: Screen all starts (named sorts + random)
        all_starts = []

        for name, sort_fn in SORT_STRATEGIES.items():
            eff, _, pl, area, cl = place_with_positions(
                pieces_list, packer, sw_px, GPU_SCALE, sort_key=sort_fn,
            )
            all_starts.append((eff, name, pl, area, cl))

        random.seed(42)
        for ri in range(N_RANDOM):
            shuffled = pieces_list.copy()
            random.shuffle(shuffled)
            eff, _, pl, area, cl = place_with_positions(
                shuffled, packer, sw_px, GPU_SCALE, sort_key=lambda p: 0,
            )
            all_starts.append((eff, f'rand_{ri}', pl, area, cl))

        # Sort by efficiency descending
        all_starts.sort(key=lambda x: -x[0])
        t_screen = time.time() - t0

        top5_effs = [s[0]*100 for s in all_starts[:5]]
        top5_names = [s[1] for s in all_starts[:5]]
        print(f"  Screen ({len(all_starts)} starts, {t_screen*1000:.0f}ms): "
              f"top5 = {[f'{e:.1f}%' for e in top5_effs]}")
        print(f"  Top5 sorts: {top5_names}")

        # Phase 2: CD on top N
        best_cd_eff = 0
        best_cd_label = ''
        t0_cd = time.time()

        for rank, (eff, name, pl, area, cl) in enumerate(all_starts[:TOP_N_CD]):
            rebuild_container(pl, packer)
            pl_cd, cl_cd, moves, passes = coordinate_descent(
                pl, packer, sw_px, area, max_passes=CD_PASSES,
            )
            eff_cd = area / (sw_px * cl_cd) if cl_cd > 0 else 0
            if eff_cd > best_cd_eff:
                best_cd_eff = eff_cd
                best_cd_label = name
            print(f"    CD #{rank+1} ({name}): {eff*100:.2f}% → {eff_cd*100:.2f}% "
                  f"({moves} moves, {passes} passes)")

        t_cd = time.time() - t0_cd

        surf = SURF_10M.get(label, 0)
        gap = surf - best_cd_eff * 100
        baseline_eff = all_starts[0][0] * 100  # best BLF without CD

        print(f"  Best: {best_cd_label} → {best_cd_eff*100:.2f}% "
              f"(gap={gap:+.2f}%, CD time={t_cd*1000:.0f}ms)")

        results[label] = {
            'baseline': baseline_eff,
            'best_cd': best_cd_eff * 100,
            'best_sort': best_cd_label,
            'surf': surf,
            'gap': gap,
            'time_screen_ms': t_screen * 1000,
            'time_cd_ms': t_cd * 1000,
        }

    return results


def experiment_3_sa_cd(patterns_data, test_markers):
    """Test SA-CD: Simulated Annealing Coordinate Descent."""
    print(f"\n{'='*130}")
    print("EXPERIMENT 3: SA-CD (Simulated Annealing Coordinate Descent, 50 passes)")
    print(f"{'='*130}")

    GPU_SCALE = 0.30

    pieces_cache = {}
    packers = {}

    for key, pat in PATTERNS.items():
        pbs = load_pieces_for_material(
            pat['dxf'], pat['rul'], pat['material'], pat['sizes'],
            GPU_SCALE, file_type=pat['file_type'],
        )
        if pat.get('swap_axes'):
            swap_gpu_piece_axes(pbs)
        pieces_cache[key] = pbs

        fabric_mm = pat['width_inches'] * 25.4
        sw_px = int(fabric_mm * GPU_SCALE)
        max_area = max(
            sum(p['area'] * p['demand'] for p in pbs.get(s, []))
            for s in pat['sizes'] if s in pbs
        )
        max_len = int((6 * max_area * 2) / sw_px) + 500
        packers[key] = (GPUPacker(sw_px, max_len), sw_px)

    results = {}

    for tm in test_markers:
        key = tm['pattern']
        label = tm['label']
        ratio = tm['ratio']
        packer, sw_px = packers[key]
        pbs = pieces_cache[key]

        pieces_list = []
        for size, count in ratio.items():
            if count <= 0 or size not in pbs:
                continue
            for _ in range(count):
                for p in pbs[size]:
                    for _ in range(p['demand']):
                        pieces_list.append(p)

        print(f"\n--- {label}: {len(pieces_list)} pieces ---")

        # Best named sort as starting point
        best_eff = 0
        best_placements = None
        best_area = 0
        best_sort = ''

        for name, sort_fn in SORT_STRATEGIES.items():
            eff, _, pl, area, cl = place_with_positions(
                pieces_list, packer, sw_px, GPU_SCALE, sort_key=sort_fn,
            )
            if eff > best_eff:
                best_eff = eff
                best_placements = pl
                best_area = area
                best_sort = name

        print(f"  BLF: {best_eff*100:.2f}% ({best_sort})")

        # Standard CD for comparison
        rebuild_container(best_placements, packer)
        pl_std = [dict(pl) for pl in best_placements]
        pl_std_cd, cl_std, moves_std, passes_std = coordinate_descent(
            pl_std, packer, sw_px, best_area, max_passes=50,
        )
        eff_std = best_area / (sw_px * cl_std) if cl_std > 0 else 0
        print(f"  Std CD(50): {eff_std*100:.2f}% ({moves_std} moves)")

        # SA-CD
        rebuild_container(best_placements, packer)
        pl_sa = [dict(pl) for pl in best_placements]
        t0 = time.time()
        pl_sa_cd, cl_sa, moves_sa, passes_sa = sa_coordinate_descent(
            pl_sa, packer, sw_px, best_area,
            max_passes=50, t_start=10.0, t_end=0.1,
        )
        t_sa = time.time() - t0
        eff_sa = best_area / (sw_px * cl_sa) if cl_sa > 0 else 0

        surf = SURF_10M.get(label, 0)
        gap_std = surf - eff_std * 100
        gap_sa = surf - eff_sa * 100

        print(f"  SA-CD(50): {eff_sa*100:.2f}% ({moves_sa} moves, {t_sa*1000:.0f}ms)")
        print(f"  StdCD gap={gap_std:+.2f}%  SA-CD gap={gap_sa:+.2f}%")

        results[label] = {
            'blf': best_eff * 100,
            'std_cd': eff_std * 100,
            'sa_cd': eff_sa * 100,
            'surf': surf,
            'gap_std': gap_std,
            'gap_sa': gap_sa,
        }

    return results


def experiment_4_combined(patterns_data, test_markers):
    """
    Combined best approach: higher resolution + multi-start + CD.

    Use 0.50 px/mm + 100 random starts + 50-pass CD on top 3.
    This is the "generous GPU compute" finetune approach.
    """
    print(f"\n{'='*130}")
    print("EXPERIMENT 4: COMBINED (0.50 px/mm + 100 random + CD(50) on top 3)")
    print(f"{'='*130}")

    GPU_SCALE = 0.50
    N_RANDOM = 100
    TOP_N_CD = 3
    CD_PASSES = 50

    pieces_cache = {}
    packers = {}

    for key, pat in PATTERNS.items():
        pbs = load_pieces_for_material(
            pat['dxf'], pat['rul'], pat['material'], pat['sizes'],
            GPU_SCALE, file_type=pat['file_type'],
        )
        if pat.get('swap_axes'):
            swap_gpu_piece_axes(pbs)
        pieces_cache[key] = pbs

        fabric_mm = pat['width_inches'] * 25.4
        sw_px = int(fabric_mm * GPU_SCALE)
        max_area = max(
            sum(p['area'] * p['demand'] for p in pbs.get(s, []))
            for s in pat['sizes'] if s in pbs
        )
        max_len = int((6 * max_area * 2) / sw_px) + 500
        packers[key] = (GPUPacker(sw_px, max_len), sw_px)

    results = {}

    for tm in test_markers:
        key = tm['pattern']
        label = tm['label']
        ratio = tm['ratio']
        packer, sw_px = packers[key]
        pbs = pieces_cache[key]

        pieces_list = []
        for size, count in ratio.items():
            if count <= 0 or size not in pbs:
                continue
            for _ in range(count):
                for p in pbs[size]:
                    for _ in range(p['demand']):
                        pieces_list.append(p)

        print(f"\n--- {label}: {len(pieces_list)} pieces, strip_width={sw_px}px ---")
        t0_total = time.time()

        # Screen phase
        all_starts = []
        for name, sort_fn in SORT_STRATEGIES.items():
            eff, _, pl, area, cl = place_with_positions(
                pieces_list, packer, sw_px, GPU_SCALE, sort_key=sort_fn,
            )
            all_starts.append((eff, name, pl, area, cl))

        random.seed(42)
        for ri in range(N_RANDOM):
            shuffled = pieces_list.copy()
            random.shuffle(shuffled)
            eff, _, pl, area, cl = place_with_positions(
                shuffled, packer, sw_px, GPU_SCALE, sort_key=lambda p: 0,
            )
            all_starts.append((eff, f'rand_{ri}', pl, area, cl))

        all_starts.sort(key=lambda x: -x[0])
        t_screen = time.time() - t0_total

        print(f"  Screen: top3 = {[f'{s[1]}={s[0]*100:.1f}%' for s in all_starts[:3]]} "
              f"({t_screen*1000:.0f}ms)")

        # CD on top N
        best_cd_eff = 0
        best_cd_label = ''
        t0_cd = time.time()

        for rank, (eff, name, pl, area, cl) in enumerate(all_starts[:TOP_N_CD]):
            rebuild_container(pl, packer)
            pl_cd, cl_cd, moves, passes = coordinate_descent(
                pl, packer, sw_px, area, max_passes=CD_PASSES,
            )
            eff_cd = area / (sw_px * cl_cd) if cl_cd > 0 else 0
            if eff_cd > best_cd_eff:
                best_cd_eff = eff_cd
                best_cd_label = name

            # Save PNG for best
            if eff_cd == best_cd_eff:
                png_bytes = packer.get_container_png(cl_cd)
                png_path = OUTPUT_DIR / f"{label}_combined.png"
                png_path.write_bytes(png_bytes)

            print(f"    CD #{rank+1} ({name}): {eff*100:.2f}% → {eff_cd*100:.2f}% ({moves}m, {passes}p)")

        t_cd = time.time() - t0_cd
        t_total = time.time() - t0_total

        surf = SURF_10M.get(label, 0)
        gap = surf - best_cd_eff * 100

        print(f"  RESULT: {best_cd_eff*100:.2f}% (gap={gap:+.2f}%, "
              f"total={t_total*1000:.0f}ms)")

        results[label] = {
            'eff': best_cd_eff * 100,
            'surf': surf,
            'gap': gap,
            'time_total_ms': t_total * 1000,
        }

    return results


def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # Run experiments sequentially
    r1 = experiment_1_resolution(PATTERNS, TEST_MARKERS)
    r2 = experiment_2_multistart(PATTERNS, TEST_MARKERS)
    r3 = experiment_3_sa_cd(PATTERNS, TEST_MARKERS)
    r4 = experiment_4_combined(PATTERNS, TEST_MARKERS)

    # Final comparison
    print(f"\n{'='*140}")
    print("FINAL COMPARISON: All Approaches vs Surface 10m")
    print(f"{'='*140}")
    print(f"{'Label':<10} {'Surf10m':>8} {'BLF030':>8} {'BLF050':>8} "
          f"{'MS+CD':>8} {'SA-CD':>8} {'Combined':>9} {'BestGap':>8}")
    print(f"{'-'*140}")

    for tm in TEST_MARKERS:
        label = tm['label']
        surf = SURF_10M.get(label, 0)
        blf030 = r1.get(label, {}).get(0.30, {}).get('eff', 0)
        blf050 = r1.get(label, {}).get(0.50, {}).get('eff', 0)
        ms_cd = r2.get(label, {}).get('best_cd', 0)
        sa_cd = r3.get(label, {}).get('sa_cd', 0)
        combined = r4.get(label, {}).get('eff', 0)

        best = max(blf030, blf050, ms_cd, sa_cd, combined)
        best_gap = surf - best

        print(f"{label:<10} {surf:>7.2f}% {blf030:>7.2f}% {blf050:>7.2f}% "
              f"{ms_cd:>7.2f}% {sa_cd:>7.2f}% {combined:>8.2f}% {best_gap:>+7.2f}%")


if __name__ == '__main__':
    main()
