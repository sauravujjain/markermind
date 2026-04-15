"""
GPU-Accelerated Batched Overlap Evaluation for Sparrow Nesting

Ports the core overlap_area_proxy (Algorithm 3) from Sparrow's Rust code
to CUDA, enabling thousands of candidate positions to be evaluated in parallel.

Architecture:
  - CPU (Sparrow): Generates candidate transforms (tx, ty, theta)
  - GPU (this module): Evaluates all candidates' overlap loss simultaneously
  - CPU: Picks best candidate, updates layout

The core insight: Sparrow's separation phase evaluates ~75 candidates per item
per iteration (50 container + 25 focused + refinement). With 20+ items colliding,
that's 1500+ sequential evaluations per iteration. GPU batches ALL of them.

Uses CuPy RawKernel for direct CUDA programming with the existing CUDA toolkit.
"""

import math
from typing import Dict, List, Optional, Tuple

import cupy as cp
import numpy as np

# -- CUDA Kernel: Batched Pole Overlap Evaluation ------------------------

# CUDA kernel source - must be pure ASCII for nvrtc
_OVERLAP_KERNEL_SRC = """\
extern "C" __global__
void batched_overlap_loss(
    // Placed items' poles (all concatenated into flat SoA arrays)
    const float* __restrict__ placed_x,      // [N_placed]
    const float* __restrict__ placed_y,       // [N_placed]
    const float* __restrict__ placed_r,       // [N_placed]
    const int N_placed,                       // total placed poles

    // Which placed item each pole belongs to (for per-pair weighting)
    const int* __restrict__ placed_item_id,   // [N_placed]

    // Per-item metadata for shape penalty
    const float* __restrict__ item_ch_area,   // [N_items] convex hull area per item
    const float* __restrict__ item_diameter,  // [N_items] diameter per item

    // Candidate item's poles (reference, at origin)
    const float* __restrict__ cand_x,         // [N_cand_poles]
    const float* __restrict__ cand_y,         // [N_cand_poles]
    const float* __restrict__ cand_r,         // [N_cand_poles]
    const int N_cand_poles,
    const float cand_ch_area,                 // candidate convex hull area
    const float cand_diameter,                // candidate diameter

    // Container bounds
    const float container_x_min,
    const float container_y_min,
    const float container_x_max,
    const float container_y_max,

    // Candidate transforms: each thread evaluates one (tx, ty, theta)
    const float* __restrict__ transforms,     // [N_transforms, 3] flattened
    const int N_transforms,

    // GLS weights per placed item for the candidate
    const float* __restrict__ gls_weights,    // [N_items]
    const float container_weight,

    // Per-item overlap epsilon
    const float epsilon_ratio,                // OVERLAP_PROXY_EPSILON_DIAM_RATIO (0.01)

    // Output: loss per transform
    float* __restrict__ out_losses            // [N_transforms]
) {
    int tid = blockIdx.x * blockDim.x + threadIdx.x;
    if (tid >= N_transforms) return;

    // Load this thread's transform
    float tx = transforms[tid * 3 + 0];
    float ty = transforms[tid * 3 + 1];
    float theta = transforms[tid * 3 + 2];  // radians

    float cos_t = cosf(theta);
    float sin_t = sinf(theta);

    // Transform candidate poles to new position
    // Rotation then translation: x' = cos*x - sin*y + tx, y' = sin*x + cos*y + ty

    // Compute transformed candidate bounding box for container collision
    float cand_bbox_xmin = 1e30f, cand_bbox_xmax = -1e30f;
    float cand_bbox_ymin = 1e30f, cand_bbox_ymax = -1e30f;

    // We'll compute total loss as sum of:
    // 1. Per-item overlap losses (weighted)
    // 2. Container collision loss (weighted)

    float total_loss = 0.0f;

    // -- Per-item overlap computation --------------------------------
    // For each placed item, accumulate the overlap_area_proxy
    // We need to track which item each placed pole belongs to

    // Strategy: iterate over candidate poles (outer), placed poles (inner)
    // Track per-item partial sums using shared memory or registers

    // Simple approach: accumulate per placed pole, then aggregate per item
    // Since N_items is typically < 100, we can use local arrays
    // But CUDA registers are limited, so we accumulate directly

    // First pass: compute overlap for each (cand_pole, placed_pole) pair
    // and accumulate into per-item overlap sum

    // We need per-item accumulators. For simplicity with variable N_items,
    // we use atomicAdd to a shared/local buffer. But since this is per-thread,
    // we can use the output array as scratch or just do the full NxM loop.

    // Actually, the simplest approach: iterate placed poles, compute overlap,
    // apply weight inline. This avoids any per-item bookkeeping.

    // But we need shape_penalty per item pair, which depends on item convex hull area.
    // shape_penalty = sqrt(sqrt(cand_ch_area) * sqrt(item_ch_area[i]))

    // Pre-compute candidate sqrt area
    float cand_sqrt_area = sqrtf(cand_ch_area);

    // -- Overlap with placed items ----------------------------------
    // Process in two passes:
    // Pass 1: For each placed pole, compute penetration depth contribution
    //         weighted by GLS weight and shape penalty

    // Group by item: We know placed poles are sorted by item_id
    int prev_item_id = -1;
    float item_overlap_sum = 0.0f;
    float item_epsilon = 0.0f;
    float item_weight = 1.0f;
    float item_penalty = 1.0f;

    for (int j = 0; j < N_placed; j++) {
        int item_id = placed_item_id[j];

        // New item boundary -- flush accumulated overlap for previous item
        if (item_id != prev_item_id) {
            if (prev_item_id >= 0 && item_overlap_sum > 0.0f) {
                float pi = 3.14159265f;
                float overlap_proxy = item_overlap_sum * pi + item_epsilon * item_epsilon;
                float loss = sqrtf(overlap_proxy) * item_penalty * item_weight;
                total_loss += loss;
            }
            // Initialize for new item
            prev_item_id = item_id;
            item_overlap_sum = 0.0f;
            item_epsilon = fmaxf(cand_diameter, item_diameter[item_id]) * epsilon_ratio;
            item_weight = gls_weights[item_id];
            item_penalty = sqrtf(cand_sqrt_area * sqrtf(item_ch_area[item_id]));
        }

        float px = placed_x[j];
        float py = placed_y[j];
        float pr = placed_r[j];

        // Check against all candidate poles
        for (int i = 0; i < N_cand_poles; i++) {
            // Transform candidate pole
            float cx = cos_t * cand_x[i] - sin_t * cand_y[i] + tx;
            float cy = sin_t * cand_x[i] + cos_t * cand_y[i] + ty;
            float cr = cand_r[i];

            // Penetration depth
            float dx = cx - px;
            float dy = cy - py;
            float dist = sqrtf(dx * dx + dy * dy);
            float pd = (cr + pr) - dist;

            // Decay function (Algorithm 3)
            float pd_decay;
            if (pd >= item_epsilon) {
                pd_decay = pd;
            } else {
                pd_decay = item_epsilon * item_epsilon / (-pd + 2.0f * item_epsilon);
            }

            float min_r = fminf(cr, pr);
            item_overlap_sum += pd_decay * min_r;
        }
    }

    // Flush last item
    if (prev_item_id >= 0 && item_overlap_sum > 0.0f) {
        float pi = 3.14159265f;
        float overlap_proxy = item_overlap_sum * pi + item_epsilon * item_epsilon;
        float loss = sqrtf(overlap_proxy) * item_penalty * item_weight;
        total_loss += loss;
    }

    // -- Container collision ----------------------------------------
    // Compute transformed candidate bbox
    for (int i = 0; i < N_cand_poles; i++) {
        float cx = cos_t * cand_x[i] - sin_t * cand_y[i] + tx;
        float cy = sin_t * cand_x[i] + cos_t * cand_y[i] + ty;
        float cr = cand_r[i];

        // Approximate piece bbox from pole positions + radii
        if (cx - cr < cand_bbox_xmin) cand_bbox_xmin = cx - cr;
        if (cx + cr > cand_bbox_xmax) cand_bbox_xmax = cx + cr;
        if (cy - cr < cand_bbox_ymin) cand_bbox_ymin = cy - cr;
        if (cy + cr > cand_bbox_ymax) cand_bbox_ymax = cy + cr;
    }

    // Check if bbox exceeds container
    float bbox_area = (cand_bbox_xmax - cand_bbox_xmin) * (cand_bbox_ymax - cand_bbox_ymin);
    if (bbox_area > 0.0f) {
        float ix_min = fmaxf(cand_bbox_xmin, container_x_min);
        float iy_min = fmaxf(cand_bbox_ymin, container_y_min);
        float ix_max = fminf(cand_bbox_xmax, container_x_max);
        float iy_max = fminf(cand_bbox_ymax, container_y_max);

        float container_overlap;
        if (ix_max > ix_min && iy_max > iy_min) {
            float intersection_area = (ix_max - ix_min) * (iy_max - iy_min);
            container_overlap = (bbox_area - intersection_area) + 0.0001f * bbox_area;
        } else {
            // No intersection -- heavily penalize
            float bbox_cx = (cand_bbox_xmin + cand_bbox_xmax) * 0.5f;
            float bbox_cy = (cand_bbox_ymin + cand_bbox_ymax) * 0.5f;
            float cont_cx = (container_x_min + container_x_max) * 0.5f;
            float cont_cy = (container_y_min + container_y_max) * 0.5f;
            float cdist = sqrtf((bbox_cx - cont_cx) * (bbox_cx - cont_cx) +
                                (bbox_cy - cont_cy) * (bbox_cy - cont_cy));
            container_overlap = bbox_area + cdist;
        }

        if (container_overlap > 0.0f) {
            float cont_penalty = sqrtf(cand_sqrt_area * cand_sqrt_area);  // calc_shape_penalty(s, s)
            float cont_loss = 2.0f * sqrtf(container_overlap) * cont_penalty * container_weight;
            total_loss += cont_loss;
        }
    }

    out_losses[tid] = total_loss;
}
"""

_OVERLAP_KERNEL = cp.RawKernel(_OVERLAP_KERNEL_SRC, "batched_overlap_loss")


class GPUOverlapEvaluator:
    """
    GPU-accelerated overlap evaluator for Sparrow's separation phase.

    Maintains placed items' pole data on GPU memory. When asked to evaluate
    candidate positions for a given item, it launches the CUDA kernel to
    evaluate all candidates in parallel.

    Typical usage:
        evaluator = GPUOverlapEvaluator(container_width=1500, container_height=5000)
        evaluator.load_placed_items(poles_data, item_metadata)
        best_idx, losses = evaluator.evaluate_candidates(
            candidate_poles, candidate_meta, transforms, gls_weights
        )
    """

    def __init__(self, container_width: float, container_height: float):
        """
        Args:
            container_width: Strip width (fixed dimension) in mm
            container_height: Strip height (variable dimension) in mm
        """
        self.container_x_min = 0.0
        self.container_y_min = 0.0
        self.container_x_max = container_height  # strip length
        self.container_y_max = container_width    # strip width (fixed)

        # GPU buffers for placed items' poles
        self._placed_x = None
        self._placed_y = None
        self._placed_r = None
        self._placed_item_id = None
        self._item_ch_area = None
        self._item_diameter = None
        self._n_placed = 0
        self._n_items = 0

        self.epsilon_ratio = 0.01  # OVERLAP_PROXY_EPSILON_DIAM_RATIO from Sparrow

    def update_container_length(self, length: float):
        """Update strip length (called when strip shrinks during optimization)."""
        self.container_x_max = length

    def load_placed_items(
        self,
        poles_per_item: List[List[Tuple[float, float, float]]],  # [(x, y, r), ...]
        ch_areas: List[float],
        diameters: List[float],
    ):
        """
        Load all placed items' poles to GPU memory.

        Args:
            poles_per_item: List of pole lists, one per placed item.
                Each pole is (center_x, center_y, radius).
            ch_areas: Convex hull area for each item.
            diameters: Diameter for each item.
        """
        n_items = len(poles_per_item)
        if n_items == 0:
            self._n_placed = 0
            self._n_items = 0
            return

        # Flatten poles into SoA arrays, tracking item ownership
        all_x, all_y, all_r, all_item_id = [], [], [], []
        for item_idx, poles in enumerate(poles_per_item):
            for px, py, pr in poles:
                all_x.append(px)
                all_y.append(py)
                all_r.append(pr)
                all_item_id.append(item_idx)

        # Sort by item_id (required by kernel's grouping logic)
        # Already sorted since we iterate items in order

        self._n_placed = len(all_x)
        self._n_items = n_items

        # Transfer to GPU
        self._placed_x = cp.array(all_x, dtype=cp.float32)
        self._placed_y = cp.array(all_y, dtype=cp.float32)
        self._placed_r = cp.array(all_r, dtype=cp.float32)
        self._placed_item_id = cp.array(all_item_id, dtype=cp.int32)
        self._item_ch_area = cp.array(ch_areas, dtype=cp.float32)
        self._item_diameter = cp.array(diameters, dtype=cp.float32)

    def evaluate_candidates(
        self,
        cand_poles: List[Tuple[float, float, float]],  # [(x, y, r), ...]
        cand_ch_area: float,
        cand_diameter: float,
        transforms: np.ndarray,  # shape (N, 3) -- [tx, ty, theta_radians]
        gls_weights: Optional[np.ndarray] = None,  # per-item weights
        container_weight: float = 1.0,
    ) -> Tuple[int, np.ndarray]:
        """
        Evaluate all candidate transforms on GPU.

        Args:
            cand_poles: Candidate item's poles (at origin position).
            cand_ch_area: Candidate's convex hull area.
            cand_diameter: Candidate's diameter.
            transforms: (N, 3) array of [tx, ty, theta] transforms to evaluate.
            gls_weights: GLS weight per placed item. Default: all 1.0.
            container_weight: GLS weight for container collision.

        Returns:
            (best_idx, losses): Index of lowest-loss transform, and all losses.
        """
        n_transforms = len(transforms)
        if n_transforms == 0:
            return -1, np.array([])

        # Prepare candidate poles
        cand_x = cp.array([p[0] for p in cand_poles], dtype=cp.float32)
        cand_y = cp.array([p[1] for p in cand_poles], dtype=cp.float32)
        cand_r = cp.array([p[2] for p in cand_poles], dtype=cp.float32)
        n_cand_poles = len(cand_poles)

        # Prepare transforms
        transforms_gpu = cp.array(transforms.astype(np.float32).reshape(-1))

        # GLS weights
        if gls_weights is None:
            gls_weights_gpu = cp.ones(self._n_items, dtype=cp.float32)
        else:
            gls_weights_gpu = cp.array(gls_weights, dtype=cp.float32)

        # Output buffer
        out_losses = cp.zeros(n_transforms, dtype=cp.float32)

        # Launch kernel
        block_size = 256
        grid_size = (n_transforms + block_size - 1) // block_size

        _OVERLAP_KERNEL(
            (grid_size,),
            (block_size,),
            (
                self._placed_x, self._placed_y, self._placed_r,
                np.int32(self._n_placed),
                self._placed_item_id,
                self._item_ch_area, self._item_diameter,
                cand_x, cand_y, cand_r,
                np.int32(n_cand_poles),
                np.float32(cand_ch_area),
                np.float32(cand_diameter),
                np.float32(self.container_x_min),
                np.float32(self.container_y_min),
                np.float32(self.container_x_max),
                np.float32(self.container_y_max),
                transforms_gpu,
                np.int32(n_transforms),
                gls_weights_gpu,
                np.float32(container_weight),
                np.float32(self.epsilon_ratio),
                out_losses,
            ),
        )

        # Synchronize and get results
        cp.cuda.Stream.null.synchronize()
        losses_cpu = out_losses.get()
        best_idx = int(np.argmin(losses_cpu))

        return best_idx, losses_cpu

    def generate_uniform_transforms(
        self,
        n_container: int = 50,
        n_focused: int = 25,
        focus_center: Optional[Tuple[float, float]] = None,
        focus_radius: float = 100.0,
        allowed_rotations: List[float] = [0.0, math.pi],  # 0 degrees and 180 degrees
        rng: Optional[np.random.Generator] = None,
    ) -> np.ndarray:
        """
        Generate candidate transforms matching Sparrow's sampling strategy.

        Args:
            n_container: Random samples across full container.
            n_focused: Focused samples around current position.
            focus_center: (x, y) center for focused sampling.
            focus_radius: Radius for focused sampling.
            allowed_rotations: Allowed rotation angles in radians.

        Returns:
            (N, 3) array of [tx, ty, theta] transforms.
        """
        if rng is None:
            rng = np.random.default_rng()

        transforms = []

        # Container-wide uniform samples
        for _ in range(n_container):
            for theta in allowed_rotations:
                tx = rng.uniform(self.container_x_min, self.container_x_max)
                ty = rng.uniform(self.container_y_min, self.container_y_max)
                transforms.append([tx, ty, theta])

        # Focused samples around current position
        if focus_center is not None:
            fx, fy = focus_center
            for _ in range(n_focused):
                for theta in allowed_rotations:
                    tx = rng.uniform(fx - focus_radius, fx + focus_radius)
                    ty = rng.uniform(fy - focus_radius, fy + focus_radius)
                    transforms.append([tx, ty, theta])

        return np.array(transforms, dtype=np.float32)


# -- Convenience: Extract poles from Spyrrow solution ---------------------

def extract_poles_from_spyrrow_items(
    items: list,
    n_poles: int = 32,
) -> Tuple[List[List[Tuple[float, float, float]]], List[float], List[float]]:
    """
    Generate approximate poles for Spyrrow items.

    Since we can't directly access jagua-rs's pole generation from Python,
    we approximate using inscribed circles computed from the polygon.

    Args:
        items: List of spyrrow.Item objects.
        n_poles: Target number of poles per item.

    Returns:
        (poles_per_item, ch_areas, diameters)
    """
    from shapely.geometry import Polygon
    from shapely.ops import polylabel

    poles_per_item = []
    ch_areas = []
    diameters = []

    for item in items:
        verts = item.shape
        poly = Polygon(verts)
        ch = poly.convex_hull
        ch_areas.append(float(ch.area))

        # Diameter: max distance between any two vertices
        max_dist = 0.0
        for i, (x1, y1) in enumerate(verts):
            for x2, y2 in verts[i + 1 :]:
                d = math.sqrt((x2 - x1) ** 2 + (y2 - y1) ** 2)
                if d > max_dist:
                    max_dist = d
        diameters.append(max_dist)

        # Generate poles using iterative PoI approach
        poles = []

        # Primary pole: pole of inaccessibility
        try:
            poi = polylabel(poly, tolerance=1.0)
            r = poly.exterior.distance(poi)
            if r > 0:
                poles.append((float(poi.x), float(poi.y), float(r)))
        except Exception:
            # Fallback: centroid
            c = poly.centroid
            r = poly.exterior.distance(c) * 0.8
            if r > 0:
                poles.append((float(c.x), float(c.y), float(r)))

        # Additional poles: sample interior points and find inscribed circles
        if len(poles) < n_poles:
            minx, miny, maxx, maxy = poly.bounds
            rng = np.random.default_rng(42)
            attempts = 0
            while len(poles) < n_poles and attempts < n_poles * 10:
                attempts += 1
                px = rng.uniform(minx, maxx)
                py = rng.uniform(miny, maxy)
                from shapely.geometry import Point as ShapelyPoint

                pt = ShapelyPoint(px, py)
                if poly.contains(pt):
                    # Inscribed circle radius = distance to boundary
                    r = poly.exterior.distance(pt)
                    # Only add if not too close to existing poles
                    if r > 0.5:
                        too_close = False
                        for ex, ey, er in poles:
                            d = math.sqrt((px - ex) ** 2 + (py - ey) ** 2)
                            if d < er * 0.5:
                                too_close = True
                                break
                        if not too_close:
                            poles.append((px, py, float(r)))

        poles_per_item.append(poles)

    return poles_per_item, ch_areas, diameters
