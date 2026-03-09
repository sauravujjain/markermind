# GPU Raster Nesting Algorithm

A GPU-accelerated raster-based nesting algorithm for rapid marker evaluation using FFT convolution for collision detection.

## Purpose

The GPU nesting algorithm is designed for **rapid screening** of thousands of marker ratio combinations. It is NOT intended to produce production-quality marker layouts, but rather to rank ratios by efficiency so the best candidates can be refined with higher-fidelity CPU algorithms (e.g., Spyrrow).

**Critical**: GPU nesting outputs are for **comparing ratios against each other**, not for absolute length estimation. GPU lengths will typically be longer than factory actuals.

## Dependencies

### Required Libraries

```python
# Core GPU libraries
pip install cupy-cuda11x      # or cupy-cuda12x for CUDA 12
pip install scipy>=1.10.0     # For cupyx.scipy.signal.fftconvolve

# Supporting libraries
pip install numpy>=1.24.0
pip install pillow            # For rasterization (PIL.Image, PIL.ImageDraw)
```

### Hardware Requirements

- NVIDIA GPU with CUDA support
- Minimum 4GB VRAM recommended for large markers (6-bundle)
- Tested on RTX 3060 Laptop GPU

### Verification

```python
import cupy as cp
from cupyx.scipy.signal import fftconvolve as fftconvolve_gpu

# Check GPU is available
print(cp.cuda.runtime.getDeviceProperties(0)['name'].decode())
```

## Algorithm Overview

### High-Level Flow

```
1. Load pattern pieces from DXF + RUL files
2. Grade pieces to all target sizes
3. Rasterize each piece to binary images
4. For each ratio combination:
   a. Build piece list (pieces × demand × bundle count)
   b. Sort pieces by chosen strategy
   c. Place pieces one-by-one using FFT convolution
   d. Record efficiency and strip length
5. Return ranked results
```

### Core Concepts

| Term | Definition |
|------|------------|
| **Raster** | Binary image of piece polygon (1=filled, 0=empty) |
| **Container** | 2D array representing the strip (width × max_length) |
| **FFT Convolution** | Fast overlap detection via frequency domain multiplication |
| **Valid Position** | Position where overlap < 0.5 (no collision) |
| **Gravity Drop** | Place piece at lowest valid y position for each x |

## Configuration Parameters

| Parameter | Default | Description |
|-----------|---------|-------------|
| `GPU_SCALE` | 0.15 px/mm | Rasterization resolution |
| `PIECE_BUFFER` | 0.1 px | Gap between pieces (in pixels) |
| `EDGE_BUFFER` | 0 | Gap from container edge |
| `FABRIC_WIDTH_INCH` | 56.5" | Strip width |
| `MAX_BUNDLES` | 6 | Maximum bundles per marker |
| `ROTATIONS` | [0°, 180°] | Allowed rotations (grain constraint) |

### Resolution Trade-off

- **Lower GPU_SCALE** (0.1): Faster, less precise, may miss tight fits
- **Higher GPU_SCALE** (0.2): Slower, more precise, better for dense packing

For screening 1000+ ratios, 0.15 px/mm provides good balance.

## Key Components

### 1. Piece Loading and Rasterization

```python
def load_pieces():
    """Load pieces from DXF+RUL, grade to sizes, rasterize."""
    pieces, rules = load_aama_pattern(str(DXF_PATH), str(RUL_PATH))
    grader = AAMAGrader(pieces, rules)
    unit_scale = 25.4 if rules.header.units == 'ENGLISH' else 1.0

    pieces_by_size = {}
    for target_size in ALL_SIZES:
        graded = grader.grade(target_size)
        pieces_by_size[target_size] = []

        for gp in graded:
            # Convert vertices to mm
            vertices_mm = [(y * unit_scale, x * unit_scale) for x, y in gp.vertices]

            # Rasterize polygon to binary image
            verts = np.array(vertices_mm)
            verts_scaled = (verts - verts.min(axis=0)) * GPU_SCALE + PIECE_BUFFER

            img = Image.new('L', (width, height), 0)
            ImageDraw.Draw(img).polygon([tuple(p) for p in verts_scaled], fill=1)
            raster = np.array(img, dtype=np.float32)

            pieces_by_size[target_size].append({
                'name': gp.name,
                'raster': raster,
                'raster_gpu': cp.asarray(raster),
                'raster_180': np.rot90(raster, 2),
                'raster_180_gpu': cp.asarray(np.rot90(raster, 2)),
                'area': float(np.sum(raster)),
                'demand': orig_piece.quantity.total,
            })

    return pieces_by_size
```

### 2. GPUPacker Class

The core placement engine using FFT convolution.

```python
class GPUPacker:
    def __init__(self, strip_width: int, max_length: int):
        self.strip_width = strip_width
        self.max_length = max_length
        self.container = cp.zeros((strip_width, max_length), dtype=cp.float32)

    def reset(self):
        """Clear container for new evaluation."""
        self.container.fill(0)

    def find_best_position(self, raster_gpu, raster_180_gpu, current_length):
        """
        Find best placement using FFT convolution.

        Algorithm:
        1. Flip kernel for convolution
        2. FFT convolve with container
        3. Find valid positions (overlap < 0.5)
        4. Apply gravity drop (minimize y per column)
        5. Prefer positions within current_length
        6. Return position that minimizes strip extension
        """
        best = None
        best_raster = None

        for raster in [raster_gpu, raster_180_gpu]:
            ph, pw = raster.shape
            if ph > self.strip_width:
                continue

            # Flip kernel for convolution
            kernel = raster[::-1, ::-1].copy()

            # FFT convolution gives overlap at each position
            overlap = fftconvolve_gpu(self.container, kernel, mode='valid')

            # Valid positions have overlap < 0.5 (no collision)
            valid = overlap < 0.5

            # Enforce strip width constraint
            max_valid_y = self.strip_width - ph
            if max_valid_y + 1 < valid.shape[0]:
                valid[max_valid_y + 1:, :] = False

            # Gravity drop: find minimum y for each x column
            y_idx = cp.arange(valid.shape[0]).reshape(-1, 1)
            y_grid = cp.where(valid, y_idx, valid.shape[0] + 1)
            drop_y = cp.min(y_grid, axis=0)
            valid_cols = drop_y <= max_valid_y

            # Prefer positions within current strip length
            x_idx = cp.arange(valid.shape[1])
            piece_right = x_idx + pw
            piece_top = drop_y + ph

            # Priority 1: Fit within current_length
            inside = valid_cols & (piece_right <= current_length)
            if cp.any(inside):
                # Among inside positions, minimize piece_top (y + height)
                tops = cp.where(inside, piece_top, cp.inf)
                min_top = float(cp.min(tops))
                mask = inside & (piece_top == min_top)
                bx = int(cp.argmax(mask))
                by = int(drop_y[bx])

                if best is None or (bx + pw <= current_length):
                    best = {'x': bx, 'y': by, 'ph': ph, 'pw': pw}
                    best_raster = raster

            # Priority 2: Extend strip minimally
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
        """Place piece at position using element-wise maximum."""
        ph, pw = raster.shape
        self.container[y:y+ph, x:x+pw] = cp.maximum(
            self.container[y:y+ph, x:x+pw], raster
        )
```

### 3. FFT Convolution for Collision Detection

The key insight is that overlap between two binary images can be computed via convolution:

```
overlap(x, y) = Σ container[y+j, x+i] × piece[j, i]
             = container ⊛ flip(piece)
```

If `overlap(x, y) > 0`, there's a collision. Using FFT:
- Time complexity: O(N log N) instead of O(N²) for direct convolution
- GPU parallelization: Massive speedup on CUDA

```python
from cupyx.scipy.signal import fftconvolve as fftconvolve_gpu

# Flip kernel for proper convolution
kernel = raster[::-1, ::-1].copy()

# mode='valid' returns only positions where piece fits entirely
overlap = fftconvolve_gpu(self.container, kernel, mode='valid')

# Valid positions have no overlap
valid_positions = overlap < 0.5  # Threshold for floating point
```

### 4. Ratio Evaluation

```python
def evaluate_ratio(pieces_by_size: Dict, ratio: Dict[str, int], packer: GPUPacker) -> Tuple[float, float]:
    """Evaluate a single ratio and return (efficiency, length_yards)."""
    packer.reset()

    # Build piece list from ratio
    pieces_list = []
    for size, count in ratio.items():
        if count <= 0:
            continue
        for _ in range(count):  # count bundles of this size
            for p in pieces_by_size[size]:
                for _ in range(p['demand']):  # demand per piece type
                    pieces_list.append(p)

    # Sort by area descending (largest first)
    pieces_list.sort(key=lambda p: -p['area'])

    placed_area = 0.0
    current_length = 0

    for p in pieces_list:
        result, raster = packer.find_best_position(
            p['raster_gpu'], p['raster_180_gpu'], current_length
        )
        if result is None:
            continue

        packer.place(raster, result['x'], result['y'])
        placed_area += p['area']
        current_length = max(current_length, result['x'] + result['pw'])

    # Calculate efficiency
    strip_area = GPU_STRIP_WIDTH_PX * current_length
    efficiency = placed_area / strip_area

    # Convert length from pixels to yards
    length_yards = current_length / GPU_SCALE / 25.4 / 36

    return efficiency, length_yards
```

## Sorting Strategies

The order in which pieces are placed significantly affects results. The improved baseline tests 5 strategies:

| Strategy | Key | Win Rate |
|----------|-----|----------|
| **width_desc** | `-p['width']` | **67.2%** |
| **area_desc** | `-p['area']` | **28.3%** |
| height_desc | `-p['height']` | 0% |
| height_width_desc | `(-p['height'], -p['width'])` | 0% |
| area_height_desc | `(-p['area'], -p['height'])` | 4.6% |

**Recommendation**: Try both `width_desc` and `area_desc`, keep the best result. This covers 95.5% of optimal cases with only 2x overhead.

```python
sort_keys = [
    lambda p: -p['area'],       # Traditional
    lambda p: -p['width'],      # Best for strip packing
    lambda p: -p['height'],
    lambda p: (-p['height'], -p['width']),
    lambda p: (-p['area'], -p['height']),
]

best_length = float('inf')
for sort_key in sort_keys:
    packer.reset()
    pieces_list.sort(key=sort_key)
    # ... place pieces ...
    if length < best_length:
        best_length = length
```

## Search Strategies

### Brute Force

For small search spaces, evaluate all combinations:

```python
from itertools import combinations_with_replacement

def generate_all_ratios(bundle_count: int, sizes: List[str]) -> List[Dict]:
    """Generate all ratio combinations for given bundle count."""
    all_ratios = []
    for combo in combinations_with_replacement(sizes, bundle_count):
        ratio = {s: 0 for s in sizes}
        for size in combo:
            ratio[size] += 1
        all_ratios.append(ratio)
    return all_ratios
```

Combination counts for 7 sizes:
| Bundles | Combinations |
|---------|--------------|
| 1 | 7 |
| 2 | 28 |
| 3 | 84 |
| 4 | 210 |
| 5 | 462 |
| 6 | 924 |
| **Total** | **1,715** |

### Island-Based Genetic Algorithm

For larger search spaces (> 50 combinations), use island GA:

```python
# Island configuration
GA_GENERATIONS = 3
MIN_ISLAND_SIZE = 50
MAX_ISLANDS = 5
MIN_ISLANDS = 3

def create_islands(all_ratios, total_combos):
    """
    Linear partitioning exploits lexicographic ordering:
    - Early combos: heavy on small sizes (46, 48)
    - Late combos: heavy on large sizes (56, 58)

    Each island naturally covers different size distributions.
    """
    if total_combos < MIN_ISLAND_SIZE:
        return [all_ratios]  # Brute force

    if total_combos <= 150:
        num_islands = 3
    elif total_combos <= 250:
        num_islands = 4
    else:
        num_islands = 5

    island_size = total_combos // num_islands
    islands = []
    for i in range(num_islands):
        start = i * island_size
        end = start + island_size if i < num_islands - 1 else total_combos
        islands.append(all_ratios[start:end])

    return islands
```

## Output Format

### JSON Results

```json
{
  "config": {
    "fabric_width_inch": 56.5,
    "piece_buffer_px": 0.1,
    "gpu_scale": 0.15,
    "sizes": ["46", "48", "50", "52", "54", "56", "58"]
  },
  "summary": {
    "total_evaluations": 1715,
    "total_time_sec": 285.3
  },
  "results": {
    "1": {
      "bundle_count": 1,
      "total_combos": 7,
      "results": [
        {"ratio": "0-0-0-0-1-0-0", "efficiency": 0.723, "length_yards": 1.45}
      ]
    }
  }
}
```

### CSV Format

```csv
ratio,bundles,efficiency,length_yards
0-0-0-0-1-0-0,1,0.723456,1.4521
0-0-1-1-0-0-0,2,0.789123,2.8340
```

## Performance Characteristics

| Pieces | Time/Marker | Notes |
|--------|-------------|-------|
| ~23 (1 bundle) | ~180ms | 7 sizes |
| ~140 (6 bundles) | ~500ms | Full marker |
| 1715 ratios (full) | ~5 min | All 1-6 bundle combos |

## Reference Implementation

- **Brute force**: `scripts/brute_force_improved.py`
- **Island GA**: `scripts/gpu_20260118_ga_ratio_optimizer.py`

## Usage Example

```python
from pathlib import Path
import cupy as cp
from cupyx.scipy.signal import fftconvolve as fftconvolve_gpu

# 1. Load and rasterize pieces
pieces_by_size = load_pieces()

# 2. Create packer
packer = GPUPacker(strip_width=215, max_length=3000)

# 3. Evaluate a ratio
ratio = {'46': 0, '48': 2, '50': 1, '52': 1, '54': 0, '56': 0, '58': 0}
efficiency, length_yards = evaluate_ratio(pieces_by_size, ratio, packer)

print(f"Ratio 0-2-1-1-0-0-0: {efficiency*100:.1f}% efficiency, {length_yards:.2f}Y")
```

## Validation

The baseline includes a validation check: 2-bundle same-size markers should always be more efficient than 1-bundle due to better piece utilization:

```python
# Expected: 2-bundle efficiency > 1-bundle efficiency
# Expected: 2-bundle length < 2× 1-bundle length

for size in ALL_SIZES:
    r1 = results_1bundle[size]
    r2 = results_2bundle[size]

    assert r2['efficiency'] > r1['efficiency'], f"Size {size} failed"
    assert r2['length_yards'] < 2 * r1['length_yards'], f"Size {size} length check failed"
```

If validation fails, the sorting strategy likely needs adjustment (switch from area_desc to width_desc).
