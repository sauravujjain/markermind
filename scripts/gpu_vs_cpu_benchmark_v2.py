#!/usr/bin/env python3
"""
GPU vs CPU Benchmark v2 — Using Real Order Data.

Pulls 10 markers from 3 real orders in the database (correctly parsed via web UI),
runs GPU nesting (fine resolution) and CPU nesting (30s Spyrrow) on each,
saves GPU SVGs + PNGs and CPU SVGs for manual review.

Orders used:
  - Order2 (style 1): AAMA parser, material=SO1, width=60", 7 sizes (46-58)
  - C2509-0360 (3) (vt 201): VT DXF parser, material=101, width=59.75", 7 sizes (XS-3XL)
  - 25138 (check_count): AAMA parser, material=SHELL, width=54.25", 8 sizes (XS-3X)

Usage:
    conda activate nester
    python scripts/gpu_vs_cpu_benchmark_v2.py
"""

import sys
import os
import time
import logging
import numpy as np
from pathlib import Path
from typing import Dict, List

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# Direct imports to avoid __init__.py pulling in bcrypt etc.
import importlib
import importlib.util

# GPU nesting runner
_spec_gpu = importlib.util.spec_from_file_location(
    "gpu_nesting_runner",
    PROJECT_ROOT / "backend" / "backend" / "services" / "gpu_nesting_runner.py",
)
_mod_gpu = importlib.util.module_from_spec(_spec_gpu)
_spec_gpu.loader.exec_module(_mod_gpu)

load_pieces_for_material = _mod_gpu.load_pieces_for_material
evaluate_ratio_with_svg = _mod_gpu.evaluate_ratio_with_svg
GPUPacker = _mod_gpu.GPUPacker

# Spyrrow (CPU) nesting runner
_spec_cpu = importlib.util.spec_from_file_location(
    "spyrrow_nesting_runner",
    PROJECT_ROOT / "backend" / "backend" / "services" / "spyrrow_nesting_runner.py",
)
_mod_cpu = importlib.util.module_from_spec(_spec_cpu)
_spec_cpu.loader.exec_module(_mod_cpu)

load_pieces_for_spyrrow = _mod_cpu.load_pieces_for_spyrrow
nest_single_marker = _mod_cpu.nest_single_marker
export_marker_svg = _mod_cpu.export_marker_svg

logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
logger = logging.getLogger(__name__)

OUTPUT_DIR = PROJECT_ROOT / "experiment_results" / "gpu_vs_cpu_v2"


def swap_gpu_piece_axes(pieces_by_size):
    """Swap X/Y axes for GPU pieces — fixes patterns where grain runs along Y instead of X."""
    import cupy as _cp
    for size, pieces in pieces_by_size.items():
        for p in pieces:
            p['vertices_mm'] = [(y, x) for x, y in p['vertices_mm']]
            p['raster'] = p['raster'].T.copy()
            p['raster_gpu'] = _cp.asarray(p['raster'])
            p['raster_180'] = np.rot90(p['raster'], 2).copy()
            p['raster_180_gpu'] = _cp.asarray(p['raster_180'])
    return pieces_by_size


def swap_cpu_piece_axes(nesting_pieces):
    """Swap X/Y axes for Spyrrow pieces — fixes patterns where grain runs along Y instead of X."""
    for p in nesting_pieces:
        p.vertices = [(y, x) for x, y in p.vertices]
    return nesting_pieces

# ── Pattern definitions (from database) ──────────────────────────────

PATTERNS = {
    'A': {
        'name': 'Order2 / style 1',
        'dxf': str(PROJECT_ROOT / "uploads/patterns/275d4d77-d17e-4686-8ceb-d53cd67b83a4/style 1.dxf"),
        'rul': str(PROJECT_ROOT / "uploads/patterns/275d4d77-d17e-4686-8ceb-d53cd67b83a4/style 1.rul"),
        'file_type': 'aama',
        'material': 'SO1',
        'sizes': ['46', '48', '50', '52', '54', '56', '58'],
        'width_inches': 60.0,
        'swap_axes': True,  # This DXF has grain along Y; swap to X
    },
    'B': {
        'name': 'C2509-0360 (3) / vt 201',
        'dxf': str(PROJECT_ROOT / "uploads/patterns/9ccb01fd-32b7-49a3-a0e6-628030a810bb/vt 201 (2).dxf"),
        'rul': None,
        'file_type': 'vt_dxf',
        'material': '201',  # VT DXF uses material from pattern, "201" for filtering
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

# 10 markers across patterns and BCs (ratios from actual nesting results in DB)
TEST_MARKERS = [
    # Pattern A: AAMA, SO1, 60", sizes=[46,48,50,52,54,56,58]
    {'pattern': 'A', 'label': 'A-bc1',  'ratio': {'50': 1}},
    {'pattern': 'A', 'label': 'A-bc2',  'ratio': {'50': 1, '54': 1}},
    {'pattern': 'A', 'label': 'A-bc4',  'ratio': {'50': 2, '54': 1, '56': 1}},
    {'pattern': 'A', 'label': 'A-bc6',  'ratio': {'50': 5, '54': 1}},

    # Pattern B: VT DXF, 201, 59.75", sizes=[XS,S,M,L,XL,2XL,3XL]
    {'pattern': 'B', 'label': 'B-bc1',  'ratio': {'M': 1}},
    {'pattern': 'B', 'label': 'B-bc3',  'ratio': {'XS': 1, 'M': 1, 'L': 1}},
    {'pattern': 'B', 'label': 'B-bc5',  'ratio': {'XL': 1, '2XL': 1, '3XL': 3}},

    # Pattern C: AAMA, SHELL, 54.25", sizes=[XS,S,M,L,XL,1X,2X,3X]
    {'pattern': 'C', 'label': 'C-bc1',  'ratio': {'XS': 1}},
    {'pattern': 'C', 'label': 'C-bc2',  'ratio': {'M': 1, '3X': 1}},
    {'pattern': 'C', 'label': 'C-bc4',  'ratio': {'XL': 2, '2X': 1, '3X': 1}},
]


def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # Verify all pattern files exist
    for key, pat in PATTERNS.items():
        assert Path(pat['dxf']).exists(), f"DXF not found: {pat['dxf']}"
        if pat['rul']:
            assert Path(pat['rul']).exists(), f"RUL not found: {pat['rul']}"
        print(f"Pattern {key}: {pat['name']} ({pat['file_type']})")

    # ── Load pieces for each pattern ──────────────────────────────────
    gpu_pieces = {}     # pattern_key -> pieces_by_size (GPU rasterized)
    cpu_pieces = {}     # pattern_key -> (nesting_pieces, piece_config)
    packers = {}        # pattern_key -> GPUPacker

    GPU_FINE_SCALE = 0.30  # Fine resolution for visual comparison

    for key, pat in PATTERNS.items():
        print(f"\n{'='*60}")
        print(f"Loading Pattern {key}: {pat['name']}")
        print(f"  Parser: {pat['file_type']}, Material: {pat['material']}")
        print(f"  Width: {pat['width_inches']}\", Sizes: {pat['sizes']}")
        print(f"{'='*60}")

        # GPU: load and rasterize
        t0 = time.time()
        pieces_by_size = load_pieces_for_material(
            dxf_path=pat['dxf'],
            rul_path=pat['rul'],
            material=pat['material'],
            sizes=pat['sizes'],
            gpu_scale=GPU_FINE_SCALE,
            file_type=pat['file_type'],
        )
        gpu_t = time.time() - t0

        total_gpu_pieces = sum(len(v) for v in pieces_by_size.values())
        print(f"  GPU: Loaded {total_gpu_pieces} pieces in {gpu_t:.1f}s")
        for s in pat['sizes']:
            pcs = pieces_by_size.get(s, [])
            if pcs:
                print(f"    {s}: {len(pcs)} pieces")

        # Fix orientation for Pattern A: pieces have grain along Y, need swap to X
        if pat.get('swap_axes'):
            swap_gpu_piece_axes(pieces_by_size)
            print(f"  GPU: Applied X/Y axis swap (grain correction)")

        gpu_pieces[key] = pieces_by_size

        # Create packer
        fabric_width_mm = pat['width_inches'] * 25.4
        strip_width_px = int(fabric_width_mm * GPU_FINE_SCALE)
        max_area = max(
            sum(p['area'] * p['demand'] for p in pieces_by_size.get(s, []))
            for s in pat['sizes'] if s in pieces_by_size
        )
        max_length = int((6 * max_area * 2) / strip_width_px) + 500
        packer = GPUPacker(strip_width_px, max_length)
        packers[key] = (packer, strip_width_px)

        # CPU: load for Spyrrow
        t0 = time.time()
        nesting_pieces, piece_config = load_pieces_for_spyrrow(
            dxf_path=pat['dxf'],
            rul_path=pat['rul'],
            material=pat['material'],
            sizes=pat['sizes'],
            allowed_rotations=[0, 180],
            file_type=pat['file_type'],
        )
        cpu_t = time.time() - t0

        if pat.get('swap_axes'):
            swap_cpu_piece_axes(nesting_pieces)
            print(f"  CPU: Applied X/Y axis swap (grain correction)")

        print(f"  CPU: Loaded {len(nesting_pieces)} pieces in {cpu_t:.1f}s")
        cpu_pieces[key] = (nesting_pieces, piece_config)

    # ── Run benchmarks ────────────────────────────────────────────────
    print(f"\n{'='*100}")
    print(f"BENCHMARK: {len(TEST_MARKERS)} markers, GPU (0.3px/mm dual-sort) vs CPU (30s Spyrrow)")
    print(f"{'='*100}")

    results = []

    for tm in TEST_MARKERS:
        key = tm['pattern']
        pat = PATTERNS[key]
        label = tm['label']
        ratio = tm['ratio']
        bc = sum(ratio.values())
        fabric_width_mm = pat['width_inches'] * 25.4

        print(f"\n--- {label}: Pattern {key}, bc={bc}, ratio={ratio} ---")

        # GPU nest
        packer, strip_width_px = packers[key]
        t0 = time.time()
        gpu_eff, gpu_len_yd, gpu_svg, gpu_perim = evaluate_ratio_with_svg(
            pieces_by_size=gpu_pieces[key],
            ratio=ratio,
            packer=packer,
            strip_width_px=strip_width_px,
            gpu_scale=GPU_FINE_SCALE,
        )
        gpu_time = time.time() - t0

        # Save GPU SVG
        gpu_svg_path = OUTPUT_DIR / f"{label}_gpu.svg"
        gpu_svg_path.write_text(gpu_svg, encoding='utf-8')

        # Save GPU PNG
        current_length = int(gpu_len_yd * 914.4 * GPU_FINE_SCALE) if gpu_len_yd > 0 else 100
        png_bytes = packer.get_container_png(current_length)
        gpu_png_path = OUTPUT_DIR / f"{label}_gpu.png"
        gpu_png_path.write_bytes(png_bytes)

        print(f"  GPU: {gpu_eff*100:.2f}%, {gpu_len_yd:.3f}yd, {gpu_time:.1f}s → {gpu_svg_path.name}, {gpu_png_path.name}")

        # CPU nest (30s)
        nesting_pieces, piece_config = cpu_pieces[key]
        t0 = time.time()
        cpu_result = nest_single_marker(
            ratio=ratio,
            nesting_pieces=nesting_pieces,
            piece_config=piece_config,
            fabric_width_mm=fabric_width_mm,
            piece_buffer_mm=0.0,
            edge_buffer_mm=0.0,
            time_limit=30.0,
            rotation_mode='free',
            quadtree_depth=4,
            early_termination=True,
            seed=42,
        )
        cpu_time = time.time() - t0

        cpu_eff = cpu_result['utilization']
        cpu_len_yd = cpu_result['length_yards']

        # Save CPU SVG
        if cpu_result['solution']:
            cpu_svg = export_marker_svg(cpu_result, fabric_width_mm)
            cpu_svg_path = OUTPUT_DIR / f"{label}_cpu.svg"
            cpu_svg_path.write_text(cpu_svg, encoding='utf-8')
            print(f"  CPU: {cpu_eff*100:.2f}%, {cpu_len_yd:.3f}yd, {cpu_time:.1f}s → {cpu_svg_path.name}")
        else:
            print(f"  CPU: FAILED (no solution)")

        gap = (cpu_eff - gpu_eff) * 100

        results.append({
            'label': label,
            'pattern': key,
            'bc': bc,
            'ratio': ratio,
            'gpu_eff': gpu_eff * 100,
            'gpu_len': gpu_len_yd,
            'gpu_time': gpu_time,
            'cpu_eff': cpu_eff * 100,
            'cpu_len': cpu_len_yd,
            'cpu_time': cpu_time,
            'gap': gap,
        })

    # ── Summary Table ─────────────────────────────────────────────────
    print(f"\n{'='*110}")
    print(f"SUMMARY: GPU vs CPU (30s Spyrrow)")
    print(f"{'='*110}")
    print(f"{'Label':<12} {'Pat':>3} {'BC':>3} {'GPU%':>8} {'CPU%':>8} {'Gap':>7} {'GPU_yd':>8} {'CPU_yd':>8} {'GPU_t':>6} {'CPU_t':>6}")
    print(f"{'-'*110}")

    total_gap = 0
    for r in results:
        print(f"{r['label']:<12} {r['pattern']:>3} {r['bc']:>3} "
              f"{r['gpu_eff']:>8.2f} {r['cpu_eff']:>8.2f} {r['gap']:>+7.2f} "
              f"{r['gpu_len']:>8.3f} {r['cpu_len']:>8.3f} "
              f"{r['gpu_time']:>5.1f}s {r['cpu_time']:>5.1f}s")
        total_gap += r['gap']

    avg_gap = total_gap / len(results) if results else 0
    print(f"\nAvg CPU-GPU gap: {avg_gap:+.2f}%")
    print(f"Files saved to: {OUTPUT_DIR}/")
    print(f"Review: *_gpu.svg, *_gpu.png, *_cpu.svg for each marker")


if __name__ == '__main__':
    main()
