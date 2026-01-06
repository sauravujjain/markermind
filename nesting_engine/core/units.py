"""
Unit conversion system for the nesting engine.

This module provides:
- LengthUnit enum for supported units
- UnitConverter for converting between any units
- UnitContext for managing units across a nesting job

Internal calculations always use millimeters (mm) as the base unit.

Supported units:
- Millimeters (mm) - base unit for internal calculations
- Centimeters (cm)
- Meters (m)
- Inches (in)
- Yards (yd)
- Pixels (px) - requires DPI context

Example usage:
    >>> from nesting_engine.core.units import LengthUnit, UnitConverter
    
    # Convert 10 inches to mm
    >>> UnitConverter.to_mm(10, LengthUnit.INCH)
    254.0
    
    # Convert between any units
    >>> UnitConverter.convert(1, LengthUnit.YARD, LengthUnit.METER)
    0.9144
    
    # Convert with pixel context (96 DPI)
    >>> UnitConverter.to_mm(96, LengthUnit.PIXEL, dpi=96.0)
    25.4  # 1 inch in mm
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import List, Tuple, Union, Optional


class LengthUnit(Enum):
    """
    Supported length units for the nesting engine.
    
    Internal calculations always use MILLIMETER as the base unit.
    """
    MILLIMETER = "mm"
    CENTIMETER = "cm"
    METER = "m"
    INCH = "in"
    YARD = "yd"
    PIXEL = "px"  # Requires DPI context for conversion
    
    def __str__(self) -> str:
        return self.value
    
    @classmethod
    def from_string(cls, value: str) -> "LengthUnit":
        """
        Parse a unit string to LengthUnit enum.
        
        Accepts common aliases: 'mm', 'millimeter', 'millimeters', etc.
        
        Args:
            value: Unit string to parse
            
        Returns:
            Corresponding LengthUnit enum value
            
        Raises:
            ValueError: If the string doesn't match any known unit
        """
        value = value.lower().strip()
        
        aliases = {
            # Millimeters
            "mm": cls.MILLIMETER,
            "millimeter": cls.MILLIMETER,
            "millimeters": cls.MILLIMETER,
            "millimetre": cls.MILLIMETER,
            "millimetres": cls.MILLIMETER,
            # Centimeters
            "cm": cls.CENTIMETER,
            "centimeter": cls.CENTIMETER,
            "centimeters": cls.CENTIMETER,
            "centimetre": cls.CENTIMETER,
            "centimetres": cls.CENTIMETER,
            # Meters
            "m": cls.METER,
            "meter": cls.METER,
            "meters": cls.METER,
            "metre": cls.METER,
            "metres": cls.METER,
            # Inches
            "in": cls.INCH,
            "inch": cls.INCH,
            "inches": cls.INCH,
            '"': cls.INCH,
            # Yards
            "yd": cls.YARD,
            "yard": cls.YARD,
            "yards": cls.YARD,
            # Pixels
            "px": cls.PIXEL,
            "pixel": cls.PIXEL,
            "pixels": cls.PIXEL,
        }
        
        if value in aliases:
            return aliases[value]
        
        raise ValueError(
            f"Unknown unit: '{value}'. "
            f"Supported units: {', '.join(sorted(set(aliases.keys())))}"
        )


# Conversion factors TO millimeters (base unit)
# All values are exact or standard definitions
_TO_MM: dict[LengthUnit, float] = {
    LengthUnit.MILLIMETER: 1.0,
    LengthUnit.CENTIMETER: 10.0,
    LengthUnit.METER: 1000.0,
    LengthUnit.INCH: 25.4,  # Exact definition
    LengthUnit.YARD: 914.4,  # 36 inches * 25.4
    # PIXEL is handled separately (DPI-dependent)
}


class UnitConverter:
    """
    Stateless utility class for unit conversions.
    
    All internal calculations in the nesting engine use millimeters (mm)
    as the base unit. This class provides methods to convert to/from mm
    and between any supported units.
    
    For pixel conversions, DPI (dots per inch) must be specified.
    Common DPI values:
    - 72: Traditional print DPI
    - 96: Windows default screen DPI
    - 300: High-quality print DPI
    
    Example:
        >>> UnitConverter.to_mm(10, LengthUnit.INCH)
        254.0
        >>> UnitConverter.from_mm(254.0, LengthUnit.INCH)
        10.0
        >>> UnitConverter.convert(1, LengthUnit.YARD, LengthUnit.METER)
        0.9144
    """
    
    BASE_UNIT = LengthUnit.MILLIMETER
    DEFAULT_DPI = 96.0  # SVG default
    
    @classmethod
    def to_mm(
        cls, 
        value: float, 
        from_unit: LengthUnit, 
        dpi: float = DEFAULT_DPI
    ) -> float:
        """
        Convert a value from any unit to millimeters.
        
        Args:
            value: The numeric value to convert
            from_unit: The source unit
            dpi: Dots per inch for pixel conversions (default: 96.0)
            
        Returns:
            Value in millimeters
            
        Example:
            >>> UnitConverter.to_mm(10, LengthUnit.INCH)
            254.0
        """
        if from_unit == LengthUnit.PIXEL:
            # 1 inch = 25.4 mm, 1 inch = dpi pixels
            # Therefore: 1 pixel = 25.4 / dpi mm
            return value * (25.4 / dpi)
        
        return value * _TO_MM[from_unit]
    
    @classmethod
    def from_mm(
        cls, 
        value_mm: float, 
        to_unit: LengthUnit, 
        dpi: float = DEFAULT_DPI
    ) -> float:
        """
        Convert a value from millimeters to any unit.
        
        Args:
            value_mm: The value in millimeters
            to_unit: The target unit
            dpi: Dots per inch for pixel conversions (default: 96.0)
            
        Returns:
            Value in the target unit
            
        Example:
            >>> UnitConverter.from_mm(254.0, LengthUnit.INCH)
            10.0
        """
        if to_unit == LengthUnit.PIXEL:
            # 1 mm = dpi / 25.4 pixels
            return value_mm * (dpi / 25.4)
        
        return value_mm / _TO_MM[to_unit]
    
    @classmethod
    def convert(
        cls,
        value: float,
        from_unit: LengthUnit,
        to_unit: LengthUnit,
        dpi: float = DEFAULT_DPI
    ) -> float:
        """
        Convert a value between any two units.
        
        Args:
            value: The numeric value to convert
            from_unit: The source unit
            to_unit: The target unit
            dpi: Dots per inch for pixel conversions (default: 96.0)
            
        Returns:
            Value in the target unit
            
        Example:
            >>> UnitConverter.convert(1, LengthUnit.YARD, LengthUnit.METER)
            0.9144
        """
        if from_unit == to_unit:
            return value
        
        mm_value = cls.to_mm(value, from_unit, dpi)
        return cls.from_mm(mm_value, to_unit, dpi)
    
    @classmethod
    def convert_point(
        cls,
        point: Tuple[float, float],
        from_unit: LengthUnit,
        to_unit: LengthUnit,
        dpi: float = DEFAULT_DPI
    ) -> Tuple[float, float]:
        """
        Convert a 2D coordinate point between units.
        
        Args:
            point: (x, y) coordinate tuple
            from_unit: The source unit
            to_unit: The target unit
            dpi: Dots per inch for pixel conversions
            
        Returns:
            Converted (x, y) tuple
        """
        if from_unit == to_unit:
            return point
        
        return (
            cls.convert(point[0], from_unit, to_unit, dpi),
            cls.convert(point[1], from_unit, to_unit, dpi)
        )
    
    @classmethod
    def convert_vertices(
        cls,
        vertices: List[Tuple[float, float]],
        from_unit: LengthUnit,
        to_unit: LengthUnit,
        dpi: float = DEFAULT_DPI
    ) -> List[Tuple[float, float]]:
        """
        Convert a list of vertices (polygon) between units.
        
        Args:
            vertices: List of (x, y) coordinate tuples
            from_unit: The source unit
            to_unit: The target unit
            dpi: Dots per inch for pixel conversions
            
        Returns:
            List of converted (x, y) tuples
        """
        if from_unit == to_unit:
            return vertices.copy()
        
        return [cls.convert_point(v, from_unit, to_unit, dpi) for v in vertices]
    
    @classmethod
    def format_value(
        cls,
        value_mm: float,
        display_unit: LengthUnit,
        precision: int = 2,
        dpi: float = DEFAULT_DPI
    ) -> str:
        """
        Format a value (stored in mm) for display in a specific unit.
        
        Args:
            value_mm: Value in millimeters
            display_unit: Unit to display in
            precision: Decimal places (default: 2)
            dpi: Dots per inch for pixel conversions
            
        Returns:
            Formatted string with unit suffix
            
        Example:
            >>> UnitConverter.format_value(254.0, LengthUnit.INCH)
            "10.00 in"
        """
        converted = cls.from_mm(value_mm, display_unit, dpi)
        return f"{converted:.{precision}f} {display_unit.value}"


@dataclass
class UnitContext:
    """
    Defines the unit context for a nesting job.
    
    Different aspects of a nesting job may use different units:
    - Piece geometry might come from DXF in centimeters
    - Container dimensions might be specified in inches
    - Inter-piece buffer might be in millimeters
    - Output display might be in a different unit
    
    This class stores the unit configuration for a job and provides
    convenient conversion methods.
    
    Attributes:
        piece_unit: Unit for piece geometry (from input files)
        container_unit: Unit for container/sheet dimensions
        buffer_unit: Unit for spacing/buffer distances
        output_unit: Unit for output display
        dpi: Dots per inch for pixel conversions
        
    Example:
        >>> ctx = UnitContext(
        ...     piece_unit=LengthUnit.CENTIMETER,
        ...     container_unit=LengthUnit.INCH,
        ...     buffer_unit=LengthUnit.MILLIMETER
        ... )
        >>> ctx.piece_to_internal(10)  # 10 cm -> 100 mm
        100.0
    """
    piece_unit: LengthUnit = LengthUnit.MILLIMETER
    container_unit: LengthUnit = LengthUnit.INCH
    buffer_unit: LengthUnit = LengthUnit.MILLIMETER
    output_unit: LengthUnit = LengthUnit.MILLIMETER
    dpi: float = 96.0
    
    # Internal base unit is always mm
    _internal_unit: LengthUnit = field(default=LengthUnit.MILLIMETER, repr=False)
    
    def piece_to_internal(self, value: float) -> float:
        """Convert from piece unit to internal (mm)."""
        return UnitConverter.to_mm(value, self.piece_unit, self.dpi)
    
    def container_to_internal(self, value: float) -> float:
        """Convert from container unit to internal (mm)."""
        return UnitConverter.to_mm(value, self.container_unit, self.dpi)
    
    def buffer_to_internal(self, value: float) -> float:
        """Convert from buffer unit to internal (mm)."""
        return UnitConverter.to_mm(value, self.buffer_unit, self.dpi)
    
    def internal_to_output(self, value: float) -> float:
        """Convert from internal (mm) to output unit."""
        return UnitConverter.from_mm(value, self.output_unit, self.dpi)
    
    def convert_piece_vertices(
        self, 
        vertices: List[Tuple[float, float]]
    ) -> List[Tuple[float, float]]:
        """Convert piece vertices from piece_unit to internal (mm)."""
        return UnitConverter.convert_vertices(
            vertices, self.piece_unit, self._internal_unit, self.dpi
        )
    
    def format_for_display(self, value_mm: float, precision: int = 2) -> str:
        """Format an internal value for display in output_unit."""
        return UnitConverter.format_value(value_mm, self.output_unit, precision, self.dpi)


# Convenience functions for common conversions
def mm_to_inches(mm: float) -> float:
    """Convert millimeters to inches."""
    return UnitConverter.convert(mm, LengthUnit.MILLIMETER, LengthUnit.INCH)


def inches_to_mm(inches: float) -> float:
    """Convert inches to millimeters."""
    return UnitConverter.convert(inches, LengthUnit.INCH, LengthUnit.MILLIMETER)


def cm_to_mm(cm: float) -> float:
    """Convert centimeters to millimeters."""
    return cm * 10.0


def mm_to_cm(mm: float) -> float:
    """Convert millimeters to centimeters."""
    return mm / 10.0
