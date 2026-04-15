from pydantic import BaseModel
from typing import Optional, List
from datetime import datetime


# ---------------------------------------------------------------------------
# Requests
# ---------------------------------------------------------------------------


class RollPlanCreateRequest(BaseModel):
    cutplan_id: str
    name: Optional[str] = None
    color_code: Optional[str] = None
    mode: str = "both"                          # monte_carlo, ga, both
    num_simulations: int = 100
    min_reuse_length_yards: float = 0.5
    # Pseudo-roll config (used when no real rolls uploaded)
    pseudo_roll_avg_yards: float = 100.0
    pseudo_roll_delta_yards: float = 20.0
    waste_threshold_pct: float = 2.0           # waste % threshold (min 0.1)
    pseudo_buffer_pct: float = 5.0              # % buffer over cutplan to trigger pseudo-roll generation (0 = no pseudo rolls)
    # GA tuning (optional overrides)
    ga_pop_size: int = 30
    ga_generations: int = 50


# ---------------------------------------------------------------------------
# Responses: Fabric Rolls
# ---------------------------------------------------------------------------


class FabricRollResponse(BaseModel):
    id: str
    roll_plan_id: str
    roll_number: str
    length_yards: float
    is_pseudo: bool
    width_inches: Optional[float] = None
    shrinkage_x_pct: Optional[float] = None
    shrinkage_y_pct: Optional[float] = None
    shade_group: Optional[str] = None
    created_at: datetime

    class Config:
        from_attributes = True


class RollUploadResponse(BaseModel):
    """Summary after parsing and saving roll Excel."""
    roll_plan_id: str
    rolls_count: int
    total_length_yards: float
    avg_length_yards: float
    median_length_yards: float
    min_length_yards: float
    max_length_yards: float
    rolls: List[FabricRollResponse]


# ---------------------------------------------------------------------------
# Responses: Cut Dockets
# ---------------------------------------------------------------------------


class RollAssignmentResponse(BaseModel):
    roll_id: str
    roll_length_yards: float
    plies_from_roll: int
    end_bit_yards: float
    is_pseudo: bool = False
    fabric_used_yards: float = 0.0


class CutDocketResponse(BaseModel):
    cut_number: int
    marker_label: str
    ratio_str: str
    marker_length_yards: float
    plies: int
    plies_planned: Optional[int] = None  # Actual plies planned; None = same as plies (no shortfall)
    assigned_rolls: List[RollAssignmentResponse]
    total_fabric_yards: float
    total_end_bit_yards: float


# ---------------------------------------------------------------------------
# Responses: Waste breakdown
# ---------------------------------------------------------------------------


class WasteStatsResponse(BaseModel):
    """Statistics for one waste category across MC runs."""
    avg: float = 0.0
    std: float = 0.0
    min: float = 0.0
    max: float = 0.0
    median: float = 0.0
    p95: float = 0.0


class WasteBreakdownResponse(BaseModel):
    """Single-run waste breakdown (for GA result)."""
    unusable_yards: float = 0.0      # Type 1: < yield per garment
    unusable_count: int = 0
    endbit_yards: float = 0.0        # Type 2: optimization target
    endbit_count: int = 0
    returnable_yards: float = 0.0    # Type 3: >= longest marker
    returnable_count: int = 0
    real_waste_yards: float = 0.0    # Type 1 + Type 2


# ---------------------------------------------------------------------------
# Responses: Roll Plan
# ---------------------------------------------------------------------------


class RollPlanStatusResponse(BaseModel):
    id: str
    status: str
    progress: int
    message: str


class RollPlanListItem(BaseModel):
    id: str
    cutplan_id: str
    name: Optional[str] = None
    color_code: Optional[str] = None
    status: str
    mode: str
    input_type: Optional[str] = None
    total_fabric_required: Optional[float] = None
    mc_endbit_avg: Optional[float] = None       # Key metric for comparison
    mc_real_waste_avg: Optional[float] = None
    ga_endbit_yards: Optional[float] = None
    ga_real_waste_yards: Optional[float] = None
    created_at: datetime

    class Config:
        from_attributes = True


class MonteCarloResultResponse(BaseModel):
    """
    MC cutplan evaluation results.
    Key metric: endbit_waste (Type 2) — the waste that COULD have been used.
    """
    num_simulations: int
    total_fabric_required: Optional[float] = None
    # Per-category waste stats
    unusable_waste: WasteStatsResponse = WasteStatsResponse()   # Type 1
    endbit_waste: WasteStatsResponse = WasteStatsResponse()     # Type 2  ← compare cutplans by this
    returnable_waste: WasteStatsResponse = WasteStatsResponse() # Type 3
    real_waste: WasteStatsResponse = WasteStatsResponse()       # Type 1 + 2
    best_run_dockets: List[CutDocketResponse] = []


class GAResultResponse(BaseModel):
    """GA roll-to-marker optimization results with waste breakdown."""
    waste: WasteBreakdownResponse = WasteBreakdownResponse()
    generations_run: Optional[int] = None
    dockets: List[CutDocketResponse] = []


class PreflightWarningResponse(BaseModel):
    level: str     # "warning" or "error"
    message: str


class WasteAssessmentResponse(BaseModel):
    waste_pct: float
    exceeds_threshold: bool
    threshold_pct: float
    waste_yards: Optional[float] = None
    total_fabric_yards: Optional[float] = None
    recommendation: str


class RollPlanResponse(BaseModel):
    id: str
    cutplan_id: str
    name: Optional[str] = None
    color_code: Optional[str] = None
    status: str
    mode: str
    input_type: Optional[str] = None
    num_simulations: int
    min_reuse_length_yards: float
    pseudo_roll_avg_yards: Optional[float] = None
    pseudo_roll_delta_yards: Optional[float] = None
    progress: int
    progress_message: Optional[str] = None
    error_message: Optional[str] = None
    preflight_warnings: Optional[List[PreflightWarningResponse]] = None
    roll_adjustment_message: Optional[str] = None

    # Results
    monte_carlo: Optional[MonteCarloResultResponse] = None
    ga: Optional[GAResultResponse] = None
    waste_assessment: Optional[WasteAssessmentResponse] = None

    # Roll count
    rolls_count: int = 0
    real_rolls_count: int = 0
    pseudo_rolls_count: int = 0

    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


# ---------------------------------------------------------------------------
# Roll Preview (parse-only, no DB save)
# ---------------------------------------------------------------------------


class RollPreviewRow(BaseModel):
    roll_number: str
    length_yards: float
    unit: str = "yd"


class RollPreviewResponse(BaseModel):
    rolls_count: int
    total_length_yards: float
    avg_length_yards: float
    median_length_yards: float
    min_length_yards: float
    max_length_yards: float
    preview_rows: List[RollPreviewRow]  # First 10 rows
    # Shortfall info (if cutplan_id provided)
    fabric_required_yards: Optional[float] = None
    shortfall_yards: Optional[float] = None
    synthetic_rolls_needed: Optional[int] = None
    synthetic_roll_length_yards: Optional[float] = None  # median


# ---------------------------------------------------------------------------
# Tune Cutplan
# ---------------------------------------------------------------------------


class TuneCutplanRequest(BaseModel):
    avg_roll_length_yards: Optional[float] = None   # Auto-derived from pseudo config if None
    roll_penalty_weight: float = 2.0


class TuneStatusResponse(BaseModel):
    status: str           # "running", "completed", "failed"
    progress: int
    message: str
    new_cutplan_id: Optional[str] = None
