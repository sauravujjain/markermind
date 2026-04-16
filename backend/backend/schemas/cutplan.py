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
    max_ply_height: Optional[int] = None  # Max plies per cut, None = use DB default
    min_plies_by_bundle: Optional[str] = None  # e.g. "6:50,5:40,4:30,3:10,2:1,1:1", None = use DB default
    cost_metric: str = "efficiency"  # "efficiency" = minimize (1-eff)*plies, "length" = minimize length*plies


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
    svg_preview: Optional[str] = None
    computation_time_s: Optional[float] = None

    class Config:
        from_attributes = True

    @classmethod
    def model_validate(cls, obj, **kwargs):
        # If ORM object with layout relationship, extract svg_preview and computation_time_s
        if hasattr(obj, 'layout') and obj.layout and hasattr(obj.layout, 'svg_preview'):
            data = {
                'id': str(obj.id),
                'cutplan_id': str(obj.cutplan_id),
                'marker_id': str(obj.marker_id) if obj.marker_id else None,
                'marker_label': obj.marker_label,
                'ratio_str': obj.ratio_str,
                'efficiency': obj.efficiency,
                'length_yards': obj.length_yards,
                'plies_by_color': obj.plies_by_color or {},
                'total_plies': obj.total_plies,
                'cuts': obj.cuts,
                'svg_preview': obj.layout.svg_preview,
                'computation_time_s': obj.layout.computation_time_s,
            }
            return super().model_validate(data, **kwargs)
        return super().model_validate(obj, **kwargs)


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
    solver_config: Optional[Dict] = None
    generation_batch_id: Optional[str] = None
    markers: List[CutplanMarkerResponse] = []
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True

    @classmethod
    def model_validate(cls, obj, **kwargs):
        # Manually convert markers so CutplanMarkerResponse.model_validate
        # is called for each marker (picks up layout.svg_preview)
        if hasattr(obj, 'markers') and obj.markers:
            marker_responses = [
                CutplanMarkerResponse.model_validate(m) for m in obj.markers
            ]
            data = {
                'id': str(obj.id),
                'order_id': str(obj.order_id),
                'name': obj.name,
                'solver_type': obj.solver_type.value if hasattr(obj.solver_type, 'value') else str(obj.solver_type),
                'status': obj.status.value if hasattr(obj.status, 'value') else str(obj.status),
                'unique_markers': obj.unique_markers,
                'total_cuts': obj.total_cuts,
                'bundle_cuts': obj.bundle_cuts,
                'total_plies': obj.total_plies,
                'total_yards': obj.total_yards,
                'efficiency': obj.efficiency,
                'total_cost': obj.total_cost,
                'fabric_cost': obj.fabric_cost,
                'spreading_cost': obj.spreading_cost,
                'cutting_cost': obj.cutting_cost,
                'prep_cost': obj.prep_cost,
                'solver_config': obj.solver_config,
                'generation_batch_id': obj.generation_batch_id if hasattr(obj, 'generation_batch_id') else None,
                'markers': marker_responses,
                'created_at': obj.created_at,
                'updated_at': obj.updated_at,
            }
            return super().model_validate(data, **kwargs)
        return super().model_validate(obj, **kwargs)


class CutplanComparisonResponse(BaseModel):
    """Compare multiple cutplan options."""
    order_id: str
    cutplans: List[CutplanResponse]
    recommended_id: Optional[str] = None
    recommendation_reason: Optional[str] = None


class RefinementRequest(BaseModel):
    """Request to start CPU refinement (final nesting) for an approved cutplan."""
    piece_buffer_mm: float = 0.0
    edge_buffer_mm: float = 0.0
    time_limit_s: float = 120.0
    rotation_mode: str = "free"  # "free" or "nap_one_way"
    quadtree_depth: int = 5
    early_termination: bool = True
    exploration_time_s: Optional[float] = None  # custom explore time (seconds), None = auto
    compression_time_s: Optional[float] = None  # custom compress time (seconds), None = auto
    seed_screening: bool = False  # run 6 seeds × 10s to find best seed
    use_cloud: bool = False  # run on Modal cloud (not used yet, reserved)


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
    quadtree_depth: Optional[int] = None

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
