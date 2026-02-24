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
    marker_label: Optional[str] = None
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


class RefinementRequest(BaseModel):
    """Request to start CPU refinement (final nesting) for an approved cutplan."""
    piece_buffer_mm: float = 2.0
    edge_buffer_mm: float = 5.0
    time_limit_s: float = 20.0
    rotation_mode: str = "free"  # "free" or "nap_safe"


class MarkerLayoutResponse(BaseModel):
    """Response for a single refined marker layout."""
    id: str
    cutplan_marker_id: str
    marker_label: Optional[str] = None
    ratio_str: str
    utilization: float
    strip_length_mm: float
    length_yards: float
    computation_time_s: float
    svg_preview: str
    dxf_file_path: Optional[str] = None
    piece_buffer_mm: Optional[float] = None
    edge_buffer_mm: Optional[float] = None
    time_limit_s: Optional[float] = None
    rotation_mode: Optional[str] = None

    class Config:
        from_attributes = True


class RefinementStatusResponse(BaseModel):
    """Response for refinement progress polling."""
    status: str  # running, completed, failed, cancelled
    progress: int  # 0-100
    message: str
    markers_total: int
    markers_done: int
    layouts: List[MarkerLayoutResponse] = []
