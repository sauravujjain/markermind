# AAMA/ASTM DXF+RUL Grading Parser Specification

## Overview

Create `src/nesting_engine/io/aama_parser.py` - a parser for AAMA/ASTM pattern grading files that generates all sizes from a base DXF + grading rules (.rul) file.

## File Format Analysis

### RUL File Structure

```
ASTM/D13Proposal 1 Version: D 6673-04
AUTHOR: BOKE TECHNOLOGY
PRODUCT: BOKE
VERSION: 2012
CREATION DATE: 18-8-2022
CREATION TIME: 14:3
UNITS: METRIC
GRADE RULE TABLE: 1016533  PROD 6 13APR20 L-2.5%W-6%
NUMBER OF SIZES: 10
SIZE LIST: 28 29 30 31 32 33 34 36 38 40
SAMPLE SIZE: 32
RULE: DELTA 1
      -10.395,      19.458      # Size 28
      -7.295,      14.835       # Size 29
      -4.000,      10.510       # Size 30
      -3.400,      4.325        # Size 31
      0.000,      -0.000        # Size 32 (SAMPLE - always zero)
      3.098,      -4.622        # Size 33
      6.495,      -8.945        # Size 34
      10.905,      -19.860      # Size 36
      17.297,      -29.087      # Size 38
      23.695,      -38.318      # Size 40
RULE: DELTA 2
      ...
END
```

**Key Points:**
- Header contains metadata until first `RULE: DELTA N`
- Each rule has N rows where N = NUMBER OF SIZES
- Row order matches SIZE LIST order
- Sample size row has (0, 0) deltas
- Rules are numbered 1 to M (this file has 674 rules)

### DXF File Structure

**Sections:**
- HEADER (minimal)
- BLOCKS (contains all pieces)
- ENTITIES (INSERTs referencing blocks + metadata text)

**Block Structure (one per piece):**
```
BLOCK
8
0
2
BK R-32              <- Block name (piece name + size)
70
64
10
0.000000
20
0.000000
0
TEXT                 <- Piece metadata
8
1
...
1
Piece Name: BK R
...
POLYLINE             <- Piece boundary (Layer 1)
8
1
66
 1
70
1
0
VERTEX
8
1
10
1842.755000          <- X coordinate
20
-3.875000            <- Y coordinate
0
VERTEX
...
SEQEND
0
POINT                <- Grade point marker (Layer 2)
8
2
10
1842.755000          <- Same X as a VERTEX
20
-3.875000            <- Same Y as a VERTEX
...
ENDBLK
```

**Layer Semantics (AAMA Standard):**
- Layer 0: Current/Default
- Layer 1: BOUNDARY_LINE - Piece boundary (POLYLINE/VERTEX)
- Layer 2: TURN_POINTS - Grade points (POINT entities marking gradable vertices)
- Layer 3: CURVE_POINTS - Additional curve points (may need interpolation)
- Layer 4: NOTCHES
- Layer 5: GRADE_REFERENCE_LINES
- Layer 6: MIRROR_LINE
- Layer 7: GRAIN_LINE
- Layer 8: INTERNAL_LINES
- Layer 14: SEW_LINES
- Layer 19: TEXT

**Critical Insight:**
- NOT all vertices are grade points (turn points)
- Grade points are marked by POINT entities on Layer 2
- POINT coordinates match corresponding VERTEX coordinates
- Example: First piece has 198 vertices but only 47 grade points
- Non-grade-point vertices MUST be interpolated between neighboring grade points

### Grading Logic

To generate a new size:
1. Find each grade point in the base piece
2. Look up which DELTA rule applies to that grade point
3. Apply the (dx, dy) for the target size to that vertex
4. Non-grade-point vertices may need interpolation or stay fixed

**Grade Point to Rule Mapping:**
- Grade points are numbered sequentially within each piece
- The rule number corresponds to the grade point's global index
- Need to track which rule applies to which point

## Data Structures

```python
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Tuple
from enum import Enum

@dataclass
class GradingRuleHeader:
    """Metadata from RUL file header."""
    author: str
    product: str
    version: str
    creation_date: str
    creation_time: str
    units: str  # "METRIC" or "ENGLISH"
    grade_rule_table: str
    num_sizes: int
    size_list: List[str]  # ["28", "29", ..., "40"]
    sample_size: str  # "32"
    sample_size_index: int  # Index in size_list (e.g., 4 for size 32)


@dataclass
class GradingRule:
    """A single DELTA rule with offsets for each size."""
    rule_id: int  # 1-based rule number
    deltas: List[Tuple[float, float]]  # [(dx, dy) for each size in size_list order]
    
    def get_delta(self, size_index: int) -> Tuple[float, float]:
        """Get (dx, dy) for a specific size index."""
        return self.deltas[size_index]


@dataclass
class GradingRules:
    """Complete grading rules from a RUL file."""
    header: GradingRuleHeader
    rules: Dict[int, GradingRule]  # rule_id -> GradingRule
    
    def get_delta_for_size(self, rule_id: int, target_size: str) -> Tuple[float, float]:
        """Get delta for a specific rule and target size."""
        size_index = self.header.size_list.index(target_size)
        return self.rules[rule_id].get_delta(size_index)


@dataclass
class GradePoint:
    """A vertex that has an associated grading rule."""
    vertex_index: int  # Index in the piece's vertex list
    x: float
    y: float
    rule_id: int  # Which DELTA rule applies


@dataclass 
class AAMAPiece:
    """A piece extracted from AAMA DXF with grade point information."""
    name: str  # e.g., "BK R"
    block_name: str  # e.g., "BK R-32"
    size: str  # e.g., "32"
    vertices: List[Tuple[float, float]]  # All boundary vertices
    grade_points: List[GradePoint]  # Subset of vertices that are grade points
    layer: str
    material: Optional[str] = None
    category: Optional[str] = None
    annotation: Optional[str] = None
    quantity: Optional[str] = None
    
    # Additional geometry from other layers
    grain_line: Optional[Tuple[Tuple[float, float], Tuple[float, float]]] = None
    sew_lines: List[List[Tuple[float, float]]] = field(default_factory=list)
    internal_points: List[Tuple[float, float]] = field(default_factory=list)


@dataclass
class GradedPiece:
    """A piece graded to a specific size."""
    name: str
    size: str
    vertices: List[Tuple[float, float]]
    source_piece: str  # Original piece name
    
    # Preserved from original
    grain_line: Optional[Tuple[Tuple[float, float], Tuple[float, float]]] = None
```

## Class Interfaces

### AAMARuleParser

```python
class AAMARuleParser:
    """Parser for AAMA/ASTM .rul grading rule files."""
    
    def __init__(self, rul_path: str):
        """
        Initialize parser with path to .rul file.
        
        Args:
            rul_path: Path to the .rul file
        """
        pass
    
    def parse(self) -> GradingRules:
        """
        Parse the RUL file and return grading rules.
        
        Returns:
            GradingRules object with header and all delta rules
            
        Raises:
            ValueError: If file format is invalid
            FileNotFoundError: If file doesn't exist
        """
        pass
    
    def _parse_header(self, lines: List[str]) -> Tuple[GradingRuleHeader, int]:
        """Parse header section, return header and line index where rules start."""
        pass
    
    def _parse_rules(self, lines: List[str], start_idx: int, num_sizes: int) -> Dict[int, GradingRule]:
        """Parse all DELTA rules starting from start_idx."""
        pass
```

### AAMADXFParser

```python
class AAMADXFParser:
    """
    Parser for AAMA/ASTM DXF pattern files with grade points.
    
    Extends standard DXF parsing to extract:
    - Piece boundaries from BLOCKS
    - Grade point markers (POINT entities on Layer 2)
    - Grain lines, sew lines, and other geometry
    """
    
    def __init__(self, dxf_path: str):
        """
        Initialize parser with path to DXF file.
        
        Args:
            dxf_path: Path to the .dxf file
        """
        pass
    
    def parse(self) -> List[AAMAPiece]:
        """
        Parse the DXF file and extract all pieces with grade points.
        
        Returns:
            List of AAMAPiece objects
        """
        pass
    
    def _extract_blocks(self) -> List[Dict]:
        """Extract all BLOCK definitions from DXF."""
        pass
    
    def _parse_block(self, block) -> Optional[AAMAPiece]:
        """Parse a single block into an AAMAPiece."""
        pass
    
    def _extract_grade_points(self, block) -> List[Tuple[float, float]]:
        """Extract POINT entities from Layer 2 within a block."""
        pass
    
    def _match_grade_points_to_vertices(
        self, 
        vertices: List[Tuple[float, float]], 
        grade_point_coords: List[Tuple[float, float]],
        tolerance: float = 0.01
    ) -> List[GradePoint]:
        """
        Match grade point coordinates to vertex indices.
        
        Grade points are POINT entities whose coordinates match
        a VERTEX coordinate within tolerance.
        """
        pass
    
    def _extract_piece_metadata(self, block) -> Dict[str, str]:
        """Extract TEXT entities with piece name, material, etc."""
        pass
```

### AAMAGrader

```python
class AAMAGrader:
    """
    Apply grading rules to generate sized patterns.
    
    Takes base pieces (sample size) and grading rules,
    produces pieces for any target size.
    """
    
    def __init__(self, pieces: List[AAMAPiece], rules: GradingRules):
        """
        Initialize grader with pieces and rules.
        
        Args:
            pieces: List of AAMAPiece from AAMADXFParser
            rules: GradingRules from AAMARuleParser
        """
        pass
    
    def grade(self, target_size: str) -> List[GradedPiece]:
        """
        Generate all pieces for a target size.
        
        Args:
            target_size: Size to generate (must be in rules.header.size_list)
            
        Returns:
            List of GradedPiece objects for the target size
        """
        pass
    
    def grade_piece(self, piece: AAMAPiece, target_size: str) -> GradedPiece:
        """
        Grade a single piece to target size.
        
        Args:
            piece: Source piece (sample size)
            target_size: Target size
            
        Returns:
            GradedPiece with adjusted vertices
        """
        pass
    
    def _apply_deltas(
        self,
        vertices: List[Tuple[float, float]],
        grade_points: List[GradePoint],
        target_size: str
    ) -> List[Tuple[float, float]]:
        """
        Apply grade point deltas to ALL vertices with interpolation.
        
        CRITICAL: This method MUST interpolate non-grade-point vertices.
        
        Algorithm:
        1. For grade point vertices: apply delta directly from RUL
        2. For non-grade-point vertices: linear interpolation between 
           neighboring grade points based on position along boundary
           
        Args:
            vertices: All boundary vertices
            grade_points: Grade points with their rule IDs
            target_size: Target size to grade to
            
        Returns:
            New vertex list with all vertices moved appropriately
        """
        pass
    
    def get_available_sizes(self) -> List[str]:
        """Return list of sizes that can be generated."""
        return self.rules.header.size_list
    
    def get_sample_size(self) -> str:
        """Return the sample/base size."""
        return self.rules.header.sample_size
```

### Convenience Functions

```python
def load_aama_pattern(
    dxf_path: str, 
    rul_path: str
) -> Tuple[List[AAMAPiece], GradingRules]:
    """
    Load an AAMA pattern file pair.
    
    Args:
        dxf_path: Path to .dxf file
        rul_path: Path to .rul file
        
    Returns:
        Tuple of (pieces, grading_rules)
    """
    pass


def grade_to_nesting_pieces(
    dxf_path: str,
    rul_path: str,
    target_sizes: List[str],
    rotations: List[float] = [0, 180],
    allow_flip: bool = False
) -> List[Piece]:
    """
    Load AAMA pattern and generate Piece objects for nesting.
    
    Args:
        dxf_path: Path to .dxf file
        rul_path: Path to .rul file  
        target_sizes: List of sizes to generate
        rotations: Allowed rotation angles
        allow_flip: Whether to allow flipping
        
    Returns:
        List of Piece objects ready for nesting engine
    """
    pass
```

## Implementation Notes

### Grade Point Rule Assignment

The trickiest part is mapping grade points to their rule IDs. Based on the file analysis:

- Grade points appear to be numbered globally across all pieces
- First piece (BK R) has 47 grade points → uses rules 1-47
- Second piece (BK PCKTING) has 37 grade points → uses rules 48-84
- And so on...

**Verification needed**: Parse the first few pieces and verify the rule assignment by checking that sample size deltas are (0, 0).

### Vertex Interpolation Strategy (CRITICAL)

**This is mandatory, not optional.** Non-grade-point vertices MUST move proportionally to maintain piece shape integrity.

In AAMA format:
- **Layer 2 (TURN_POINTS)**: Grade points with explicit delta rules from RUL file
- **Layer 3 (CURVE_POINTS)**: Curve points (may be few or none in some files)
- **Other boundary vertices**: Must be interpolated between neighboring grade points

**Algorithm: Distance-Based Linear Interpolation Along Boundary**

**Why distance-based (not index-based)?**
- Curve vertices are often unevenly spaced (tighter spacing on sharp curves)
- Index-based interpolation causes distortion on curves
- Distance-based is the industry standard (confirmed by Optitex, Gerber documentation)

```python
def calculate_cumulative_distances(vertices: List[Tuple[float, float]]) -> List[float]:
    """
    Calculate cumulative arc length along the boundary.
    Returns list where distances[i] = total distance from vertex 0 to vertex i.
    """
    distances = [0.0]
    for i in range(1, len(vertices)):
        x1, y1 = vertices[i - 1]
        x2, y2 = vertices[i]
        segment_length = math.sqrt((x2 - x1)**2 + (y2 - y1)**2)
        distances.append(distances[-1] + segment_length)
    return distances


def interpolate_vertex_delta(
    vertex_index: int,
    vertices: List[Tuple[float, float]],
    cumulative_distances: List[float],
    grade_point_indices: List[int],  # Sorted indices of grade points
    grade_point_deltas: Dict[int, Tuple[float, float]]  # index -> (dx, dy)
) -> Tuple[float, float]:
    """
    Calculate interpolated delta for a non-grade-point vertex using
    DISTANCE-BASED interpolation (not index-based).
    
    1. Find the previous grade point (P1) and next grade point (P2) 
       walking around the boundary
    2. Calculate proportional position t based on arc length distance
    3. Linearly interpolate: delta = (1-t) * delta_P1 + t * delta_P2
    """
    # Find bracketing grade points
    prev_gp_idx = None
    next_gp_idx = None
    
    for i, gp_idx in enumerate(grade_point_indices):
        if gp_idx > vertex_index:
            next_gp_idx = gp_idx
            prev_gp_idx = grade_point_indices[i - 1] if i > 0 else grade_point_indices[-1]
            break
    
    if next_gp_idx is None:
        # Vertex is after last grade point, wraps to first
        prev_gp_idx = grade_point_indices[-1]
        next_gp_idx = grade_point_indices[0]
    
    # Calculate position ratio using DISTANCE (not index)
    dist_to_vertex = cumulative_distances[vertex_index]
    dist_to_prev_gp = cumulative_distances[prev_gp_idx]
    dist_to_next_gp = cumulative_distances[next_gp_idx]
    
    if next_gp_idx > prev_gp_idx:
        # Normal case: no wrap-around
        t = (dist_to_vertex - dist_to_prev_gp) / (dist_to_next_gp - dist_to_prev_gp)
    else:
        # Wrapping case: goes past end of vertex list
        total_perimeter = cumulative_distances[-1]
        # Add distance for the closing segment (last vertex to first vertex)
        x1, y1 = vertices[-1]
        x2, y2 = vertices[0]
        closing_dist = math.sqrt((x2 - x1)**2 + (y2 - y1)**2)
        total_perimeter += closing_dist
        
        # Distance from prev_gp to next_gp going forward (wrapping)
        span = (total_perimeter - dist_to_prev_gp) + dist_to_next_gp
        
        # Distance from prev_gp to vertex
        if vertex_index >= prev_gp_idx:
            dist_from_prev = dist_to_vertex - dist_to_prev_gp
        else:
            dist_from_prev = (total_perimeter - dist_to_prev_gp) + dist_to_vertex
        
        t = dist_from_prev / span
    
    # Clamp t to [0, 1] for safety
    t = max(0.0, min(1.0, t))
    
    # Interpolate deltas
    dx1, dy1 = grade_point_deltas[prev_gp_idx]
    dx2, dy2 = grade_point_deltas[next_gp_idx]
    
    dx = (1 - t) * dx1 + t * dx2
    dy = (1 - t) * dy1 + t * dy2
    
    return (dx, dy)
```

**Why this works for both lines AND curves:**

1. **Straight lines**: Vertices are evenly spaced, so distance-based ≈ index-based. 
   Linear interpolation of deltas on a straight line produces another straight line. ✅

2. **Curves**: Vertices may be unevenly spaced (denser on sharp curves).
   Distance-based ensures each vertex moves proportionally to its actual position
   along the arc, preserving the curve shape. ✅

**Example - Straight Line:**
```
G1 ----V1----V2----V3---- G2  (evenly spaced at 10mm intervals)

Distances: 0, 10, 20, 30, 40mm
t values:  0, 0.25, 0.5, 0.75, 1.0

If G1 delta = (10, 0) and G2 delta = (20, 0):
V1 delta = 0.75*(10,0) + 0.25*(20,0) = (12.5, 0)
V2 delta = 0.5*(10,0) + 0.5*(20,0) = (15, 0)
V3 delta = 0.25*(10,0) + 0.75*(20,0) = (17.5, 0)

Result: Still a straight line, just stretched. ✅
```

**Example - Curve (armhole):**
```
G1 ...V1.V2.V3....... V4 ..... G2  (denser vertices on tight curve)

Distances: 0, 3, 5, 8, 25, 40mm (uneven spacing)
t values:  0, 0.075, 0.125, 0.2, 0.625, 1.0

With distance-based: vertices on tight curve get small t values (correct!)
With index-based:    vertices would get t=0.2, 0.4, 0.6, 0.8 (wrong!)

Distance-based preserves curve shape. ✅
```

**Edge case - Smooth continuity at grade points:**
When a curve passes through a grade point G2 between G1 and G3:
- Vertices approaching G2 from G1 side: interpolate toward G2's delta
- Vertices after G2 toward G3: interpolate from G2's delta
- At G2: delta is exactly G2's value
- Result: **C0 continuous** (no discontinuity in position)

**Implementation Note:**
Pre-compute cumulative distances once per piece, then use for all vertex interpolations.
This is O(n) preprocessing for O(1) lookup per vertex.

### Coordinate Tolerance

When matching POINT coordinates to VERTEX coordinates:
- Use tolerance of 0.01 units (0.01mm for METRIC)
- Coordinates should match exactly in well-formed files

### Unit Handling

- RUL file specifies units (METRIC = mm, ENGLISH = inches)
- DXF file may have $INSUNITS header
- Ensure consistency; convert to mm internally

## Testing

### Test Cases

1. **RUL parsing**: Parse the sample .rul file, verify:
   - 10 sizes extracted correctly
   - Sample size is "32" at index 4
   - 674 rules parsed
   - Rule 1 deltas match expected values

2. **DXF parsing**: Parse the sample .dxf file, verify:
   - 27 pieces extracted (26 pattern pieces + ENTITIES block to skip)
   - First piece "BK R" has 198 vertices, 47 grade points
   - Grade point coordinates match corresponding vertices

3. **Grading**: Generate size "28" and "40", verify:
   - Vertex count unchanged
   - Grade point vertices shifted by expected deltas
   - **Non-grade-point vertices interpolated correctly** (not fixed in place)
   - Piece remains valid polygon (no self-intersection)
   - Curves maintain smooth shape (no kinking)

4. **Interpolation Validation**: For a piece with known geometry:
   - Mark a non-grade-point vertex at 50% distance between two grade points
   - Verify its delta is exactly average of the two bracketing grade point deltas
   - Check boundary maintains smooth curves after grading

5. **Visual Curve Test**: Generate multiple sizes and overlay them:
   - Curves (armholes, necklines) should nest smoothly without crossing
   - No "kinking" or sharp angles where original was smooth
   - Grade points should align on smooth scaling paths

6. **Distance-based Validation**: 
   - For a curve with uneven vertex spacing, verify that vertices 
     physically closer to G1 get deltas closer to G1's delta
   - Compare with what index-based would produce - distance-based 
     should be more accurate on curved segments

7. **Integration**: Load both files, grade to size "M", convert to nesting Pieces

## Update __init__.py

After implementation, update `src/nesting_engine/io/__init__.py`:

```python
from nesting_engine.io.dxf_parser import (
    DXFParser,
    DXFParseResult,
    ParsedPiece,
    load_pieces_from_dxf,
)

from nesting_engine.io.aama_parser import (
    AAMARuleParser,
    AAMADXFParser,
    AAMAGrader,
    GradingRules,
    GradingRule,
    AAMAPiece,
    GradedPiece,
    load_aama_pattern,
    grade_to_nesting_pieces,
)

__all__ = [
    # Existing
    "DXFParser",
    "DXFParseResult", 
    "ParsedPiece",
    "load_pieces_from_dxf",
    # New AAMA
    "AAMARuleParser",
    "AAMADXFParser",
    "AAMAGrader",
    "GradingRules",
    "GradingRule",
    "AAMAPiece",
    "GradedPiece",
    "load_aama_pattern",
    "grade_to_nesting_pieces",
]
```

## Dependencies

Use existing dependencies from the project:
- `ezdxf` - DXF file parsing
- `shapely` - Polygon validation
- Standard library: `pathlib`, `re`, `dataclasses`, `typing`

## Example Usage

```python
from nesting_engine.io import load_aama_pattern, AAMAGrader

# Load pattern files
pieces, rules = load_aama_pattern("style.dxf", "style.rul")

print(f"Loaded {len(pieces)} pieces")
print(f"Available sizes: {rules.header.size_list}")
print(f"Sample size: {rules.header.sample_size}")

# Create grader
grader = AAMAGrader(pieces, rules)

# Generate specific sizes
for size in ["28", "32", "40"]:
    graded = grader.grade(size)
    print(f"Size {size}: {len(graded)} pieces")
    
# Convert to nesting pieces
from nesting_engine.io import grade_to_nesting_pieces

nesting_pieces = grade_to_nesting_pieces(
    "style.dxf", 
    "style.rul",
    target_sizes=["S", "M", "L"],
    rotations=[0, 180]
)
```
