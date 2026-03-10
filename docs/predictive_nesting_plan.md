# Predictive GPU Nesting — Surrogate Modeling Plan

## Goal
Reduce GPU nesting computation by sampling a subset of marker ratio combinations and using predictive modeling to estimate efficiency/length for the rest. Target: 98%+ accuracy with ~10-15% of markers actually nested.

## Problem
- 7 sizes → ~1700 candidate ratio combinations
- Each GPU nesting call: ~180-500ms
- Brute force: 5-15 minutes
- Target: <1-2 minutes with prediction

## Recommended Model: Gaussian Process Regression (GPR)
- `sklearn.gaussian_process.GaussianProcessRegressor`
- Gold standard for surrogate modeling of expensive simulations
- Uncertainty estimates → adaptive sampling (sample where model is least confident)
- Matérn kernel recommended for smooth but non-trivial relationships

### Alternative Models to Benchmark
| Model | Library | Notes |
|-------|---------|-------|
| RBF Interpolation | `scipy.interpolate.RBFInterpolator` | Fast, smooth, no uncertainty |
| Random Forest | `sklearn.ensemble.RandomForestRegressor` | Handles nonlinearity, needs more samples |
| Polynomial Response Surface | `sklearn.preprocessing.PolynomialFeatures` + `LinearRegression` | Fast, may miss complex interactions |

## Sampling Strategy
- **Stratified Latin Hypercube** across bundle counts
- Group markers by total bundles (1-6), sample proportionally
- `scipy.stats.qmc.LatinHypercube` for space-filling within each stratum
- Start with ~10-15% (~170-250 out of 1700)

## Features
- Input: ratio vector `[r1, r2, ..., r7]` (7D)
- Derived: total_bundles, max_ratio, size_spread
- Output: efficiency (%), length (yards)

## Validation Plan
1. Load brute-force GPU results (all ~1700 markers)
2. Sample X% → train, rest → test
3. Fit GPR / RBF / RF on train
4. Predict test set
5. Metrics: RMSE, R², max absolute error
6. Sweep sample sizes: 5%, 10%, 15%, 20%
7. Find minimum sample for 98% accuracy

## Integration Plan
1. Validation script first (use existing brute-force data)
2. If proven, wire into `gpu_nesting_runner.py`:
   - Phase 1: Sample markers via LHS
   - Phase 2: GPU nest sampled markers
   - Phase 3: Predict remaining markers
   - Phase 4: (Optional) Adaptive — nest high-uncertainty predictions
3. Feed predicted efficiencies into ILP solver as usual
