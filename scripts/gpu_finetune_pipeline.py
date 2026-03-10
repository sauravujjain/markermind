#!/usr/bin/env python3
"""
GPU Finetune Pipeline: Combined best-of-all approaches.

This is the recommended GPU finetune strategy for production use.
Runs all proven approaches and keeps the best result per marker:

1. Multi-sort BLF (4 strategies) + CD(20)           [~2-5s]
2. Contact-aware placement (4 strategies) + StdCD(20) [~5-15s]
3. Fast ILS from best CD (30s, k=33%)                [30s]
4. Ruin-and-Recreate (30s guided restarts) + CD(20)  [30s]

Total budget: ~60-80s per marker.
Expected improvement over baseline BLF+CD: +1-4% efficiency.

Usage:
    conda activate nester
    python scripts/gpu_finetune_pipeline.py
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
from cupyx.scipy.signal import fftconvolve as fftconvolve_gpu

OUTPUT_DIR = PROJECT_ROOT / "experiment_results" / "gpu_finetune"

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


# ── Contact-aware placement ──────────────────────────────────────────

def compute_border_map(container):
    h, w = container.shape
    dilated = container.copy()
    dilated[:-1, :] = cp.maximum(dilated[:-1, :], container[1:, :])
    dilated[1:, :] = cp.maximum(dilated[1:, :], container[:-1, :])
    dilated[:, :-1] = cp.maximum(dilated[:, :-1], container[:, 1:])
    dilated[:, 1:] = cp.maximum(dilated[:, 1:], container[:, :-1])
    border = cp.maximum(dilated - container, 0)
    edge_bonus = cp.zeros_like(container)
    edge_bonus[0, :] = 1.0
    edge_bonus[-1, :] = 1.0
    border = cp.maximum(border, edge_bonus * (1 - container))
    return border


def find_contact_position(packer, raster_gpu, strip_width_px, current_length):
    ph, pw = raster_gpu.shape
    if ph > strip_width_px:
        return None

    kernel = raster_gpu[::-1, ::-1].copy()
    overlap = fftconvolve_gpu(packer.container, kernel, mode='valid')
    if overlap.size == 0:
        return None

    valid = overlap < 0.5
    result_h, result_w = valid.shape
    max_valid_y = strip_width_px - ph
    if max_valid_y < 0:
        return None
    if max_valid_y + 1 < result_h:
        valid[max_valid_y + 1:, :] = False
    if int(cp.sum(valid)) == 0:
        return None

    border = compute_border_map(packer.container)
    contact_conv = fftconvolve_gpu(border, raster_gpu, mode='valid')
    if contact_conv.shape != valid.shape:
        return None

    if current_length > 0:
        x_idx = cp.arange(result_w, dtype=cp.int32)
        piece_right = x_idx + pw
        inside = valid & (piece_right.reshape(1, -1) <= current_length)
        if int(cp.sum(inside)) > 0:
            contact_inside = cp.where(inside, contact_conv, -1.0)
            max_contact = float(cp.max(contact_inside))
            if max_contact > 0:
                threshold = max_contact * 0.95
                top = inside & (contact_conv >= threshold)
                if int(cp.sum(top)) > 0:
                    y_g = cp.arange(result_h, dtype=cp.int32).reshape(-1, 1)
                    x_g = cp.arange(result_w, dtype=cp.int32).reshape(1, -1)
                    enc = cp.where(top, x_g * 10000 + y_g, 999999999)
                    flat = int(cp.argmin(enc))
                    return flat % result_w, flat // result_w, max_contact

    contact_valid = cp.where(valid, contact_conv, -1.0)
    max_c = float(cp.max(contact_valid))
    if max_c > 0:
        threshold = max_c * 0.95
        top = valid & (contact_conv >= threshold)
        if int(cp.sum(top)) > 0:
            y_g = cp.arange(result_h, dtype=cp.int32).reshape(-1, 1)
            x_g = cp.arange(result_w, dtype=cp.int32).reshape(1, -1)
            enc = cp.where(top, x_g * 10000 + y_g, 999999999)
            flat = int(cp.argmin(enc))
            return flat % result_w, flat // result_w, max_c

    # BLF fallback
    y_grid = cp.where(valid, cp.arange(result_h, dtype=cp.int32).reshape(-1, 1), result_h + 1)
    drop_y = cp.min(y_grid, axis=0)
    valid_cols = drop_y <= max_valid_y
    if int(cp.sum(valid_cols)) == 0:
        return None
    x_v = cp.where(valid_cols, cp.arange(result_w, dtype=cp.int32), result_w + 1)
    bx = int(cp.min(x_v))
    by = int(drop_y[bx])
    return bx, by, 0.0


def contact_place(pieces_list, packer, strip_width_px, gpu_scale, sort_key):
    packer.reset()
    pieces_sorted = sorted(pieces_list, key=sort_key)
    placed_area = 0.0
    current_length = 0
    placements = []

    for p in pieces_sorted:
        best_result = None
        best_raster = None
        best_contact = -1.0
        best_right = float('inf')

        for raster in [p['raster_gpu'], p['raster_180_gpu']]:
            result = find_contact_position(packer, raster, strip_width_px, current_length)
            if result is None:
                continue
            x, y, contact = result
            right = x + raster.shape[1]
            is_inside = right <= current_length if current_length > 0 else True

            if best_result is None:
                better = True
            elif is_inside and best_right > (current_length if current_length > 0 else 0):
                better = True
            elif not is_inside and best_right <= (current_length if current_length > 0 else 0):
                better = False
            elif contact > best_contact:
                better = True
            elif contact == best_contact and right < best_right:
                better = True
            else:
                better = False

            if better:
                best_result = (x, y)
                best_raster = raster
                best_contact = contact
                best_right = right

        if best_result is None:
            continue

        x, y = best_result
        packer.place(best_raster, x, y)
        placed_area += p['area']
        current_length = max(current_length, x + best_raster.shape[1])
        placements.append({
            'piece': p, 'x': x, 'y': y, 'raster': best_raster,
            'rotated': not cp.array_equal(best_raster, p['raster_gpu']),
        })

    if current_length == 0:
        return 0.0, 0.0, [], 0.0, 0
    efficiency = placed_area / (strip_width_px * current_length)
    length_yards = current_length / gpu_scale / 25.4 / 36
    return efficiency, length_yards, placements, placed_area, current_length


# ── Fast ILS ──────────────────────────────────────────────────────────

def fast_ils(placements, packer, strip_width_px, placed_area, time_limit=30.0):
    n = len(placements)
    k = max(2, int(n * 0.33))

    best_pl = [dict(p) for p in placements]
    best_length = compute_strip_length(best_pl)

    iterations = 0
    improvements = 0
    t0 = time.time()

    while time.time() - t0 < time_limit:
        iterations += 1
        current = [dict(p) for p in best_pl]
        packer.reset()
        for pl in current:
            packer.place(pl['raster'], pl['x'], pl['y'])

        if random.random() < 0.5:
            remove_idx = random.sample(range(n), k)
        else:
            sorted_by_right = sorted(range(n),
                key=lambda i: -(current[i]['x'] + current[i]['raster'].shape[1]))
            pool = sorted_by_right[:max(k, int(n * 0.6))]
            remove_idx = random.sample(pool, k)

        for idx in remove_idx:
            pl = current[idx]
            r = pl['raster']
            ph, pw = r.shape
            packer.container[pl['y']:pl['y']+ph, pl['x']:pl['x']+pw] -= r
        packer.container = cp.maximum(packer.container, 0)

        remaining = set(range(n)) - set(remove_idx)
        current_length = max((current[i]['x'] + current[i]['raster'].shape[1] for i in remaining), default=0)

        removed_sorted = sorted(remove_idx, key=lambda i: -current[i]['piece']['area'])
        for idx in removed_sorted:
            piece = current[idx]['piece']
            best_pos = None
            best_raster = None
            best_right = float('inf')
            for try_r in [piece['raster_gpu'], piece['raster_180_gpu']]:
                pos = find_best_position_for_piece(packer, try_r, strip_width_px, current_length)
                if pos and pos[0] + try_r.shape[1] < best_right:
                    best_pos = pos
                    best_raster = try_r
                    best_right = pos[0] + try_r.shape[1]
            if best_pos:
                packer.place(best_raster, best_pos[0], best_pos[1])
                current[idx] = {
                    'piece': piece, 'x': best_pos[0], 'y': best_pos[1],
                    'raster': best_raster,
                    'rotated': not cp.array_equal(best_raster, piece['raster_gpu']),
                }
                current_length = max(current_length, best_pos[0] + best_raster.shape[1])
            else:
                packer.place(current[idx]['raster'], current[idx]['x'], current[idx]['y'])
                current_length = max(current_length,
                    current[idx]['x'] + current[idx]['raster'].shape[1])

        new_length = compute_strip_length(current)
        if new_length < best_length:
            best_length = new_length
            best_pl = [dict(p) for p in current]
            improvements += 1

    return best_pl, best_length, iterations, improvements


# ── Ruin-and-Recreate ─────────────────────────────────────────────────

def ruin_and_recreate(pieces_list, packer, strip_width_px, gpu_scale, time_limit=30.0):
    sort_fns = list(SORT_STRATEGIES.values())
    best_eff = 0
    best_pl = None
    best_area = 0
    iterations = 0
    t0 = time.time()

    while time.time() - t0 < time_limit:
        iterations += 1
        base_sort = random.choice(sort_fns)
        sorted_pieces = sorted(pieces_list, key=base_sort)
        n_swaps = random.randint(len(sorted_pieces) // 10, len(sorted_pieces) // 3)
        for _ in range(n_swaps):
            i = random.randint(0, len(sorted_pieces) - 2)
            sorted_pieces[i], sorted_pieces[i+1] = sorted_pieces[i+1], sorted_pieces[i]

        eff, _, pl, area, cl = place_with_positions(
            sorted_pieces, packer, strip_width_px, gpu_scale,
            sort_key=lambda p: 0,
        )
        if eff > best_eff:
            best_eff = eff
            best_pl = pl
            best_area = area

    return best_pl, best_eff, best_area, iterations


# ── Main finetune pipeline ────────────────────────────────────────────

def finetune_marker(pieces_list, packer, strip_width_px, gpu_scale,
                    ils_time=30.0, rnr_time=30.0, cd_passes=20):
    """
    Full finetune pipeline for a single marker.

    Returns: (best_placements, best_efficiency, best_length, placed_area, breakdown)
    """
    breakdown = {}
    overall_best_eff = 0
    overall_best_pl = None
    overall_best_area = 0
    overall_best_cl = 0
    overall_source = ''

    # ── Phase 1: Multi-sort BLF + CD ──
    t0 = time.time()
    for name, sort_fn in SORT_STRATEGIES.items():
        eff, _, pl, area, cl = place_with_positions(
            pieces_list, packer, strip_width_px, gpu_scale, sort_key=sort_fn,
        )
        if eff > overall_best_eff:
            overall_best_eff = eff
            overall_best_pl = pl
            overall_best_area = area
            overall_best_cl = cl
            overall_source = f'blf_{name}'

    # CD on best BLF
    rebuild_container(overall_best_pl, packer)
    pl_cd, cl_cd, _, _ = coordinate_descent(
        overall_best_pl, packer, strip_width_px, overall_best_area, max_passes=cd_passes,
    )
    eff_cd = overall_best_area / (strip_width_px * cl_cd) if cl_cd > 0 else 0
    if eff_cd > overall_best_eff:
        overall_best_eff = eff_cd
        overall_best_pl = pl_cd
        overall_best_cl = cl_cd
        overall_source = f'blf+cd'
    breakdown['blf_cd'] = eff_cd * 100
    breakdown['t_blf_cd'] = time.time() - t0

    # ── Phase 2: Contact-aware placement + CD ──
    t0 = time.time()
    best_contact_eff = 0
    best_contact_pl = None
    best_contact_area = 0

    for name, sort_fn in SORT_STRATEGIES.items():
        eff, _, pl, area, cl = contact_place(
            pieces_list, packer, strip_width_px, gpu_scale, sort_key=sort_fn,
        )
        if eff > best_contact_eff:
            best_contact_eff = eff
            best_contact_pl = pl
            best_contact_area = area

    if best_contact_pl:
        rebuild_container(best_contact_pl, packer)
        pl_cc, cl_cc, _, _ = coordinate_descent(
            best_contact_pl, packer, strip_width_px, best_contact_area, max_passes=cd_passes,
        )
        eff_cc = best_contact_area / (strip_width_px * cl_cc) if cl_cc > 0 else 0
        breakdown['contact_cd'] = eff_cc * 100
        if eff_cc > overall_best_eff:
            overall_best_eff = eff_cc
            overall_best_pl = pl_cc
            overall_best_area = best_contact_area
            overall_best_cl = cl_cc
            overall_source = 'contact+cd'
    breakdown['t_contact_cd'] = time.time() - t0

    # ── Phase 3: Fast ILS from best so far ──
    t0 = time.time()
    rebuild_container(overall_best_pl, packer)
    pl_ils, cl_ils, iters_ils, impr_ils = fast_ils(
        [dict(p) for p in overall_best_pl], packer, strip_width_px,
        overall_best_area, time_limit=ils_time,
    )
    eff_ils = overall_best_area / (strip_width_px * cl_ils) if cl_ils > 0 else 0
    breakdown['fast_ils'] = eff_ils * 100
    breakdown['ils_iters'] = iters_ils
    breakdown['ils_impr'] = impr_ils
    if eff_ils > overall_best_eff:
        overall_best_eff = eff_ils
        overall_best_pl = pl_ils
        overall_best_cl = cl_ils
        overall_source = 'fast_ils'
    breakdown['t_ils'] = time.time() - t0

    # ── Phase 4: Ruin-and-Recreate ──
    t0 = time.time()
    pl_rnr, eff_rnr, area_rnr, iters_rnr = ruin_and_recreate(
        pieces_list, packer, strip_width_px, gpu_scale, time_limit=rnr_time,
    )
    if pl_rnr:
        rebuild_container(pl_rnr, packer)
        pl_rnr_cd, cl_rnr_cd, _, _ = coordinate_descent(
            pl_rnr, packer, strip_width_px, area_rnr, max_passes=cd_passes,
        )
        eff_rnr_cd = area_rnr / (strip_width_px * cl_rnr_cd) if cl_rnr_cd > 0 else 0
        breakdown['rnr_cd'] = eff_rnr_cd * 100
        breakdown['rnr_iters'] = iters_rnr
        if eff_rnr_cd > overall_best_eff:
            overall_best_eff = eff_rnr_cd
            overall_best_pl = pl_rnr_cd
            overall_best_area = area_rnr
            overall_best_cl = cl_rnr_cd
            overall_source = 'rnr+cd'
    breakdown['t_rnr'] = time.time() - t0

    # ── Phase 5: Final CD on overall best ──
    t0 = time.time()
    rebuild_container(overall_best_pl, packer)
    pl_final, cl_final, moves_f, _ = coordinate_descent(
        overall_best_pl, packer, strip_width_px, overall_best_area, max_passes=50,
    )
    eff_final = overall_best_area / (strip_width_px * cl_final) if cl_final > 0 else 0
    breakdown['t_final_cd'] = time.time() - t0

    if eff_final > overall_best_eff:
        overall_best_eff = eff_final
        overall_best_pl = pl_final
        overall_best_cl = cl_final
        overall_source += '+finalcd'

    length_yards = overall_best_cl / gpu_scale / 25.4 / 36
    breakdown['source'] = overall_source
    breakdown['total_eff'] = overall_best_eff * 100

    return overall_best_pl, overall_best_eff, length_yards, overall_best_area, breakdown


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
    print(f"GPU FINETUNE PIPELINE: BLF+CD → Contact+CD → FastILS → R&R+CD → FinalCD")
    print(f"{'='*140}")

    results = []
    t0_total = time.time()

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
        random.seed(42)

        t0_marker = time.time()
        pl, eff, length_yd, area, bd = finetune_marker(
            pieces_list, packer, sw_px, GPU_SCALE,
            ils_time=30.0, rnr_time=30.0, cd_passes=20,
        )
        t_marker = time.time() - t0_marker

        surf = SURF_10M.get(label, 0)
        gap = surf - eff * 100
        baseline_gap = surf - bd.get('blf_cd', 0)

        print(f"  BLF+CD: {bd.get('blf_cd', 0):.2f}%  Contact+CD: {bd.get('contact_cd', 0):.2f}%  "
              f"ILS: {bd.get('fast_ils', 0):.2f}%  R&R+CD: {bd.get('rnr_cd', 0):.2f}%")
        print(f"  FINAL: {eff*100:.2f}% ({bd['source']}) | gap={gap:+.2f}% | "
              f"Δ vs baseline={eff*100 - bd.get('blf_cd', 0):+.2f}% | {t_marker:.0f}s")

        png_bytes = packer.get_container_png(int(length_yd * 36 * 25.4 * GPU_SCALE))
        (OUTPUT_DIR / f"{label}_finetune.png").write_bytes(png_bytes)

        results.append({
            'label': label, 'bc': bc, 'n': n,
            'blf_cd': bd.get('blf_cd', 0),
            'contact_cd': bd.get('contact_cd', 0),
            'fast_ils': bd.get('fast_ils', 0),
            'rnr_cd': bd.get('rnr_cd', 0),
            'final': eff * 100,
            'source': bd['source'],
            'surf': surf,
            'gap_base': baseline_gap,
            'gap_final': gap,
            'improvement': eff * 100 - bd.get('blf_cd', 0),
            'time_s': t_marker,
        })

    total_time = time.time() - t0_total

    # Summary
    print(f"\n{'='*150}")
    print(f"FINETUNE PIPELINE SUMMARY (total: {total_time:.0f}s)")
    print(f"{'='*150}")
    print(f"{'Label':<10} {'BC':>3} {'BLF+CD':>8} {'Cntct':>8} {'ILS':>8} {'R&R':>8} "
          f"{'FINAL':>8} {'Source':<14} {'Surf':>8} {'Gap_B':>7} {'Gap_F':>7} {'Δ':>7} {'Time':>5}")
    print(f"{'-'*150}")

    for r in results:
        print(f"{r['label']:<10} {r['bc']:>3} "
              f"{r['blf_cd']:>7.2f}% {r['contact_cd']:>7.2f}% "
              f"{r['fast_ils']:>7.2f}% {r['rnr_cd']:>7.2f}% "
              f"{r['final']:>7.2f}% {r['source']:<14} {r['surf']:>7.2f}% "
              f"{r['gap_base']:>+6.2f}% {r['gap_final']:>+6.2f}% "
              f"{r['improvement']:>+6.2f}% {r['time_s']:>4.0f}s")

    if results:
        avg_base = sum(r['gap_base'] for r in results) / len(results)
        avg_final = sum(r['gap_final'] for r in results) / len(results)
        avg_impr = sum(r['improvement'] for r in results) / len(results)
        print(f"\nAvg gap baseline: {avg_base:+.2f}%  →  Avg gap finetune: {avg_final:+.2f}%")
        print(f"Avg improvement: {avg_impr:+.2f}%")
        print(f"Total time: {total_time:.0f}s ({total_time/len(results):.0f}s/marker)")


if __name__ == '__main__':
    main()
