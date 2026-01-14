#!/usr/bin/env python3
"""
Compare GPU raster vs CPU Spyrrow results and generate analysis.

Usage:
    PYTHONPATH=. python scripts/compare_gpu_cpu_v2.py
"""

import json
from pathlib import Path
from datetime import datetime

import numpy as np

OUTPUT_DIR = Path("experiment_results/gpu_vs_cpu_v2")


def spearman_correlation(x: list, y: list) -> float:
    """
    Calculate Spearman rank correlation coefficient.

    Args:
        x: First list of values
        y: Second list of values

    Returns:
        Spearman's rho correlation coefficient (-1 to 1)
    """
    x = np.array(x)
    y = np.array(y)
    n = len(x)

    if n < 2:
        return 0.0

    def rankdata(arr):
        sorted_idx = np.argsort(arr)
        ranks = np.empty_like(sorted_idx, dtype=float)
        ranks[sorted_idx] = np.arange(1, len(arr) + 1, dtype=float)
        return ranks

    rank_x = rankdata(x)
    rank_y = rankdata(y)

    d = rank_x - rank_y
    d_sq_sum = np.sum(d ** 2)

    rho = 1 - (6 * d_sq_sum) / (n * (n ** 2 - 1))
    return float(rho)


def pearson_correlation(x: list, y: list) -> float:
    """
    Calculate Pearson correlation coefficient.
    """
    x = np.array(x)
    y = np.array(y)

    x_centered = x - np.mean(x)
    y_centered = y - np.mean(y)

    numerator = np.sum(x_centered * y_centered)
    denominator = np.sqrt(np.sum(x_centered ** 2)) * np.sqrt(np.sum(y_centered ** 2))

    if denominator < 1e-10:
        return 0.0

    return float(numerator / denominator)


def main():
    print("=" * 90)
    print("GPU vs CPU COMPARISON - V2")
    print("=" * 90)

    # Check for result files
    gpu_file = OUTPUT_DIR / "gpu_results.json"
    cpu_file = OUTPUT_DIR / "cpu_results.json"

    if not gpu_file.exists():
        print(f"\nERROR: GPU results not found at {gpu_file}")
        print("Run: PYTHONPATH=. python scripts/gpu_raster_experiment_v2.py")
        return

    if not cpu_file.exists():
        print(f"\nERROR: CPU results not found at {cpu_file}")
        print("Run: PYTHONPATH=. python scripts/cpu_spyrrow_experiment_v2.py")
        return

    # Load results
    with open(gpu_file) as f:
        gpu_data = json.load(f)

    with open(cpu_file) as f:
        cpu_data = json.load(f)

    gpu_results = gpu_data.get("results", gpu_data)
    cpu_results = cpu_data.get("results", cpu_data)

    # Handle both list and dict formats
    if isinstance(gpu_results, dict):
        gpu_results = list(gpu_results.values())
    if isinstance(cpu_results, dict):
        cpu_results = list(cpu_results.values())

    # Index by combo_id
    gpu_by_id = {r["combo_id"]: r for r in gpu_results}
    cpu_by_id = {r["combo_id"]: r for r in cpu_results}

    # Print comparison table
    print(f"\n" + "-" * 100)
    print(f"{'ID':<4} {'Combo':<32} {'GPU%':>8} {'CPU%':>8} {'Delta':>8} {'GPU Pcs':>10} {'CPU Pcs':>10} {'Match':>6}")
    print("-" * 100)

    gpu_utils = []
    cpu_utils = []
    both_successful = []

    for combo_id in sorted(set(gpu_by_id.keys()) | set(cpu_by_id.keys())):
        gpu = gpu_by_id.get(combo_id, {})
        cpu = cpu_by_id.get(combo_id, {})

        combo = gpu.get("combo", cpu.get("combo", {}))
        combo_str = ", ".join(f"{s}:{n}" for s, n in sorted(combo.items()) if n > 0)

        gpu_util = gpu.get("utilization", 0)
        cpu_util = cpu.get("utilization", 0)
        delta = gpu_util - cpu_util

        gpu_placed = gpu.get("placed_pieces", 0)
        gpu_expected = gpu.get("expected_pieces", 0)
        cpu_placed = cpu.get("placed_pieces", 0)
        cpu_expected = cpu.get("expected_pieces", 0)

        gpu_pcs = f"{gpu_placed}/{gpu_expected}"
        cpu_pcs = f"{cpu_placed}/{cpu_expected}"

        # Check if both methods placed all pieces
        gpu_ok = gpu_placed == gpu_expected and gpu_expected > 0
        cpu_ok = cpu_placed == cpu_expected and cpu_expected > 0
        both_ok = gpu_ok and cpu_ok

        match = "YES" if both_ok else "NO"

        print(f"{combo_id:<4} {combo_str:<32} {gpu_util:>7.1f}% {cpu_util:>7.1f}% {delta:>+7.1f}% {gpu_pcs:>10} {cpu_pcs:>10} {match:>6}")

        gpu_utils.append(gpu_util)
        cpu_utils.append(cpu_util)

        if both_ok:
            both_successful.append({
                "combo_id": combo_id,
                "gpu_util": gpu_util,
                "cpu_util": cpu_util
            })

    print("-" * 100)

    # Correlation analysis
    print(f"\n" + "=" * 90)
    print("CORRELATION ANALYSIS")
    print("=" * 90)

    # All combinations
    rho_all = spearman_correlation(gpu_utils, cpu_utils)
    pearson_all = pearson_correlation(gpu_utils, cpu_utils)

    print(f"\nAll {len(gpu_utils)} combinations:")
    print(f"  Spearman Rank Correlation: rho = {rho_all:.4f}")
    print(f"  Pearson Correlation:       r   = {pearson_all:.4f}")

    # Only successful combinations
    if both_successful:
        gpu_success = [r["gpu_util"] for r in both_successful]
        cpu_success = [r["cpu_util"] for r in both_successful]

        rho_success = spearman_correlation(gpu_success, cpu_success)
        pearson_success = pearson_correlation(gpu_success, cpu_success)

        print(f"\n{len(both_successful)} fully-placed combinations only:")
        print(f"  Spearman Rank Correlation: rho = {rho_success:.4f}")
        print(f"  Pearson Correlation:       r   = {pearson_success:.4f}")

    # Interpretation
    print(f"\n" + "-" * 90)
    print("INTERPRETATION")
    print("-" * 90)

    if rho_all > 0.85:
        verdict = "STRONG"
        symbol = "OK"
        desc = "GPU rasterization IS VIABLE for screening"
    elif rho_all > 0.70:
        verdict = "MODERATE"
        symbol = "~"
        desc = "GPU screening USABLE with caution"
    elif rho_all > 0.50:
        verdict = "WEAK"
        symbol = "?"
        desc = "GPU method needs refinement"
    else:
        verdict = "POOR"
        symbol = "X"
        desc = "GPU method does NOT predict CPU rankings"

    print(f"\n  [{symbol}] {verdict} correlation (rho = {rho_all:.3f})")
    print(f"      {desc}")

    # Timing comparison
    print(f"\n" + "=" * 90)
    print("TIMING COMPARISON")
    print("=" * 90)

    gpu_total = sum(r.get("time", 0) for r in gpu_results)
    cpu_total = sum(r.get("time", 0) for r in cpu_results)

    print(f"\n  GPU total: {gpu_total:.2f}s ({gpu_total/len(gpu_results):.3f}s avg per combo)")
    print(f"  CPU total: {cpu_total:.1f}s ({cpu_total/len(cpu_results):.1f}s avg per combo)")

    if gpu_total > 0:
        speedup = cpu_total / gpu_total
        print(f"\n  Speedup:   {speedup:.1f}x faster with GPU rasterization")

    # Placement success rate
    print(f"\n" + "=" * 90)
    print("PLACEMENT SUCCESS")
    print("=" * 90)

    gpu_success_count = sum(1 for r in gpu_results if r.get("placed_pieces", 0) == r.get("expected_pieces", 0))
    cpu_success_count = sum(1 for r in cpu_results if r.get("placed_pieces", 0) == r.get("expected_pieces", 0) and not r.get("error"))

    print(f"\n  GPU: {gpu_success_count}/{len(gpu_results)} combinations fully placed")
    print(f"  CPU: {cpu_success_count}/{len(cpu_results)} combinations fully placed")
    print(f"  Both: {len(both_successful)}/{len(gpu_results)} combinations")

    # Save comparison results
    comparison = {
        "timestamp": datetime.now().isoformat(),
        "summary": {
            "spearman_rho_all": rho_all,
            "pearson_r_all": pearson_all,
            "spearman_rho_successful": rho_success if both_successful else None,
            "pearson_r_successful": pearson_success if both_successful else None,
            "verdict": verdict,
            "gpu_total_time_s": gpu_total,
            "cpu_total_time_s": cpu_total,
            "speedup_factor": cpu_total / gpu_total if gpu_total > 0 else 0,
            "gpu_success_rate": gpu_success_count / len(gpu_results),
            "cpu_success_rate": cpu_success_count / len(cpu_results),
            "both_success_rate": len(both_successful) / len(gpu_results)
        },
        "per_combo": [
            {
                "combo_id": combo_id,
                "combo": gpu_by_id.get(combo_id, cpu_by_id.get(combo_id, {})).get("combo", {}),
                "gpu_util": gpu_by_id.get(combo_id, {}).get("utilization", 0),
                "cpu_util": cpu_by_id.get(combo_id, {}).get("utilization", 0),
                "gpu_pieces": f"{gpu_by_id.get(combo_id, {}).get('placed_pieces', 0)}/{gpu_by_id.get(combo_id, {}).get('expected_pieces', 0)}",
                "cpu_pieces": f"{cpu_by_id.get(combo_id, {}).get('placed_pieces', 0)}/{cpu_by_id.get(combo_id, {}).get('expected_pieces', 0)}",
                "both_successful": combo_id in [r["combo_id"] for r in both_successful]
            }
            for combo_id in sorted(set(gpu_by_id.keys()) | set(cpu_by_id.keys()))
        ]
    }

    comparison_file = OUTPUT_DIR / "comparison.json"
    with open(comparison_file, "w") as f:
        json.dump(comparison, f, indent=2)

    print(f"\nComparison saved to: {comparison_file}")

    # Generate text report
    report_file = OUTPUT_DIR / "comparison_report.txt"
    with open(report_file, "w") as f:
        f.write("GPU vs CPU Nesting Comparison Report\n")
        f.write("=" * 50 + "\n\n")
        f.write(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n")

        f.write("Correlation Analysis\n")
        f.write("-" * 30 + "\n")
        f.write(f"Spearman rho (all): {rho_all:.4f}\n")
        f.write(f"Pearson r (all):    {pearson_all:.4f}\n")
        f.write(f"Verdict: {verdict} - {desc}\n\n")

        f.write("Timing\n")
        f.write("-" * 30 + "\n")
        f.write(f"GPU total: {gpu_total:.2f}s\n")
        f.write(f"CPU total: {cpu_total:.1f}s\n")
        f.write(f"Speedup:   {cpu_total/gpu_total:.1f}x\n\n")

        f.write("Success Rates\n")
        f.write("-" * 30 + "\n")
        f.write(f"GPU: {gpu_success_count}/{len(gpu_results)}\n")
        f.write(f"CPU: {cpu_success_count}/{len(cpu_results)}\n")

    print(f"Report saved to: {report_file}")


if __name__ == "__main__":
    main()
