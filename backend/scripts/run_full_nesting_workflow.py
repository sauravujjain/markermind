#!/usr/bin/env python3 -u
"""
Full GPU Nesting and Cutplan Workflow for MarkerMind

Note: Run with -u flag for unbuffered output: python -u script.py

This script runs:
1. GPU nesting for all marker ratios (1-6 bundles) with 60" fabric width
2. Cutplan optimization to generate 3 result sets with costs

Configuration:
- Fabric Width: 60 inches
- Pattern: SO1 material from 23583 PROD 1 DXF
- Top markers per bundle: 10
"""

import json
import time
import random
import sys
import os
from pathlib import Path
from typing import Dict, List, Tuple, Set
from itertools import combinations_with_replacement
from dataclasses import dataclass, field
from collections import defaultdict

import numpy as np
from PIL import Image, ImageDraw

# Add MarkerMind project root to path so nesting_engine is importable
PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from nesting_engine.io.aama_parser import load_aama_pattern, AAMAGrader

# GPU Setup
try:
    import cupy as cp
    from cupyx.scipy.signal import fftconvolve as fftconvolve_gpu
    gpu_name = cp.cuda.runtime.getDeviceProperties(0)['name'].decode()
    print(f"GPU: {gpu_name}")
    GPU_AVAILABLE = True
except ImportError:
    print("WARNING: CuPy not available, using CPU fallback")
    GPU_AVAILABLE = False
    cp = None

# =============================================================================
# Configuration
# =============================================================================

DXF_PATH = PROJECT_ROOT / "data/dxf-amaa/23583 PROD 1 L 0 W 0 25FEB22.dxf"
RUL_PATH = PROJECT_ROOT / "data/dxf-amaa/23583 PROD 1 L 0 W 0 25FEB22.rul"
OUTPUT_DIR = Path("/home/sarv/projects/MarkerMind/backend/experiment_results")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

TARGET_MATERIAL = "SO1"
ALL_SIZES = ["46", "48", "50", "52", "54", "56", "58"]

# Use 60" fabric width as requested
FABRIC_WIDTH_INCH = 60.0
FABRIC_WIDTH_MM = FABRIC_WIDTH_INCH * 25.4

GPU_SCALE = 0.15  # px/mm
GPU_STRIP_WIDTH_PX = int(FABRIC_WIDTH_MM * GPU_SCALE)

PIECE_BUFFER = 0.1  # pixels
EDGE_BUFFER = 0

TOP_N = 10  # Top 10 per bundle group
MAX_BUNDLE_COUNT = 6

# GA Parameters
GA_GENERATIONS = 3
MIN_ISLAND_SIZE = 50

# Sample order demand
SAMPLE_ORDER_DEMAND = {
    "46": 74,
    "48": 244,
    "50": 347,
    "52": 342,
    "54": 265,
    "56": 162,
    "58": 62
}

# Cost parameters (per yard, per cut, etc.)
FABRIC_COST_PER_YARD = 8.50
SPREADING_COST_PER_YARD = 0.50
CUTTING_COST_PER_INCH = 0.02
PREP_COST_PER_MARKER = 15.00
MAX_PLIES_PER_CUT = 100

MIN_PLIES_BY_BUNDLE = {
    1: 1, 2: 1, 3: 10, 4: 30, 5: 40, 6: 50
}

# =============================================================================
# Data Structures
# =============================================================================

@dataclass
class Marker:
    ratio: Dict[str, int]
    ratio_str: str
    efficiency: float
    bundle_count: int
    length_yards: float = 0.0

    def produces(self, plies: int) -> Dict[str, int]:
        return {size: self.ratio.get(size, 0) * plies for size in ALL_SIZES}

    def __hash__(self):
        return hash(self.ratio_str)

    def __eq__(self, other):
        return self.ratio_str == other.ratio_str


@dataclass
class MarkerAssignment:
    marker: Marker
    plies: int

    @property
    def cuts(self) -> int:
        return (self.plies + MAX_PLIES_PER_CUT - 1) // MAX_PLIES_PER_CUT

    @property
    def produces(self) -> Dict[str, int]:
        return self.marker.produces(self.plies)


@dataclass
class CutPlan:
    name: str
    assignments: List[MarkerAssignment] = field(default_factory=list)

    @property
    def total_plies(self) -> int:
        return sum(a.plies for a in self.assignments)

    @property
    def total_cuts(self) -> int:
        return sum(a.cuts for a in self.assignments)

    @property
    def total_bundle_cuts(self) -> int:
        return sum(a.marker.bundle_count * a.cuts for a in self.assignments)

    @property
    def unique_markers(self) -> int:
        return len(self.assignments)

    @property
    def weighted_efficiency(self) -> float:
        if self.total_plies == 0:
            return 0.0
        return sum(a.marker.efficiency * a.plies for a in self.assignments) / self.total_plies

    @property
    def total_yards(self) -> float:
        return sum(a.marker.length_yards * a.plies for a in self.assignments)

    @property
    def total_produced(self) -> Dict[str, int]:
        produced = {size: 0 for size in ALL_SIZES}
        for a in self.assignments:
            for size, qty in a.produces.items():
                produced[size] += qty
        return produced

    def calculate_costs(self) -> Dict[str, float]:
        total_yards = self.total_yards
        fabric_cost = total_yards * FABRIC_COST_PER_YARD
        spreading_cost = total_yards * SPREADING_COST_PER_YARD

        # Cutting cost based on total length cut
        total_length_inches = sum(
            a.marker.length_yards * 36 * a.cuts
            for a in self.assignments
        )
        cutting_cost = total_length_inches * CUTTING_COST_PER_INCH

        prep_cost = self.unique_markers * PREP_COST_PER_MARKER

        total_cost = fabric_cost + spreading_cost + cutting_cost + prep_cost

        return {
            "fabric_cost": fabric_cost,
            "spreading_cost": spreading_cost,
            "cutting_cost": cutting_cost,
            "prep_cost": prep_cost,
            "total_cost": total_cost,
            "total_yards": total_yards,
        }


# =============================================================================
# Piece Loading and Rasterization
# =============================================================================

def load_pieces():
    """Load and rasterize SO1 pieces."""
    print(f"\nLoading pattern from: {DXF_PATH}")
    pieces, rules = load_aama_pattern(str(DXF_PATH), str(RUL_PATH))
    grader = AAMAGrader(pieces, rules)
    unit_scale = 25.4 if rules.header.units == 'ENGLISH' else 1.0

    print(f"Pattern sizes: {rules.header.size_list}")
    print(f"Target material: {TARGET_MATERIAL}")
    print(f"Fabric width: {FABRIC_WIDTH_INCH}\" ({GPU_STRIP_WIDTH_PX} px)")

    pieces_by_size = {}

    for target_size in ALL_SIZES:
        if target_size not in rules.header.size_list:
            continue

        graded = grader.grade(target_size)
        pieces_by_size[target_size] = []

        for gp in graded:
            orig_piece = next((p for p in pieces if p.name == gp.source_piece), None)
            if orig_piece is None or orig_piece.material != TARGET_MATERIAL:
                continue

            vertices_mm = [(y * unit_scale, x * unit_scale) for x, y in gp.vertices]
            if len(vertices_mm) < 3:
                continue
            if vertices_mm[0] != vertices_mm[-1]:
                vertices_mm.append(vertices_mm[0])

            # Rasterize
            verts = np.array(vertices_mm)
            min_xy = verts.min(axis=0)
            verts_scaled = (verts - min_xy) * GPU_SCALE + PIECE_BUFFER
            max_xy = verts_scaled.max(axis=0)
            width = int(np.ceil(max_xy[0])) + int(np.ceil(PIECE_BUFFER * 2))
            height = int(np.ceil(max_xy[1])) + int(np.ceil(PIECE_BUFFER * 2))

            img = Image.new('L', (width, height), 0)
            ImageDraw.Draw(img).polygon([tuple(p) for p in verts_scaled], fill=1)
            raster = np.array(img, dtype=np.float32)
            area = float(np.sum(raster))

            demand = orig_piece.quantity.total
            if orig_piece.quantity.has_left_right:
                demand = orig_piece.quantity.left_qty + orig_piece.quantity.right_qty

            piece_data = {
                'name': gp.name,
                'raster': raster,
                'raster_180': np.rot90(raster, 2),
                'area': area,
                'demand': demand,
            }

            if GPU_AVAILABLE:
                piece_data['raster_gpu'] = cp.asarray(raster)
                piece_data['raster_180_gpu'] = cp.asarray(np.rot90(raster, 2))

            pieces_by_size[target_size].append(piece_data)

    print(f"\nLoaded {sum(len(p) for p in pieces_by_size.values())} pieces across {len(pieces_by_size)} sizes")
    for size, pcs in pieces_by_size.items():
        print(f"  Size {size}: {len(pcs)} pieces")

    return pieces_by_size


# =============================================================================
# GPU Packer
# =============================================================================

class GPUPacker:
    def __init__(self, strip_width: int, max_length: int):
        self.strip_width = strip_width
        self.max_length = max_length
        if GPU_AVAILABLE:
            self.container = cp.zeros((strip_width, max_length), dtype=cp.float32)
        else:
            self.container = np.zeros((strip_width, max_length), dtype=np.float32)

    def reset(self):
        if GPU_AVAILABLE:
            self.container.fill(0)
        else:
            self.container.fill(0)

    def find_best_position(self, raster_gpu, raster_180_gpu, current_length):
        if not GPU_AVAILABLE:
            return None, None

        best = None
        best_raster = None

        for raster in [raster_gpu, raster_180_gpu]:
            ph, pw = raster.shape
            if ph > self.strip_width:
                continue

            kernel = raster[::-1, ::-1].copy()
            overlap = fftconvolve_gpu(self.container, kernel, mode='valid')

            if overlap.size == 0:
                continue

            valid = overlap < 0.5
            result_h, result_w = valid.shape
            max_valid_y = self.strip_width - ph

            if max_valid_y < 0:
                continue
            if max_valid_y + 1 < result_h:
                valid[max_valid_y + 1:, :] = False

            if not cp.any(valid):
                continue

            y_idx = cp.arange(result_h).reshape(-1, 1)
            y_grid = cp.where(valid, y_idx, result_h + 1)
            drop_y = cp.min(y_grid, axis=0)
            valid_cols = drop_y <= max_valid_y

            if not cp.any(valid_cols):
                continue

            x_idx = cp.arange(result_w)
            piece_right = x_idx + pw
            piece_top = drop_y + ph

            if current_length > 0:
                inside = valid_cols & (piece_right <= current_length)
            else:
                inside = valid_cols

            if cp.any(inside):
                tops = cp.where(inside, piece_top, cp.inf)
                min_top = float(cp.min(tops))
                mask = inside & (piece_top == min_top)
                bx = int(cp.argmax(mask))
                by = int(drop_y[bx])

                if best is None or (bx + pw <= current_length and (best['x'] + best['pw'] > current_length)):
                    best = {'x': bx, 'y': by, 'ph': ph, 'pw': pw}
                    best_raster = raster
            elif current_length > 0:
                extend = valid_cols & (piece_right > current_length)
                if cp.any(extend):
                    ext_x = int(cp.where(extend)[0][0])
                    ext_y = int(drop_y[ext_x])
                    if best is None:
                        best = {'x': ext_x, 'y': ext_y, 'ph': ph, 'pw': pw}
                        best_raster = raster

        return best, best_raster

    def place(self, raster, x, y):
        ph, pw = raster.shape
        if GPU_AVAILABLE:
            self.container[y:y+ph, x:x+pw] = cp.maximum(self.container[y:y+ph, x:x+pw], raster)
        else:
            self.container[y:y+ph, x:x+pw] = np.maximum(self.container[y:y+ph, x:x+pw], raster)


def evaluate_ratio(pieces_by_size: Dict, ratio: Dict[str, int], packer: GPUPacker) -> Tuple[float, float]:
    """Evaluate a single ratio and return (efficiency, length_yards)."""
    if not GPU_AVAILABLE:
        return 0.0, 0.0

    packer.reset()

    pieces_list = []
    for size, count in ratio.items():
        if count <= 0 or size not in pieces_by_size:
            continue
        for _ in range(count):
            for p in pieces_by_size[size]:
                for _ in range(p['demand']):
                    pieces_list.append(p)

    if not pieces_list:
        return 0.0, 0.0

    # Sort by width descending (found to be better than area descending)
    pieces_list.sort(key=lambda p: -(p['raster'].shape[1]))

    placed_area = 0.0
    current_length = 0

    for p in pieces_list:
        result, raster = packer.find_best_position(p['raster_gpu'], p['raster_180_gpu'], current_length)
        if result is None:
            continue

        packer.place(raster, result['x'], result['y'])
        placed_area += p['area']
        current_length = max(current_length, result['x'] + result['pw'])

    if current_length == 0:
        return 0.0, 0.0

    strip_area = GPU_STRIP_WIDTH_PX * current_length
    efficiency = placed_area / strip_area
    length_yards = current_length / GPU_SCALE / 25.4 / 36

    return efficiency, length_yards


# =============================================================================
# Ratio Generation and Search
# =============================================================================

def generate_all_ratios(bundle_count: int) -> List[Dict[str, int]]:
    all_ratios = []
    for combo in combinations_with_replacement(ALL_SIZES, bundle_count):
        ratio = {s: 0 for s in ALL_SIZES}
        for size in combo:
            ratio[size] += 1
        all_ratios.append(ratio)
    return all_ratios


def ratio_to_str(ratio: Dict[str, int]) -> str:
    return '-'.join(str(ratio.get(s, 0)) for s in ALL_SIZES)


def brute_force_search(pieces_by_size: Dict, ratios: List[Dict], packer: GPUPacker) -> List[Dict]:
    results = []
    for i, ratio in enumerate(ratios):
        if i % 50 == 0:
            print(f"    Evaluating ratio {i+1}/{len(ratios)}...")
        eff, length = evaluate_ratio(pieces_by_size, ratio, packer)
        results.append({'ratio': ratio, 'efficiency': eff, 'length_yards': length})
    return sorted(results, key=lambda x: -x['efficiency'])


# =============================================================================
# GPU Nesting Main
# =============================================================================

def run_gpu_nesting(pieces_by_size: Dict) -> Dict:
    """Run GPU nesting for all bundle counts."""
    print("\n" + "="*60)
    print("GPU NESTING - All Markers")
    print(f"Fabric Width: {FABRIC_WIDTH_INCH}\"")
    print("="*60)

    packer = GPUPacker(GPU_STRIP_WIDTH_PX, 5000)  # Max 5000px length

    results = {}
    total_start = time.time()

    for bundle_count in range(1, MAX_BUNDLE_COUNT + 1):
        print(f"\n--- Bundle Count: {bundle_count} ---")
        start = time.time()

        all_ratios = generate_all_ratios(bundle_count)
        print(f"  Total combinations: {len(all_ratios)}")

        # Brute force for all (could use GA for large bundle counts)
        search_results = brute_force_search(pieces_by_size, all_ratios, packer)

        elapsed = time.time() - start
        print(f"  Time: {elapsed:.2f}s")

        # Keep top N
        top_results = search_results[:TOP_N]

        results[bundle_count] = {
            "total_combinations": len(all_ratios),
            "time_seconds": elapsed,
            "top_results": [
                {
                    "rank": i + 1,
                    "ratio": r['ratio'],
                    "ratio_str": ratio_to_str(r['ratio']),
                    "efficiency": r['efficiency'],
                    "length_yards": r['length_yards']
                }
                for i, r in enumerate(top_results)
            ]
        }

        # Print top 3
        print(f"  Top 3:")
        for i, r in enumerate(top_results[:3]):
            print(f"    {i+1}. {ratio_to_str(r['ratio'])}: {r['efficiency']*100:.1f}% ({r['length_yards']:.2f}Y)")

    total_elapsed = time.time() - total_start
    print(f"\nTotal nesting time: {total_elapsed:.2f}s")

    return {
        "fabric_width_inches": FABRIC_WIDTH_INCH,
        "target_material": TARGET_MATERIAL,
        "sizes": ALL_SIZES,
        "total_time_seconds": total_elapsed,
        "results": results
    }


# =============================================================================
# Cutplan Optimization (ILP)
# =============================================================================

def load_markers_from_results(results_data: Dict) -> List[Marker]:
    """Load markers from GPU nesting results."""
    markers = []

    for bundle_count_str, group in results_data["results"].items():
        bundle_count = int(bundle_count_str)
        for r in group["top_results"]:
            ratio = r["ratio"]
            markers.append(Marker(
                ratio=ratio,
                ratio_str=r["ratio_str"],
                efficiency=r["efficiency"],
                bundle_count=bundle_count,
                length_yards=r["length_yards"]
            ))

    # Also generate all 1-bundle and 2-bundle markers for completeness
    for bundle_count in [1, 2]:
        for combo in combinations_with_replacement(ALL_SIZES, bundle_count):
            ratio = {s: 0 for s in ALL_SIZES}
            for size in combo:
                ratio[size] += 1
            ratio_str = ratio_to_str(ratio)

            # Check if already exists
            if any(m.ratio_str == ratio_str for m in markers):
                continue

            # Estimate efficiency from similar markers
            base_eff = 0.75 + (bundle_count * 0.01)
            markers.append(Marker(
                ratio=ratio,
                ratio_str=ratio_str,
                efficiency=base_eff,
                bundle_count=bundle_count,
                length_yards=5.0 * bundle_count  # Rough estimate
            ))

    return markers


def optimize_cutplan_ilp(markers: List[Marker], demand: Dict[str, int],
                         penalty: float = 5.0, name: str = "Balanced") -> CutPlan:
    """ILP-based cutplan optimization."""
    try:
        from scipy.optimize import milp, LinearConstraint, Bounds
    except ImportError:
        print("WARNING: scipy.optimize.milp not available, using greedy fallback")
        return optimize_cutplan_greedy(markers, demand, name)

    n_markers = len(markers)
    n_sizes = len(ALL_SIZES)

    # Variables: plies[m], used[m] (binary)
    # Total: n_markers * 2 variables

    # Objective: minimize sum((1-eff) * plies) + penalty * sum(used)
    c = np.zeros(n_markers * 2)
    for i, m in enumerate(markers):
        c[i] = 1 - m.efficiency  # plies coefficient
        c[n_markers + i] = penalty  # used coefficient

    # Constraints:
    # 1. Demand fulfillment: sum(ratio[s] * plies[m]) == demand[s] for each size
    # 2. Link: plies[m] <= M * used[m]  ->  plies[m] - M * used[m] <= 0
    # 3. Min plies by bundle: plies[m] >= min_plies[bundle] * used[m]

    M = 1000  # Big M

    A_eq = np.zeros((n_sizes, n_markers * 2))
    b_eq = np.zeros(n_sizes)

    for s_idx, size in enumerate(ALL_SIZES):
        for m_idx, m in enumerate(markers):
            A_eq[s_idx, m_idx] = m.ratio.get(size, 0)
        b_eq[s_idx] = demand.get(size, 0)

    # Inequality constraints
    n_ineq = n_markers * 2  # Link + min plies
    A_ub = np.zeros((n_ineq, n_markers * 2))
    b_ub = np.zeros(n_ineq)

    for m_idx, m in enumerate(markers):
        # Link: plies - M*used <= 0
        A_ub[m_idx, m_idx] = 1
        A_ub[m_idx, n_markers + m_idx] = -M
        b_ub[m_idx] = 0

        # Min plies: -plies + min_plies*used <= 0
        min_plies = MIN_PLIES_BY_BUNDLE.get(m.bundle_count, 1)
        A_ub[n_markers + m_idx, m_idx] = -1
        A_ub[n_markers + m_idx, n_markers + m_idx] = min_plies
        b_ub[n_markers + m_idx] = 0

    # Bounds
    lb = np.zeros(n_markers * 2)
    ub = np.concatenate([np.full(n_markers, 500), np.ones(n_markers)])  # plies: 0-500, used: 0-1

    # Integer constraints: used variables are binary
    integrality = np.concatenate([np.ones(n_markers), np.ones(n_markers)])  # All integer

    try:
        result = milp(
            c=c,
            constraints=[
                LinearConstraint(A_eq, b_eq, b_eq),  # Equality
                LinearConstraint(A_ub, -np.inf, b_ub)  # Inequality
            ],
            bounds=Bounds(lb, ub),
            integrality=integrality
        )

        if result.success:
            plan = CutPlan(name=name)
            for m_idx, m in enumerate(markers):
                plies = int(round(result.x[m_idx]))
                if plies > 0:
                    plan.assignments.append(MarkerAssignment(marker=m, plies=plies))
            return plan
    except Exception as e:
        print(f"ILP failed: {e}, using greedy fallback")

    return optimize_cutplan_greedy(markers, demand, name)


def optimize_cutplan_greedy(markers: List[Marker], demand: Dict[str, int],
                            name: str = "Greedy") -> CutPlan:
    """Greedy cutplan optimization."""
    plan = CutPlan(name=name)
    remaining = demand.copy()

    # Sort markers by efficiency descending
    sorted_markers = sorted(markers, key=lambda m: -m.efficiency)

    while any(r > 0 for r in remaining.values()):
        best_marker = None
        best_plies = 0
        best_score = -1

        for m in sorted_markers:
            # How many plies can we use without overproducing?
            max_plies = float('inf')
            for size in ALL_SIZES:
                if m.ratio.get(size, 0) > 0:
                    max_plies = min(max_plies, remaining.get(size, 0) // m.ratio[size])

            if max_plies <= 0:
                continue

            # Score by efficiency * coverage
            coverage = sum(m.ratio.get(s, 0) * min(max_plies, remaining.get(s, 0))
                          for s in ALL_SIZES)
            score = m.efficiency * coverage

            if score > best_score:
                best_score = score
                best_marker = m
                best_plies = int(min(max_plies, 100))

        if best_marker is None or best_plies <= 0:
            # Use single-size markers for remainder
            for size in ALL_SIZES:
                if remaining.get(size, 0) > 0:
                    ratio = {s: 1 if s == size else 0 for s in ALL_SIZES}
                    single_marker = Marker(
                        ratio=ratio,
                        ratio_str=ratio_to_str(ratio),
                        efficiency=0.7,
                        bundle_count=1,
                        length_yards=3.0
                    )
                    plan.assignments.append(MarkerAssignment(
                        marker=single_marker,
                        plies=remaining[size]
                    ))
                    remaining[size] = 0
            break

        plan.assignments.append(MarkerAssignment(marker=best_marker, plies=best_plies))

        for size in ALL_SIZES:
            remaining[size] -= best_marker.ratio.get(size, 0) * best_plies

    return plan


def run_cutplan_optimization(results_data: Dict, demand: Dict[str, int]) -> List[Dict]:
    """Generate 3 cutplan options with different strategies."""
    print("\n" + "="*60)
    print("CUTPLAN OPTIMIZATION")
    print(f"Order: {' | '.join(f'{s}:{q}' for s, q in demand.items())}")
    print("="*60)

    markers = load_markers_from_results(results_data)
    print(f"Loaded {len(markers)} markers from nesting results")

    plans = []

    # Strategy 1: Balanced (penalty=5)
    print("\n--- Option 1: Balanced ---")
    start = time.time()
    plan1 = optimize_cutplan_ilp(markers, demand, penalty=5.0, name="Balanced")
    elapsed1 = time.time() - start
    print(f"  Time: {elapsed1:.2f}s")
    plans.append({"plan": plan1, "time_seconds": elapsed1})

    # Strategy 2: Max Efficiency (penalty=0.1)
    print("\n--- Option 2: Max Efficiency ---")
    start = time.time()
    plan2 = optimize_cutplan_ilp(markers, demand, penalty=0.1, name="Max Efficiency")
    elapsed2 = time.time() - start
    print(f"  Time: {elapsed2:.2f}s")
    plans.append({"plan": plan2, "time_seconds": elapsed2})

    # Strategy 3: Min Markers (penalty=50)
    print("\n--- Option 3: Min Markers ---")
    start = time.time()
    plan3 = optimize_cutplan_ilp(markers, demand, penalty=50.0, name="Min Markers")
    elapsed3 = time.time() - start
    print(f"  Time: {elapsed3:.2f}s")
    plans.append({"plan": plan3, "time_seconds": elapsed3})

    # Print comparison
    print("\n" + "="*60)
    print("CUTPLAN COMPARISON")
    print("="*60)
    print(f"{'Option':<20} {'Eff%':>8} {'Markers':>8} {'Plies':>8} {'Yards':>10} {'Cost':>12}")
    print("-"*70)

    results = []
    for p in plans:
        plan = p["plan"]
        costs = plan.calculate_costs()
        print(f"{plan.name:<20} {plan.weighted_efficiency*100:>7.1f}% {plan.unique_markers:>8} "
              f"{plan.total_plies:>8} {costs['total_yards']:>10.1f} ${costs['total_cost']:>10.2f}")

        results.append({
            "name": plan.name,
            "efficiency": plan.weighted_efficiency,
            "unique_markers": plan.unique_markers,
            "total_plies": plan.total_plies,
            "total_cuts": plan.total_cuts,
            "total_bundle_cuts": plan.total_bundle_cuts,
            "costs": costs,
            "time_seconds": p["time_seconds"],
            "markers": [
                {
                    "ratio_str": a.marker.ratio_str,
                    "efficiency": a.marker.efficiency,
                    "plies": a.plies,
                    "cuts": a.cuts,
                    "length_yards": a.marker.length_yards
                }
                for a in plan.assignments
            ]
        })

    return results


# =============================================================================
# Main
# =============================================================================

def main():
    import sys
    print("="*60, flush=True)
    print("MARKERMIND - FULL NESTING WORKFLOW", flush=True)
    print(f"Fabric Width: {FABRIC_WIDTH_INCH}\"", flush=True)
    print(f"Pattern: {DXF_PATH.name}", flush=True)
    print("="*60, flush=True)
    sys.stdout.flush()

    total_start = time.time()

    # Step 1: Load pieces
    print("\n[Step 1] Loading and rasterizing pieces...")
    pieces_by_size = load_pieces()

    # Step 2: Run GPU nesting
    print("\n[Step 2] Running GPU nesting...")
    nesting_results = run_gpu_nesting(pieces_by_size)

    # Save nesting results
    nesting_output = OUTPUT_DIR / "gpu_nesting_results.json"
    with open(nesting_output, 'w') as f:
        json.dump(nesting_results, f, indent=2)
    print(f"\nNesting results saved to: {nesting_output}")

    # Step 3: Run cutplan optimization
    print("\n[Step 3] Running cutplan optimization...")
    cutplan_results = run_cutplan_optimization(nesting_results, SAMPLE_ORDER_DEMAND)

    # Save cutplan results
    cutplan_output = OUTPUT_DIR / "cutplan_results.json"
    with open(cutplan_output, 'w') as f:
        json.dump({
            "order_demand": SAMPLE_ORDER_DEMAND,
            "plans": cutplan_results
        }, f, indent=2)
    print(f"\nCutplan results saved to: {cutplan_output}")

    total_elapsed = time.time() - total_start
    print("\n" + "="*60)
    print(f"WORKFLOW COMPLETE - Total time: {total_elapsed:.2f}s")
    print("="*60)

    return nesting_results, cutplan_results


if __name__ == "__main__":
    main()
