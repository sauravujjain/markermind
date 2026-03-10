# Nesting Research: Sparrow Internals & Improvement Roadmap

*Date: 2026-02-26*

---

## Table of Contents

1. [Software Stack](#1-software-stack)
2. [How Sparrow Actually Works](#2-how-sparrow-actually-works)
3. [jagua-rs Collision Detection](#3-jagua-rs-collision-detection)
4. [All Configuration Parameters](#4-all-configuration-parameters)
5. [What We CAN vs CANNOT Customize](#5-what-we-can-vs-cannot-customize)
6. [What Our Wrapper Currently Exposes](#6-what-our-wrapper-currently-exposes)
7. [Recent Ecosystem Updates](#7-recent-ecosystem-updates)
8. [Sparrow Benchmark Results](#8-sparrow-benchmark-results)
9. [Recent Academic Papers (2024-2026)](#9-recent-academic-papers-2024-2026)
10. [Actionable Improvements for MarkerMind](#10-actionable-improvements-for-markermind)
11. [Key References](#11-key-references)

---

## 1. Software Stack

Three layers:

| Layer | Name | Author | Language | Repository |
|-------|------|--------|----------|------------|
| Python wrapper | **spyrrow** v0.8.1 | Paul Durand-Lupinski (Reeverse Systems) | Python/Rust (PyO3) | [github.com/PaulDL-RS/spyrrow](https://github.com/PaulDL-RS/spyrrow) |
| Optimization engine | **sparrow** | Jeroen Gardeyn (KU Leuven) | Rust | [github.com/JeroenGar/sparrow](https://github.com/JeroenGar/sparrow) |
| Collision detection | **jagua-rs** | Jeroen Gardeyn (KU Leuven) | Rust | [github.com/JeroenGar/jagua-rs](https://github.com/JeroenGar/jagua-rs) |

**Academic papers:**
- Sparrow: ["An open-source heuristic to reboot 2D nesting research"](https://arxiv.org/abs/2509.13329) (EJOR, Sep 2025)
- jagua-rs: ["Decoupling Geometry from Optimization in 2D Irregular C&P Problems"](https://arxiv.org/abs/2508.08341) (INFORMS J. Computing)

---

## 2. How Sparrow Actually Works

### The Common Misconception

Sparrow is **NOT** a simple Bottom-Left Fill (BLF) heuristic. BLF is used **only once** — to create the initial starting solution. The actual optimization is a sophisticated two-phase metaheuristic.

### Core Idea: Feasibility Decomposition

Instead of directly minimizing strip length, Sparrow converts the problem into a sequence of feasibility subproblems:

> "Can all items be placed without collisions within this strip length?"

It then **progressively shrinks the strip**. The key insight from the paper: high-quality solutions are "rare oases in a vast desert of infeasibility." Algorithms that stay within the feasible region cannot make meaningful progress. Sparrow **allows temporary collisions** and uses Guided Local Search (GLS) to resolve them.

### Phase 1: Exploration (80% of time budget)

1. **Initial solution**: BLF heuristic creates a feasible starting configuration
2. **Iterative shrinking**: After each successful feasibility resolution, shrinks the strip by **Rx = 0.1%**
3. **Collision resolution via GLS**: When shrinking creates collisions, repositions items to resolve them
4. **When stuck**: Maintains a pool (S) of infeasible solutions. Selects a random infeasible solution and perturbs it by **swapping two large items**
5. **Termination**: Mx = 3 strikes (failed attempts), Nx = 200 iterations without improvement, or time limit TLx (default: 80% of total time)

### Phase 2: Compression (20% of time budget)

1. Takes the best feasible solution from exploration
2. Applies **progressively smaller shrink ratios** that decay linearly from **Rcs = 0.05%** to **Rce = 0.001%**
3. Always restores to the incumbent (best known) solution before attempting further compression
4. Only accepts feasible solutions; failed separations trigger restoration
5. **Termination**: Mc = 5 strikes, Nc = 100 iterations without improvement, or time limit TLc

### Item Repositioning Strategy (NOT BLF)

When resolving collisions, Sparrow uses **continuous sampling with local refinement**:

1. **Diverse samples (Tdiv)**: Random positions uniformly distributed across the strip
2. **Focused samples (Tfoc)**: Random positions near the item's current location
3. **Refinement**: Selects promising samples and applies **adaptive coordinate descent** to find local optimum
4. Returns the globally best position found

Items are repositioned in **randomized order** (not area-decreasing).

### GLS Collision Metric

The collision quantification combines three components:

- **Overlap proxy with hyperbolic decay**: Weighted sum of penetration depths between shape poles
- **Shape-based penalty**: Penalizes collisions involving large/concave shapes using geometric mean of square-root convex hull areas
- **Dynamic weight updating**: Colliding pairs get weights multiplied by m in [1.2, 2.0] (proportional to severity); non-colliding pairs decay by 0.95

### Summary of Hardcoded Hyperparameters

| Parameter | Value | Phase |
|-----------|-------|-------|
| Rx (exploration shrink ratio) | 0.1% | Exploration |
| Rcs (compression start ratio) | 0.05% | Compression |
| Rce (compression end ratio) | 0.001% | Compression |
| Mx (exploration max strikes) | 3 | Exploration |
| Mc (compression max strikes) | 5 | Compression |
| Nx (exploration max stale iterations) | 200 | Exploration |
| Nc (compression max stale iterations) | 100 | Compression |
| Mu (max weight multiplier) | 2.0 | GLS |
| Ml (min weight multiplier) | 1.2 | GLS |
| Md (weight decay factor) | 0.95 | GLS |

---

## 3. jagua-rs Collision Detection

jagua-rs does **NOT** use No-Fit Polygons (NFP). It uses **trigonometric collision detection** with hierarchical acceleration.

### Data Structures

1. **Quadtree**: Recursively divides 2D space into quadrants. Configurable max depth (default: 5 in jagua-rs, exposed as `quadtree_depth` in spyrrow with default 4). Stores hazard occupation status per node.

2. **Fail-Fast Surrogates**:
   - **Poles**: Inscribed circles derived from the Pole of Inaccessibility (interior point farthest from boundary). Cheap to test, high collision probability.
   - **Piers**: Line segments fully contained within shapes, covering narrow features like extremities.

3. **Hazard Proximity Grid**: Additional spatial optimization for fast spatial queries.

### Two-Phase Detection

| Phase | Method | Cost |
|-------|--------|------|
| **Broad** | Quadtree queries eliminate impossible collisions | Very fast |
| **Narrow** | Line segment intersection + point-in-polygon (ray-casting) | More expensive, only for unresolved cases |

### Key Advantage Over NFP

NFP-based methods require precomputing (m x n)^2 unique polygons for n shapes and m rotations. Trigonometric detection handles **continuous rotation directly** without precomputation. This is why Sparrow can support free rotation without discrete angle sets.

---

## 4. All Configuration Parameters

From spyrrow v0.8.1 `StripPackingConfig`:

```python
class StripPackingConfig:
    def __init__(
        self,
        early_termination: bool = True,
        quadtree_depth: int = 4,
        min_items_separation: Optional[float] = None,
        total_computation_time: Optional[int] = 600,
        exploration_time: Optional[int] = None,
        compression_time: Optional[int] = None,
        num_workers: Optional[int] = None,
        seed: Optional[int] = None,
    ) -> None:
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `early_termination` | bool | True | Stop early if no further improvement possible |
| `quadtree_depth` | int | 4 | Max depth of collision quadtree. 3=faster/less precise, 5=slower/more precise |
| `min_items_separation` | float/None | None | Minimum distance between packed items (mm). None = items can touch |
| `total_computation_time` | int/None | 600 | Total time budget (seconds). Auto-split 80/20 between phases |
| `exploration_time` | int/None | None | Exploration phase time (seconds). Must pair with `compression_time` |
| `compression_time` | int/None | None | Compression phase time (seconds). Must pair with `exploration_time` |
| `num_workers` | int/None | None | Parallel threads. None = auto-detect CPU cores |
| `seed` | int/None | None | Random seed for reproducibility |

**Time budget rule**: Either provide `total_computation_time` alone (auto-split 80/20), OR provide both `exploration_time` AND `compression_time`. Providing all three, or only one of the latter two, raises ValueError.

### Per-Item Configuration

```python
class Item:
    def __init__(
        self,
        id: str,
        shape: Sequence[tuple[float, float]],
        demand: int,
        allowed_orientations: Sequence[float] | None,
    ):
```

| Parameter | Description |
|-----------|-------------|
| `id` | Unique identifier across all items |
| `shape` | Ordered (x,y) polygon vertices |
| `demand` | Quantity to place (>0) |
| `allowed_orientations` | Angle list in degrees, or None for free rotation. Paper notes: "The algorithm is only very weakly sensitive to the length of the Sequence given" |

---

## 5. What We CAN vs CANNOT Customize

### CAN Customize

- Time budget and exploration/compression split
- Quadtree depth (collision detection precision)
- Minimum item separation (piece buffer at engine level)
- Number of worker threads
- Random seed
- Early termination on/off
- Allowed rotations per item (0, 180, free, any discrete set)
- Item geometry (polygon vertices) — we pre-flip for paired items
- Item demand (quantity per shape)

### CANNOT Customize (hardcoded in Rust)

- Sorting order of items within solver (randomized internally)
- GLS hyperparameters (Mu, Ml, Md, Rx, Rcs, Rce)
- Strike thresholds (Mx=3, Mc=5)
- Iteration limits (Nx=200, Nc=100)
- Number of diverse/focused repositioning samples
- Coordinate descent refinement parameters
- The two-phase structure (cannot skip either phase)
- Collision detection method (always trigonometric, no NFP option)
- Holes in items (NOT supported)
- Nesting items inside other items (NOT supported)
- Flip/mirror (NOT natively supported — we pre-generate flipped geometry)

---

## 6. What Our Wrapper Currently Exposes

Our `SpyrrowConfig` (in `nesting_engine/engine/spyrrow_engine.py`) wraps **4 of 8** available parameters:

```python
@dataclass
class SpyrrowConfig:
    time_limit: float = 60.0              # -> total_computation_time
    num_workers: int = 0                   # -> num_workers (0 -> None = auto)
    seed: Optional[int] = None            # -> seed
    early_termination: bool = True        # -> early_termination (ENABLED)
    quadtree_depth: int = 4               # -> quadtree_depth (3-5)
    min_items_separation: Optional[float] = None  # -> min_items_separation (maps from piece_buffer)
```

### Parameters Now Threaded Through (Feb 27, 2026)

| Parameter | Status | Default | Notes |
|-----------|--------|---------|-------|
| `quadtree_depth` | **ACTIVE** | 5 (UI), 4 (engine) | Frontend-configurable, stored in MarkerLayout |
| `min_items_separation` | **ACTIVE** | None (0 buffer) | Auto-mapped from `piece_buffer` if set |
| `early_termination` | **ACTIVE** | True | Solver stops when no improvement found |
| `exploration_time` + `compression_time` | Not yet | — | Override 80/20 split for short time budgets |

### Key Findings from Buffer Audit

- **No piece geometry inflation anywhere** in codebase — verified Feb 27
- `piece_buffer` and `edge_buffer` were stored but **never applied to Spyrrow** — now fixed:
  - `piece_buffer` → mapped to `min_items_separation` in SpyrrowConfig
  - `edge_buffer` → reduces effective container width (`strip_height`)
- GPU raster nesting correctly applies buffer as pixel offset during rasterization (separate mechanism)

---

## 7. Recent Ecosystem Updates

### Sparrow (last commit: Feb 25, 2026)

| Date | Commit | Change |
|------|--------|--------|
| Feb 25 | `0527ca44` | Bump jagua-rs dependency |
| Feb 22 | `eb84c3e0` | Fix duplicate orientation in continuous rotation sampling |
| Feb 18 | `08efdc2b` | Fix rotation normalization bug |
| Feb 12 | `eed07866` | Replace `rand_xoshiro` with `rand` |

Actively maintained. Bug fixes and stability — no major new features. Mature solver.

### jagua-rs (significant new features)

| Date | Feature | Significance |
|------|---------|-------------|
| **Dec 22, 2025** | **Multi-Strip Packing Problem (MSPP)** | Optimize multiple markers simultaneously. Directly relevant to cutplan optimization. |
| **Jan 5, 2026** | **External solution import** | Import solutions from external sources (e.g., GPU raster) into jagua-rs for validation or refinement. Currently strip packing only. |
| Feb 25, 2026 | Fix narrow concavities | Bug fix |
| Feb 10, 2026 | `change_strip_width`, `n_placed_items` | API improvements |

**Both MSPP and external solution import are NOT yet exposed through spyrrow Python wrapper.** These would need a spyrrow update or direct Rust integration.

### Spyrrow Release History

| Version | Date | Key Changes |
|---------|------|-------------|
| **v0.8.1** | Jan 6, 2026 | Fix: propagate num_workers to exploration phase (**we are on this**) |
| v0.8 | Nov 24, 2025 | API breaking change: typo correction in StripPackingConfig |
| v0.7.3 | Oct 23, 2025 | Edge case handling |
| v0.7.0 | Aug 28, 2025 | Continuous rotation; early termination |
| v0.6.0 | Jun 18, 2025 | Free rotation |
| v0.5.0 | Jun 12, 2025 | String-based IDs; rotation in degrees |

---

## 8. Sparrow Benchmark Results

From the paper (20 min runtime, 3 threads, 100 runs per instance):

| Instance | Sparrow | Previous SOTA | Solver | Improvement |
|----------|---------|---------------|--------|-------------|
| SWIM | 78.26% | 74.66% | FLD | +3.6 pp |
| SHIRTS | 89.66% | 88.21% | FLD | +1.5 pp |
| TROUSERS | 91.73% | 90.48% | ROMA | +1.3 pp |
| ALBANO | 89.47% | 88.01% | FLD | +1.5 pp |
| MARQUES | 90.93% | 89.97% | ROMA | +1.0 pp |
| JAKOBS1 | 86.16% | 82.86% | VLM | +3.3 pp |
| JAKOBS2 | 82.25% | 78.90% | VLM | +3.4 pp |
| FU | 93.10% | 92.63% | ROMA | +0.5 pp |
| SHAPES | 70.49% | 68.50% | FLD | +2.0 pp |
| DAGLI | 89.53% | 89.15% | ROMA | +0.4 pp |

Best-known solutions surpassed prior best on **ALL 13 standard instances**.

**Known weakness**: Homogeneous instances — sparrow cannot efficiently repeat compact local patterns. Relevant for our case with multiple copies of the same size bundle.

---

## 9. Recent Academic Papers (2024-2026)

### Paper A: "Nest Smarter, Not Harder" (Springer, May 2025)

- **Link**: [springer.com/10.1007/s10845-025-02620-6](https://link.springer.com/article/10.1007/s10845-025-02620-6)
- **Approach**: Hybrid vision-based DRL agent for 2D irregular packing with rotational placement
- **Results**: 97% faster computation, 11% utilization improvement vs open-source nesting
- **Key idea**: CNN extracts geometric semantics from rasterized layout, DRL policy selects rotation
- **Applicability for us**: **Low**. The 11% improvement is vs basic tools (SVGnest-class), not vs sparrow. We're constrained to 0/180 rotation. Would need ML infrastructure.

### Paper B: "GFPack++: Gradient Field Learning with Attention" (June 2024)

- **Link**: [arxiv.org/html/2406.07579v1](https://arxiv.org/html/2406.07579v1)
- **Approach**: Score-based diffusion model learning gradient fields for simultaneous placement
- **Results**: 77.04% on garment data (vs 69.53% XAtlas)
- **Applicability for us**: **None**. 77% utilization is well below sparrow's 85%+. Different paradigm (3D printing/UV packing).

### Paper C: "Fidelity-Adaptive Evolutionary Optimization" (J. Intelligent Manufacturing, 2025)

- **Link**: [springer.com/10.1007/s10845-024-02329-y](https://link.springer.com/article/10.1007/s10845-024-02329-y)
- **Approach**: Dynamically switch between nesting strategies of varying fidelity during optimization
- **Key idea**: Use low-fidelity evaluation early, high-fidelity for refinement
- **Applicability for us**: **Validates our approach**. This is exactly our GPU-screen-then-CPU-refine workflow.

### Paper D: Coordinate Descent for Rasterized Shapes (EJOR, 2022)

- **Link**: [sciencedirect.com/S0377221722002582](https://www.sciencedirect.com/science/article/pii/S0377221722002582)
- **Authors**: Umetani & Murakami
- **Approach**: Double scanline representation + coordinate descent on rasterized pieces
- **Key idea**: After initial placement, iteratively shift pieces by small amounts, checking overlaps via raster operations
- **Applicability for us**: **High**. Most relevant to our GPU raster approach. Could add a post-placement refinement step.

### Paper E: Backtracking Heuristic for Strip Packing (2025)

- **Link**: [sagepub.com/10.1177/00368504241301530](https://journals.sagepub.com/doi/full/10.1177/00368504241301530)
- **Approach**: Multi-start strategy with backtracking; studies impact of first piece placement
- **Key finding**: The first piece has outsized impact on the final result
- **Applicability for us**: **Medium**. For GPU nesting, trying multiple initial piece orderings (multi-start) is a cheap way to improve results.

### Paper F: Optimizing 2D Irregular Packing via Image Processing (Scientific Reports, 2025)

- **Link**: [nature.com/s41598-025-97202-0](https://www.nature.com/articles/s41598-025-97202-0)
- **Approach**: Pixel count-based overlap detection, overlap ratio optimization, dynamic expansion
- **Key idea**: Dynamic expansion — adjusting search space based on packing conditions
- **Applicability for us**: **Low-Medium**. Similar to our GPU raster approach. Dynamic expansion idea is interesting.

### Paper G: Multi-Objective EA with Incremental SVR (Annals of OR, 2025)

- **Link**: [springer.com/10.1007/s10479-025-06506-x](https://link.springer.com/article/10.1007/s10479-025-06506-x)
- **Approach**: Evolutionary algorithm with Support Vector Regression as surrogate fitness model
- **Key idea**: Use ML surrogate to avoid expensive NFP evaluations during EA search
- **Applicability for us**: **Low**. We already bypass NFP via raster (GPU) and trigonometric (jagua-rs).

---

## 10. Actionable Improvements for MarkerMind

### Tier 1: HIGH PRIORITY (do first, low effort)

#### 10.1 Tune Spyrrow Configuration

- Expose `quadtree_depth` — try 5 for complex garment curves
- Expose `min_items_separation` — use piece_buffer at engine level
- Expose `exploration_time` / `compression_time` — override 80/20 for short runs
- Verify `num_workers` is being set properly (not defaulting to 1)
- **Effort**: Config changes only
- **Expected gain**: 0.5-1%

#### 10.2 Multi-Start GPU Raster Screening

- Currently we try 2 sort strategies (`width_desc`, `area_desc`)
- Add 3-5 more: `height_desc`, random permutations, perimeter-descending
- Take the best result across all orderings
- **Effort**: Low (modify GPU evaluator loop)
- **Expected gain**: 0.5-1% better ranking accuracy

### Tier 2: HIGH PRIORITY (medium effort, biggest potential gain)

#### 10.3 Coordinate Descent Post-Refinement on GPU Raster

- After initial FFT-convolution placement, run coordinate descent:
  - For each placed piece, try shifting left/down by small pixel amounts
  - Check overlaps via simple raster operations (fast on GPU)
  - Accept moves that reduce total strip length
- **Reference**: Umetani & Murakami (EJOR 2022)
- **Effort**: Medium (new algorithm code within GPU framework)
- **Expected gain**: 1-2% tighter layouts

### Tier 3: MEDIUM PRIORITY (high effort, blocked on upstream)

#### 10.4 Warm-Start Sparrow from GPU Solutions

- Use jagua-rs "external solution import" (Jan 2026 feature)
- Import GPU raster placement positions as starting point for Sparrow
- Sparrow refines from an already-good solution instead of starting from BLF
- **Blocker**: Not yet exposed in spyrrow Python wrapper
- **Action**: File feature request on [PaulDL-RS/spyrrow](https://github.com/PaulDL-RS/spyrrow)
- **Expected gain**: 1-3%

#### 10.5 Multi-Strip Packing (MSPP)

- jagua-rs MSPP feature (Dec 2025) could optimize multiple markers simultaneously
- Share piece layouts across markers in a cutplan
- **Blocker**: Not yet exposed in spyrrow Python wrapper
- **Expected gain**: Better overall cutplan utilization

### Tier 4: LOW PRIORITY (interesting, not practical now)

| Approach | Why Skip |
|----------|----------|
| DRL rotation policy | We're constrained to 0/180; sparrow handles rotation well |
| Diffusion model packing (GFPack++) | 77% utilization, well below our 85%+ |
| Quantum approaches | Not production-ready |
| ML piece ordering | Needs training infra; multi-start is simpler |

---

## 11. Key References

### Core Solver

| Resource | Link |
|----------|------|
| Sparrow paper | [arxiv.org/abs/2509.13329](https://arxiv.org/abs/2509.13329) |
| jagua-rs paper | [arxiv.org/abs/2508.08341](https://arxiv.org/abs/2508.08341) |
| Sparrow GitHub | [github.com/JeroenGar/sparrow](https://github.com/JeroenGar/sparrow) |
| jagua-rs GitHub | [github.com/JeroenGar/jagua-rs](https://github.com/JeroenGar/jagua-rs) |
| Spyrrow GitHub | [github.com/PaulDL-RS/spyrrow](https://github.com/PaulDL-RS/spyrrow) |
| Spyrrow PyPI | [pypi.org/project/spyrrow](https://pypi.org/project/spyrrow/) |
| Spyrrow Docs | [spyrrow.readthedocs.io](https://spyrrow.readthedocs.io/) |

### Research Papers

| Paper | Link | Relevance |
|-------|------|-----------|
| Umetani & Murakami — Coordinate Descent for Rasterized Shapes (EJOR 2022) | [sciencedirect.com/S0377221722002582](https://www.sciencedirect.com/science/article/pii/S0377221722002582) | High |
| Fidelity-Adaptive Evolutionary Optimization (2025) | [springer.com/10.1007/s10845-024-02329-y](https://link.springer.com/article/10.1007/s10845-024-02329-y) | Validates our approach |
| Backtracking Heuristic — Multi-Start (2025) | [sagepub.com/10.1177/00368504241301530](https://journals.sagepub.com/doi/full/10.1177/00368504241301530) | Medium |
| Nest Smarter Not Harder — DRL (2025) | [springer.com/10.1007/s10845-025-02620-6](https://link.springer.com/article/10.1007/s10845-025-02620-6) | Low |
| Image Processing for 2D Packing (2025) | [nature.com/s41598-025-97202-0](https://www.nature.com/articles/s41598-025-97202-0) | Low |
| GFPack++ — Diffusion Model (2024) | [arxiv.org/html/2406.07579v1](https://arxiv.org/html/2406.07579v1) | None |
| Quantum Heuristic (2024) | [arxiv.org/abs/2402.17542](https://arxiv.org/abs/2402.17542) | None |

---

## Summary

We are on the **state-of-the-art open-source solver** (Sparrow). The 1.5% gap to Lectra FlexOffer is addressable through:

1. **Parameter tuning** (quadtree_depth, time split, num_workers) — quick wins
2. **Multi-start GPU screening** (more sort strategies) — quick wins
3. **Coordinate descent post-refinement** on GPU raster — biggest near-term gain
4. **Warm-start from GPU solutions** — biggest future gain, blocked on spyrrow wrapper

The solver itself (Sparrow's GLS + feasibility decomposition) is not the bottleneck. The bottleneck is likely in how we feed it (initial conditions, piece buffer handling) and how much time we give it.
