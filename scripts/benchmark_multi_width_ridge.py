#!/usr/bin/env python3
"""
Benchmark: Multi-Width Ridge Prediction Accuracy

Validates whether a Ridge model trained on a base fabric width can accurately
predict nesting efficiency/length at other widths, using `fabric_width_mm` as
a feature.

Test data: AAMA pattern (23583) with 5 sizes, bundle counts 1-4 = 125 ratios.
Widths: 54", 58" (base), 62".

Scenarios:
  A — Ridge trained on base width only (zero cross-width samples)
  B — Base + 20 diverse anchors per extra width
  C — Base + 50 diverse anchors per extra width

Success criteria:
  - R² > 0.95 for Scenario C
  - Top-10 overlap ≥ 8/10
  - Worst-case error < 5%
"""

import sys
import time
import math
from pathlib import Path
from itertools import combinations_with_replacement

import numpy as np

# Add project root and backend to path
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "backend"))

from backend.services.gpu_nesting_runner import (
    _init_gpu,
    GPUPacker,
    load_pieces_for_material,
    evaluate_ratio,
    generate_all_ratios,
    ratio_to_str,
    _extract_geometry,
    _compute_perimeter_mm,
)


# ── Configuration ──────────────────────────────────────────────────────────

DXF_PATH = str(PROJECT_ROOT / "data/dxf-amaa/23583 PROD 1 L 0 W 0 25FEB22.dxf")
RUL_PATH = str(PROJECT_ROOT / "data/dxf-amaa/23583 PROD 1 L 0 W 0 25FEB22.rul")
MATERIAL = "SO1"
GPU_SCALE = 0.15

WIDTHS_INCHES = [54.0, 58.0, 62.0]
BASE_WIDTH_INCHES = 58.0

MAX_BUNDLE_COUNT = 4  # 5 sizes × 4 bc = 125 ratios
BENCHMARK_SIZES = ['46', '48', '50', '52', '54']  # Pick 5 middle sizes from the 13 available


# ── Helpers ────────────────────────────────────────────────────────────────

def brute_force_all_widths(
    pieces_by_size: dict,
    sizes: list,
    widths_inches: list,
    gpu_scale: float,
) -> dict:
    """
    Evaluate ALL ratios at ALL widths.

    Returns: {width_inches: {ratio_str: {efficiency, length_yards}}}
    """
    # Generate all ratios for bc=1..MAX_BUNDLE_COUNT
    all_ratios = []
    for bc in range(1, MAX_BUNDLE_COUNT + 1):
        all_ratios.extend(generate_all_ratios(bc, sizes))

    print(f"Total ratios: {len(all_ratios)} × {len(widths_inches)} widths = {len(all_ratios) * len(widths_inches)} evals")

    ground_truth = {}

    for width_in in widths_inches:
        width_mm = width_in * 25.4
        strip_width_px = int(width_mm * gpu_scale)

        # Estimate max container length
        max_area = max(
            sum(p['area'] * p['demand'] for p in pieces_by_size.get(s, []))
            for s in sizes if s in pieces_by_size
        )
        max_length = int((MAX_BUNDLE_COUNT * max_area * 2) / strip_width_px) + 500
        packer = GPUPacker(strip_width_px, max_length)

        width_results = {}
        t0 = time.time()

        for i, ratio in enumerate(all_ratios):
            eff, length, _, perim = evaluate_ratio(
                pieces_by_size, ratio, packer, strip_width_px, gpu_scale,
                dual_sort=True,  # Best possible result
            )
            rstr = ratio_to_str(ratio, sizes)
            bc = sum(ratio.get(s, 0) for s in sizes)
            width_results[rstr] = {
                'efficiency': eff,
                'length_yards': length,
                'ratio': ratio,
                'bundle_count': bc,
            }

        elapsed = time.time() - t0
        print(f"  {width_in}\" — {len(all_ratios)} ratios in {elapsed:.1f}s ({elapsed/len(all_ratios)*1000:.0f}ms/ratio)")
        ground_truth[width_in] = width_results

    return ground_truth


def build_features_with_width(
    ratio: dict,
    sizes: list,
    geom: dict,
    fabric_width_mm: float,
) -> list:
    """
    Build Ridge feature vector for a ratio, including fabric_width_mm as the
    first feature. Mirrors _build_features from gpu_nesting_runner but prepends
    the absolute width.
    """
    feats = []

    # Feature 0: absolute fabric width in mm (the key cross-width feature)
    feats.append(fabric_width_mm)

    bc = sum(ratio.get(s, 0) for s in sizes)
    if bc == 0:
        bc = 1

    # Group 1: Ratio features
    props = [ratio.get(s, 0) / bc for s in sizes]
    feats.extend(props)
    feats.append(bc)

    # Total bundle area (mm²) for this ratio
    total_area = sum(
        ratio.get(s, 0) * geom['bundle_area'].get(s, 0)
        for s in sizes
    )
    feats.append(total_area)

    # Max piece width ratio (relative to this width)
    max_wr = max(
        (geom['max_piece_width'].get(s, 0) / fabric_width_mm
         if ratio.get(s, 0) > 0 and fabric_width_mm > 0 else 0)
        for s in sizes
    )
    feats.append(max_wr)

    # Group 2: Ratio × geometry cross features
    for s in sizes:
        s_count = ratio.get(s, 0)
        if s_count > 0:
            feats.append(s_count * geom['bundle_area'].get(s, 0))
            feats.append(s_count * geom['max_piece_width'].get(s, 0))
        else:
            feats.append(0)
            feats.append(0)

    # Group 3: Pairwise interactions (proportion products)
    for i in range(len(sizes)):
        for j in range(i + 1, len(sizes)):
            feats.append(props[i] * props[j])

    return feats


def select_diverse_anchors(
    base_results: dict,
    sizes: list,
    n: int,
) -> list:
    """
    Greedy farthest-first selection of n diverse ratios from base results.
    Spans all bundle counts.

    Returns list of ratio_str keys.
    """
    items = list(base_results.items())
    if len(items) <= n:
        return [rstr for rstr, _ in items]

    n_sizes = len(sizes)

    def _to_props(result):
        ratio = result['ratio']
        bc = result['bundle_count']
        if bc == 0:
            bc = 1
        return [ratio.get(s, 0) / bc for s in sizes] + [bc / MAX_BUNDLE_COUNT]

    # Start with best overall
    items_sorted = sorted(items, key=lambda x: -x[1]['efficiency'])
    selected = [items_sorted[0][0]]
    selected_props = [_to_props(items_sorted[0][1])]

    remaining_keys = {rstr for rstr, _ in items_sorted[1:]}

    while len(selected) < n and remaining_keys:
        best_key = None
        best_min_dist = -1

        for rstr in remaining_keys:
            result = base_results[rstr]
            props = _to_props(result)
            min_dist = min(
                sum((a - b) ** 2 for a, b in zip(props, sp))
                for sp in selected_props
            )
            if min_dist > best_min_dist:
                best_min_dist = min_dist
                best_key = rstr

        if best_key:
            selected.append(best_key)
            selected_props.append(_to_props(base_results[best_key]))
            remaining_keys.discard(best_key)
        else:
            break

    return selected


def train_and_predict(
    train_data: list,
    predict_data: list,
    sizes: list,
    geom: dict,
) -> list:
    """
    Train Ridge on train_data, predict on predict_data.

    train_data / predict_data: list of {ratio, width_inches, length_yards}
    Returns: list of {ratio_str, width_inches, predicted_length_yards}
    """
    from sklearn.linear_model import Ridge

    X_train = []
    y_train = []
    for item in train_data:
        width_mm = item['width_inches'] * 25.4
        feats = build_features_with_width(item['ratio'], sizes, geom, width_mm)
        X_train.append(feats)
        y_train.append(item['length_yards'])

    X_train = np.array(X_train)
    y_train = np.array(y_train)

    model = Ridge(alpha=1.0)
    model.fit(X_train, y_train)

    # Training R²
    y_pred_train = model.predict(X_train)
    ss_res = np.sum((y_train - y_pred_train) ** 2)
    ss_tot = np.sum((y_train - np.mean(y_train)) ** 2)
    r2_train = 1 - ss_res / ss_tot if ss_tot > 0 else 0

    predictions = []
    X_pred = []
    for item in predict_data:
        width_mm = item['width_inches'] * 25.4
        feats = build_features_with_width(item['ratio'], sizes, geom, width_mm)
        X_pred.append(feats)

    if X_pred:
        X_pred = np.array(X_pred)
        y_pred = model.predict(X_pred)
        for i, item in enumerate(predict_data):
            predictions.append({
                'ratio_str': item['ratio_str'],
                'width_inches': item['width_inches'],
                'predicted_length_yards': float(y_pred[i]),
            })

    return predictions, r2_train


def compute_metrics(predictions, ground_truth, width_inches, sizes, pieces_by_size, gpu_scale):
    """Compute R², RMSE, max error for predictions at a given width."""
    actuals = []
    preds = []

    for p in predictions:
        if p['width_inches'] != width_inches:
            continue
        actual = ground_truth[width_inches][p['ratio_str']]['length_yards']
        pred = p['predicted_length_yards']
        actuals.append(actual)
        preds.append(pred)

    if not actuals:
        return {'r2': 0, 'rmse': 0, 'max_error_pct': 0, 'n': 0}

    actuals = np.array(actuals)
    preds = np.array(preds)

    ss_res = np.sum((actuals - preds) ** 2)
    ss_tot = np.sum((actuals - np.mean(actuals)) ** 2)
    r2 = 1 - ss_res / ss_tot if ss_tot > 0 else 0

    rmse = np.sqrt(np.mean((actuals - preds) ** 2))

    errors_pct = np.abs(actuals - preds) / np.maximum(actuals, 1e-6) * 100
    max_error_pct = float(np.max(errors_pct))

    return {
        'r2': float(r2),
        'rmse': float(rmse),
        'max_error_pct': max_error_pct,
        'n': len(actuals),
    }


def ranking_overlap(predictions, ground_truth, width_inches, top_k=10):
    """
    Compare top-K ratios by predicted length (shortest) vs actual length (shortest)
    at a given width. Returns overlap count.
    """
    width_gt = ground_truth[width_inches]

    # Actual top-K (shortest length)
    actual_sorted = sorted(width_gt.items(), key=lambda x: x[1]['length_yards'])
    actual_top = {rstr for rstr, _ in actual_sorted[:top_k]}

    # Get all predictions for this width
    width_preds = [p for p in predictions if p['width_inches'] == width_inches]

    # Also include any training samples that were directly evaluated
    # (they should be available from ground truth but not in predictions)
    predicted_map = {p['ratio_str']: p['predicted_length_yards'] for p in width_preds}

    # Add ground truth for ratios that were used as training data (not predicted)
    for rstr, data in width_gt.items():
        if rstr not in predicted_map:
            predicted_map[rstr] = data['length_yards']  # Use actual for training data

    pred_sorted = sorted(predicted_map.items(), key=lambda x: x[1])
    pred_top = {rstr for rstr, _ in pred_sorted[:top_k]}

    overlap = len(actual_top & pred_top)
    return overlap, actual_top, pred_top


# ── Main ───────────────────────────────────────────────────────────────────

def main():
    print("=" * 70)
    print("Multi-Width Ridge Benchmark")
    print("=" * 70)

    # Initialize GPU
    if not _init_gpu():
        print("ERROR: GPU not available")
        sys.exit(1)

    # Load pieces for 5 benchmark sizes only
    print(f"\nLoading AAMA pattern (benchmark sizes: {BENCHMARK_SIZES})...")

    pieces_by_size = load_pieces_for_material(
        DXF_PATH, RUL_PATH, MATERIAL,
        sizes=BENCHMARK_SIZES,
        gpu_scale=GPU_SCALE,
    )

    sizes = sorted(pieces_by_size.keys())
    print(f"Sizes: {sizes}")
    for s in sizes:
        print(f"  {s}: {len(pieces_by_size[s])} pieces, "
              f"demand sum={sum(p['demand'] for p in pieces_by_size[s])}")

    # Count total ratios
    total_ratios = 0
    for bc in range(1, MAX_BUNDLE_COUNT + 1):
        n = math.comb(len(sizes) + bc - 1, bc)
        total_ratios += n
        print(f"  BC={bc}: {n} ratios")
    print(f"  Total: {total_ratios} ratios")

    # ── Ground Truth ───────────────────────────────────────────────────
    print("\n" + "─" * 70)
    print("Phase 1: Brute-force ground truth at all widths")
    print("─" * 70)

    t0 = time.time()
    ground_truth = brute_force_all_widths(pieces_by_size, sizes, WIDTHS_INCHES, GPU_SCALE)
    gt_time = time.time() - t0
    print(f"\nGround truth complete in {gt_time:.1f}s ({len(WIDTHS_INCHES) * total_ratios} total evals)")

    # Quick sanity: show best ratio per width
    for w in WIDTHS_INCHES:
        best = max(ground_truth[w].items(), key=lambda x: x[1]['efficiency'])
        print(f"  {w}\": best = {best[0]} @ {best[1]['efficiency']*100:.1f}%, {best[1]['length_yards']:.2f}yd")

    # Extract geometry (needed for features)
    base_width_mm = BASE_WIDTH_INCHES * 25.4
    geom = _extract_geometry(pieces_by_size, sizes, base_width_mm, GPU_SCALE)

    # Generate all ratios for reference
    all_ratios = []
    for bc in range(1, MAX_BUNDLE_COUNT + 1):
        all_ratios.extend(generate_all_ratios(bc, sizes))
    all_ratio_strs = [ratio_to_str(r, sizes) for r in all_ratios]
    ratio_map = {ratio_to_str(r, sizes): r for r in all_ratios}

    extra_widths = [w for w in WIDTHS_INCHES if w != BASE_WIDTH_INCHES]

    # ── Scenario A ─────────────────────────────────────────────────────
    print("\n" + "─" * 70)
    print("Scenario A: Base-only Ridge (zero cross-width anchors)")
    print("─" * 70)

    # Training data: all ratios at base width only
    train_a = []
    for rstr, data in ground_truth[BASE_WIDTH_INCHES].items():
        train_a.append({
            'ratio': data['ratio'],
            'ratio_str': rstr,
            'width_inches': BASE_WIDTH_INCHES,
            'length_yards': data['length_yards'],
        })

    # Predict: all ratios at extra widths
    predict_a = []
    for w in extra_widths:
        for rstr in all_ratio_strs:
            predict_a.append({
                'ratio': ratio_map[rstr],
                'ratio_str': rstr,
                'width_inches': w,
            })

    preds_a, r2_train_a = train_and_predict(train_a, predict_a, sizes, geom)
    print(f"  Training R² (base only): {r2_train_a:.4f}")
    print(f"  Training samples: {len(train_a)}")
    print(f"  Predictions: {len(preds_a)}")

    for w in extra_widths:
        metrics = compute_metrics(preds_a, ground_truth, w, sizes, pieces_by_size, GPU_SCALE)
        overlap_10, actual_top10, pred_top10 = ranking_overlap(preds_a, ground_truth, w, 10)
        overlap_25, _, _ = ranking_overlap(preds_a, ground_truth, w, 25)
        print(f"\n  {w}\" — R²={metrics['r2']:.4f}, RMSE={metrics['rmse']:.4f}yd, "
              f"MaxErr={metrics['max_error_pct']:.1f}%, n={metrics['n']}")
        print(f"       Top-10 overlap: {overlap_10}/10, Top-25 overlap: {overlap_25}/25")

    # ── Scenario B ─────────────────────────────────────────────────────
    print("\n" + "─" * 70)
    print("Scenario B: Base + 20 diverse anchors per extra width")
    print("─" * 70)

    anchor_count_b = 20
    base_gt = ground_truth[BASE_WIDTH_INCHES]

    # Select diverse anchors
    anchor_keys_b = select_diverse_anchors(base_gt, sizes, anchor_count_b)
    print(f"  Selected {len(anchor_keys_b)} diverse anchors from base width")

    train_b = list(train_a)  # Start with all base width data
    predict_b = []

    for w in extra_widths:
        # Add anchor evaluations as training data
        for rstr in anchor_keys_b:
            gt_data = ground_truth[w][rstr]
            train_b.append({
                'ratio': gt_data['ratio'],
                'ratio_str': rstr,
                'width_inches': w,
                'length_yards': gt_data['length_yards'],
            })

        # Predict remaining
        for rstr in all_ratio_strs:
            if rstr not in anchor_keys_b:
                predict_b.append({
                    'ratio': ratio_map[rstr],
                    'ratio_str': rstr,
                    'width_inches': w,
                })

    preds_b, r2_train_b = train_and_predict(train_b, predict_b, sizes, geom)
    print(f"  Training R² (base + anchors): {r2_train_b:.4f}")
    print(f"  Training samples: {len(train_b)}")
    print(f"  Predictions: {len(preds_b)}")

    for w in extra_widths:
        metrics = compute_metrics(preds_b, ground_truth, w, sizes, pieces_by_size, GPU_SCALE)
        overlap_10, _, _ = ranking_overlap(preds_b, ground_truth, w, 10)
        overlap_25, _, _ = ranking_overlap(preds_b, ground_truth, w, 25)
        print(f"\n  {w}\" — R²={metrics['r2']:.4f}, RMSE={metrics['rmse']:.4f}yd, "
              f"MaxErr={metrics['max_error_pct']:.1f}%, n={metrics['n']}")
        print(f"       Top-10 overlap: {overlap_10}/10, Top-25 overlap: {overlap_25}/25")

    # ── Scenario C ─────────────────────────────────────────────────────
    print("\n" + "─" * 70)
    print("Scenario C: Base + 50 diverse anchors per extra width")
    print("─" * 70)

    anchor_count_c = 50
    anchor_keys_c = select_diverse_anchors(base_gt, sizes, anchor_count_c)
    print(f"  Selected {len(anchor_keys_c)} diverse anchors from base width")

    train_c = list(train_a)  # Start with all base width data
    predict_c = []

    for w in extra_widths:
        for rstr in anchor_keys_c:
            gt_data = ground_truth[w][rstr]
            train_c.append({
                'ratio': gt_data['ratio'],
                'ratio_str': rstr,
                'width_inches': w,
                'length_yards': gt_data['length_yards'],
            })

        for rstr in all_ratio_strs:
            if rstr not in anchor_keys_c:
                predict_c.append({
                    'ratio': ratio_map[rstr],
                    'ratio_str': rstr,
                    'width_inches': w,
                })

    preds_c, r2_train_c = train_and_predict(train_c, predict_c, sizes, geom)
    print(f"  Training R² (base + anchors): {r2_train_c:.4f}")
    print(f"  Training samples: {len(train_c)}")
    print(f"  Predictions: {len(preds_c)}")

    for w in extra_widths:
        metrics = compute_metrics(preds_c, ground_truth, w, sizes, pieces_by_size, GPU_SCALE)
        overlap_10, _, _ = ranking_overlap(preds_c, ground_truth, w, 10)
        overlap_25, _, _ = ranking_overlap(preds_c, ground_truth, w, 25)
        print(f"\n  {w}\" — R²={metrics['r2']:.4f}, RMSE={metrics['rmse']:.4f}yd, "
              f"MaxErr={metrics['max_error_pct']:.1f}%, n={metrics['n']}")
        print(f"       Top-10 overlap: {overlap_10}/10, Top-25 overlap: {overlap_25}/25")

    # ── Summary ────────────────────────────────────────────────────────
    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)
    print(f"\n{'Scenario':<12} {'Width':<8} {'R²':<8} {'RMSE(yd)':<10} {'MaxErr%':<10} {'Top10':<8} {'Top25':<8}")
    print("─" * 64)

    for label, preds in [("A (0)", preds_a), ("B (20)", preds_b), ("C (50)", preds_c)]:
        for w in extra_widths:
            m = compute_metrics(preds, ground_truth, w, sizes, pieces_by_size, GPU_SCALE)
            o10, _, _ = ranking_overlap(preds, ground_truth, w, 10)
            o25, _, _ = ranking_overlap(preds, ground_truth, w, 25)
            print(f"{label:<12} {w:<8.0f} {m['r2']:<8.4f} {m['rmse']:<10.4f} {m['max_error_pct']:<10.1f} {o10}/10{'':<3} {o25}/25")

    # ── Pass/Fail ──────────────────────────────────────────────────────
    print("\n" + "─" * 70)
    print("PASS/FAIL Criteria")
    print("─" * 70)

    all_pass = True
    for w in extra_widths:
        m = compute_metrics(preds_c, ground_truth, w, sizes, pieces_by_size, GPU_SCALE)
        o10, _, _ = ranking_overlap(preds_c, ground_truth, w, 10)

        r2_ok = m['r2'] > 0.95
        overlap_ok = o10 >= 8
        error_ok = m['max_error_pct'] < 5.0

        print(f"  {w}\":")
        print(f"    R² > 0.95:           {'PASS' if r2_ok else 'FAIL'} ({m['r2']:.4f})")
        print(f"    Top-10 overlap ≥ 8:  {'PASS' if overlap_ok else 'FAIL'} ({o10}/10)")
        print(f"    Max error < 5%:      {'PASS' if error_ok else 'FAIL'} ({m['max_error_pct']:.1f}%)")

        if not (r2_ok and overlap_ok and error_ok):
            all_pass = False

    print(f"\n  Overall: {'ALL PASS' if all_pass else 'SOME FAILED'}")
    print("=" * 70)


if __name__ == "__main__":
    main()
