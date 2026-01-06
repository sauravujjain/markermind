"""
Geometry primitives for the nesting engine.

This module provides:
- Point: 2D coordinate with operations
- BoundingBox: Axis-aligned bounding box
- Polygon: 2D polygon with transformations

All coordinates are in millimeters (internal unit).

Coordinate system:
- Origin (0, 0) at bottom-left
- +X points right
- +Y points up
- Polygon vertices are counter-clockwise (CCW) for exterior
- Holes (if any) are clockwise (CW)

Example:
    >>> from nesting_engine.core.geometry import Point, Polygon
    
    # Create a simple rectangle
    >>> vertices = [(0, 0), (100, 0), (100, 50), (0, 50), (0, 0)]
    >>> poly = Polygon(vertices)
    >>> poly.area
    5000.0
    >>> poly.centroid
    Point(50.0, 25.0)
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import List, Tuple, Optional, Iterator, Union

# Type aliases for clarity
Coordinate = Tuple[float, float]
VertexList = List[Coordinate]


@dataclass(frozen=True)
class Point:
    """
    Immutable 2D point.
    
    Attributes:
        x: X coordinate (mm)
        y: Y coordinate (mm)
    """
    x: float
    y: float
    
    def __add__(self, other: Union["Point", Coordinate]) -> "Point":
        if isinstance(other, Point):
            return Point(self.x + other.x, self.y + other.y)
        return Point(self.x + other[0], self.y + other[1])
    
    def __sub__(self, other: Union["Point", Coordinate]) -> "Point":
        if isinstance(other, Point):
            return Point(self.x - other.x, self.y - other.y)
        return Point(self.x - other[0], self.y - other[1])
    
    def __mul__(self, scalar: float) -> "Point":
        return Point(self.x * scalar, self.y * scalar)
    
    def __rmul__(self, scalar: float) -> "Point":
        return self.__mul__(scalar)
    
    def __neg__(self) -> "Point":
        return Point(-self.x, -self.y)
    
    def as_tuple(self) -> Coordinate:
        """Return as (x, y) tuple."""
        return (self.x, self.y)
    
    def distance_to(self, other: Union["Point", Coordinate]) -> float:
        """Calculate Euclidean distance to another point."""
        if isinstance(other, Point):
            dx, dy = self.x - other.x, self.y - other.y
        else:
            dx, dy = self.x - other[0], self.y - other[1]
        return math.sqrt(dx * dx + dy * dy)
    
    def rotate(self, angle_degrees: float, center: Optional["Point"] = None) -> "Point":
        """
        Rotate point around a center point.
        
        Args:
            angle_degrees: Rotation angle in degrees (CCW positive)
            center: Center of rotation (default: origin)
            
        Returns:
            New rotated Point
        """
        if center is None:
            center = Point(0.0, 0.0)
        
        # Translate to origin
        dx = self.x - center.x
        dy = self.y - center.y
        
        # Rotate
        angle_rad = math.radians(angle_degrees)
        cos_a = math.cos(angle_rad)
        sin_a = math.sin(angle_rad)
        
        new_x = dx * cos_a - dy * sin_a
        new_y = dx * sin_a + dy * cos_a
        
        # Translate back
        return Point(new_x + center.x, new_y + center.y)
    
    @classmethod
    def from_tuple(cls, coord: Coordinate) -> "Point":
        """Create Point from (x, y) tuple."""
        return cls(coord[0], coord[1])


@dataclass(frozen=True)
class BoundingBox:
    """
    Axis-aligned bounding box (AABB).
    
    Attributes:
        min_x: Minimum X coordinate
        min_y: Minimum Y coordinate
        max_x: Maximum X coordinate
        max_y: Maximum Y coordinate
    """
    min_x: float
    min_y: float
    max_x: float
    max_y: float
    
    @property
    def width(self) -> float:
        """Width of the bounding box (X extent)."""
        return self.max_x - self.min_x
    
    @property
    def height(self) -> float:
        """Height of the bounding box (Y extent)."""
        return self.max_y - self.min_y
    
    @property
    def area(self) -> float:
        """Area of the bounding box."""
        return self.width * self.height
    
    @property
    def center(self) -> Point:
        """Center point of the bounding box."""
        return Point(
            (self.min_x + self.max_x) / 2,
            (self.min_y + self.max_y) / 2
        )
    
    @property
    def min_point(self) -> Point:
        """Bottom-left corner."""
        return Point(self.min_x, self.min_y)
    
    @property
    def max_point(self) -> Point:
        """Top-right corner."""
        return Point(self.max_x, self.max_y)
    
    def contains_point(self, point: Union[Point, Coordinate]) -> bool:
        """Check if a point is inside the bounding box."""
        if isinstance(point, Point):
            x, y = point.x, point.y
        else:
            x, y = point
        return self.min_x <= x <= self.max_x and self.min_y <= y <= self.max_y
    
    def contains_box(self, other: "BoundingBox") -> bool:
        """Check if another bounding box is fully inside this one."""
        return (
            self.min_x <= other.min_x and
            self.min_y <= other.min_y and
            self.max_x >= other.max_x and
            self.max_y >= other.max_y
        )
    
    def intersects(self, other: "BoundingBox") -> bool:
        """Check if this bounding box intersects another."""
        return not (
            self.max_x < other.min_x or
            self.min_x > other.max_x or
            self.max_y < other.min_y or
            self.min_y > other.max_y
        )
    
    def expand(self, margin: float) -> "BoundingBox":
        """Return a new bounding box expanded by margin on all sides."""
        return BoundingBox(
            self.min_x - margin,
            self.min_y - margin,
            self.max_x + margin,
            self.max_y + margin
        )
    
    def union(self, other: "BoundingBox") -> "BoundingBox":
        """Return bounding box that contains both boxes."""
        return BoundingBox(
            min(self.min_x, other.min_x),
            min(self.min_y, other.min_y),
            max(self.max_x, other.max_x),
            max(self.max_y, other.max_y)
        )
    
    @classmethod
    def from_points(cls, points: List[Union[Point, Coordinate]]) -> "BoundingBox":
        """Create bounding box from a list of points."""
        if not points:
            raise ValueError("Cannot create bounding box from empty point list")
        
        xs = []
        ys = []
        for p in points:
            if isinstance(p, Point):
                xs.append(p.x)
                ys.append(p.y)
            else:
                xs.append(p[0])
                ys.append(p[1])
        
        return cls(min(xs), min(ys), max(xs), max(ys))


@dataclass
class Polygon:
    """
    2D polygon representation.
    
    Vertices should be in counter-clockwise (CCW) order for the exterior.
    The polygon should be closed (first vertex == last vertex).
    
    Attributes:
        vertices: List of (x, y) coordinates defining the polygon boundary.
                  Must be closed (first == last vertex).
    
    Note on transformations:
        Transformation methods return NEW Polygon instances.
        The original polygon is not modified.
    """
    vertices: VertexList
    
    # Cached computed properties
    _area: Optional[float] = field(default=None, repr=False, compare=False)
    _centroid: Optional[Point] = field(default=None, repr=False, compare=False)
    _bounding_box: Optional[BoundingBox] = field(default=None, repr=False, compare=False)
    
    def __post_init__(self):
        """Validate polygon after creation."""
        if len(self.vertices) < 4:  # Minimum: 3 unique vertices + closing vertex
            raise ValueError(
                f"Polygon must have at least 3 vertices (plus closing vertex). "
                f"Got {len(self.vertices)}"
            )
        
        # Ensure polygon is closed
        if self.vertices[0] != self.vertices[-1]:
            # Auto-close the polygon
            self.vertices = list(self.vertices) + [self.vertices[0]]
    
    @property
    def num_vertices(self) -> int:
        """Number of unique vertices (excluding closing vertex)."""
        return len(self.vertices) - 1
    
    @property
    def area(self) -> float:
        """
        Calculate polygon area using the shoelace formula.
        
        Returns positive area regardless of vertex winding order.
        """
        if self._area is None:
            self._area = self._compute_area()
        return self._area
    
    @property
    def signed_area(self) -> float:
        """
        Calculate signed area.
        
        Positive for CCW winding, negative for CW winding.
        """
        return self._compute_signed_area()
    
    @property
    def is_ccw(self) -> bool:
        """Check if vertices are in counter-clockwise order."""
        return self.signed_area > 0
    
    @property
    def centroid(self) -> Point:
        """Calculate the centroid (center of mass) of the polygon."""
        if self._centroid is None:
            self._centroid = self._compute_centroid()
        return self._centroid
    
    @property
    def bounding_box(self) -> BoundingBox:
        """Calculate axis-aligned bounding box."""
        if self._bounding_box is None:
            self._bounding_box = BoundingBox.from_points(self.vertices)
        return self._bounding_box
    
    @property
    def width(self) -> float:
        """Width of the bounding box."""
        return self.bounding_box.width
    
    @property
    def height(self) -> float:
        """Height of the bounding box."""
        return self.bounding_box.height
    
    @property
    def perimeter(self) -> float:
        """Calculate the perimeter (total edge length)."""
        total = 0.0
        for i in range(len(self.vertices) - 1):
            p1 = self.vertices[i]
            p2 = self.vertices[i + 1]
            dx = p2[0] - p1[0]
            dy = p2[1] - p1[1]
            total += math.sqrt(dx * dx + dy * dy)
        return total
    
    def _compute_signed_area(self) -> float:
        """Compute signed area using shoelace formula."""
        n = len(self.vertices)
        area = 0.0
        for i in range(n - 1):
            j = i + 1
            area += self.vertices[i][0] * self.vertices[j][1]
            area -= self.vertices[j][0] * self.vertices[i][1]
        return area / 2.0
    
    def _compute_area(self) -> float:
        """Compute absolute area."""
        return abs(self._compute_signed_area())
    
    def _compute_centroid(self) -> Point:
        """Compute centroid using the standard formula."""
        n = len(self.vertices)
        cx = 0.0
        cy = 0.0
        signed_area = self._compute_signed_area()
        
        if abs(signed_area) < 1e-10:
            # Degenerate polygon, return average of vertices
            for v in self.vertices[:-1]:
                cx += v[0]
                cy += v[1]
            num = n - 1
            return Point(cx / num, cy / num)
        
        for i in range(n - 1):
            x0, y0 = self.vertices[i]
            x1, y1 = self.vertices[i + 1]
            cross = x0 * y1 - x1 * y0
            cx += (x0 + x1) * cross
            cy += (y0 + y1) * cross
        
        factor = 1.0 / (6.0 * signed_area)
        return Point(cx * factor, cy * factor)
    
    def _clear_cache(self):
        """Clear cached computed properties."""
        self._area = None
        self._centroid = None
        self._bounding_box = None
    
    def translate(self, dx: float, dy: float) -> "Polygon":
        """
        Return a new polygon translated by (dx, dy).
        
        Args:
            dx: Translation in X
            dy: Translation in Y
            
        Returns:
            New translated Polygon
        """
        new_vertices = [(x + dx, y + dy) for x, y in self.vertices]
        return Polygon(new_vertices)
    
    def rotate(
        self, 
        angle_degrees: float, 
        center: Optional[Union[Point, Coordinate]] = None
    ) -> "Polygon":
        """
        Return a new polygon rotated around a center point.
        
        Args:
            angle_degrees: Rotation angle in degrees (CCW positive)
            center: Center of rotation (default: polygon centroid)
            
        Returns:
            New rotated Polygon
        """
        if center is None:
            center = self.centroid
        elif not isinstance(center, Point):
            center = Point(center[0], center[1])
        
        angle_rad = math.radians(angle_degrees)
        cos_a = math.cos(angle_rad)
        sin_a = math.sin(angle_rad)
        
        new_vertices = []
        for x, y in self.vertices:
            # Translate to origin
            dx = x - center.x
            dy = y - center.y
            # Rotate
            new_x = dx * cos_a - dy * sin_a + center.x
            new_y = dx * sin_a + dy * cos_a + center.y
            new_vertices.append((new_x, new_y))
        
        return Polygon(new_vertices)
    
    def flip_horizontal(self, axis_x: Optional[float] = None) -> "Polygon":
        """
        Return a new polygon flipped horizontally (reflected across vertical axis).
        
        This is used for NESTING when placing mirrored pieces.
        NOT to be confused with a fold_line inside the pattern geometry.
        
        Args:
            axis_x: X coordinate of the vertical axis to flip across.
                    Default: center of bounding box.
                    
        Returns:
            New flipped Polygon
        """
        if axis_x is None:
            axis_x = self.bounding_box.center.x
        
        new_vertices = [(2 * axis_x - x, y) for x, y in self.vertices]
        # Reverse to maintain CCW winding after flip
        new_vertices = new_vertices[::-1]
        return Polygon(new_vertices)
    
    def flip_vertical(self, axis_y: Optional[float] = None) -> "Polygon":
        """
        Return a new polygon flipped vertically (reflected across horizontal axis).
        
        This is used for NESTING when placing mirrored pieces.
        NOT to be confused with a fold_line inside the pattern geometry.
        
        Args:
            axis_y: Y coordinate of the horizontal axis to flip across.
                    Default: center of bounding box.
                    
        Returns:
            New flipped Polygon
        """
        if axis_y is None:
            axis_y = self.bounding_box.center.y
        
        new_vertices = [(x, 2 * axis_y - y) for x, y in self.vertices]
        # Reverse to maintain CCW winding after flip
        new_vertices = new_vertices[::-1]
        return Polygon(new_vertices)
    
    def scale(self, factor: float, center: Optional[Union[Point, Coordinate]] = None) -> "Polygon":
        """
        Return a new polygon scaled by a factor around a center point.
        
        Args:
            factor: Scale factor (1.0 = no change, 2.0 = double size)
            center: Center of scaling (default: polygon centroid)
            
        Returns:
            New scaled Polygon
        """
        if center is None:
            center = self.centroid
        elif not isinstance(center, Point):
            center = Point(center[0], center[1])
        
        new_vertices = []
        for x, y in self.vertices:
            new_x = center.x + (x - center.x) * factor
            new_y = center.y + (y - center.y) * factor
            new_vertices.append((new_x, new_y))
        
        return Polygon(new_vertices)
    
    def normalize_to_origin(self) -> "Polygon":
        """
        Return a new polygon with bounding box starting at (0, 0).
        
        Useful for standardizing pieces before storage or comparison.
        
        Returns:
            New Polygon with min corner at origin
        """
        bb = self.bounding_box
        return self.translate(-bb.min_x, -bb.min_y)
    
    def ensure_ccw(self) -> "Polygon":
        """Return polygon with counter-clockwise vertex ordering."""
        if self.is_ccw:
            return Polygon(self.vertices.copy())
        else:
            return Polygon(self.vertices[::-1])
    
    def reverse(self) -> "Polygon":
        """Return polygon with reversed vertex ordering."""
        return Polygon(self.vertices[::-1])
    
    def __iter__(self) -> Iterator[Coordinate]:
        """Iterate over vertices (excluding closing vertex)."""
        return iter(self.vertices[:-1])
    
    def __len__(self) -> int:
        """Number of unique vertices."""
        return self.num_vertices
    
    def to_tuple_list(self) -> VertexList:
        """Return vertices as a list of tuples (copy)."""
        return list(self.vertices)
    
    @classmethod
    def from_points(cls, points: List[Union[Point, Coordinate]]) -> "Polygon":
        """Create polygon from a list of Points or coordinate tuples."""
        vertices = []
        for p in points:
            if isinstance(p, Point):
                vertices.append(p.as_tuple())
            else:
                vertices.append((float(p[0]), float(p[1])))
        return cls(vertices)
    
    @classmethod
    def rectangle(cls, width: float, height: float, center: bool = False) -> "Polygon":
        """
        Create a rectangle polygon.
        
        Args:
            width: Width (X extent)
            height: Height (Y extent)
            center: If True, center at origin. If False, corner at origin.
            
        Returns:
            Rectangle Polygon
        """
        if center:
            hw, hh = width / 2, height / 2
            vertices = [(-hw, -hh), (hw, -hh), (hw, hh), (-hw, hh), (-hw, -hh)]
        else:
            vertices = [(0, 0), (width, 0), (width, height), (0, height), (0, 0)]
        return cls(vertices)


# Utility functions

def distance(p1: Union[Point, Coordinate], p2: Union[Point, Coordinate]) -> float:
    """Calculate Euclidean distance between two points."""
    if isinstance(p1, Point):
        x1, y1 = p1.x, p1.y
    else:
        x1, y1 = p1
    if isinstance(p2, Point):
        x2, y2 = p2.x, p2.y
    else:
        x2, y2 = p2
    
    dx, dy = x2 - x1, y2 - y1
    return math.sqrt(dx * dx + dy * dy)


def angle_between(p1: Coordinate, p2: Coordinate, p3: Coordinate) -> float:
    """
    Calculate angle at p2 formed by p1-p2-p3.
    
    Returns angle in degrees (0-360).
    """
    v1 = (p1[0] - p2[0], p1[1] - p2[1])
    v2 = (p3[0] - p2[0], p3[1] - p2[1])
    
    angle1 = math.atan2(v1[1], v1[0])
    angle2 = math.atan2(v2[1], v2[0])
    
    angle = math.degrees(angle2 - angle1)
    if angle < 0:
        angle += 360
    return angle
