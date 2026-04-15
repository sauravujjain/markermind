# Pattern Parser Index

Reference for all CAD format parsers in MarkerMind. When encountering a new file format variant, find the closest existing parser below and extend from there.

---

## Quick Lookup

| Format | Parser File | Entry Function | Inputs | Status |
|--------|------------|----------------|--------|--------|
| AAMA/ASTM DXF+RUL (Boke) | `nesting_engine/io/aama_parser.py` | `load_aama_pattern()` | DXF + RUL | Production |
| OptiTex AAMA DXF+RUL | `nesting_engine/io/optitex_aama_parser.py` | `load_aama_pattern()` | DXF + RUL | Production |
| Block-based DXF | `nesting_engine/io/dxf_block_parser.py` | `parse_block_dxf()` | DXF only | Production |
| Text-label DXF (Gerber-style) | `nesting_engine/io/dxf_text_parser.py` | `DXFParser.parse()` | DXF only | Production |
| VT DXF (Graded Nest) | `nesting_engine/io/vt_dxf_parser.py` | `parse_vt_dxf()` | DXF only | Production |
| Orchestrator (tries all) | `nesting_engine/io/dxf_parser.py` | `load_dxf_pieces_by_size()` | DXF only | Production |
| Gerber native | `backend/models/pattern.py` | N/A | TBD | Enum only |
| Lectra | `backend/models/pattern.py` | N/A | TBD | Enum only |

---

## Format 1: AAMA/ASTM (DXF + RUL)

**File:** `nesting_engine/io/aama_parser.py`
**FileType enum:** `aama`
**Frontend workflow:** User uploads DXF + RUL files, selects "AAMA (DXF + RUL)"

### What it is

Industry standard where a DXF holds base-size piece geometry with grade points, and a companion RUL file holds per-size delta offsets. Together they generate any size from one base pattern.

### DXF structure

- Pieces stored as named BLOCKs: `PIECE_NAME-SIZE` (e.g., `BK R-32`)
- Layer 1: Piece boundary (POLYLINE)
- Layer 2: Grade points (POINT entities with rule IDs)
- Layer 4: Sew line
- Layer 6: Mirror/fold line
- Layer 7: Grain line
- Layer 8: Annotation text (material, quantity, L/R info)

### RUL structure

```
AUTHOR: ...
UNITS: METRIC           # or ENGLISH
NUMBER OF SIZES: 13
SIZE LIST: 28 29 30 31 32 33 34 35 36 37 38 39 40
SAMPLE SIZE: 32

RULE: DELTA 1
0.0, 0.0                # one (dx, dy) pair per size
1.5, -2.3
...
```

### Key classes

| Class | Purpose |
|-------|---------|
| `AAMARuleParser` | Parses RUL file into `GradingRules` (header + delta rules) |
| `AAMADXFParser` | Parses DXF blocks into `AAMAPiece` list (base geometry + grade points) |
| `AAMAGrader` | Applies deltas to grade a piece to any target size |
| `AAMAPiece` | Piece with vertices, grade points, material, annotation, L/R type |
| `PieceQuantity` | Parsed from annotation: total, left_qty, right_qty, material |
| `LRType` | Enum: `NONE`, `SEPARATE_LEFT`, `SEPARATE_RIGHT`, `FLIP_FOR_LR` |

### Annotation parsing

The parser handles multiple annotation formats for material and quantity:

```
"SHELL*2"           -> material=SHELL, total=2, no L/R
"IL(L*1-R*1)"       -> material=IL, total=2, left=1, right=1
"SHELL(L*2-R*2)"    -> material=SHELL, total=4, left=2, right=2
"FINISH"            -> material=FINISH, total=1
```

### Entry points

```python
# Load raw pieces + grading rules
pieces, rules = load_aama_pattern(dxf_path, rul_path)

# Grade to specific sizes and get nesting-ready Piece objects
nesting_pieces = grade_to_nesting_pieces(dxf_path, rul_path, target_sizes=["S","M","L"])

# Grade only a specific material
nesting_pieces = grade_material_to_nesting_pieces(dxf_path, rul_path, material="SHELL", target_sizes=["S","M","L"])

# Inspect
grader = AAMAGrader(pieces, rules)
print(grader.get_available_sizes())   # ["28","29",..."40"]
print(grader.get_sample_size())       # "32"
```

### What it outputs

- `available_sizes`: From RUL header size list
- `available_materials`: Parsed from piece annotations (SHELL, IL, FINISH, SO1, etc.)
- `perimeter_by_size`: `{material: {size: total_perimeter_cm}}` for cost calculation
- Full L/R handling: separate left/right pieces, or flip-for-pair

### Limitations

- Requires both DXF and RUL files -- if RUL is missing, falls back to DXF-only
- Grade points must be on Layer 2 as POINT entities
- Annotation format is specific to AAMA convention

---

## Format 1b: OptiTex AAMA (DXF + RUL)

**File:** `nesting_engine/io/optitex_aama_parser.py`
**FileType enum:** `optitex_aama`
**Frontend workflow:** User uploads DXF + RUL files, selects "OptiTex"

### What it is

Self-contained fork of the AAMA parser for OptiTex-exported pattern files. Same AAMA/ANSI standard but with format differences in how data is written.

### Differences from Boke AAMA (Format 1)

| Feature | Boke/ASTM (Format 1) | OptiTex (Format 1b) |
|---------|----------------------|---------------------|
| RUL header | `ASTM/D13Proposal 1 Version: D 6673-04` | `ANSI/AAMA VERSION: 1.0.0` (blank first line) |
| RUL delta layout | One `dx, dy` pair per line | Multiple `dx, dy` pairs packed per line |
| DXF block names | Piece names (`CTR HD-M`) | Numeric (`1`, `2`, `3`) |
| Piece name source | Block name split on `-` | TEXT metadata `Piece_Name:` field |

### RUL delta format

OptiTex packs 2 pairs per line for 6 sizes (3 data lines × 2 pairs = 6):
```
RULE: DELTA 42
       0.500006,      0.000404       0.250003,      0.000193
       0.000000,      0.000000      -0.250010,     -0.000560
      -0.625012,     -0.001611      -1.000014,     -0.000776
```

vs Boke (one pair per line, 5 sizes = 5 data lines):
```
RULE: DELTA 42
      0.500,      0.000
      0.250,      0.000
      0.000,      0.000
     -0.250,     -0.001
     -0.625,     -0.002
```

### Key classes

Same as Format 1 — all classes are self-contained copies in the parser file.

### Entry points

Same API as Format 1:
```python
from nesting_engine.io.optitex_aama_parser import load_aama_pattern, AAMAGrader

pieces, rules = load_aama_pattern(dxf_path, rul_path)
nesting_pieces = grade_material_to_nesting_pieces(dxf_path, rul_path, material="L10", target_sizes=["XXS","XS","S","M","L","XL"])
```

### Tested with

- Armada ARFW2410011 (OptiTex export, 85 pieces, 12 materials, 6 sizes, 3671 grading rules)

---

## Format 2: Block-based DXF (Production/Pre-sized)

**File:** `nesting_engine/io/dxf_block_parser.py` -> `parse_block_dxf()`
**FileType enum:** `dxf_only`
**Frontend workflow:** User uploads DXF, selects "DXF Only", optionally provides comma-separated size names

### What it is

Production DXF files where all sizes are already present as separate blocks. No grading needed -- each block IS the final piece geometry for a specific size. Common output from Gerber AccuMark, OptiTex, and similar CAD systems.

### DXF structure

- Pieces stored as named BLOCKs with naming convention: `PIECE_NAMEXqty-index`
  - Example: `BACK YOKEX1-0`, `LO BACKX2-35`
  - `qty` = number of pieces per bundle
  - `index` = sequential block index across all sizes
- Layer 1 POLYLINE inside each block = piece boundary
- Blocks are grouped into sizes by index stride (index // group_size = size_idx)

### Size detection

The parser determines sizes by:
1. Finding the stride between same-piece instances across size groups
2. Dividing max_index by stride to get num_sizes
3. Assigning labels: user-provided names or auto-generated `SIZE_1`, `SIZE_2`, ...

### Auto-ordering detection

After size labels are assigned, the parser checks whether the DXF has sizes in increasing or decreasing order:
1. Picks a piece that appears in both the first and last size group
2. Computes its boundary area using the shoelace formula
3. If area[first] > area[last] (larger pieces at lower indices), reverses the label mapping

This means users always provide size names smallest-to-largest, and the parser figures out the DXF's direction.

### Entry points

```python
# High-level: tries standard parser first, falls back to block-based
pieces, piece_config, sizes = load_dxf_pieces_by_size(
    dxf_path,
    size_names=["S", "M", "L", "XL", "2X"],  # optional
)

# Low-level: block-based parser directly
pieces, sizes = _parse_block_dxf(
    dxf_path,
    size_names=["S", "M", "L"],
    rotations=[0, 180],
    allow_flip=True,
)
```

### What it outputs

- `available_sizes`: User-provided or auto-generated `SIZE_N` labels
- `available_materials`: Always `["MAIN"]` (single fabric, user maps later)
- `piece_config`: `{piece_name: {demand: 1, flipped: False}}`
- No L/R detection -- each DXF polyline is treated as a unique instance

### Unit handling

- Reads `$INSUNITS` from DXF header
- Heuristic: if INSUNITS is unset and coordinates < 200, assumes inches
- Converts everything to mm internally

### Limitations

- No L/R auto-detection
- No material detection (everything is "MAIN")
- Size ordering depends on area heuristic if DXF doesn't follow smallest-first convention
- No grading -- every size must already exist as separate blocks in the DXF

---

## Format 3: Text-label DXF (Gerber-style nested markers)

**File:** `nesting_engine/io/dxf_text_parser.py` -> `DXFParser`
**FileType enum:** `dxf_only` (same as block-based; the code tries this first)
**Frontend workflow:** Same as block-based DXF Only

### What it is

Nested marker DXF files where pieces are stored as closed POLYLINE/LWPOLYLINE entities, and text labels (TEXT/MTEXT entities) inside each piece identify the piece name, size, and pattern ID.

### DXF structure

- Pieces as closed polylines on various layers (e.g., `T001L001`)
- TEXT entities positioned inside piece boundaries
- Often includes a container rectangle (largest polyline) representing the marker boundary
- May include marker info text: `W=70.000IN L=17.9997 YD U=71.623%`

### Text matching

For each closed polyline, the parser uses Shapely point-in-polygon to find texts inside:

| Text pattern | Interpretation |
|-------------|---------------|
| Single letter A-Z | Pattern ID |
| Known size keyword (S, M, L, XL, 10, 12, etc.) | Size label |
| String >5 chars with `-` | Piece name (e.g., `24-0391-P2-BKX1`) |

### Entry points

```python
parser = DXFParser("marker.dxf")
result = parser.parse()  # -> DXFParseResult

# Convert to nesting pieces
pieces = parser.to_nesting_pieces(result, default_rotations=[0, 180])

# Or use convenience function
pieces, result = load_pieces_from_dxf("marker.dxf")
```

### Limitations

- Size detection relies on hardcoded `SIZE_KEYWORDS` list -- uncommon size codes may not be recognized
- Piece name detection heuristic (>5 chars with dash) can miss non-standard names
- Container detection may fail if no polyline is significantly larger than others
- Not suitable for production patterns where pieces aren't nested into a marker

---

## Format 4: VT DXF (Optitex Graded Nest)

**File:** `nesting_engine/io/vt_dxf_parser.py` -> `parse_vt_dxf()`
**FileType enum:** `vt_dxf`
**Frontend workflow:** User uploads DXF, selects "VT DXF (Graded)" -- no RUL or size names needed

### What it is

Optitex "Graded Nest" export where each material gets its own DXF file, and all sizes are pre-graded as separate blocks. Every block has TEXT annotations identifying piece name, size, quantity, and material. No companion RUL file needed.

### DXF structure

- One DXF per material (e.g., `25528-101.dxf`, `25528-201.dxf`)
- Blocks come in pairs: primary (odd) + shadow (even) -- shadow blocks are skipped
- **Primary blocks** have 8 TEXT annotations: `Piece Name:`, `Size:`, `Quantity:`, `Material:`, `Annotation:`, `Style Name:`, `Sample Size`, `Quality:`
- **Shadow blocks** have only 2 TEXT annotations: `Piece Name:`, `Size:` (with `-1` suffix) -- these are filtered out by checking for missing `Quantity:` annotation
- Layer 1 POLYLINE: piece boundary vertices
- Layer 7 LINE: grain direction

### Piece name format

`PieceNum_SizeLabel` (e.g., `4_M` -> piece `4`, size `M`). Parser uses `rsplit("_", 1)` to handle piece names that might contain underscores.

### Quantity handling

- `Quantity: 1` -- single piece per bundle
- `Quantity: 2` -- piece needs a mirrored copy (L/R pair). The DXF only contains one shape; the nesting engine creates the mirror.

### Unit detection

Coordinates are in centimetres. Parser detects by sampling Layer 1 POLYLINE vertices:
- Max coordinate < 1000 -> cm (x10 for mm)
- Max coordinate >= 2000 -> mm (x1)

### Size ordering

Sizes are sorted into canonical garment order: XXS, XS, S, M, L, XL, 2XL, 3XL, 4XL, 5XL.

### Entry points

```python
from nesting_engine.io.vt_dxf_parser import parse_vt_dxf

pieces, sizes, piece_quantities = parse_vt_dxf("25528-101.dxf")
# pieces: 56 Piece objects (8 shapes x 7 sizes)
# sizes: ['XS', 'S', 'M', 'L', 'XL', '2XL', '3XL']
# piece_quantities: {'1': 1, '2': 1, '3': 1, '4': 2, '5': 1, '6': 2, '7': 2, '8': 2}
```

### What it outputs

- `available_sizes`: Auto-detected and sorted (e.g., XS..3XL)
- `available_materials`: Single material per file, extracted from filename
- `piece_quantities`: `{piece_name: qty}` -- used by nesting runners for L/R pair handling

### Limitations

- One material per DXF file (by design)
- Shadow block filtering assumes "Quantity:" is only in primary blocks
- Piece name parsing assumes `_` separates piece number from size label

---

## Format routing

### Backend decision tree

```
pattern.file_type
├── "aama"
│   ├── Uses: aama_parser.load_aama_pattern(dxf, rul)
│   ├── Grading: AAMAGrader generates sized pieces
│   └── Materials: Parsed from annotations
│
├── "dxf_only"
│   ├── Step 1: Try DXFParser (text-label based)
│   ├── Step 2: If 0 pieces found, try _parse_block_dxf()
│   ├── Size names: From user input or auto-generated SIZE_N
│   └── Materials: Always "MAIN"
│
├── "vt_dxf"
│   ├── Uses: vt_dxf_parser.parse_vt_dxf(dxf)
│   ├── Sizes: Auto-detected from piece names (PieceNum_SizeLabel)
│   ├── Materials: Extracted from filename or annotations
│   └── Quantities: From Quantity annotation (qty=2 → L/R pair)
│
├── "gerber"  (NOT IMPLEMENTED)
│   └── Enum exists in FileType, no parser
│
└── "lectra"  (NOT IMPLEMENTED)
    └── Enum exists in FileType, no parser
```

### Where routing happens

| Layer | File | Logic |
|-------|------|-------|
| Frontend | `api.ts` -> `uploadPattern()` | Sends `file_type` and optional `size_names` as FormData |
| Upload route | `backend/api/routes/patterns.py` | Stores `file_type` on pattern, passes `size_names` to parse |
| Parse dispatch | `backend/services/pattern_service.py` -> `parse_pattern()` | Branches on `pattern.file_type` |
| Nesting dispatch | `gpu_nesting_runner.py` / `spyrrow_nesting_runner.py` | Checks if `rul_file_path` exists to pick parser |

---

## Adding a new format

When a new CAD format is encountered:

### 1. Identify the closest existing parser

- Has grading (base size + rules)? Start from `aama_parser.py`
- Pre-sized pieces in blocks? Start from `_parse_block_dxf()`
- Text labels inside polygons? Start from `DXFParser`
- Entirely new structure? Create new file in `nesting_engine/io/`

### 2. Create the parser

```
nesting_engine/io/
├── aama_parser.py       # AAMA/ASTM DXF+RUL grading
├── dxf_text_parser.py   # Text-label DXF (Gerber-style nested markers)
├── dxf_block_parser.py  # Block-based production DXF (pre-sized)
├── dxf_parser.py        # Orchestrator: tries text-label then block-based
├── <new_format>.py      # New parser
└── __init__.py
```

Every parser must output:
- `List[Piece]` -- nesting-ready pieces with vertices in mm
- `List[str]` -- available sizes
- `List[str]` -- available materials
- Per-piece: `PieceIdentifier` with `piece_name` and `size`

### 3. Add FileType enum

```python
# backend/backend/models/pattern.py
class FileType(str, enum.Enum):
    AAMA = "aama"
    GERBER = "gerber"
    LECTRA = "lectra"
    DXF_ONLY = "dxf_only"
    NEW_FORMAT = "new_format"  # <-- add here
```

Then create a DB migration: `alembic revision --autogenerate -m "add_new_format_filetype"`

### 4. Wire up the backend

```python
# backend/backend/services/pattern_service.py -> parse_pattern()
if pattern.file_type == "new_format":
    return self._parse_new_format(db, pattern, dxf_path)
```

### 5. Wire up the frontend

- Add option to file type selector in `orders/[id]/page.tsx` and `patterns/page.tsx`
- Show/hide format-specific inputs (e.g., size names for DXF-only, RUL for AAMA)
- Pass any extra params through `api.uploadPattern()`

### 6. Wire up nesting runners

Both `gpu_nesting_runner.py` and `spyrrow_nesting_runner.py` need to know how to load pieces for the new format. Currently they branch on whether `rul_file_path` exists -- a new format may need an explicit `file_type` check.

### 7. Update this document

Add the new format to the Quick Lookup table and give it a full section.

---

## Potential future formats

| Format | Vendor | Likely structure | Starting point |
|--------|--------|-----------------|----------------|
| Gerber AccuMark `.am` | Gerber Technology | Proprietary binary | May need vendor SDK or export-to-DXF |
| Lectra Modaris `.mdl` | Lectra | Proprietary | Export to DXF/AAMA first |
| OptiTex `.pds` | EFI OptiTex | Proprietary binary | Export to DXF |
| Assyst `.zst` | Assyst/Bullmer | Proprietary | Export to DXF |
| ASTM D6673 DXF | ASTM standard | Very similar to AAMA | Extend `aama_parser.py` |
| ISO 13584 (STEP) | ISO standard | STEP/IFC geometry | New parser needed |
| SVG/PDF patterns | Various | Vector graphics | New parser with different approach |
| Gerber DXF (nested marker) | Gerber | Text-label polylines | Already handled by `DXFParser` |

For most proprietary formats, the practical path is: **export from vendor CAD to DXF/AAMA**, then use existing parsers. Only build native parsers when DXF export loses critical metadata.
