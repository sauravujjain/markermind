#!/usr/bin/env python3
"""
Coordinate Descent Post-Refinement Experiment for GPU Raster Nesting.

Takes markers already nested by dual-sort BLF placement, then applies
iterative coordinate descent to compact pieces toward the origin.

Similar in spirit to Spyrrow's compression stage:
- Remove a piece from the container
- Re-place it at the best valid position (leftmost/lowest)
- Accept if strip length doesn't increase
- Repeat until convergence

Usage:
    python scripts/gpu_coordinate_descent_experiment.py
"""

import sys
import time
import math
from pathlib import Path

import numpy as np
import importlib

# Add project root
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# Direct import to avoid __init__.py pulling in bcrypt etc.
_spec = importlib.util.spec_from_file_location(
    "gpu_nesting_runner",
    PROJECT_ROOT / "backend" / "backend" / "services" / "gpu_nesting_runner.py",
)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

_init_gpu = _mod._init_gpu
load_pieces_for_material = _mod.load_pieces_for_material
GPUPacker = _mod.GPUPacker
_compute_perimeter_mm = _mod._compute_perimeter_mm
generate_all_ratios = _mod.generate_all_ratios
ratio_to_str = _mod.ratio_to_str

# Lazy GPU imports
cp = None
fftconvolve_gpu = None


def init():
    global cp, fftconvolve_gpu
    _init_gpu()
    import cupy as _cp
    from cupyx.scipy.signal import fftconvolve as _fft
    cp = _cp
    fftconvolve_gpu = _fft


def place_with_positions(
    pieces_list, packer, strip_width_px, gpu_scale, sort_key
):
    """
    BLF placement that returns per-piece positions.
    Returns: (efficiency, length_yards, placements_list, placed_area, current_length)
    """
    packer.reset()
    pieces_sorted = sorted(pieces_list, key=sort_key)

    placed_area = 0.0
    current_length = 0
    placements = []

    for p in pieces_sorted:
        result, raster = packer.find_best_position(
            p['raster_gpu'], p['raster_180_gpu'], current_length
        )
        if result is None:
            continue
        packer.place(raster, result['x'], result['y'])
        placed_area += p['area']
        current_length = max(current_length, result['x'] + result['pw'])

        is_rotated = (raster.shape != p['raster_gpu'].shape) or \
                     (raster.shape == p['raster_gpu'].shape and
                      not cp.array_equal(raster, p['raster_gpu']))
        placements.append({
            'piece': p,
            'x': result['x'],
            'y': result['y'],
            'raster': raster,  # the actual raster placed (0° or 180°)
            'rotated': is_rotated,
        })

    if current_length == 0:
        return 0.0, 0.0, [], 0.0, 0

    strip_area = strip_width_px * current_length
    efficiency = placed_area / strip_area
    length_yards = current_length / gpu_scale / 25.4 / 36
    return efficiency, length_yards, placements, placed_area, current_length


def dual_sort_with_positions(pieces_list, packer, strip_width_px, gpu_scale):
    """Run both sort strategies, return the better one with positions."""
    eff_w, len_w, pl_w, area_w, cl_w = place_with_positions(
        pieces_list, packer, strip_width_px, gpu_scale,
        sort_key=lambda p: -p['raster'].shape[0],
    )
    eff_a, len_a, pl_a, area_a, cl_a = place_with_positions(
        pieces_list, packer, strip_width_px, gpu_scale,
        sort_key=lambda p: -p['area'],
    )
    if eff_a > eff_w:
        return eff_a, len_a, pl_a, area_a, cl_a, 'area_desc'
    return eff_w, len_w, pl_w, area_w, cl_w, 'width_desc'


def rebuild_container(placements, packer):
    """Rebuild the container raster from a list of placements."""
    packer.reset()
    for pl in placements:
        packer.place(pl['raster'], pl['x'], pl['y'])


def compute_strip_length(placements):
    """Compute the rightmost extent of all placed pieces."""
    if not placements:
        return 0
    return max(pl['x'] + pl['raster'].shape[1] for pl in placements)


def find_best_position_for_piece(packer, raster, strip_width_px, current_length):
    """
    Find the best (leftmost, then lowest) valid position for a piece.
    Returns (x, y) or None.
    """
    ph, pw = raster.shape
    if ph > strip_width_px:
        return None

    kernel = raster[::-1, ::-1].copy()
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

    # For coordinate descent, we want: minimize x first, then minimize y
    # (compact toward the left/origin)
    y_idx = cp.arange(result_h, dtype=cp.int32).reshape(-1, 1)
    y_grid = cp.where(valid, y_idx, result_h + 1)
    drop_y = cp.min(y_grid, axis=0)  # best y per x column
    valid_cols = drop_y <= max_valid_y

    valid_col_count = int(cp.sum(valid_cols))
    if valid_col_count == 0:
        return None

    # Find leftmost valid column (minimize x = minimize strip extension)
    x_positions = cp.arange(result_w, dtype=cp.int32)
    x_valid = cp.where(valid_cols, x_positions, result_w + 1)
    best_x = int(cp.min(x_valid))

    if best_x > result_w:
        return None

    best_y = int(drop_y[best_x])
    return best_x, best_y


def coordinate_descent_pass(placements, packer, strip_width_px, placed_area):
    """
    One pass of coordinate descent: try to move each piece to a better position.

    Process pieces from rightmost to leftmost (pieces furthest right have the
    most room to improve). For each piece:
    1. Remove it from the container
    2. Find the best position (leftmost, then lowest)
    3. If the new position reduces or equals strip length, accept it
    4. Otherwise, put it back in its original position

    Returns: (new_placements, new_length, moves_made)
    """
    # Sort by rightmost edge descending — process rightmost pieces first
    order = sorted(range(len(placements)), key=lambda i: -(placements[i]['x'] + placements[i]['raster'].shape[1]))

    moves_made = 0
    current_length = compute_strip_length(placements)

    for idx in order:
        pl = placements[idx]
        old_x, old_y = pl['x'], pl['y']
        old_raster = pl['raster']
        ph, pw = old_raster.shape

        # Remove this piece from the container
        packer.container[old_y:old_y+ph, old_x:old_x+pw] -= old_raster
        # Clamp to 0 (avoid floating point negatives)
        packer.container = cp.maximum(packer.container, 0)

        # Try both rotations
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
            # Accept if: (a) reduces strip length or (b) same length but moved left/down
            new_right = nx + best_raster.shape[1]
            old_right = old_x + pw

            # Compute what strip length would be without this piece at old pos
            other_length = 0
            for j, pl2 in enumerate(placements):
                if j == idx:
                    continue
                other_length = max(other_length, pl2['x'] + pl2['raster'].shape[1])

            new_length_candidate = max(other_length, new_right)

            if new_length_candidate <= current_length:
                # Accept the move
                packer.place(best_raster, nx, ny)
                placements[idx] = {
                    'piece': piece,
                    'x': nx,
                    'y': ny,
                    'raster': best_raster,
                    'rotated': not cp.array_equal(best_raster, piece['raster_gpu']),
                }
                current_length = new_length_candidate
                if nx != old_x or ny != old_y or best_raster is not old_raster:
                    moves_made += 1
            else:
                # Reject — put it back
                packer.place(old_raster, old_x, old_y)
        else:
            # Couldn't find valid position — put it back
            packer.place(old_raster, old_x, old_y)

    return placements, current_length, moves_made


def coordinate_descent(placements, packer, strip_width_px, placed_area, max_passes=10):
    """
    Run coordinate descent until convergence or max_passes.

    Returns: (final_placements, final_length, total_moves, passes_used)
    """
    total_moves = 0
    passes = 0
    current_length = compute_strip_length(placements)

    for pass_num in range(max_passes):
        passes += 1
        placements, new_length, moves = coordinate_descent_pass(
            placements, packer, strip_width_px, placed_area
        )
        total_moves += moves

        improvement = current_length - new_length
        print(f"    Pass {pass_num + 1}: {moves} moves, length {new_length}px "
              f"(delta={improvement}px)")

        if moves == 0:
            print(f"    Converged after {passes} passes.")
            break
        current_length = new_length

    return placements, current_length, total_moves, passes


def run_experiment():
    """Run coordinate descent experiment on test markers."""
    init()

    # ── Load pattern ──────────────────────────────────────────────────
    dxf_path = "data/dxf-amaa/23583 PROD 1 L 0 W 0 25FEB22.dxf"
    rul_path = "data/dxf-amaa/23583 PROD 1 L 0 W 0 25FEB22.rul"

    if not Path(dxf_path).exists():
        print(f"DXF not found: {dxf_path}")
        return

    gpu_scale = 0.15
    fabric_width_inches = 60.0
    fabric_width_mm = fabric_width_inches * 25.4
    strip_width_px = int(fabric_width_mm * gpu_scale)

    print(f"Loading pattern: {Path(dxf_path).name}")
    print(f"Fabric: {fabric_width_inches}\" = {strip_width_px}px @ {gpu_scale} px/mm")

    # Detect material and sizes
    from nesting_engine.io.aama_parser import load_aama_pattern
    pieces, rules = load_aama_pattern(dxf_path, rul_path)
    materials = list(set(p.material for p in pieces.values()))
    material = materials[0]
    available_sizes = list(rules.header.sizes)
    print(f"Material: {material}, Sizes: {available_sizes}")

    pieces_by_size = load_pieces_for_material(
        dxf_path, rul_path, material, available_sizes, gpu_scale
    )

    sizes_with_pieces = [s for s in available_sizes if s in pieces_by_size]
    print(f"Loaded {sum(len(v) for v in pieces_by_size.values())} piece types "
          f"across {len(sizes_with_pieces)} sizes\n")

    # ── Select test ratios ────────────────────────────────────────────
    # Mix of small and large markers
    test_ratios = []

    # bc=1: single size markers (a few)
    for s in sizes_with_pieces[:3]:
        test_ratios.append({s: 1})

    # bc=2: pair markers
    if len(sizes_with_pieces) >= 2:
        test_ratios.append({sizes_with_pieces[0]: 1, sizes_with_pieces[1]: 1})
        test_ratios.append({sizes_with_pieces[0]: 2})

    # bc=3-6: multi-size markers
    if len(sizes_with_pieces) >= 3:
        test_ratios.append({sizes_with_pieces[0]: 1, sizes_with_pieces[1]: 1, sizes_with_pieces[2]: 1})
    if len(sizes_with_pieces) >= 4:
        test_ratios.append({sizes_with_pieces[0]: 1, sizes_with_pieces[1]: 1,
                           sizes_with_pieces[2]: 1, sizes_with_pieces[3]: 1})
    if len(sizes_with_pieces) >= 5:
        test_ratios.append({sizes_with_pieces[0]: 1, sizes_with_pieces[1]: 2,
                           sizes_with_pieces[2]: 1, sizes_with_pieces[3]: 1, sizes_with_pieces[4]: 1})

    # A large 6-bundle marker
    if len(sizes_with_pieces) >= 7:
        test_ratios.append({
            sizes_with_pieces[0]: 1, sizes_with_pieces[1]: 1,
            sizes_with_pieces[2]: 1, sizes_with_pieces[3]: 1,
            sizes_with_pieces[4]: 1, sizes_with_pieces[5]: 1,
        })

    print(f"Testing {len(test_ratios)} marker ratios")
    print("=" * 90)

    # ── Evaluate ──────────────────────────────────────────────────────
    max_area = max(
        sum(p['area'] * p['demand'] for p in pieces_by_size.get(s, []))
        for s in sizes_with_pieces
    )
    max_length = int((6 * max_area * 2) / strip_width_px) + 500
    packer = GPUPacker(strip_width_px, max_length)

    results = []

    for i, ratio in enumerate(test_ratios):
        ratio_str = '-'.join(str(ratio.get(s, 0)) for s in sizes_with_pieces)
        bc = sum(ratio.values())

        # Build piece list
        pieces_list = []
        for size, count in ratio.items():
            if count <= 0 or size not in pieces_by_size:
                continue
            for _ in range(count):
                for p in pieces_by_size[size]:
                    for _ in range(p['demand']):
                        pieces_list.append(p)

        n_pieces = len(pieces_list)
        print(f"\n[{i+1}/{len(test_ratios)}] Ratio: {ratio_str} (bc={bc}, {n_pieces} pieces)")

        # ── Step 1: Dual-sort BLF placement ──
        t0 = time.time()
        eff_blf, len_blf, placements, placed_area, cl_blf, sort_used = \
            dual_sort_with_positions(pieces_list, packer, strip_width_px, gpu_scale)
        t_blf = time.time() - t0

        print(f"  BLF ({sort_used}):  eff={eff_blf*100:.2f}%, "
              f"length={len_blf:.3f}yd, {cl_blf}px, {t_blf*1000:.0f}ms")

        if not placements:
            print("  Skipping — no placements")
            continue

        # ── Step 2: Coordinate descent ──
        # Rebuild container from placements (clean state)
        rebuild_container(placements, packer)

        t0 = time.time()
        placements_cd, cl_cd, total_moves, passes = coordinate_descent(
            placements, packer, strip_width_px, placed_area, max_passes=10,
        )
        t_cd = time.time() - t0

        eff_cd = placed_area / (strip_width_px * cl_cd) if cl_cd > 0 else 0
        len_cd = cl_cd / gpu_scale / 25.4 / 36

        delta_eff = (eff_cd - eff_blf) * 100
        delta_len = len_cd - len_blf
        pct_improvement = (1 - cl_cd / cl_blf) * 100 if cl_blf > 0 else 0

        print(f"  CD result:       eff={eff_cd*100:.2f}%, "
              f"length={len_cd:.3f}yd, {cl_cd}px, {t_cd*1000:.0f}ms")
        print(f"  Improvement:     {delta_eff:+.2f}% eff, "
              f"{delta_len:+.4f}yd, {pct_improvement:.2f}% shorter, "
              f"{total_moves} moves in {passes} passes")

        results.append({
            'ratio': ratio_str,
            'bc': bc,
            'n_pieces': n_pieces,
            'eff_blf': eff_blf,
            'eff_cd': eff_cd,
            'len_blf': len_blf,
            'len_cd': len_cd,
            'delta_eff_pct': delta_eff,
            'pct_shorter': pct_improvement,
            'moves': total_moves,
            'passes': passes,
            'time_blf_ms': t_blf * 1000,
            'time_cd_ms': t_cd * 1000,
        })

    # ── Summary ───────────────────────────────────────────────────────
    print("\n" + "=" * 90)
    print("SUMMARY")
    print("=" * 90)
    print(f"{'Ratio':<20} {'BC':>3} {'Pcs':>4} {'BLF%':>7} {'CD%':>7} "
          f"{'Delta':>7} {'%Short':>7} {'Moves':>6} {'BLF ms':>7} {'CD ms':>7}")
    print("-" * 90)

    total_delta = 0
    for r in results:
        print(f"{r['ratio']:<20} {r['bc']:>3} {r['n_pieces']:>4} "
              f"{r['eff_blf']*100:>7.2f} {r['eff_cd']*100:>7.2f} "
              f"{r['delta_eff_pct']:>+6.2f}% {r['pct_shorter']:>6.2f}% "
              f"{r['moves']:>6} {r['time_blf_ms']:>7.0f} {r['time_cd_ms']:>7.0f}")
        total_delta += r['delta_eff_pct']

    if results:
        avg_delta = total_delta / len(results)
        avg_pct_shorter = sum(r['pct_shorter'] for r in results) / len(results)
        avg_cd_ms = sum(r['time_cd_ms'] for r in results) / len(results)
        max_delta = max(r['delta_eff_pct'] for r in results)
        print("-" * 90)
        print(f"Average improvement: {avg_delta:+.2f}% efficiency, "
              f"{avg_pct_shorter:.2f}% shorter")
        print(f"Max improvement:     {max_delta:+.2f}%")
        print(f"Average CD time:     {avg_cd_ms:.0f}ms")


if __name__ == '__main__':
    run_experiment()
