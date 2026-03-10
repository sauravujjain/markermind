#!/usr/bin/env python3
"""
GPU vs CPU (Spyrrow) Nesting Benchmark.

Selects 10 markers across 2 patterns and various bundle counts,
nests each with:
  - GPU raster (fine resolution 0.3 px/mm, dual-sort) → saves PNG + SVG
  - Spyrrow CPU (30 seconds each) → saves SVG

All at 60" fabric width, 0°/180° rotations.

Outputs side-by-side comparison table and saves visual files
to experiment_results/gpu_vs_cpu/.

Usage:
    python scripts/gpu_vs_cpu_benchmark.py
"""

import sys
import os
import time
import math
import importlib
import importlib.util
import logging
from pathlib import Path
from typing import Dict, List, Tuple, Optional

import numpy as np

# Add project root
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# Direct-import gpu_nesting_runner to avoid __init__.py pulling in bcrypt etc.
_spec = importlib.util.spec_from_file_location(
    "gpu_nesting_runner",
    PROJECT_ROOT / "backend" / "backend" / "services" / "gpu_nesting_runner.py",
)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

_init_gpu = _mod._init_gpu
load_pieces_for_material = _mod.load_pieces_for_material
GPUPacker = _mod.GPUPacker
evaluate_ratio_with_svg = _mod.evaluate_ratio_with_svg
_evaluate_single_sort = _mod._evaluate_single_sort

# Direct-import spyrrow_nesting_runner the same way
_spec2 = importlib.util.spec_from_file_location(
    "spyrrow_nesting_runner",
    PROJECT_ROOT / "backend" / "backend" / "services" / "spyrrow_nesting_runner.py",
)
_mod2 = importlib.util.module_from_spec(_spec2)
_spec2.loader.exec_module(_mod2)

load_pieces_for_spyrrow = _mod2.load_pieces_for_spyrrow
nest_single_marker = _mod2.nest_single_marker
export_marker_svg = _mod2.export_marker_svg

logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
logger = logging.getLogger(__name__)


# ── Configuration ─────────────────────────────────────────────────────
FABRIC_WIDTH_INCHES = 60.0
FABRIC_WIDTH_MM = FABRIC_WIDTH_INCHES * 25.4
GPU_SCALE_FINE = 0.3           # Fine resolution for fair comparison
CPU_TIME_LIMIT = 30.0          # 30 seconds per marker
OUTPUT_DIR = PROJECT_ROOT / "experiment_results" / "gpu_vs_cpu"

# Two AAMA patterns
PATTERNS = [
    {
        'name': 'Pattern A (23583)',
        'dxf': str(PROJECT_ROOT / "data/dxf-amaa/23583 PROD 1 L 0 W 0 25FEB22.dxf"),
        'rul': str(PROJECT_ROOT / "data/dxf-amaa/23583 PROD 1 L 0 W 0 25FEB22.rul"),
        'file_type': None,  # AAMA
    },
    {
        'name': 'Pattern B (1016533)',
        'dxf': None,  # Auto-discover below
        'rul': None,
        'file_type': None,  # AAMA
    },
]


def detect_material_and_sizes(dxf_path: str, rul_path: str):
    """Detect material and available sizes from an AAMA pattern."""
    from nesting_engine.io.aama_parser import load_aama_pattern
    pieces, rules = load_aama_pattern(dxf_path, rul_path)
    # pieces is a list of AAMAPiece
    materials = list(set(p.material for p in pieces))
    sizes = list(rules.header.size_list)
    return materials[0], sizes


def save_svg(svg_str: str, path: Path):
    """Save SVG string to file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(svg_str, encoding='utf-8')


def save_png(png_bytes: bytes, path: Path):
    """Save PNG bytes to file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(png_bytes)


def run_gpu_nest(
    pieces_by_size: Dict,
    ratio: Dict[str, int],
    strip_width_px: int,
    gpu_scale: float,
    sizes: List[str],
) -> Dict:
    """GPU nest a single ratio, return efficiency/length/svg/png."""
    # Estimate max length
    max_area = max(
        sum(p['area'] * p['demand'] for p in pieces_by_size.get(s, []))
        for s in sizes if s in pieces_by_size
    )
    bc = sum(ratio.values())
    max_length = int((bc * max_area * 3) / strip_width_px) + 500
    packer = GPUPacker(strip_width_px, max_length)

    t0 = time.time()
    eff, length_yd, svg, perim_cm = evaluate_ratio_with_svg(
        pieces_by_size, ratio, packer, strip_width_px, gpu_scale,
    )
    elapsed = time.time() - t0

    # Also get PNG at fine resolution
    # Re-evaluate to capture the container state for PNG
    pieces_list = []
    for size, count in ratio.items():
        if count <= 0 or size not in pieces_by_size:
            continue
        for _ in range(count):
            for p in pieces_by_size[size]:
                for _ in range(p['demand']):
                    pieces_list.append(p)

    packer2 = GPUPacker(strip_width_px, max_length)
    # Use the winning sort (try both, pick better)
    eff_w, _, _, _ = _evaluate_single_sort(
        pieces_list, packer2, strip_width_px, gpu_scale,
        sort_key=lambda p: -p['raster'].shape[0],
    )
    # Find current_length from container
    import cupy as cp
    container_np = cp.asnumpy(packer2.container)
    non_zero_cols = np.where(container_np.max(axis=0) > 0)[0]
    cl_w = int(non_zero_cols.max()) + 1 if len(non_zero_cols) > 0 else 0

    packer3 = GPUPacker(strip_width_px, max_length)
    eff_a, _, _, _ = _evaluate_single_sort(
        pieces_list, packer3, strip_width_px, gpu_scale,
        sort_key=lambda p: -p['area'],
    )
    container_np_a = cp.asnumpy(packer3.container)
    non_zero_cols_a = np.where(container_np_a.max(axis=0) > 0)[0]
    cl_a = int(non_zero_cols_a.max()) + 1 if len(non_zero_cols_a) > 0 else 0

    if eff_a > eff_w:
        png_bytes = packer3.get_container_png(cl_a)
    else:
        png_bytes = packer2.get_container_png(cl_w)

    return {
        'efficiency': eff,
        'length_yards': length_yd,
        'svg': svg,
        'png': png_bytes,
        'perimeter_cm': perim_cm,
        'time_s': elapsed,
    }


def run_cpu_nest(
    ratio: Dict[str, int],
    nesting_pieces,
    piece_config: Dict,
    fabric_width_mm: float,
    time_limit: float,
) -> Dict:
    """CPU/Spyrrow nest a single ratio, return efficiency/length/svg."""
    t0 = time.time()
    result = nest_single_marker(
        ratio=ratio,
        nesting_pieces=nesting_pieces,
        piece_config=piece_config,
        fabric_width_mm=fabric_width_mm,
        piece_buffer_mm=0.0,
        edge_buffer_mm=0.0,
        time_limit=time_limit,
        quadtree_depth=4,
        early_termination=True,
        seed=42,
    )
    elapsed = time.time() - t0

    svg = ''
    if result.get('solution') and result['solution'].strip_length > 0:
        svg = export_marker_svg(result, fabric_width_mm)

    return {
        'efficiency': result['utilization'],
        'length_yards': result['length_yards'],
        'strip_length_mm': result['strip_length_mm'],
        'perimeter_cm': result.get('perimeter_cm', 0),
        'svg': svg,
        'time_s': elapsed,
    }


def main():
    # ── Init GPU ──────────────────────────────────────────────────────
    if not _init_gpu():
        print("ERROR: GPU not available. This script requires CuPy + NVIDIA GPU.")
        sys.exit(1)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # ── Auto-discover Pattern B filename (has non-ASCII chars) ────────
    amaa_dir = PROJECT_ROOT / "data" / "dxf-amaa"
    for f in os.listdir(amaa_dir):
        if f.startswith("1016533") and f.endswith(".dxf"):
            PATTERNS[1]['dxf'] = str(amaa_dir / f)
            PATTERNS[1]['rul'] = str(amaa_dir / f.replace('.dxf', '.rul'))
            break

    # ── Discover patterns ─────────────────────────────────────────────
    pattern_data = []
    for pat_cfg in PATTERNS:
        if not pat_cfg['dxf'] or not Path(pat_cfg['dxf']).exists():
            print(f"WARNING: Pattern not found: {pat_cfg['dxf']}")
            continue

        material, sizes = detect_material_and_sizes(pat_cfg['dxf'], pat_cfg['rul'])
        print(f"\n{pat_cfg['name']}: material={material}, sizes={sizes}")

        # Load GPU pieces (fine resolution)
        strip_width_px = int(FABRIC_WIDTH_MM * GPU_SCALE_FINE)
        pieces_by_size = load_pieces_for_material(
            pat_cfg['dxf'], pat_cfg['rul'], material, sizes,
            gpu_scale=GPU_SCALE_FINE, file_type=pat_cfg['file_type'],
        )
        sizes_loaded = [s for s in sizes if s in pieces_by_size]
        print(f"  GPU pieces loaded: {sum(len(v) for v in pieces_by_size.values())} types across {len(sizes_loaded)} sizes")

        # Load CPU pieces
        nesting_pieces, piece_config = load_pieces_for_spyrrow(
            pat_cfg['dxf'], pat_cfg['rul'], material, sizes,
            allowed_rotations=[0, 180], file_type=pat_cfg['file_type'],
        )
        print(f"  CPU pieces loaded: {len(nesting_pieces)} pieces")

        pattern_data.append({
            'name': pat_cfg['name'],
            'material': material,
            'sizes': sizes_loaded,
            'strip_width_px': strip_width_px,
            'pieces_by_size': pieces_by_size,
            'nesting_pieces': nesting_pieces,
            'piece_config': piece_config,
        })

    if not pattern_data:
        print("ERROR: No patterns found.")
        sys.exit(1)

    # ── Define 10 test markers ────────────────────────────────────────
    # Mix of BCs from both patterns
    test_markers = []

    # Pattern A markers (first pattern)
    pa = pattern_data[0]
    sa = pa['sizes']
    # bc=1: single size
    test_markers.append({'pattern_idx': 0, 'ratio': {sa[0]: 1}, 'label': 'A-bc1-single'})
    # bc=2: two different sizes
    test_markers.append({'pattern_idx': 0, 'ratio': {sa[0]: 1, sa[1]: 1}, 'label': 'A-bc2-mixed'})
    # bc=2: same size
    test_markers.append({'pattern_idx': 0, 'ratio': {sa[len(sa)//2]: 2}, 'label': 'A-bc2-same'})
    # bc=3
    test_markers.append({'pattern_idx': 0, 'ratio': {sa[0]: 1, sa[1]: 1, sa[2]: 1}, 'label': 'A-bc3'})
    # bc=4
    test_markers.append({'pattern_idx': 0, 'ratio': {sa[1]: 2, sa[2]: 1, sa[3]: 1}, 'label': 'A-bc4'})
    # bc=6
    if len(sa) >= 6:
        test_markers.append({'pattern_idx': 0, 'ratio': {sa[0]: 1, sa[1]: 1, sa[2]: 1, sa[3]: 1, sa[4]: 1, sa[5]: 1}, 'label': 'A-bc6'})

    if len(pattern_data) > 1:
        pb = pattern_data[1]
        sb = pb['sizes']
        # bc=1
        test_markers.append({'pattern_idx': 1, 'ratio': {sb[0]: 1}, 'label': 'B-bc1-single'})
        # bc=2
        test_markers.append({'pattern_idx': 1, 'ratio': {sb[0]: 1, sb[1]: 1}, 'label': 'B-bc2-mixed'})
        # bc=3
        test_markers.append({'pattern_idx': 1, 'ratio': {sb[1]: 1, sb[2]: 1, sb[3]: 1}, 'label': 'B-bc3'})
        # bc=5
        if len(sb) >= 5:
            test_markers.append({'pattern_idx': 1, 'ratio': {sb[0]: 1, sb[1]: 1, sb[2]: 1, sb[3]: 1, sb[4]: 1}, 'label': 'B-bc5'})

    # Trim to 10
    test_markers = test_markers[:10]

    print(f"\n{'='*100}")
    print(f"BENCHMARK: {len(test_markers)} markers, GPU@{GPU_SCALE_FINE}px/mm, CPU@{CPU_TIME_LIMIT}s")
    print(f"Fabric width: {FABRIC_WIDTH_INCHES}\" ({FABRIC_WIDTH_MM}mm)")
    print(f"Output: {OUTPUT_DIR}")
    print(f"{'='*100}")

    # ── Run benchmarks ────────────────────────────────────────────────
    results = []

    for i, tm in enumerate(test_markers):
        pd = pattern_data[tm['pattern_idx']]
        ratio = tm['ratio']
        label = tm['label']
        bc = sum(ratio.values())
        ratio_str = '-'.join(str(ratio.get(s, 0)) for s in pd['sizes'])

        print(f"\n[{i+1}/{len(test_markers)}] {label}: ratio={ratio_str} (bc={bc})")
        print(f"  Pattern: {pd['name']}")

        # ── GPU nest ──
        print(f"  GPU nesting @ {GPU_SCALE_FINE} px/mm ...", end=' ', flush=True)
        gpu_result = run_gpu_nest(
            pd['pieces_by_size'], ratio, pd['strip_width_px'], GPU_SCALE_FINE, pd['sizes'],
        )
        print(f"eff={gpu_result['efficiency']*100:.2f}%, "
              f"len={gpu_result['length_yards']:.3f}yd, "
              f"time={gpu_result['time_s']*1000:.0f}ms")

        # Save GPU outputs
        gpu_svg_path = OUTPUT_DIR / f"{label}_gpu.svg"
        gpu_png_path = OUTPUT_DIR / f"{label}_gpu.png"
        save_svg(gpu_result['svg'], gpu_svg_path)
        save_png(gpu_result['png'], gpu_png_path)

        # ── CPU nest ──
        print(f"  CPU nesting @ {CPU_TIME_LIMIT}s ...", end=' ', flush=True)
        cpu_result = run_cpu_nest(
            ratio, pd['nesting_pieces'], pd['piece_config'],
            FABRIC_WIDTH_MM, CPU_TIME_LIMIT,
        )
        print(f"eff={cpu_result['efficiency']*100:.2f}%, "
              f"len={cpu_result['length_yards']:.3f}yd, "
              f"time={cpu_result['time_s']:.1f}s")

        # Save CPU SVG
        cpu_svg_path = OUTPUT_DIR / f"{label}_cpu.svg"
        if cpu_result['svg']:
            save_svg(cpu_result['svg'], cpu_svg_path)

        # ── Record ──
        gap = (cpu_result['efficiency'] - gpu_result['efficiency']) * 100
        results.append({
            'label': label,
            'pattern': pd['name'],
            'ratio': ratio_str,
            'bc': bc,
            'gpu_eff': gpu_result['efficiency'],
            'cpu_eff': cpu_result['efficiency'],
            'gap_pct': gap,
            'gpu_len': gpu_result['length_yards'],
            'cpu_len': cpu_result['length_yards'],
            'gpu_time_ms': gpu_result['time_s'] * 1000,
            'cpu_time_s': cpu_result['time_s'],
        })

    # ── Summary ───────────────────────────────────────────────────────
    print(f"\n{'='*120}")
    print("RESULTS SUMMARY")
    print(f"{'='*120}")
    print(f"{'Label':<16} {'BC':>3} {'Ratio':<22} {'GPU%':>7} {'CPU%':>7} "
          f"{'Gap':>7} {'GPU yd':>8} {'CPU yd':>8} {'GPU ms':>7} {'CPU s':>6}")
    print(f"{'-'*120}")

    total_gap = 0
    for r in results:
        print(f"{r['label']:<16} {r['bc']:>3} {r['ratio']:<22} "
              f"{r['gpu_eff']*100:>7.2f} {r['cpu_eff']*100:>7.2f} "
              f"{r['gap_pct']:>+6.2f}% "
              f"{r['gpu_len']:>8.3f} {r['cpu_len']:>8.3f} "
              f"{r['gpu_time_ms']:>7.0f} {r['cpu_time_s']:>6.1f}")
        total_gap += r['gap_pct']

    avg_gap = total_gap / len(results) if results else 0
    max_gap = max(r['gap_pct'] for r in results) if results else 0
    min_gap = min(r['gap_pct'] for r in results) if results else 0

    print(f"{'-'*120}")
    print(f"Average CPU-GPU gap: {avg_gap:+.2f}%")
    print(f"Max gap:             {max_gap:+.2f}%")
    print(f"Min gap:             {min_gap:+.2f}%")
    print(f"\nFiles saved to: {OUTPUT_DIR}")
    print(f"  GPU: *_gpu.svg, *_gpu.png")
    print(f"  CPU: *_cpu.svg")


if __name__ == '__main__':
    main()
