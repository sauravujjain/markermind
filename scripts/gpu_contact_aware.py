#!/usr/bin/env python3
"""
GPU Contact-Aware Placement Experiment.

The fundamental limitation of BLF is that it places pieces at the
leftmost/lowest valid position without considering how well pieces
interlock with already-placed pieces.

Contact-aware placement changes the scoring:
- Instead of: minimize y, then minimize x
- Use: maximize contact area with neighbors (pieces + edges)

Contact area is computed on GPU using a second FFT convolution:
1. Compute border_map = dilate(container, 1px) - container
   (pixels that are adjacent to placed pieces but not occupied)
2. Convolve piece raster with border_map
   (at each position, gives count of piece pixels touching borders)
3. Among valid positions within current strip: pick max contact score
   - Ties broken by leftmost x, then lowest y

This mimics Spyrrow's NFP-based interlocking without NFP computation.

Usage:
    conda activate nester
    python scripts/gpu_contact_aware.py
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

rebuild_container = _mod_cd.rebuild_container
coordinate_descent = _mod_cd.coordinate_descent
compute_strip_length = _mod_cd.compute_strip_length
find_best_position_for_piece = _mod_cd.find_best_position_for_piece

import cupy as cp
from cupyx.scipy.signal import fftconvolve as fftconvolve_gpu

OUTPUT_DIR = PROJECT_ROOT / "experiment_results" / "gpu_contact_aware"

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


def swap_gpu_piece_axes(pieces_by_size):
    for size, pieces in pieces_by_size.items():
        for p in pieces:
            p['vertices_mm'] = [(y, x) for x, y in p['vertices_mm']]
            p['raster'] = p['raster'].T.copy()
            p['raster_gpu'] = cp.asarray(p['raster'])
            p['raster_180'] = np.rot90(p['raster'], 2).copy()
            p['raster_180_gpu'] = cp.asarray(p['raster_180'])


def compute_border_map(container):
    """
    Compute the border map: pixels adjacent to placed pieces but not occupied.
    This is the 1-pixel dilation of container minus container itself.
    """
    h, w = container.shape
    # Create dilated version (max of 4 neighbors + self)
    dilated = container.copy()

    # Shift in 4 directions and take max
    # Up
    dilated[:-1, :] = cp.maximum(dilated[:-1, :], container[1:, :])
    # Down
    dilated[1:, :] = cp.maximum(dilated[1:, :], container[:-1, :])
    # Left
    dilated[:, :-1] = cp.maximum(dilated[:, :-1], container[:, 1:])
    # Right
    dilated[:, 1:] = cp.maximum(dilated[:, 1:], container[:, :-1])

    # Border = dilated - container (adjacent to pieces but not occupied)
    border = cp.maximum(dilated - container, 0)

    # Also add the strip edges (top and bottom walls) as "contact"
    # Top edge: row 0 if not occupied
    # Bottom edge: last row if not occupied
    # This encourages pieces to pack against walls
    # We'll add a virtual border on the strip edges
    edge_bonus = cp.zeros_like(container)
    # Top wall contact (row -1 is wall, so row 0 pieces touch it)
    edge_bonus[0, :] = 1.0
    # Bottom wall contact
    edge_bonus[-1, :] = 1.0

    # Combine: internal borders + edge contacts (where not occupied)
    border = cp.maximum(border, edge_bonus * (1 - container))
    return border


def find_contact_position(packer, raster_gpu, strip_width_px, current_length):
    """
    Find the best position that maximizes contact with neighbors.

    Returns: (x, y, contact_score) or None
    """
    ph, pw = raster_gpu.shape
    if ph > strip_width_px:
        return None

    # Step 1: Valid positions via FFT collision detection
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

    valid_count = int(cp.sum(valid))
    if valid_count == 0:
        return None

    # Step 2: Contact score via FFT with border map
    border = compute_border_map(packer.container)
    # Contact score = convolution of piece with border
    # (count of piece pixels adjacent to already-placed pieces at each offset)
    contact_conv = fftconvolve_gpu(border, raster_gpu, mode='valid')

    if contact_conv.shape != valid.shape:
        # Sizes should match since both use 'valid' mode with same inputs
        return None

    # Step 3: Score valid positions
    # Priority 1: within current strip length
    # Priority 2: maximize contact score
    # Tiebreak: minimize x, then minimize y

    if current_length > 0:
        x_idx = cp.arange(result_w, dtype=cp.int32)
        piece_right = x_idx + pw
        inside = valid & (piece_right.reshape(1, -1) <= current_length)
        inside_count = int(cp.sum(inside))

        if inside_count > 0:
            # Among inside positions, find max contact
            contact_inside = cp.where(inside, contact_conv, -1.0)
            max_contact = float(cp.max(contact_inside))

            if max_contact > 0:
                # Find positions with contact score >= 95% of max (allow near-ties)
                threshold = max_contact * 0.95
                top_positions = inside & (contact_conv >= threshold)

                if int(cp.sum(top_positions)) > 0:
                    # Among top contact positions: pick leftmost x, then lowest y
                    y_idx_grid = cp.arange(result_h, dtype=cp.int32).reshape(-1, 1)
                    x_idx_grid = cp.arange(result_w, dtype=cp.int32).reshape(1, -1)

                    # Encode as (x * 10000 + y) to find argmin in one shot
                    encoded = cp.where(top_positions,
                                       x_idx_grid * 10000 + y_idx_grid,
                                       999999999)
                    flat_idx = int(cp.argmin(encoded))
                    best_y = flat_idx // result_w
                    best_x = flat_idx % result_w
                    return best_x, best_y, max_contact

            # No contact found inside — fall back to leftmost inside
            y_grid = cp.where(inside, cp.arange(result_h, dtype=cp.int32).reshape(-1, 1), result_h + 1)
            drop_y = cp.min(y_grid, axis=0)
            valid_inside_cols = (drop_y <= max_valid_y) & inside.any(axis=0)
            if int(cp.sum(valid_inside_cols)) > 0:
                x_valid = cp.where(valid_inside_cols, cp.arange(result_w, dtype=cp.int32), result_w + 1)
                best_x = int(cp.min(x_valid))
                best_y = int(drop_y[best_x])
                return best_x, best_y, 0.0

    # Outside / no current_length — use contact-first among all valid
    contact_valid = cp.where(valid, contact_conv, -1.0)
    max_contact = float(cp.max(contact_valid))

    if max_contact > 0:
        threshold = max_contact * 0.95
        top_positions = valid & (contact_conv >= threshold)
        if int(cp.sum(top_positions)) > 0:
            y_idx_grid = cp.arange(result_h, dtype=cp.int32).reshape(-1, 1)
            x_idx_grid = cp.arange(result_w, dtype=cp.int32).reshape(1, -1)
            encoded = cp.where(top_positions,
                               x_idx_grid * 10000 + y_idx_grid,
                               999999999)
            flat_idx = int(cp.argmin(encoded))
            best_y = flat_idx // result_w
            best_x = flat_idx % result_w
            return best_x, best_y, max_contact

    # Fallback: leftmost valid (standard BLF)
    y_grid = cp.where(valid, cp.arange(result_h, dtype=cp.int32).reshape(-1, 1), result_h + 1)
    drop_y = cp.min(y_grid, axis=0)
    valid_cols = drop_y <= max_valid_y
    if int(cp.sum(valid_cols)) == 0:
        return None

    x_valid = cp.where(valid_cols, cp.arange(result_w, dtype=cp.int32), result_w + 1)
    best_x = int(cp.min(x_valid))
    best_y = int(drop_y[best_x])
    return best_x, best_y, 0.0


def contact_place_with_positions(pieces_list, packer, strip_width_px, gpu_scale, sort_key):
    """
    Contact-aware BLF placement.
    Same as standard BLF but uses find_contact_position instead of find_best_position.
    """
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
            result = find_contact_position(
                packer, raster, strip_width_px, current_length
            )
            if result is None:
                continue
            x, y, contact = result
            right_edge = x + raster.shape[1]

            # Prefer: (1) inside current strip, (2) highest contact, (3) leftmost
            is_inside = right_edge <= current_length if current_length > 0 else True

            if best_result is None:
                better = True
            elif is_inside and best_right > current_length:
                better = True  # New fits inside, old doesn't
            elif not is_inside and best_right <= current_length:
                better = False  # Old fits inside, new doesn't
            elif contact > best_contact:
                better = True  # More contact
            elif contact == best_contact and right_edge < best_right:
                better = True  # Same contact, less extension
            else:
                better = False

            if better:
                best_result = (x, y)
                best_raster = raster
                best_contact = contact
                best_right = right_edge

        if best_result is None:
            continue

        x, y = best_result
        packer.place(best_raster, x, y)
        placed_area += p['area']
        current_length = max(current_length, x + best_raster.shape[1])

        is_rotated = (best_raster.shape != p['raster_gpu'].shape) or \
                     (best_raster.shape == p['raster_gpu'].shape and
                      not cp.array_equal(best_raster, p['raster_gpu']))
        placements.append({
            'piece': p,
            'x': x,
            'y': y,
            'raster': best_raster,
            'rotated': is_rotated,
        })

    if current_length == 0:
        return 0.0, 0.0, [], 0.0, 0

    strip_area = strip_width_px * current_length
    efficiency = placed_area / strip_area
    length_yards = current_length / gpu_scale / 25.4 / 36
    return efficiency, length_yards, placements, placed_area, current_length


def contact_cd_pass(placements, packer, strip_width_px, placed_area):
    """
    Contact-aware coordinate descent pass.
    Instead of re-placing at leftmost position, re-places at max-contact position.
    """
    order = sorted(range(len(placements)),
                   key=lambda i: -(placements[i]['x'] + placements[i]['raster'].shape[1]))

    moves_made = 0
    current_length = compute_strip_length(placements)

    for idx in order:
        pl = placements[idx]
        old_x, old_y = pl['x'], pl['y']
        old_raster = pl['raster']
        ph, pw = old_raster.shape

        # Remove piece
        packer.container[old_y:old_y+ph, old_x:old_x+pw] -= old_raster
        packer.container = cp.maximum(packer.container, 0)

        piece = pl['piece']
        best_pos = None
        best_raster_try = None
        best_contact = -1.0
        best_right = float('inf')

        for try_raster in [piece['raster_gpu'], piece['raster_180_gpu']]:
            result = find_contact_position(
                packer, try_raster, strip_width_px, current_length
            )
            if result is None:
                continue
            x, y, contact = result
            right_edge = x + try_raster.shape[1]

            if best_pos is None or contact > best_contact or \
               (contact == best_contact and right_edge < best_right):
                best_pos = (x, y)
                best_raster_try = try_raster
                best_contact = contact
                best_right = right_edge

        if best_pos is not None:
            nx, ny = best_pos
            new_right = nx + best_raster_try.shape[1]

            other_length = 0
            for j, pl2 in enumerate(placements):
                if j == idx:
                    continue
                other_length = max(other_length, pl2['x'] + pl2['raster'].shape[1])

            new_length_candidate = max(other_length, new_right)

            if new_length_candidate <= current_length:
                packer.place(best_raster_try, nx, ny)
                placements[idx] = {
                    'piece': piece,
                    'x': nx, 'y': ny,
                    'raster': best_raster_try,
                    'rotated': not cp.array_equal(best_raster_try, piece['raster_gpu']),
                }
                current_length = new_length_candidate
                if nx != old_x or ny != old_y or best_raster_try is not old_raster:
                    moves_made += 1
            else:
                packer.place(old_raster, old_x, old_y)
        else:
            packer.place(old_raster, old_x, old_y)

    return placements, current_length, moves_made


def contact_cd(placements, packer, strip_width_px, placed_area, max_passes=20):
    """Run contact-aware CD."""
    total_moves = 0
    passes = 0
    current_length = compute_strip_length(placements)

    for pass_num in range(max_passes):
        passes += 1
        placements, new_length, moves = contact_cd_pass(
            placements, packer, strip_width_px, placed_area
        )
        total_moves += moves
        improvement = current_length - new_length
        if moves == 0:
            break
        current_length = new_length

    return placements, current_length, total_moves, passes


SORT_STRATEGIES = {
    'width_desc': lambda p: -p['raster'].shape[0],
    'area_desc': lambda p: -p['area'],
    'height_desc': lambda p: -p['raster'].shape[1],
    'perimeter_desc': lambda p: -(np.sum(np.abs(np.diff(np.pad(p['raster'], 1, 'constant'), axis=1))) +
                                   np.sum(np.abs(np.diff(np.pad(p['raster'], 1, 'constant'), axis=0)))),
    'fill_asc': lambda p: p['area'] / max(1, p['raster'].shape[0] * p['raster'].shape[1]),
    'width_area': lambda p: (-p['raster'].shape[0], -p['area']),
}


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
    print(f"CONTACT-AWARE PLACEMENT: 6 sort strategies × (BLF vs Contact) + CD")
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

        print(f"\n--- {label}: bc={bc}, {len(pieces_list)} pieces ---")

        # Standard BLF: best of all sorts + standard CD
        best_blf_eff = 0
        best_blf_sort = ''
        best_blf_pl = None
        best_blf_area = 0
        place_with_positions = _mod_cd.place_with_positions

        for name, sort_fn in SORT_STRATEGIES.items():
            eff, _, pl, area, cl = place_with_positions(
                pieces_list, packer, sw_px, GPU_SCALE, sort_key=sort_fn,
            )
            if eff > best_blf_eff:
                best_blf_eff = eff
                best_blf_sort = name
                best_blf_pl = pl
                best_blf_area = area

        rebuild_container(best_blf_pl, packer)
        pl_blf_cd, cl_blf_cd, moves_blf, passes_blf = coordinate_descent(
            best_blf_pl, packer, sw_px, best_blf_area, max_passes=20,
        )
        eff_blf_cd = best_blf_area / (sw_px * cl_blf_cd) if cl_blf_cd > 0 else 0

        # Contact-aware: best of all sorts + contact CD
        best_contact_eff = 0
        best_contact_sort = ''
        best_contact_pl = None
        best_contact_area = 0
        t0_contact = time.time()

        for name, sort_fn in SORT_STRATEGIES.items():
            eff, _, pl, area, cl = contact_place_with_positions(
                pieces_list, packer, sw_px, GPU_SCALE, sort_key=sort_fn,
            )
            if eff > best_contact_eff:
                best_contact_eff = eff
                best_contact_sort = name
                best_contact_pl = pl
                best_contact_area = area

        t_contact_place = time.time() - t0_contact

        # Contact CD
        rebuild_container(best_contact_pl, packer)
        t0_ccd = time.time()
        pl_contact_cd, cl_contact_cd, moves_ccd, passes_ccd = contact_cd(
            best_contact_pl, packer, sw_px, best_contact_area, max_passes=20,
        )
        t_ccd = time.time() - t0_ccd
        eff_contact_cd = best_contact_area / (sw_px * cl_contact_cd) if cl_contact_cd > 0 else 0

        # Also try standard CD on contact-placed pieces (contact placement → std CD)
        rebuild_container(best_contact_pl, packer)
        pl_contact_stdcd, cl_stdcd2, moves_std2, passes_std2 = coordinate_descent(
            [dict(pl) for pl in best_contact_pl], packer, sw_px, best_contact_area, max_passes=20,
        )
        eff_contact_stdcd = best_contact_area / (sw_px * cl_stdcd2) if cl_stdcd2 > 0 else 0

        surf = SURF_10M.get(label, 0)
        gap_blf = surf - eff_blf_cd * 100
        gap_contact = surf - eff_contact_cd * 100
        gap_hybrid = surf - eff_contact_stdcd * 100
        improvement = eff_contact_cd * 100 - eff_blf_cd * 100

        print(f"  BLF ({best_blf_sort})+CD:     {eff_blf_cd*100:.2f}%  gap={gap_blf:+.2f}%")
        print(f"  Contact ({best_contact_sort}):  {best_contact_eff*100:.2f}%  (place={t_contact_place*1000:.0f}ms)")
        print(f"  Contact+ContactCD:     {eff_contact_cd*100:.2f}%  gap={gap_contact:+.2f}%  (cd={t_ccd*1000:.0f}ms)")
        print(f"  Contact+StdCD:         {eff_contact_stdcd*100:.2f}%  gap={gap_hybrid:+.2f}%")
        print(f"  Δ vs BLF+CD:           {improvement:+.2f}%")

        # Save PNG
        png_bytes = packer.get_container_png(cl_contact_cd)
        png_path = OUTPUT_DIR / f"{label}_contact.png"
        png_path.write_bytes(png_bytes)

        results.append({
            'label': label, 'bc': bc,
            'blf_cd': eff_blf_cd * 100,
            'contact_cd': eff_contact_cd * 100,
            'hybrid': eff_contact_stdcd * 100,
            'surf': surf,
            'gap_blf': gap_blf,
            'gap_contact': gap_contact,
            'gap_hybrid': gap_hybrid,
            'improvement': improvement,
            'time_contact_ms': t_contact_place * 1000,
            'time_ccd_ms': t_ccd * 1000,
        })

    # Summary
    print(f"\n{'='*140}")
    print(f"SUMMARY: Contact-Aware vs Standard BLF")
    print(f"{'='*140}")
    print(f"{'Label':<10} {'BC':>3} {'BLF+CD':>8} {'Cntct+CCD':>10} {'Cntct+SCD':>10} "
          f"{'Surf10m':>8} {'Gap_BLF':>8} {'Gap_Cntct':>10} {'Δ':>7}")
    print(f"{'-'*140}")

    for r in results:
        best = max(r['blf_cd'], r['contact_cd'], r['hybrid'])
        best_gap = r['surf'] - best
        print(f"{r['label']:<10} {r['bc']:>3} {r['blf_cd']:>7.2f}% {r['contact_cd']:>9.2f}% "
              f"{r['hybrid']:>9.2f}% {r['surf']:>7.2f}% {r['gap_blf']:>+7.2f}% "
              f"{r['surf'] - best:>+9.2f}% {r['improvement']:>+6.2f}%")

    if results:
        avg_blf_gap = sum(r['gap_blf'] for r in results) / len(results)
        avg_contact_gap = sum(r['gap_contact'] for r in results) / len(results)
        avg_improvement = sum(r['improvement'] for r in results) / len(results)
        print(f"\nAvg gap BLF+CD: {avg_blf_gap:+.2f}%  |  Avg gap Contact+CCD: {avg_contact_gap:+.2f}%")
        print(f"Avg improvement from contact-aware: {avg_improvement:+.2f}%")


if __name__ == '__main__':
    main()
