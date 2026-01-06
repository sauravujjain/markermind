"""
Spyrrow engine wrapper for the nesting engine.

This module provides integration with the spyrrow library, a Python wrapper
for the state-of-the-art Sparrow nesting algorithm.

TERMINOLOGY MAPPING:
    Spyrrow uses different terminology than our engine:
    
    | Spyrrow              | Our Engine        | Description                    |
    |----------------------|-------------------|--------------------------------|
    | strip_height         | container.width   | Fixed dimension of container   |
    | solution.width       | strip_length      | Variable dimension (result)    |
    | allowed_orientations | allowed_rotations | Permitted rotation angles      |
    | Item                 | Piece + NestingItem| Geometry + quantity + constraints |

FLIP HANDLING:
    Spyrrow does NOT have native flip/mirror support.
    
    For FlipMode.PAIRED items, we generate two separate spyrrow Items:
    1. Original geometry
    2. Flipped geometry (pre-computed)
    
    This allows left/right paired pieces to be handled correctly.

Example:
    >>> from nesting_engine.engine.spyrrow_engine import SpyrrowEngine
    >>> from nesting_engine.core import NestingInstance, Container, NestingItem, Piece
    
    >>> engine = SpyrrowEngine()
    >>> solution = engine.solve(instance, time_limit=60)
    >>> print(solution.utilization_percent)
    85.5
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import List, Optional, Dict, Any, Tuple
from abc import ABC, abstractmethod

# Import spyrrow (will fail gracefully if not installed)
try:
    import spyrrow
    SPYRROW_AVAILABLE = True
except ImportError:
    SPYRROW_AVAILABLE = False
    spyrrow = None

from nesting_engine.core.piece import Piece
from nesting_engine.core.geometry import Polygon
from nesting_engine.core.instance import NestingInstance, NestingItem, FlipMode
from nesting_engine.core.solution import NestingSolution, PlacedPiece


class NestingEngine(ABC):
    """Abstract base class for nesting engines."""
    
    @property
    @abstractmethod
    def name(self) -> str:
        """Engine name."""
        pass
    
    @property
    @abstractmethod
    def version(self) -> str:
        """Engine version."""
        pass
    
    @abstractmethod
    def solve(
        self,
        instance: NestingInstance,
        time_limit: float = 60.0,
        **kwargs
    ) -> NestingSolution:
        """
        Solve a nesting instance.
        
        Args:
            instance: The nesting problem to solve
            time_limit: Maximum computation time in seconds
            **kwargs: Engine-specific parameters
            
        Returns:
            NestingSolution with placements and metrics
        """
        pass


@dataclass
class SpyrrowConfig:
    """
    Configuration for the Spyrrow solver.
    
    Attributes:
        time_limit: Maximum computation time in seconds
        num_workers: Number of parallel threads (0 = auto)
        seed: Random seed for reproducibility (None = random)
        early_termination: Stop early if optimal found
    """
    time_limit: float = 60.0
    num_workers: int = 0  # 0 = auto-detect
    seed: Optional[int] = None
    early_termination: bool = False


class SpyrrowEngine(NestingEngine):
    """
    Nesting engine using the Spyrrow library.
    
    Spyrrow is a Python wrapper for Sparrow, a state-of-the-art
    nesting algorithm for 2D irregular strip packing.
    
    FLIP HANDLING:
        Since spyrrow doesn't support flip/mirror natively, this engine
        handles FlipMode by pre-generating flipped geometry:
        
        - FlipMode.NONE: Single spyrrow Item
        - FlipMode.PAIRED: Two spyrrow Items (original + flipped geometry)
        - FlipMode.ANY: Not currently supported (falls back to NONE)
    
    Example:
        >>> engine = SpyrrowEngine()
        >>> config = SpyrrowConfig(time_limit=120, num_workers=4)
        >>> solution = engine.solve(instance, config=config)
    """
    
    def __init__(self):
        if not SPYRROW_AVAILABLE:
            raise ImportError(
                "spyrrow is not installed. Install with: pip install spyrrow"
            )
        self._version = "0.8.0"  # Will be updated from actual package
    
    @property
    def name(self) -> str:
        return "spyrrow"
    
    @property
    def version(self) -> str:
        return self._version
    
    def solve(
        self,
        instance: NestingInstance,
        time_limit: float = 60.0,
        config: Optional[SpyrrowConfig] = None,
        **kwargs
    ) -> NestingSolution:
        """
        Solve a nesting instance using Spyrrow.
        
        Args:
            instance: The nesting problem to solve
            time_limit: Maximum computation time in seconds (overridden by config)
            config: SpyrrowConfig with solver parameters
            **kwargs: Additional parameters (passed to spyrrow)
            
        Returns:
            NestingSolution with placements and metrics
        """
        if config is None:
            config = SpyrrowConfig(time_limit=time_limit)
        
        start_time = time.time()
        
        # Convert our instance to spyrrow format
        spyrrow_items, item_mapping = self._convert_items(instance)
        
        # Create spyrrow instance
        # Note: spyrrow's strip_height is our container width
        spyrrow_instance = spyrrow.StripPackingInstance(
            instance.id,
            strip_height=instance.container.width,
            items=spyrrow_items
        )
        
        # Create spyrrow config
        # Note: spyrrow 0.8.0 fixed the 'num_wokers' typo to 'num_workers'
        # Pass None for auto-detection of CPU cores
        spyrrow_config = spyrrow.StripPackingConfig(
            early_termination=config.early_termination,
            total_computation_time=int(config.time_limit),
            num_workers=config.num_workers if config.num_workers and config.num_workers > 0 else None,
            seed=config.seed if config.seed is not None else 0
        )
        
        # Solve
        spyrrow_solution = spyrrow_instance.solve(spyrrow_config)
        
        computation_time_ms = (time.time() - start_time) * 1000
        
        # Convert solution back to our format
        solution = self._convert_solution(
            instance,
            spyrrow_solution,
            item_mapping,
            computation_time_ms
        )
        
        return solution
    
    def _convert_items(
        self,
        instance: NestingInstance
    ) -> Tuple[List[Any], Dict[str, Dict[str, Any]]]:
        """
        Convert NestingItems to spyrrow Items.
        
        Handles flip by generating separate items for flipped geometry.
        
        Returns:
            (spyrrow_items, item_mapping)
            
            item_mapping maps spyrrow item IDs back to our pieces and flip state:
            {
                "piece_001": {"piece_id": "piece_001", "flipped": False},
                "piece_001_flipped": {"piece_id": "piece_001", "flipped": True},
            }
        """
        spyrrow_items = []
        item_mapping = {}
        
        for nesting_item in instance.items:
            piece = nesting_item.piece
            
            # Get placement breakdown (handles FlipMode)
            breakdown = nesting_item.get_placement_breakdown()
            
            for is_flipped, count in breakdown:
                if count == 0:
                    continue
                
                # Generate item ID
                if is_flipped:
                    item_id = f"{piece.id}_flipped"
                    # Generate flipped geometry
                    flipped_poly = piece.polygon.flip_horizontal()
                    vertices = flipped_poly.to_tuple_list()
                else:
                    item_id = piece.id
                    vertices = piece.to_spyrrow_format()
                
                # Get allowed orientations (rotations)
                allowed_orientations = piece.to_spyrrow_orientations()
                
                # Create spyrrow Item
                spyrrow_item = spyrrow.Item(
                    item_id,
                    vertices,
                    demand=count,
                    allowed_orientations=allowed_orientations
                )
                
                spyrrow_items.append(spyrrow_item)
                
                # Store mapping
                item_mapping[item_id] = {
                    "piece_id": piece.id,
                    "flipped": is_flipped,
                    "piece": piece
                }
        
        return spyrrow_items, item_mapping
    
    def _convert_solution(
        self,
        instance: NestingInstance,
        spyrrow_solution: Any,
        item_mapping: Dict[str, Dict[str, Any]],
        computation_time_ms: float
    ) -> NestingSolution:
        """
        Convert spyrrow solution to our NestingSolution format.
        """
        placements = []
        instance_counters = {}  # Track instance_index per piece
        
        for placed_item in spyrrow_solution.placed_items:
            item_id = placed_item.id
            mapping = item_mapping.get(item_id)
            
            if mapping is None:
                # Unknown item - skip
                continue
            
            original_piece_id = mapping["piece_id"]
            is_flipped = mapping["flipped"]
            
            # Get or initialize instance counter
            if original_piece_id not in instance_counters:
                instance_counters[original_piece_id] = 0
            instance_index = instance_counters[original_piece_id]
            instance_counters[original_piece_id] += 1
            
            # Extract translation (position)
            translation = placed_item.translation
            x, y = translation[0], translation[1]
            
            # Extract rotation
            rotation = placed_item.rotation
            
            # Create PlacedPiece
            # Note: 'flipped' here is a NESTING decision (from FlipMode)
            placement = PlacedPiece(
                piece_id=original_piece_id,
                instance_index=instance_index,
                x=x,
                y=y,
                rotation=rotation,
                flipped=is_flipped  # NESTING flip state
            )
            placements.append(placement)
        
        # Note: spyrrow's solution.width is our strip_length
        strip_length = spyrrow_solution.width
        
        # Create solution
        solution = NestingSolution(
            instance_id=instance.id,
            placements=placements,
            strip_length=strip_length,
            container_width=instance.container.width,
            container_height=instance.container.height,
            computation_time_ms=computation_time_ms,
            engine_name=self.name,
            engine_version=self.version
        )
        
        # Set piece areas for utilization calculation
        piece_areas = {}
        for nesting_item in instance.items:
            piece_areas[nesting_item.piece_id] = nesting_item.piece.area
        solution.set_piece_areas(piece_areas)
        
        return solution


def check_spyrrow_available() -> bool:
    """Check if spyrrow is available."""
    return SPYRROW_AVAILABLE


def get_spyrrow_version() -> Optional[str]:
    """Get installed spyrrow version, or None if not installed."""
    if not SPYRROW_AVAILABLE:
        return None
    try:
        import importlib.metadata
        return importlib.metadata.version("spyrrow")
    except Exception:
        return "unknown"
