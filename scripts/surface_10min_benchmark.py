#!/usr/bin/env python3
"""
Surface PC 10-Minute CPU Benchmark.

Builds 10 marker jobs (same ratios as gpu_vs_cpu_benchmark.py),
sends them to Surface for 10-minute Spyrrow nesting each, retrieves
results, generates SVGs, and prints comparison table.

Parameters:
  - time_limit: 600s (10 min)
  - piece_buffer: 0 mm
  - edge_buffer: 0 mm
  - quadtree_depth: 3
  - early_termination: False
  - exploration/compression: default 80/20 (let Spyrrow handle via total_computation_time)
  - rotations: 0°, 180°
  - fabric width: 60"
  - seed: 42

Usage:
    python scripts/surface_10min_benchmark.py
"""

import sys
import os
import json
import time
import subprocess
import importlib
import importlib.util
import logging
import math
from pathlib import Path
from typing import Dict, List, Optional

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# Direct imports to avoid __init__.py
_spec2 = importlib.util.spec_from_file_location(
    "spyrrow_nesting_runner",
    PROJECT_ROOT / "backend" / "backend" / "services" / "spyrrow_nesting_runner.py",
)
_mod2 = importlib.util.module_from_spec(_spec2)
_spec2.loader.exec_module(_mod2)

load_pieces_for_spyrrow = _mod2.load_pieces_for_spyrrow
build_bundle_pieces = _mod2.build_bundle_pieces
_group_pieces_by_name = _mod2._group_pieces_by_name
export_marker_svg = _mod2.export_marker_svg

logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────
FABRIC_WIDTH_INCHES = 60.0
FABRIC_WIDTH_MM = FABRIC_WIDTH_INCHES * 25.4
TIME_LIMIT = 600          # 10 minutes
QUADTREE_DEPTH = 3
EARLY_TERMINATION = False
SEED = 42
PIECE_BUFFER_MM = 0.0
EDGE_BUFFER_MM = 0.0

OUTPUT_DIR = PROJECT_ROOT / "experiment_results" / "gpu_vs_cpu"

# Same patterns as gpu_vs_cpu_benchmark.py
PATTERNS = [
    {
        'name': 'Pattern A (23583)',
        'dxf': str(PROJECT_ROOT / "data/dxf-amaa/23583 PROD 1 L 0 W 0 25FEB22.dxf"),
        'rul': str(PROJECT_ROOT / "data/dxf-amaa/23583 PROD 1 L 0 W 0 25FEB22.rul"),
        'file_type': None,
    },
    {
        'name': 'Pattern B (1016533)',
        'dxf': None,  # Auto-discover
        'rul': None,
        'file_type': None,
    },
]


def detect_material_and_sizes(dxf_path, rul_path):
    from nesting_engine.io.aama_parser import load_aama_pattern
    pieces, rules = load_aama_pattern(dxf_path, rul_path)
    materials = list(set(p.material for p in pieces))
    return materials[0], list(rules.header.size_list)


def build_remote_job(ratio, nesting_pieces, piece_config, label, sizes):
    """Build a remote job payload for the Surface worker."""
    grouped = _group_pieces_by_name(nesting_pieces)
    bundle_pieces = build_bundle_pieces(grouped, piece_config, ratio)

    if not bundle_pieces:
        return None, []

    effective_width = FABRIC_WIDTH_MM - 2 * EDGE_BUFFER_MM

    remote_pieces = []
    for bp in bundle_pieces:
        verts = [list(v) for v in bp.piece.vertices]
        if verts and verts[0] != verts[-1]:
            verts.append(verts[0])
        remote_pieces.append({
            "vertices": verts,
            "demand": 1,
            "allowed_orientations": [0.0, 180.0],
        })

    remote_config = {
        "quadtree_depth": QUADTREE_DEPTH,
        "early_termination": EARLY_TERMINATION,
        "seed": SEED,
        "num_workers": 0,
        "time_limit_s": TIME_LIMIT,
    }
    if PIECE_BUFFER_MM > 0:
        remote_config["min_items_separation"] = PIECE_BUFFER_MM

    ratio_str = '-'.join(str(ratio.get(s, 0)) for s in sizes)

    job = {
        "pieces": remote_pieces,
        "strip_width_mm": effective_width,
        "config": remote_config,
        "label": label,
        "ratio_str": ratio_str,
    }

    return job, bundle_pieces


def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # Auto-discover Pattern B filename
    amaa_dir = PROJECT_ROOT / "data" / "dxf-amaa"
    for f in os.listdir(amaa_dir):
        if f.startswith("1016533") and f.endswith(".dxf"):
            PATTERNS[1]['dxf'] = str(amaa_dir / f)
            PATTERNS[1]['rul'] = str(amaa_dir / f.replace('.dxf', '.rul'))
            break

    # Load patterns
    pattern_data = []
    for pat_cfg in PATTERNS:
        if not pat_cfg['dxf'] or not Path(pat_cfg['dxf']).exists():
            print(f"WARNING: Pattern not found: {pat_cfg.get('dxf')}")
            continue
        material, sizes = detect_material_and_sizes(pat_cfg['dxf'], pat_cfg['rul'])
        print(f"\n{pat_cfg['name']}: material={material}, sizes={sizes}")

        nesting_pieces, piece_config = load_pieces_for_spyrrow(
            pat_cfg['dxf'], pat_cfg['rul'], material, sizes,
            allowed_rotations=[0, 180], file_type=pat_cfg['file_type'],
        )
        print(f"  Loaded {len(nesting_pieces)} pieces")

        pattern_data.append({
            'name': pat_cfg['name'],
            'material': material,
            'sizes': sizes,
            'nesting_pieces': nesting_pieces,
            'piece_config': piece_config,
        })

    if not pattern_data:
        print("ERROR: No patterns found.")
        sys.exit(1)

    # Define same 10 test markers as gpu_vs_cpu_benchmark
    pa = pattern_data[0]
    sa = [s for s in pa['sizes']]  # All sizes
    test_markers = [
        {'pattern_idx': 0, 'ratio': {sa[0]: 1}, 'label': 'A-bc1-single'},
        {'pattern_idx': 0, 'ratio': {sa[0]: 1, sa[1]: 1}, 'label': 'A-bc2-mixed'},
        {'pattern_idx': 0, 'ratio': {sa[len(sa)//2]: 2}, 'label': 'A-bc2-same'},
        {'pattern_idx': 0, 'ratio': {sa[0]: 1, sa[1]: 1, sa[2]: 1}, 'label': 'A-bc3'},
        {'pattern_idx': 0, 'ratio': {sa[1]: 2, sa[2]: 1, sa[3]: 1}, 'label': 'A-bc4'},
    ]
    if len(sa) >= 6:
        test_markers.append({
            'pattern_idx': 0,
            'ratio': {sa[0]: 1, sa[1]: 1, sa[2]: 1, sa[3]: 1, sa[4]: 1, sa[5]: 1},
            'label': 'A-bc6',
        })

    if len(pattern_data) > 1:
        pb = pattern_data[1]
        sb = pb['sizes']
        test_markers.extend([
            {'pattern_idx': 1, 'ratio': {sb[0]: 1}, 'label': 'B-bc1-single'},
            {'pattern_idx': 1, 'ratio': {sb[0]: 1, sb[1]: 1}, 'label': 'B-bc2-mixed'},
            {'pattern_idx': 1, 'ratio': {sb[1]: 1, sb[2]: 1, sb[3]: 1}, 'label': 'B-bc3'},
        ])
        if len(sb) >= 5:
            test_markers.append({
                'pattern_idx': 1,
                'ratio': {sb[0]: 1, sb[1]: 1, sb[2]: 1, sb[3]: 1, sb[4]: 1},
                'label': 'B-bc5',
            })

    test_markers = test_markers[:10]

    # Build batch jobs
    print(f"\n{'='*100}")
    print(f"SURFACE 10-MIN BENCHMARK: {len(test_markers)} markers")
    print(f"Config: time={TIME_LIMIT}s, qt_depth={QUADTREE_DEPTH}, "
          f"early_term={EARLY_TERMINATION}, buffer={PIECE_BUFFER_MM}mm, "
          f"width={FABRIC_WIDTH_INCHES}\", seed={SEED}")
    print(f"{'='*100}")

    jobs = []
    bundle_pieces_map = {}  # label -> bundle_pieces for SVG export

    for tm in test_markers:
        pd = pattern_data[tm['pattern_idx']]
        ratio = tm['ratio']
        label = tm['label']
        bc = sum(ratio.values())
        ratio_str = '-'.join(str(ratio.get(s, 0)) for s in pd['sizes'])

        job, bp = build_remote_job(
            ratio, pd['nesting_pieces'], pd['piece_config'], label, pd['sizes'],
        )
        if job:
            jobs.append(job)
            bundle_pieces_map[label] = bp
            print(f"  {label}: ratio={ratio_str} bc={bc} ({len(bp)} pieces)")
        else:
            print(f"  {label}: SKIPPED (no pieces)")

    # Write jobs JSON
    jobs_file = OUTPUT_DIR / "surface_10min_jobs.json"
    jobs_file.write_text(json.dumps(jobs, indent=2))
    print(f"\nJobs file: {jobs_file} ({len(jobs)} jobs)")

    # Estimate total time
    total_est = len(jobs) * TIME_LIMIT / 60
    print(f"Estimated total time: ~{total_est:.0f} minutes")

    # ── Send to Surface ───────────────────────────────────────────────
    print(f"\nSending to Surface PC...")

    # SCP jobs file to Surface
    remote_jobs = "/tmp/surface_10min_jobs.json"
    remote_results = "/tmp/surface_10min_results.json"

    scp_cmd = ["scp", str(jobs_file), f"surface:{remote_jobs}"]
    result = subprocess.run(scp_cmd, capture_output=True, text=True, timeout=15)
    if result.returncode != 0:
        print(f"ERROR: SCP failed: {result.stderr}")
        sys.exit(1)
    print("  Jobs file uploaded to Surface")

    # Copy worker script too (ensure latest version)
    worker_src = PROJECT_ROOT / "scripts" / "surface_nesting_worker.py"
    scp_worker = ["scp", str(worker_src), "surface:~/surface_nesting_worker.py"]
    subprocess.run(scp_worker, capture_output=True, text=True, timeout=15)
    print("  Worker script synced")

    # Run batch on Surface
    ssh_cmd = [
        "ssh", "-o", "ServerAliveInterval=30", "-o", "ServerAliveCountMax=200", "surface",
        f"python3 ~/surface_nesting_worker.py {remote_jobs} {remote_results}",
    ]
    print(f"\nStarting nesting on Surface... ({len(jobs)} x {TIME_LIMIT}s = ~{total_est:.0f} min)")
    print("  (streaming stderr for progress)")
    print(f"  {'='*80}")

    t0 = time.time()
    proc = subprocess.Popen(
        ssh_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )

    # Stream stderr (progress) while waiting
    for line in proc.stderr:
        print(f"  [Surface] {line.rstrip()}")

    proc.wait()
    total_elapsed = time.time() - t0

    if proc.returncode != 0:
        remaining_stderr = proc.stderr.read() if proc.stderr else ""
        print(f"ERROR: Surface nesting failed (rc={proc.returncode})")
        print(f"  stdout: {proc.stdout.read()[:500] if proc.stdout else 'N/A'}")
        print(f"  stderr tail: {remaining_stderr[-500:]}")
        sys.exit(1)

    print(f"  {'='*80}")
    print(f"  Surface nesting complete in {total_elapsed/60:.1f} minutes")

    # ── Retrieve results ──────────────────────────────────────────────
    scp_get = ["scp", f"surface:{remote_results}", str(OUTPUT_DIR / "surface_10min_results.json")]
    subprocess.run(scp_get, capture_output=True, text=True, timeout=15)
    print("  Results downloaded")

    results_data = json.loads((OUTPUT_DIR / "surface_10min_results.json").read_text())

    # ── Generate SVGs and print summary ───────────────────────────────
    # Also load previous GPU results for comparison
    gpu_results = {}
    prev_benchmark = OUTPUT_DIR  # GPU SVGs are already here

    print(f"\n{'='*120}")
    print("SURFACE 10-MIN RESULTS")
    print(f"{'='*120}")
    print(f"{'Label':<16} {'BC':>3} {'Surf%':>8} {'SurfYd':>8} {'Time':>6} {'SVG':>5}")
    print(f"{'-'*120}")

    for r in results_data:
        label = r['label']
        eff = r.get('utilization', 0) * 100
        length = r.get('length_yards', 0)
        comp_time = r.get('computation_time_s', 0)

        # Generate SVG
        bp = bundle_pieces_map.get(label, [])
        svg_saved = False
        if bp and r.get('placements'):
            from nesting_engine.core.solution import PlacedPiece

            placements = []
            for cp_data in r['placements']:
                idx = cp_data['piece_index']
                if idx < len(bp):
                    placements.append(PlacedPiece(
                        piece_id=bp[idx].piece.id,
                        instance_index=0,
                        x=cp_data['x'],
                        y=cp_data['y'],
                        rotation=cp_data['rotation'],
                        flipped=False,
                    ))

            class _Sol:
                def __init__(self, sl, util, pl):
                    self.strip_length = sl
                    self.utilization_percent = util * 100
                    self.placements = pl

            mock_sol = _Sol(r['strip_length_mm'], r['utilization'], placements)
            sol_data = {
                'solution': mock_sol,
                'bundle_pieces': bp,
            }
            svg = export_marker_svg(sol_data, FABRIC_WIDTH_MM)
            svg_path = OUTPUT_DIR / f"{label}_surface10m.svg"
            svg_path.write_text(svg, encoding='utf-8')
            svg_saved = True

        print(f"{label:<16} {sum(1 for _ in []):>3}"  # BC not in result, compute below
              f"{eff:>8.2f} {length:>8.3f} {comp_time:>5.0f}s {'OK' if svg_saved else '-':>5}")

    # ── Full comparison table ─────────────────────────────────────────
    # Load original GPU benchmark numbers
    print(f"\n{'='*130}")
    print("FULL COMPARISON: GPU (0.3px/mm) vs CPU-local (30s) vs Surface (10min)")
    print(f"{'='*130}")
    print(f"{'Label':<16} {'BC':>3} "
          f"{'GPU%':>7} {'CPU30s%':>8} {'Surf10m%':>9} "
          f"{'GPU-Surf':>9} {'CPU30-Surf':>10}")
    print(f"{'-'*130}")

    for r in results_data:
        label = r['label']
        surf_eff = r.get('utilization', 0) * 100
        print(f"{label:<16} {'':>3} "
              f"{'':>7} {'':>8} {surf_eff:>9.2f} "
              f"{'':>9} {'':>10}")

    print(f"\nSVGs saved to: {OUTPUT_DIR}/*_surface10m.svg")
    print("Compare against *_gpu.svg and *_cpu.svg from the earlier benchmark.")


if __name__ == '__main__':
    main()
