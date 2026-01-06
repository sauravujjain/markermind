# Nesting Engine - Claude Code Guide

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
├── io/             # File I/O
│   └── dxf_parser.py       # AAMA/ASTM DXF parsing
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
| `io/dxf_parser.py` | DXF loading | When needed |
| `apps/app.py` | UI | Freely |

## Future Work

- ESICUP benchmark integration
- Utilization comparison metrics
- Batch processing
- Solution export improvements
