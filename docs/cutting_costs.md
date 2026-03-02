# Cutting Costs - Cost Calculation Documentation

This document describes the cost calculation methodology for evaluating cutplan efficiency in garment manufacturing.

## Cost Centers Overview

| Cost Center | Metric | Unit Cost | Unit |
|-------------|--------|-----------|------|
| **Fabric Cost** | Total yards of fabric | $3.00 | per yard |
| **Spreading Cost** | Metric 1: (Marker Length × Plies) | $0.00122 | per yard |
| | Metric 2: Total number of plies | $0.013 | per ply |
| **Cutting Cost** | Perimeter (inches) per marker | $0.000424 | per inch (per cut) |
| **Preparation Cost** | Papers + marker print | $0.03 | per marker |

---

## Detailed Calculations

### 1. Fabric Cost

**Formula:**
```
Fabric_Cost = Total_Yards × $3.00
```

**Where:**
- `Total_Yards` = Sum of (Marker_Length × Total_Plies) for each marker in the cutplan
- In our CSV output, `Length_Yards` already represents `Marker_Length × Total_Plies`

**Example:**
```
Marker "1-1-3-1-0-0-0" with 65 plies, marker length ~8.7 yards
Length_Yards = 8.7 × 65 = 565.36 yards
Fabric_Cost = 565.36 × $3.00 = $1,696.08
```

---

### 2. Spreading Cost

Spreading cost accounts for the labor and equipment cost of laying fabric layers on the cutting table.

**Formula:**
```
Spreading_Cost = (Length_Yards × $0.00122) + (Total_Plies × $0.013)
```

**Components:**
- **Metric 1** ($0.00122/yard): Cost proportional to total fabric area spread
  - `Length_Yards` = Marker_Length × Plies (already computed)
- **Metric 2** ($0.013/ply): Fixed cost per layer regardless of marker length

**Example:**
```
Marker with Length_Yards = 565.36, Total_Plies = 65
Spreading_M1 = 565.36 × $0.00122 = $0.69
Spreading_M2 = 65 × $0.013 = $0.85
Total_Spreading = $0.69 + $0.85 = $1.54
```

---

### 3. Cutting Cost

Cutting cost is based on the total perimeter of all pieces in the marker, charged per cut operation.

**Formula:**
```
Cutting_Cost = Marker_Perimeter_Inches × Number_of_Cuts × $0.000424
```

**Where:**
- `Marker_Perimeter_Inches` = Sum of perimeters of ALL pieces in the marker
- `Number_of_Cuts` = ceil(Total_Plies / Max_Ply_Height)
- `Max_Ply_Height` = 120 plies (maximum layers that can be cut at once)

**Calculating Marker Perimeter:**

For a marker with ratio "2-1-3-0-0-0-0" (2×Size46 + 1×Size48 + 3×Size50):
```
Marker_Perimeter = (2 × Perimeter_Bundle_46) + (1 × Perimeter_Bundle_48) + (3 × Perimeter_Bundle_50)
```

Where `Perimeter_Bundle_XX` = Sum of perimeters of all pieces in one garment of size XX.

**Perimeter per Bundle (Style 23583, Material SO1):**

| Size | Perimeter (inches) |
|------|-------------------|
| 46 | 988.4" |
| 48 | 989.1" |
| 50 | 988.2" |
| 52 | 1,004.4" |
| 54 | 1,024.6" |
| 56 | 1,031.4" |
| 58 | 1,055.6" |

**Example:**
```
Marker "1-1-3-1-0-0-0" (6 bundles):
  Perimeter = 1×988.4 + 1×989.1 + 3×988.2 + 1×1004.4 = 5,946.5"

With 65 plies:
  Number_of_Cuts = ceil(65 / 120) = 1

Cutting_Cost = 5,946.5 × 1 × $0.000424 = $2.52
```

**Example with multiple cuts:**
```
Marker with 157 plies:
  Number_of_Cuts = ceil(157 / 120) = 2 (one cut of 120 plies, one of 37 plies)

If Perimeter = 6,102":
  Cutting_Cost = 6,102 × 2 × $0.000424 = $5.17
```

---

### 4. Preparation Cost

Fixed cost per unique marker for printing and paper.

**Formula:**
```
Prep_Cost = Number_of_Unique_Markers × $0.03
```

**Example:**
```
Cutplan with 9 unique markers:
Prep_Cost = 9 × $0.03 = $0.27
```

---

## Multicolor Stacking

In multicolor cutplans, plies of different fabric colors are stacked together on the cutting table:

- **Example:** Marker with plies: 8320=34, 8535=13, 8820=10, 9990=8
- **Total Plies:** 34 + 13 + 10 + 8 = 65 plies
- **Number of Cuts:** ceil(65 / 120) = 1 cut

All colors are cut simultaneously, reducing cutting costs compared to cutting each color separately.

---

## Total Cost Formula

```
Total_Cost = Fabric_Cost + Spreading_Cost + Cutting_Cost + Prep_Cost

Where:
  Fabric_Cost   = Σ (Length_Yards[m] × $3.00)
  Spreading_Cost = Σ (Length_Yards[m] × $0.00122 + Plies[m] × $0.013)
  Cutting_Cost  = Σ (Perimeter[m] × Cuts[m] × $0.000424)
  Prep_Cost     = Unique_Markers × $0.03
```

---

## Cost Distribution Analysis

Based on actual cutplan analysis (4-color order, ~3,900 garments):

| Cost Component | Amount | % of Total |
|----------------|--------|------------|
| Fabric | $17,617.74 | 99.8% |
| Spreading | $17.03 | 0.1% |
| Cutting | $26.40 | 0.1% |
| Prep | $0.27 | 0.0% |
| **TOTAL** | **$17,661.44** | 100% |

**Key Insight:** Fabric cost dominates at 99.8% of total cost. Optimizing for minimum yardage is the primary cost driver.

---

## Reference Implementation

Script: `scripts/cutplan_cost_analysis_v2.py`

**Usage:**
```bash
python scripts/cutplan_cost_analysis_v2.py
```

**Output:**
- Console report with detailed breakdown per marker
- CSV file: `experiment_results/multicolor_solver/cutplan_cost_analysis.csv`

---

## Comparison Results (Jan 2026)

| Metric | Joint Multicolor | Two-Stage (96%) | Difference |
|--------|------------------|-----------------|------------|
| Unique Markers | 9 | 12 | +3 |
| Total Cuts | 12 | 15 | +3 |
| Total Plies | 759 | 929 | +170 |
| Total Yards | 5,872.58 | 5,894.90 | +22.32 |
| **Total Cost** | **$17,661.44** | **$17,729.14** | **+$67.69** |

**Winner:** Joint Multicolor (saves $67.69)

---

## Input Data Requirements

1. **Cutplan CSV** with columns:
   - Ratio (e.g., "1-1-3-1-0-0-0")
   - Size counts (46, 48, 50, 52, 54, 56, 58)
   - Bundles, Efficiency
   - Plies per color
   - Total_Plies, Length_Yards

2. **Piece geometry** (from DXF/RUL files):
   - Used to calculate perimeter per bundle per size
   - Loaded via `nesting_engine.io.aama_parser`

---

## Notes

- All perimeters are in **inches**
- All lengths/yards are in **yards**
- Max ply height of 120 is industry standard for most fabrics
- Costs are estimates and may vary by factory/region
