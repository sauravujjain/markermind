#!/usr/bin/env python3
"""
GPU Iterated Local Search (ILS) Experiment.

The fundamental limitation of BLF + CD is that it's stuck in a single
local optimum basin. CD can only do small moves and never extends the strip.

ILS breaks out of local optima:
1. Start with best BLF+Contact placement
2. PERTURB: remove K random pieces from the solution
3. RE-INSERT: place removed pieces using best available position
4. LOCAL SEARCH: CD to convergence
5. ACCEPT: if result is better than best-so-far, keep it
6. Repeat for T seconds

This mirrors Spyrrow's GLS (Guided Local Search) approach but runs on GPU.
Each ILS iteration takes ~0.5-2s on GPU vs ~30s on CPU, so we get
many more restarts in the same time budget.

Also tests:
- Different perturbation sizes (K = 20%, 33%, 50% of pieces)
- Contact-aware re-insertion vs standard BLF re-insertion
- Time budgets (10s, 30s, 60s)

Usage:
    conda activate nester
    python scripts/gpu_iterated_local_search.py
"""

import sys
import time
import random
import copy
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
from cupyx.scipy.signal import fftconvolve as fftconvolve_gpu

OUTPUT_DIR = PROJECT_ROOT / "experiment_results" / "gpu_ils"

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


def copy_placements(placements):
    """Deep-copy placement list (dicts with CuPy arrays)."""
    return [dict(pl) for pl in placements]


def remove_pieces_from_container(placements, indices, packer):
    """Remove specific pieces from the container raster."""
    for idx in indices:
        pl = placements[idx]
        x, y = pl['x'], pl['y']
        raster = pl['raster']
        ph, pw = raster.shape
        packer.container[y:y+ph, x:x+pw] -= raster
    packer.container = cp.maximum(packer.container, 0)


def reinsert_pieces(placements, removed_indices, packer, strip_width_px):
    """
    Re-insert removed pieces using BLF (find best position).
    Returns updated placements and new strip length.
    """
    # Current length from non-removed pieces
    remaining_indices = set(range(len(placements))) - set(removed_indices)
    current_length = 0
    for i in remaining_indices:
        pl = placements[i]
        current_length = max(current_length, pl['x'] + pl['raster'].shape[1])

    # Re-insert in random order (the randomness is key for diversity)
    random.shuffle(removed_indices)

    for idx in removed_indices:
        pl = placements[idx]
        piece = pl['piece']

        best_pos = None
        best_raster = None
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
                best_raster = try_raster

        if best_pos is not None:
            nx, ny = best_pos
            packer.place(best_raster, nx, ny)
            placements[idx] = {
                'piece': piece,
                'x': nx, 'y': ny,
                'raster': best_raster,
                'rotated': not cp.array_equal(best_raster, piece['raster_gpu']),
            }
            current_length = max(current_length, nx + best_raster.shape[1])
        # If can't place, leave at original position (will be rebuilt later)

    return placements, current_length


def ils_search(placements, packer, strip_width_px, placed_area, gpu_scale,
               time_limit=30.0, perturb_frac=0.33, cd_passes=10):
    """
    Iterated Local Search on GPU.

    Args:
        placements: Initial placement solution
        packer: GPUPacker instance
        strip_width_px: Strip width in pixels
        placed_area: Total area of all placed pieces
        gpu_scale: Resolution scale
        time_limit: Time budget in seconds
        perturb_frac: Fraction of pieces to remove per perturbation
        cd_passes: Max CD passes per iteration
    """
    best_placements = copy_placements(placements)
    best_length = compute_strip_length(best_placements)
    best_eff = placed_area / (strip_width_px * best_length) if best_length > 0 else 0

    n_pieces = len(placements)
    k = max(2, int(n_pieces * perturb_frac))  # Number of pieces to perturb

    iterations = 0
    improvements = 0
    t0 = time.time()

    while time.time() - t0 < time_limit:
        iterations += 1

        # Start from best known solution
        current = copy_placements(best_placements)
        rebuild_container(current, packer)

        # Perturbation: remove K random pieces
        indices = list(range(n_pieces))
        removed = random.sample(indices, k)
        remove_pieces_from_container(current, removed, packer)

        # Re-insert removed pieces
        current, new_length = reinsert_pieces(current, removed, packer, strip_width_px)

        # Local search: CD
        current, cd_length, cd_moves, cd_passes_used = coordinate_descent(
            current, packer, strip_width_px, placed_area, max_passes=cd_passes,
        )

        # Accept if better
        cd_eff = placed_area / (strip_width_px * cd_length) if cd_length > 0 else 0
        if cd_eff > best_eff:
            best_eff = cd_eff
            best_length = cd_length
            best_placements = copy_placements(current)
            improvements += 1

    elapsed = time.time() - t0
    return best_placements, best_length, best_eff, iterations, improvements, elapsed


def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    GPU_SCALE = 0.30

    # Load pieces
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
    print(f"ITERATED LOCAL SEARCH: perturb + re-insert + CD")
    print(f"Time budget: 30s per marker, perturb_frac: 33%")
    print(f"{'='*140}")

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

        n_pieces = len(pieces_list)
        print(f"\n--- {label}: bc={bc}, {n_pieces} pieces ---")

        # Phase 1: Generate best initial solution from all sort strategies
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

        # CD on best initial
        rebuild_container(best_init_pl, packer)
        pl_cd, cl_cd, moves_cd, passes_cd = coordinate_descent(
            best_init_pl, packer, sw_px, best_init_area, max_passes=20,
        )
        eff_cd = best_init_area / (sw_px * cl_cd) if cl_cd > 0 else 0

        print(f"  Baseline: BLF({best_init_sort})+CD = {eff_cd*100:.2f}%")

        # Phase 2: ILS from the CD solution
        random.seed(42)
        rebuild_container(pl_cd, packer)
        t0_ils = time.time()

        # Try multiple perturbation fractions
        best_overall_eff = eff_cd
        best_overall_pl = copy_placements(pl_cd)
        best_overall_length = cl_cd

        for perturb_frac in [0.20, 0.33, 0.50]:
            # Reset to best CD solution
            current_pl = copy_placements(pl_cd)
            rebuild_container(current_pl, packer)

            pl_ils, len_ils, eff_ils, iters, impr, elapsed = ils_search(
                current_pl, packer, sw_px, best_init_area, GPU_SCALE,
                time_limit=10.0,  # 10s per fraction variant
                perturb_frac=perturb_frac,
                cd_passes=5,  # Quick CD between perturbations
            )

            print(f"  ILS(k={perturb_frac:.0%}, 10s): {eff_ils*100:.2f}% "
                  f"({iters} iters, {impr} improvements)")

            if eff_ils > best_overall_eff:
                best_overall_eff = eff_ils
                best_overall_pl = copy_placements(pl_ils)
                best_overall_length = len_ils

        t_ils = time.time() - t0_ils

        # Phase 3: Final CD on best ILS result (generous passes)
        rebuild_container(best_overall_pl, packer)
        pl_final, cl_final, moves_final, passes_final = coordinate_descent(
            best_overall_pl, packer, sw_px, best_init_area, max_passes=50,
        )
        eff_final = best_init_area / (sw_px * cl_final) if cl_final > 0 else 0

        surf = SURF_10M.get(label, 0)
        gap_cd = surf - eff_cd * 100
        gap_ils = surf - eff_final * 100
        improvement = eff_final * 100 - eff_cd * 100

        print(f"  ILS+FinalCD: {eff_final*100:.2f}% (gap={gap_ils:+.2f}%, "
              f"Δ={improvement:+.2f}%, total={t_ils:.0f}s)")

        # Save PNG
        png_bytes = packer.get_container_png(cl_final)
        png_path = OUTPUT_DIR / f"{label}_ils.png"
        png_path.write_bytes(png_bytes)

        results.append({
            'label': label, 'bc': bc, 'n_pieces': n_pieces,
            'baseline_cd': eff_cd * 100,
            'ils_final': eff_final * 100,
            'surf': surf,
            'gap_cd': gap_cd,
            'gap_ils': gap_ils,
            'improvement': improvement,
            'time_ils_s': t_ils,
        })

    # Summary
    print(f"\n{'='*140}")
    print(f"SUMMARY: ILS vs Baseline CD vs Surface 10m")
    print(f"{'='*140}")
    print(f"{'Label':<10} {'BC':>3} {'Pcs':>4} {'BLF+CD':>8} {'ILS':>8} "
          f"{'Δ':>7} {'Surf10m':>8} {'Gap_CD':>8} {'Gap_ILS':>8} {'Time':>6}")
    print(f"{'-'*140}")

    for r in results:
        print(f"{r['label']:<10} {r['bc']:>3} {r['n_pieces']:>4} "
              f"{r['baseline_cd']:>7.2f}% {r['ils_final']:>7.2f}% "
              f"{r['improvement']:>+6.2f}% {r['surf']:>7.2f}% "
              f"{r['gap_cd']:>+7.2f}% {r['gap_ils']:>+7.2f}% {r['time_ils_s']:>5.0f}s")

    if results:
        avg_gap_cd = sum(r['gap_cd'] for r in results) / len(results)
        avg_gap_ils = sum(r['gap_ils'] for r in results) / len(results)
        avg_impr = sum(r['improvement'] for r in results) / len(results)
        print(f"\nAvg gap CD: {avg_gap_cd:+.2f}%  |  Avg gap ILS: {avg_gap_ils:+.2f}%")
        print(f"Avg ILS improvement over CD: {avg_impr:+.2f}%")


if __name__ == '__main__':
    main()
