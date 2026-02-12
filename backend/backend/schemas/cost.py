from pydantic import BaseModel
from typing import Optional
from datetime import datetime


class CostConfigUpdate(BaseModel):
    fabric_cost_per_yard: Optional[float] = None
    spreading_cost_per_yard: Optional[float] = None
    spreading_cost_per_ply: Optional[float] = None
    spreading_labor_cost_per_hour: Optional[float] = None
    spreading_speed_m_per_min: Optional[float] = None
    spreading_prep_buffer_pct: Optional[float] = None
    spreading_workers_per_lay: Optional[int] = None
    ply_end_cut_time_s: Optional[float] = None
    cutting_cost_per_inch: Optional[float] = None
    cutting_speed_cm_per_s: Optional[float] = None
    cutting_labor_cost_per_hour: Optional[float] = None
    cutting_workers_per_cut: Optional[int] = None
    prep_cost_per_marker: Optional[float] = None
    prep_cost_per_meter: Optional[float] = None
    prep_perf_paper_cost_per_m: Optional[float] = None
    prep_perf_paper_enabled: Optional[bool] = None
    prep_underlayer_cost_per_m: Optional[float] = None
    prep_underlayer_enabled: Optional[bool] = None
    prep_top_layer_cost_per_m: Optional[float] = None
    prep_top_layer_enabled: Optional[bool] = None
    max_ply_height: Optional[int] = None
    min_plies_by_bundle: Optional[str] = None


class CostConfigResponse(BaseModel):
    id: str
    customer_id: str
    name: str
    fabric_cost_per_yard: float
    spreading_cost_per_yard: float
    spreading_cost_per_ply: float
    spreading_labor_cost_per_hour: float
    spreading_speed_m_per_min: float
    spreading_prep_buffer_pct: float
    spreading_workers_per_lay: int
    ply_end_cut_time_s: float
    cutting_cost_per_inch: float
    cutting_speed_cm_per_s: float
    cutting_labor_cost_per_hour: float
    cutting_workers_per_cut: int
    prep_cost_per_marker: float
    prep_cost_per_meter: float
    prep_perf_paper_cost_per_m: float
    prep_perf_paper_enabled: bool
    prep_underlayer_cost_per_m: float
    prep_underlayer_enabled: bool
    prep_top_layer_cost_per_m: float
    prep_top_layer_enabled: bool
    max_ply_height: int
    min_plies_by_bundle: str
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True
