# MarkerMind - Claude Code Guide

## CRITICAL: No Geometry Changes Without User Review

**Any code change that modifies, transforms, simplifies, filters, or otherwise alters the geometry (vertices/polygons) of pattern pieces MUST be reviewed and explicitly approved by the user before being applied. No exceptions.**

This applies to:
- Parser files (`aama_parser.py`, `dxf_block_parser.py`, `dxf_text_parser.py`, `vt_dxf_parser.py`, `dxf_parser.py`)
- Vertex cleaning/processing functions (`_clean_polygon_vertices`, `_simplify_polygon`, etc.)
- Any pre-processing in nesting runners (`spyrrow_nesting_runner.py`, `gpu_nesting_runner.py`) that touches piece vertices before solving
- Grading functions, coordinate transforms, polygon simplification, tolerance changes
- Any new function that operates on piece vertex data

**Procedure:** Present the proposed change, show the geometric impact (area diff, vertex count diff) on existing patterns, and wait for explicit approval before writing/editing code.

---

## CRITICAL: Parser Architecture — Self-Contained Units

**Each parser is a self-contained, deployable unit.** All logic specific to a parser — file reading, geometry extraction, vertex cleaning/preprocessing, validation — MUST live inside that parser's own file.

**Rationale:** In production, each customer deploys only the parser(s) they need (potentially 1 out of 20+). A parser must be deployable without dragging in code from other parsers.

**Rules:**
- **No shared vertex cleaning functions** across parsers. Each parser owns its own `clean_vertices_for_spyrrow()` (or equivalent) because each format has different geometry quirks.
- **No cross-parser imports.** Parser A must never import from Parser B.
- **Nesting runners (`spyrrow_nesting_runner.py`, `gpu_nesting_runner.py`) must not contain parser-specific geometry logic.** They call the parser's cleaning function, not their own.
- **When adding a new parser:** create a new file, include its own vertex cleaning, register it in `dxf_parser.py` orchestrator. Do not modify existing parsers.
- **The orchestrator (`dxf_parser.py`)** handles format detection and routing only — no geometry processing.

---

## CRITICAL: Business Units Are Always Garments

**Never calculate or report "pieces" — the business unit is always garments.** A garment has multiple pattern pieces (e.g., Front + Back + 2 Sleeves = 4 pieces), but all demand, production counts, shortfall analysis, and comparisons must be expressed in **garments**, not pieces.

---

## CRITICAL: GPU Nesting — Bundle Counts 1 and 2 Always Brute-Forced

**Bundle counts 1 and 2 MUST ALWAYS be brute-forced in GPU nesting.** They have trivially few ratios (≤28 for 7 sizes), complete in seconds, and their results are saved to the marker bank. Never apply sampling, prediction, or any shortcut to bc=1 or bc=2. This is a hard rule across all future iterations of GPU nesting.

---

## CRITICAL: Testing First Requirement

**Before any design, improvement, or feature work, you MUST first complete the full workflow test through the web application using Puppeteer.**

### Mandatory Testing Checklist

1. **Start the web app** (frontend: localhost:3000, backend: localhost:8000)
2. **Use Puppeteer** to test the full workflow:
   - Login/Register
   - Import an order (Excel/CSV)
   - Upload a pattern (DXF + RUL)
   - Assign pattern to order
   - Run GPU nesting
   - Generate cutplan (3 options with costs)
   - Review and approve a cutplan

3. **Verify end-to-end** that the full cycle completes with real data
4. **Log all times** for GPU nesting and cutplan generation
5. **Capture any errors** and fix them before moving to new features

This is a **toll booth** - no design or improvement work proceeds until a full successful test cycle is demonstrated through the web UI.

### App Startup (all 4 required)

The app needs **4 services**: PostgreSQL (Docker), Redis (Docker), Backend (uvicorn), Frontend (next dev). Run these commands in order:

```bash
# 1. Check what's already running
docker ps --filter "name=markermind" --format "{{.Names}} {{.Status}}"
lsof -i :8000 -sTCP:LISTEN 2>/dev/null && echo "Backend running" || echo "Backend not running"
lsof -i :3000 -sTCP:LISTEN 2>/dev/null && echo "Frontend running" || echo "Frontend not running"

# 2. Start Docker containers (PostgreSQL + Redis)
docker start markermind_postgres markermind_redis

# 3. Start Backend (bind 0.0.0.0 for remote access)
cd /home/sarv/projects/MarkerMind/backend && source venv/bin/activate && uvicorn backend.main:app --reload --host 0.0.0.0 > /home/sarv/projects/MarkerMind/logs/backend.log 2>&1 &

# 4. Start Frontend (bind 0.0.0.0 for remote access)
cd /home/sarv/projects/MarkerMind/frontend && npx next dev -H 0.0.0.0 > /home/sarv/projects/MarkerMind/logs/frontend.log 2>&1 &
```

**Tailscale fallback** (if VS Code port forwarding drops): `http://100.104.25.113:3000` / `:8000`

---

## CRITICAL: Development Server Mode

**During development, ALWAYS use dev mode (`next dev`), NOT production mode (`next start`).**

| What changed | Action needed | Why |
|---|---|---|
| **Frontend files** (`.tsx`, `.ts`, `.css`) | **Nothing.** HMR picks it up. | `next dev` watches the filesystem. |
| **Backend files** (`.py`) | **Nothing.** Uvicorn `--reload` restarts. | The `--reload` flag watches Python files. |
| **package.json / config files** | Restart the frontend dev server. | Config changes aren't covered by HMR. |

**Do NOT run `npx next build`.** That is only for production deployment.
**Do NOT kill and restart the frontend** after normal code edits — HMR handles it.

### If the frontend is stuck (rare):
```bash
kill $(lsof -t -i :3000) 2>/dev/null; sleep 1
cd /home/sarv/projects/MarkerMind/frontend && npx next dev -H 0.0.0.0 > /home/sarv/projects/MarkerMind/logs/frontend.log 2>&1 &
```

---

## Future Requirements & Roadmap

**See [`REQUIREMENTS_CHECKLIST.md`](./REQUIREMENTS_CHECKLIST.md)** for:
- Database robustness improvements (soft delete, audit logs, versioning)
- Authentication & authorization enhancements
- Nesting & optimization features
- UI/UX improvements
- Integration & export capabilities
- Performance & scalability requirements
- DevOps & monitoring setup
- Compliance & security requirements

Update this checklist as new requirements are identified during development.

---

## CRITICAL: Primary User Interface

**The order detail page (`orders/[id]/page.tsx`) is the PRIMARY user workflow.** All pattern upload, nesting, and cutplan operations happen from within an order context — not from the standalone `/patterns` page.

**User flow:**
1. Create/import an order → lands on `/orders/[id]`
2. Upload pattern **from the order page** (inline upload form)
3. Assign pattern to order
4. Run GPU nesting
5. Generate cutplan
6. Review and approve

**Pattern upload lives in two places:**
- **`orders/[id]/page.tsx`** — PRIMARY. This is where users upload patterns during their workflow. Any new parser options or upload UI changes MUST be added here first.
- **`patterns/page.tsx`** — SECONDARY. Standalone pattern library page. Keep in sync but this is not the main user path.

When adding new features to pattern upload (new parser types, new fields, etc.), always update `orders/[id]/page.tsx` first — that's what users actually see.

---

## Cutplan Pipeline Stages

> **Full details**: [`docs/cutplan_optimizer.md`](docs/cutplan_optimizer.md)

| Stage | Trigger | What happens | Frontend shows |
|-------|---------|-------------|----------------|
| **Stage 1: ILP Solve** | User clicks "Generate Cutplan" | ILP selects marker ratios + ply counts using GPU lengths. EndBit (Option D) runs its own solver. | Cutplan options with ratios/plies only. **No lengths, efficiencies, or costs.** |
| **Stage 2: Quick CPU Nest** | Auto after Stage 1 | Each unique marker is CPU-vector nested (~20s). Costs + floor MC waste calculated from CPU lengths. | Marker lengths, efficiencies, SVG thumbnails, costs, Est. Floor Waste |
| **Stage 3: Refine** | User clicks "Refine" | Longer CPU nest with advanced settings. Lengths/efficiencies updated. Marker ready for roll plan. | Refined SVGs, updated costs, gold/amber card styling |

**Key files**: `cutplan_service.py` (orchestrator), `ilp_solver_runner.py` (Stage 1), `endbit_solver.py` (Stage 1 Option D), `spyrrow_nesting_runner.py` (Stages 2-3)

---

## Project Overview

A 2D irregular nesting engine for garment manufacturing, targeting state-of-the-art material utilization using the Spyrrow heuristic as the core solver.

**Primary Goal**: Achieve competitive utilization (85%+) on industry garment patterns with proper handling of grain direction, piece pairing (left/right), and orientation constraints.

## Architecture

```
src/nesting_engine/
├── core/           # Data structures (DO NOT MODIFY without discussion)
│   ├── units.py        # Unit conversion (mm, cm, inch, etc.)
│   ├── geometry.py     # Point, Polygon, BoundingBox
│   ├── piece.py        # Piece with industry metadata
│   ├── instance.py     # Container, NestingItem, NestingInstance
│   └── solution.py     # NestingSolution, PlacedPiece
├── engine/         # Nesting solvers
│   └── spyrrow_engine.py   # Spyrrow wrapper (primary solver)
├── io/             # File I/O  (see docs/parser_index.md for full format guide)
│   ├── aama_parser.py           # AAMA/ASTM DXF+RUL grading parser (Boke)
│   ├── optitex_aama_parser.py   # OptiTex AAMA DXF+RUL grading parser
│   ├── dxf_text_parser.py       # Text-label DXF (Gerber-style markers)
│   ├── dxf_block_parser.py      # Block-based production DXF (pre-sized)
│   └── dxf_parser.py            # Orchestrator + backward-compat re-exports
└── apps/
    └── app.py      # Streamlit UI
```

## Key Concepts

### CRITICAL: Fabric Axis Convention — ABSOLUTE INVARIANT

**This must NEVER be gotten wrong. It applies to ALL nesting code, rendering, and piece orientation logic.**

| Axis | Direction | Meaning |
|------|-----------|---------|
| **X** | Fabric LENGTH | Grain direction, parallel to fabric edges, the OPEN/extending dimension in strip packing |
| **Y** | Fabric WIDTH | Perpendicular to edges, the FIXED/constrained dimension (e.g., 63.5") |

- **Grain ALWAYS runs along X** (fabric length, parallel to edges)
- **GPU packer container**: shape `(strip_width_px, max_length_px)` = `(Y, X)` — axis 0 = fabric width, axis 1 = fabric length
- **Spyrrow**: `strip_height` = fabric WIDTH (Y constraint), `sol.width` = marker LENGTH (X extent)
- **PIL rasterization**: `(x, y)` where x = columns = X = fabric length, y = rows = Y = fabric width
- **SVG rendering**: x axis = fabric length (horizontal), y axis = fabric width (vertical)
- **Piece orientation**: the piece's grain line direction must map to the X axis when placed

### CRITICAL: Fold Line vs Flip

These are COMPLETELY SEPARATE concepts - don't confuse them:

| Concept | `fold_line` | `allow_flip` / `FlipMode` |
|---------|-------------|---------------------------|
| What | Geometric feature from DXF Layer 6 | Nesting placement decision |
| Purpose | Marks symmetry axis for "cut on fold" | Creates left/right pairs |
| Source | Read from pattern file | Set by user/nesting order |
| Used by | Reference only | Nesting engine |

### Orientation Modes

- **Free**: Each piece rotates independently (0° or 180°)
- **Nap-Safe**: All pieces face same direction (for directional fabrics)
- **Garment-Linked**: Pieces of same garment rotate together

### Bundle System

Each garment instance gets a unique `bundle_id` (e.g., "M_1", "M_2"). All pieces of the same garment share the same color in visualization.

## API Usage Pattern

```python
from nesting_engine.core import (
    Piece, PieceIdentifier, Container,
    NestingItem, NestingInstance, FlipMode
)
from nesting_engine.engine import SpyrrowEngine, SpyrrowConfig

# 1. Create pieces
piece = Piece(
    vertices=[(0,0), (100,0), (100,50), (0,50)],
    identifier=PieceIdentifier(piece_name="Front", size="M")
)

# 2. Create nesting instance
container = Container(width=1500, height=None)  # Strip packing
items = [NestingItem(piece=piece, demand=2, flip_mode=FlipMode.PAIRED)]
instance = NestingInstance.create(
    name="Marker",
    container=container,
    items=items,
    piece_buffer=2.0,
    edge_buffer=5.0
)

# 3. Solve
engine = SpyrrowEngine()
solution = engine.solve(instance, config=SpyrrowConfig(time_limit=30))

# 4. Use results
for p in solution.placements:
    print(f"{p.piece_id}: ({p.x}, {p.y}) rot={p.rotation}° flip={p.flipped}")
```

## Development Commands

```bash
# Run the app
streamlit run apps/app.py

# Run tests
pytest tests/ -v

# Run specific test
pytest tests/test_core.py::TestPiece -v
```

## Current Limitations

- Rotation limited to 0° and 180° (grain constraint)
- Strip packing only (fixed width, variable height)
- Spyrrow uses jagua-rs collision detection (not NFP)

## Things to AVOID

1. **Don't rebuild the core** - The data structures are stable and tested
2. **Don't bypass Spyrrow** - It's the validated solver; build on top of it
3. **Don't confuse fold_line with flip** - Read the Key Concepts section
4. **Don't modify piece.py lightly** - It has careful distinctions baked in

## Testing Checklist

Before any PR:
- [ ] `pytest tests/test_core.py -v` passes
- [ ] `pytest tests/test_spyrrow_integration.py -v` passes
- [ ] App runs: `streamlit run apps/app.py`
- [ ] Can load DXF, configure pieces, run nesting, see results

## File Purposes

| File | Purpose | Modify? |
|------|---------|---------|
| `core/units.py` | Unit conversion | Rarely |
| `core/geometry.py` | Polygon math | Rarely |
| `core/piece.py` | Piece definition | Carefully |
| `core/instance.py` | Problem definition | Carefully |
| `core/solution.py` | Solution format | Carefully |
| `engine/spyrrow_engine.py` | Solver wrapper | When needed |
| `io/aama_parser.py` | AAMA/ASTM DXF+RUL grading parser (Boke) | When needed |
| `io/optitex_aama_parser.py` | OptiTex AAMA DXF+RUL grading parser | When needed |
| `io/dxf_text_parser.py` | Text-label DXF parser (Gerber-style) | When needed |
| `io/dxf_block_parser.py` | Block-based production DXF parser | When needed |
| `io/vt_dxf_parser.py` | Optitex Graded Nest DXF parser (VT format) | When needed |
| `io/dxf_parser.py` | Orchestrator, backward-compat re-exports | Rarely |
| `apps/app.py` | UI | Freely |
| `scripts/gpu_*_ga_ratio_optimizer.py` | GPU raster nesting & ratio optimization | When needed |
| `scripts/brute_force_improved.py` | Brute force marker ratio evaluation | When needed |
| `scripts/multicolor_solver.py` | Multi-order joint ILP optimization | When needed |
| `scripts/multicolor_solver_twostage.py` | Two-stage multi-order solver | When needed |
| `scripts/marker_selection_optimizer_v2.py` | Single-order ILP marker selection | When needed |
| `scripts/cutplan_cost_analysis_v2.py` | Cutplan cost evaluation (fabric, spreading, cutting, prep) | When needed |

## Detailed Documentation

For in-depth algorithm documentation, see:

| Document | Description |
|----------|-------------|
| [`docs/parser_index.md`](docs/parser_index.md) | **Pattern parser index**: all CAD format parsers, routing logic, how to add new formats |
| [`docs/gpu_nesting.md`](docs/gpu_nesting.md) | GPU raster nesting algorithm: FFT convolution, piece placement, sorting strategies, island GA |
| [`docs/cutplan_optimizer.md`](docs/cutplan_optimizer.md) | ILP cutplan optimization: single-color, multicolor joint, two-stage solvers |
| [`docs/cutting_costs.md`](docs/cutting_costs.md) | Cost calculation methodology: fabric, spreading, cutting, prep costs |
| [`docs/production_deployment.md`](docs/production_deployment.md) | **Production deployment guide**: GCP architecture, Cloud Run GPU, multi-tenant subdomain routing, pricing, cost analysis |
| [`docs/multi_width_nesting.md`](docs/multi_width_nesting.md) | Multi-width GPU nesting: cross-width Ridge prediction, sampling strategy |
| [`docs/endbit_optimized_solver.md`](docs/endbit_optimized_solver.md) | **EndBit Optimized cutplan**: two-phase strategy, floor MC, end-bit marker selection, validation |
| [`docs/multilot_cutplan_solver.md`](docs/multilot_cutplan_solver.md) | **Multi-lot cutplan solver**: greedy+GA pipeline, lot grouping, area-model lengths, validated on 2 colors |
| [`docs/gpu_batched_packer.md`](docs/gpu_batched_packer.md) | **GPU batched packer**: monolithic CUDA kernel for bulk ratio screening. 3.17x faster than per-ratio Python BLF, bit-identical lengths at `prefer_rot0=True` (default). Wired into `_evaluate_ratios_batch` for N >= 32. |

**IMPORTANT: Keep docs in sync with code.** When modifying algorithm logic in any solver (especially `endbit_solver.py`, `ilp_solver_runner.py`, `rollplan_simulator.py`), update the corresponding doc in `docs/`. If branching an algorithm to handle a new scenario, document the variant in the same doc file with a clear section heading.

## GPU Raster Nesting (Fast Marker Algorithm)

> **Full documentation**: [`docs/gpu_nesting.md`](docs/gpu_nesting.md)

A GPU-accelerated raster-based nesting algorithm for rapid marker evaluation. Used for screening large numbers of marker ratio combinations before CPU refinement with Spyrrow.

### Environment Requirements

**Hardware:**
- NVIDIA GPU with CUDA support (tested on RTX 3060 Laptop GPU)
- Minimum 4GB VRAM recommended for large markers

**Software Dependencies:**
```bash
# Core GPU libraries
pip install cupy-cuda11x      # or cupy-cuda12x depending on CUDA version
pip install scipy>=1.10.0     # For cupyx.scipy.signal.fftconvolve

# Supporting libraries
pip install numpy>=1.24.0
pip install pillow            # For rasterization (PIL.Image, PIL.ImageDraw)
```

**Conda Environment (recommended):**
```bash
conda create -n nester python=3.11
conda activate nester
conda install -c conda-forge cupy cudatoolkit=11.8
pip install scipy numpy pillow
```

**Verification:**
```python
import cupy as cp
from cupyx.scipy.signal import fftconvolve

# Check GPU is available
print(cp.cuda.runtime.getDeviceProperties(0)['name'].decode())
# Expected: "NVIDIA GeForce RTX 3060 Laptop GPU" (or similar)
```

**Common Issues:**
- `NumPy version >=1.25.2 and <2.6.0 required` - Install compatible NumPy version
- `CuPy not found` - Ensure CUDA toolkit matches CuPy version (cuda11x vs cuda12x)
- Out of memory - Reduce `GPU_SCALE` or `max_length` parameter

### Algorithm Overview

1. **Rasterization**: Convert piece polygons to binary raster images at configurable resolution
2. **Collision Detection**: Use FFT convolution to find valid placements (no overlap)
3. **Placement Strategy**: Bottom-left fill with gravity drop
4. **Rotation**: Pre-compute 0° and 180° rotations for each piece

### Core Parameters

| Parameter | Default | Description |
|-----------|---------|-------------|
| `GPU_SCALE` | 0.15 px/mm | Rasterization resolution |
| `PIECE_BUFFER` | 0.1 px | Gap between pieces |
| `EDGE_BUFFER` | 0 | Gap from container edge |
| `ROTATIONS` | [0°, 180°] | Allowed rotations (grain constraint) |

### Key Classes

```python
class GPUPacker:
    """GPU-accelerated strip packer using FFT convolution."""

    def __init__(self, strip_width: int, max_length: int):
        self.container = cp.zeros((strip_width, max_length), dtype=cp.float32)

    def find_best_position(self, raster_gpu, raster_180_gpu, current_length):
        """Find best placement using FFT convolution for collision detection."""
        # 1. Flip kernel for convolution
        # 2. FFT convolve with container
        # 3. Find valid positions (overlap < 0.5)
        # 4. Apply gravity drop (minimize y)
        # 5. Prefer positions within current_length (minimize strip extension)

    def place(self, raster, x, y):
        """Place piece raster at position using element-wise maximum."""
```

### Placement Heuristic

1. **Sort pieces by area** (largest first)
2. For each piece, try both rotations (0°, 180°)
3. Find valid positions via FFT convolution
4. **Priority**: Fit within current strip length > Minimize y (gravity) > Leftmost x
5. Place and update container raster

### Performance

| Pieces | Time/Marker | Notes |
|--------|-------------|-------|
| ~23 (1 bundle) | ~180ms | 7 sizes |
| ~140 (6 bundles) | ~500ms | Full marker |

### Usage Pattern

```python
from cupyx.scipy.signal import fftconvolve as fftconvolve_gpu

# 1. Rasterize pieces
pieces_by_size = load_and_rasterize_pieces(scale=0.15)

# 2. Create packer
packer = GPUPacker(strip_width=215, max_length=3000)

# 3. Evaluate a ratio
ratio = {'46': 2, '48': 1, '50': 1}  # 4 bundles
efficiency = evaluate_ratio(pieces_by_size, ratio, packer)
```

### CRITICAL: Purpose of GPU Nesting

**GPU nesting is a RANKING TOOL, not an absolute length calculator.**

- GPU nesting outputs (efficiency, length_yards) are for **comparing ratios against each other**
- Do NOT compare GPU lengths directly to factory marker lengths
- Factory uses different nesting algorithms, resolutions, and optimizations
- GPU lengths will typically be longer than factory actuals

### CRITICAL: GPU Nesting Baseline Calibration (Jan 2026)

**Sorting Strategy Findings:**

When evaluating marker ratios with GPU raster nesting, sorting strategy matters significantly:

| Strategy | Win Rate | Description |
|----------|----------|-------------|
| **width_desc** | **67.2%** | Sort by width descending - wider pieces first fill strip width efficiently |
| **area_desc** | **28.3%** | Sort by area descending - traditional approach |
| height_desc | 0% | Not effective |
| height_width_desc | 0% | Not effective |
| area_height_desc | 4.6% | Marginal improvement |

**Recommended approach:** Try both `width_desc` and `area_desc`, keep the best result. This covers 95.5% of optimal cases with only 2x overhead.

**Key insight:** The original baseline failed for same-size 2-bundle markers because it only used `area_desc`. Switching to `width_desc` produced 7-8% shorter lengths for these cases, correctly showing that 2-bundle same-size markers are MORE efficient than 1-bundle (as expected).

**Correct usage:**
1. Run GPU nesting on all ratio combinations (e.g., 1715 ratios)
2. Use efficiency/length to **rank** which ratios are best
3. Feed top ratios into ILP for cutplan optimization
4. Final production markers use Spyrrow or factory nesting for actual lengths

**Incorrect usage:**
- "GPU says 9.5Y but factory is 8.4Y, so we're worse" ❌
- GPU lengths are relative measures for ratio selection, not production estimates

### CRITICAL: GPU Efficiency Calculation — Use Vector Area (IMPLEMENTED)

**GPU marker efficiency MUST be computed using vector polygon area (shoelace formula), NOT raster pixel area.**

```
efficiency = total_vector_area_mm2 / (fabric_width_mm × gpu_length_mm)
```

- **Vector area** = `sum(_polygon_area_mm2(piece.vertices_mm) × demand)` — exact geometric area via shoelace formula
- **Raster pixel area** overestimates by ~3-8% due to rasterization aliasing at any scale (0.15–1.0 px/mm)
- GPU **lengths** are accurate (within 0.3–2.7% of CPU/SS) — the length measurement is reliable
- `_polygon_area_mm2()` added to `gpu_nesting_runner.py` — used in all 6 efficiency calculation sites
- **DONE in production code** — all `_evaluate_single_sort`, `_evaluate_with_svg_single_sort`, typed/column-fill variants, and Ridge prediction efficiency now use vector area

### GPU Scale: 0.08 px/mm (Production Default)

| Scale | GP Pred MAPE vs 0.15 | Spearman rho | Speed | Notes |
|-------|----------------------|-------------|-------|-------|
| 0.08 | 2.53% | 0.9993 | ~7.8/s | **Production default** — fast, GP absorbs error |
| 0.15 | baseline | 1.0 | ~5/s | Higher quality, use for calibration |
| 0.30 | N/A | N/A | ~1/s | Best raw GPU accuracy |

- Validated Apr 2026: 275 markers, 0.08 vs 0.15, ranking near-perfect (rho=0.9993)
- Use `round()` for strip width (not `int()`) to avoid 0.1" truncation
- Dual sort: `width_desc` + `area_desc`, pick shortest length

### DEAD END: Batch GPU Nesting via CUDA Streams/Threading

**Do NOT attempt parallel GPU marker evaluation using CUDA streams, Python threading, or batched 3D kernels.** This has been tested twice and **makes things slower** (0.57x of sequential).

**Why it fails:** A single BLF kernel already saturates the RTX 3060's 30 SMs with ~500 thread blocks. Adding concurrent streams creates GPU contention, not parallelism. The Python BLF loop overhead (argmin, place, update per piece) is the real bottleneck, not GPU compute.

**NVIDIA library survey (Apr 2026):** cuOpt, cuSpatial, Warp, CUDA Graphs, Cooperative Groups, CUB — none help. 2D irregular strip packing is too niche. No open-source GPU irregular nesting exists.

**The only viable path to 50+ markers/sec:** A monolithic CUDA kernel that runs the entire BLF loop (all pieces) in one launch, eliminating Python roundtrips. Significant CUDA engineering effort (weeks).

### PROVEN: Monolithic Batched GPU BLF Kernel (Apr 2026)

**The monolithic path flagged above was built and shipped.** `backend/backend/services/gpu_batched_packer.py` implements a single CUDA kernel where each CUDA block processes one full sequence (all pieces), threads cooperate via shared-memory `atomicMin` on a 64-bit packed `(score, x, y, rot)` word, then parallel-update the container. One kernel launch evaluates `batch_size` sequences simultaneously.

**Measured on 126H010C / 461 ratios, bc=1..5, gpu_scale=0.15:**
- **3.17x faster** than per-ratio Python BLF loop (20s vs 63s total).
- **Bit-identical length output** to production Python BLF with `prefer_rot0=True` (default). Verified 461/461 ratios match to 0 mm.
- Speedup scales with batch size: 2.0x at N=56, 3.1x at N=461.

**Wired into production**: `_evaluate_ratios_batch` dispatches to the batched kernel when `N >= 32` and falls back to the Python loop below that threshold or on exception. Frontend workflows using `run_nesting_for_material` get the speedup automatically with zero API changes and zero regression risk.

**When NOT to use it:** single-ratio eval (N=1 uses 1 of 30 SMs and is slower than the existing `evaluate_ratio`). The per-ratio path is still the right call for single-marker recomputation.

**Doc:** [`docs/gpu_batched_packer.md`](docs/gpu_batched_packer.md)
**Commits:** `74b4a9a` (add module), `0bf498a` (wire into default path).

### PROVEN: Lookahead BLF for GPU Nesting Quality

**GPU BLF quality can be improved 0.5-3.7% by evaluating top-K alternative positions with L-step greedy rollout.** Time cost: 0.3-3.8s per marker (vs 0.13s greedy). Best config: K=10 positions, L=5-8 lookahead steps.

**How it works:**
1. GPU kernel computes valid (x, y) for ALL positions and both rotations (already done in one launch)
2. Extract top-K diverse positions (different x, y, or rotation) from kernel output
3. For each candidate: snapshot container → place piece → greedily place next L pieces → measure strip length → rollback
4. Commit the position that minimizes strip length after rollout

**Validated Apr 2026** across 3 garment types:

| Pattern | Marker | Pieces | Gain | Notes |
|---------|--------|--------|------|-------|
| FGL shirt | bc9 (5 sizes) | 36 | **+3.7%** | Best gain — moderate piece variety |
| P5 panty | bc12 | 12 | **+2.6%** | Single body piece |
| FGL shirt | bc12 | 48 | +0.2% | Large markers — greedy already good |
| AAMA jacket | bc1-2 | 23-46 | 0.0% | High shape diversity — greedy interlocks well |

**Key insight:** Mid-complexity markers (20-40 pieces, moderate shape variety) benefit most. Very simple or very complex markers give greedy BLF less room for error.

**Scripts:** `scripts/ilp_lookahead_v2_poc.py`, `scripts/lookahead_multipattern_test.py`
**PNGs:** `experiment_results/lookahead_pngs/`

### Correlation with Spyrrow

GPU raster results correlate well with Spyrrow CPU results:
- Useful for rapid screening of 1000+ ratio combinations
- Top GPU candidates refined with Spyrrow for production markers
- Typical workflow: GPU screen (5-10 min) → CPU refine top N (variable time)

### Reference Implementation

`scripts/gpu_20260118_ga_ratio_optimizer.py` - Complete implementation with:
- Piece loading and rasterization
- GPUPacker class
- Island-based GA for ratio optimization
- Configurable parameters

## Marker Selection Optimizer

> **Full documentation**: [`docs/cutplan_optimizer.md`](docs/cutplan_optimizer.md)

Given an order demand (garments per size), select optimal marker combinations to fulfill exactly.

### Key Concepts

| Term | Definition |
|------|------------|
| **Bundle** | 1 complete garment (all pieces for one size) |
| **Marker** | Layout with multiple bundles nested (e.g., "0-3-1-1-1-0-0" = 6 bundles) |
| **Plies** | Fabric layers stacked for cutting (max 100 per cut) |
| **Cuts** | ceil(plies / 100) - cutting operations needed |
| **Bundle-Cuts** | Σ(bundles × cuts) - total cutting work |

### Baseline: Option E (Balanced ILP)

**Objective**: `min Σ((1-eff[m]) × plies[m]) + penalty × Σ(used[m])`

**Constraints**:
- Exact demand fulfillment: Σ(ratio[m][s] × plies[m]) = demand[s]
- Minimum plies by bundle count (prevents wasteful small-ply large markers):
  - 6-bundle: 50 min plies
  - 5-bundle: 40 min plies
  - 4-bundle: 30 min plies
  - 3-bundle: 10 min plies
  - 1-2 bundle: 1 min ply

**Reference Result** (order: 74-244-347-342-265-162-62):
```
Option E: Balanced (penalty=5.0)
  Efficiency: 79.79%
  Unique Markers: 7
  Total Cuts: 8
  Bundle-Cuts: 33

Markers:
  1-3-1-0-0-1-0 (6-bndl, 80.8%) × 55 plies
  0-0-5-0-0-0-0 (5-bndl, 80.5%) × 46 plies
  0-1-1-0-2-0-1 (5-bndl, 80.0%) × 62 plies
  0-0-0-3-1-1-0 (5-bndl, 79.4%) × 107 plies
  0-1-0-0-2-0-0 (3-bndl, 78.9%) × 17 plies
  1-0-0-1-0-0-0 (2-bndl, 77.7%) × 19 plies
  0-0-0-2-0-0-0 (2-bndl, 77.3%) × 1 ply
```

### Script Location

`scripts/marker_selection_optimizer_v2.py`

### Available Options

| Option | Objective | Use Case |
|--------|-----------|----------|
| A | Max Efficiency | Best utilization |
| B | Min Markers | Simplest cutting plan |
| C | Min Plies | Minimize fabric layers |
| D | Min Bundle-Cuts | Minimize cutting work |
| E | Balanced (baseline) | Best trade-off |
| F | Hybrid Greedy + ILP | Fast but lower efficiency |

### Multicolor Solver Variants

For multiple orders/colors with shared markers:

| Variant | Description | Script |
|---------|-------------|--------|
| **Joint ILP** | Optimizes all colors simultaneously, shared `used[m]` encourages marker reuse | `scripts/multicolor_solver.py` |
| **Two-Stage** | Stage 1: 4,6-bundle markers (93-96% demand), Stage 2: 2-bundle exact remainder | `scripts/multicolor_solver_twostage.py` |

**Key insight**: Joint multicolor optimization typically reduces unique markers by 5-8 compared to independent per-color solving, with similar or better fabric usage.

### Multi-Lot Cutplan Solver (Greedy + GA Polish) — Pre-Final

> **Full documentation**: [`docs/multilot_cutplan_solver.md`](docs/multilot_cutplan_solver.md)

For orders with multiple fabric lots (different shrinkage × width combinations):

**Pipeline**: Lot Grouping → Area-Model Lengths → Greedy Construction → GA Polish

| Phase | What | Time |
|-------|------|------|
| **Lot Grouping** | Cumulative waste heuristic reduces raw lots (18→9), cross-shrinkage never merged | <1ms |
| **Area Model** | `length = Σ(ratio[s] × area[s]) / (eff × width)` — 2-3% error, pre-compute ~16K lengths | 0.02s |
| **Greedy** | Fill lots biggest-first, score = demand_pct × lot_fill × bc_bonus, tail fill BC 1-2 | 0.03s |
| **GA Polish** | 200 gen, pop 50, mutations: adjust plies / swap ratio / move lot / remove / split | 2-3s |

**Validated results** (HD FGL 2 order, AEO-5907):
- **210BONEWHI**: 8 markers, 4,120 yd, 0.906 yd/gmt — **matches SS benchmark**
- **100BWNW**: 7 markers, 4,434 yd, 0.898 yd/gmt — **beats SS by 9 yd**

**Key files**: `scripts/linear_lot_ilp.py` (210BONEWHI), `scripts/multilot_solver_100bwnw.py` (100BWNW)

## GPU-Accelerated Sparrow: Internal Collision Detection on GPU

**Goal**: Replace the hot inner loop of Sparrow's collision detection with GPU batch evaluation, achieving 10-50x speedup while keeping Sparrow's orchestration logic (exploration, compression, GLS) on CPU.

**North Star**: Sparrow is already the best open-source 2D nesting solver. Don't try to replace it or skip its phases — **accelerate its internals with GPU**.

### Why This Architecture (Lessons Learned)

**DEAD END — Warm-start / GPU seeding (Apr 2026)**: We spent a full session building a GPU raster → Spyrrow warm-start pipeline. Results: warm-start matches cold-start at same time budgets but provides zero speed advantage. GPU BLF produces ~75% efficiency placements vs Spyrrow's ~85%. Spyrrow's exploration phase IS the value — trying to skip it was the wrong approach entirely. The forked Spyrrow 0.9.0 with `initial_solution` works mechanically but adds no production value.

**RIGHT APPROACH**: Put GPU inside Sparrow's hot loop, not outside it.

### The Hot Loop (90% of Runtime)

From profiling: **`collect_poly_collisions_in_detector_custom`** in `eval/specialized_jaguars_pipeline.rs` takes over 90% of time. This calls `overlap_area_proxy` (Algorithm 3) which is an O(N×M) nested loop over pole pairs:

```rust
// vendor/sparrow/src/quantify/overlap_proxy.rs
pub fn overlap_area_proxy(sp1: &SPSurrogate, sp2: &SPSurrogate, epsilon: f32) -> f32 {
    for p1 in &sp1.poles {
        for p2 in &sp2.poles {
            let pd = (p1.radius + p2.radius) - p1.center.distance_to(&p2.center);
            // ... decay function, accumulate
        }
    }
}
```

This is called ~75 times per colliding item per iteration (50 container-wide + 25 focused samples). With 20+ colliding items, that's 1500+ sequential evaluations per iteration.

### Implementation Plan

1. **Modify jagua-rs** — expose pole data (SoA format: x[], y[], r[], item_id[]) via FFI or shared memory
2. **In Sparrow's separation loop** — when evaluating candidate positions for a colliding item, collect ALL candidates into a batch
3. **Send batch to GPU** — evaluate 1000+ (tx, ty, theta) transforms in parallel using the CUDA kernel
4. **GPU returns loss values** — CPU picks best, updates layout, continues GLS orchestration

### Existing Components

- **CUDA kernel**: `nesting_engine/engine/gpu_overlap_evaluator.py` — implements Algorithm 3 on GPU, benchmarked at 3.4M evals/sec (112x vs estimated sequential Sparrow)
- **Forked repos**: `vendor/sparrow/`, `vendor/jagua-rs/`, `vendor/spyrrow/`
- **Forked Spyrrow 0.9.0**: Has `initial_solution` warm-start (works but not useful)

### Key Implementation Decisions

- **Rust ↔ GPU bridge**: Options are (a) cudarc crate in Rust, (b) FFI to Python/CuPy, (c) shared memory + subprocess. cudarc is cleanest but requires CUDA in the Rust build chain.
- **Batch size**: Sparrow evaluates ~75 candidates/item × ~20 items = 1500/iteration. GPU is efficient at 1000+, so batching one full iteration is ideal.
- **Granularity**: Replace ONLY the pole overlap evaluation, not the quadtree traversal or edge collision checks. Quadtree is CPU-optimal (branch-heavy), pole overlap is GPU-optimal (data-parallel).

**Sparrow repos**:
- Sparrow (solver): https://github.com/JeroenGar/sparrow
- jagua-rs (collision engine): https://github.com/JeroenGar/jagua-rs
- Spyrrow (Python wrapper): https://github.com/PaulDL-RS/spyrrow

## Future Work

- ESICUP benchmark integration
- Utilization comparison metrics
- Batch processing
- Solution export improvements
- GPU raster sampling strategy optimization
- **GPU-accelerated Sparrow internals** (see section above)
- **Shading/stripe marker constraints in GPU nester**
