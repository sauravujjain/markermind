# MarkerMind - Claude Code Guide

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

### Quick Start for Testing

```bash
# Terminal 1: Start backend
cd /home/sarv/projects/MarkerMind/backend
source venv/bin/activate
uvicorn backend.main:app --reload

# Terminal 2: Start frontend
cd /home/sarv/projects/MarkerMind/frontend
npm run dev

# Terminal 3: Run Puppeteer tests
# (Puppeteer MCP server should be configured)
```

---

## CRITICAL: Development Server Mode

**During development, ALWAYS use dev mode (`next dev`), NOT production mode (`next start`).**

### After editing code — what to do:

| What changed | Action needed | Why |
|---|---|---|
| **Frontend files** (`.tsx`, `.ts`, `.css`) | **Nothing.** Dev server picks up changes via HMR instantly. | `next dev` watches the filesystem and hot-reloads the browser automatically. |
| **Backend files** (`.py`) | **Nothing.** Uvicorn `--reload` detects changes and restarts. | The `--reload` flag watches Python files. |
| **package.json / config files** | Restart the frontend dev server (see below). | Config changes aren't covered by HMR. |

**Do NOT run `npx next build`.** That is only for production deployment.
**Do NOT kill and restart the frontend** after normal code edits — HMR handles it.

### Starting the servers (only needed once per session):

**Frontend:**
```bash
cd /home/sarv/projects/MarkerMind/frontend
npx next dev > /home/sarv/projects/MarkerMind/logs/frontend.log 2>&1 &
```

**Backend:**
```bash
cd /home/sarv/projects/MarkerMind/backend
uvicorn backend.main:app --reload &
```

### Before starting, verify servers aren't already running:
```bash
lsof -i :3000 -sTCP:LISTEN 2>/dev/null && echo "Frontend already running" || echo "Frontend not running"
lsof -i :8000 -sTCP:LISTEN 2>/dev/null && echo "Backend already running" || echo "Backend not running"
```

### If the frontend is stuck or not reflecting changes (rare):
```bash
kill $(lsof -t -i :3000) 2>/dev/null
sleep 1
cd /home/sarv/projects/MarkerMind/frontend
npx next dev > /home/sarv/projects/MarkerMind/logs/frontend.log 2>&1 &
```

### Production mode (deployment only — via `start.sh`):
Only use `next build && next start` for production deployment. Never use it during active development.

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
│   ├── aama_parser.py      # AAMA/ASTM DXF+RUL grading parser
│   ├── dxf_text_parser.py  # Text-label DXF (Gerber-style markers)
│   ├── dxf_block_parser.py # Block-based production DXF (pre-sized)
│   └── dxf_parser.py       # Orchestrator + backward-compat re-exports
└── apps/
    └── app.py      # Streamlit UI
```

## Key Concepts

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
| `io/aama_parser.py` | AAMA/ASTM DXF+RUL grading parser | When needed |
| `io/dxf_text_parser.py` | Text-label DXF parser (Gerber-style) | When needed |
| `io/dxf_block_parser.py` | Block-based production DXF parser | When needed |
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

## Future Work

- ESICUP benchmark integration
- Utilization comparison metrics
- Batch processing
- Solution export improvements
- GPU raster sampling strategy optimization
