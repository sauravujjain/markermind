from pydantic import BaseModel
from typing import Optional, List, Dict
from datetime import datetime


class CutplanOptimizeRequest(BaseModel):
    order_id: str
    solver_type: str = "single_color"  # single_color, multicolor_joint, two_stage
    penalty: float = 5.0  # Marker penalty for ILP
    generate_options: List[str] = ["balanced"]  # Which options to generate
    color_code: Optional[str] = None  # Filter to specific color, None = all colors
    fabric_cost_per_yard: Optional[float] = None  # User-specified fabric cost, None = use DB default


class CostBreakdownResponse(BaseModel):
    total_cost: float
    fabric_cost: float
    spreading_cost: float
    cutting_cost: float
    prep_cost: float
    fabric_yards: float
    total_plies: int
    unique_markers: int


class CutplanMarkerResponse(BaseModel):
    id: str
    cutplan_id: str
    marker_id: Optional[str]
    ratio_str: str
    efficiency: Optional[float]
    length_yards: Optional[float]
    plies_by_color: Dict[str, int]
    total_plies: int
    cuts: int

    class Config:
        from_attributes = True


class CutplanResponse(BaseModel):
    id: str
    order_id: str
    name: Optional[str]
    solver_type: str
    status: str
    unique_markers: Optional[int]
    total_cuts: Optional[int]
    bundle_cuts: Optional[int]
    total_plies: Optional[int]
    total_yards: Optional[float]
    efficiency: Optional[float]
    total_cost: Optional[float]
    fabric_cost: Optional[float]
    spreading_cost: Optional[float]
    cutting_cost: Optional[float]
    prep_cost: Optional[float]
    markers: List[CutplanMarkerResponse] = []
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class CutplanComparisonResponse(BaseModel):
    """Compare multiple cutplan options."""
    order_id: str
    cutplans: List[CutplanResponse]
    recommended_id: Optional[str] = None
    recommendation_reason: Optional[str] = None
