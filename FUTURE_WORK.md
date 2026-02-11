# MarkerMind - Future Work

## Pattern Database Schema

**Priority:** Medium
**Added:** 2026-02-10

### Current State
- DXF/RUL files stored on disk
- Parsed on-demand when needed
- Only metadata (sizes, materials) in DB

### Proposed Enhancement
Store pattern vertices and piece data directly in database:

```sql
-- Pieces table
pieces (
  id, pattern_id, name, material,
  vertices JSONB,  -- or PostGIS geometry
  area_mm2, bbox_width, bbox_height,
  is_mirrored, grain_direction
)

-- Graded pieces per size
graded_pieces (
  id, piece_id, size_code,
  vertices JSONB,
  scale_factor
)
```

### Benefits
- Faster access (no file parsing)
- Query patterns by piece characteristics
- Better for multi-tenant SaaS
- Enable piece-level reuse across styles
- Pattern versioning

### When to Implement
- After MVP stable
- When scaling to multiple customers
- If pattern library sharing needed

---

---

## GPU Nesting Performance Optimization

**Priority:** High
**Added:** 2026-02-10

### Current Performance
- ~250ms per ratio evaluation
- ~7 minutes for full 6-bundle scan (1715 ratios)
- GPU utilization: ~39%, 367MB VRAM

### Bottleneck Analysis
Raw FFT convolution is fast (0.5ms), but **GPU→CPU sync points** kill performance:

| Operation | Time | Sync Points |
|-----------|------|-------------|
| Raw FFT convolution | 0.5ms | 1 |
| Full piece placement | 4-5ms | 5-6 syncs |
| Full ratio (23 pieces) | 250ms | ~120 syncs |

### Optimization Paths (by impact)

1. **CuPy RawKernel / Numba CUDA**
   - Write position-finding as single GPU kernel
   - Eliminate all intermediate syncs
   - Expected: 10-50x speedup

2. **Batch Rotations**
   - Pre-flip all kernels once
   - Process 0° and 180° in parallel

3. **CUDA Streams**
   - Overlap FFT computation with coordinate extraction
   - Hide latency of small syncs

4. **Cached FFT Plans**
   - cuFFT plan reuse (fftconvolve recreates plans each call)
   - Use cupyx.scipy.fft with plan caching

5. **Width-first Sorting**
   - Per CLAUDE.md, width_desc has 67% win rate
   - Try both strategies, keep best

### Target Performance
- <50ms per ratio (5x improvement)
- <2 minutes for full scan
- >80% GPU utilization

---
