from pydantic import BaseModel
from typing import Optional, List, Dict
from datetime import datetime


class NestingJobCreate(BaseModel):
    order_id: str
    pattern_id: str
    fabric_width_inches: float
    max_bundle_count: int = 6
    top_n_results: int = 10
    full_coverage: bool = False  # If True, evaluate ALL ratios (brute force / 100% coverage)
    gpu_scale: float = 0.15     # Rasterization resolution (px/mm). Default 0.15, use 0.3 for higher-quality demos
    selected_sizes: Optional[List[str]] = None  # Subset of pattern sizes to nest; None = all sizes


class NestingJobResultResponse(BaseModel):
    id: str
    nesting_job_id: str
    bundle_count: int
    rank: int
    ratio_str: str
    efficiency: float
    length_yards: float
    length_mm: Optional[float]
    svg_preview: Optional[str] = None

    class Config:
        from_attributes = True


class NestingJobResultSummary(BaseModel):
    """Lightweight result without SVG for list endpoints."""
    id: str
    nesting_job_id: str
    bundle_count: int
    rank: int
    ratio_str: str
    efficiency: float
    length_yards: float
    length_mm: Optional[float]

    class Config:
        from_attributes = True


class NestingJobResponse(BaseModel):
    id: str
    order_id: str
    pattern_id: str
    status: str
    progress: int
    progress_message: Optional[str]
    error_message: Optional[str]
    fabric_width_inches: Optional[float]
    max_bundle_count: int
    top_n_results: int
    full_coverage: bool = False
    gpu_scale: float = 0.15
    results: List[NestingJobResultResponse] = []
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class NestingJobListResponse(BaseModel):
    """Lightweight job response for list endpoint — results without SVG."""
    id: str
    order_id: str
    pattern_id: str
    status: str
    progress: int
    progress_message: Optional[str]
    error_message: Optional[str]
    fabric_width_inches: Optional[float]
    max_bundle_count: int
    top_n_results: int
    full_coverage: bool = False
    gpu_scale: float = 0.15
    results: List[NestingJobResultSummary] = []
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class NestingProgressUpdate(BaseModel):
    """WebSocket message for progress updates."""
    job_id: str
    status: str
    progress: int
    message: str
    bundle_count: Optional[int] = None  # Current bundle count being processed


class TestMarkerRequest(BaseModel):
    """Request to run a quick test marker nest."""
    pattern_id: str
    fabric_width_inches: float
    size_bundles: Dict[str, int]  # e.g., {"32": 1, "34": 1}
    material: Optional[str] = None  # e.g., "SHELL" — defaults to first available
    time_limit: float = 120.0     # seconds (max allowed time per marker)
    piece_buffer_mm: float = 0.0   # gap between pieces in mm (0-10)
    edge_buffer_mm: float = 0.0    # gap from container edge in mm (0-20)
    orientation: str = "free"      # "free" or "nap_one_way"
    quadtree_depth: int = 5
    early_termination: bool = True
    exploration_time_s: Optional[int] = None   # custom explore time (seconds)
    compression_time_s: Optional[int] = None   # custom compress time (seconds)
    order_id: Optional[str] = None             # optional order context
    use_cloud: bool = False                    # run on Modal cloud instead of local CPU
    seed_screening: bool = False               # run 6 seeds × 10s to find best seed


class TestMarkerResponse(BaseModel):
    """Response from a quick CPU test marker nest."""
    id: Optional[str] = None       # DB id of saved result
    efficiency: float              # 0.0 - 1.0
    length_mm: float
    length_yards: float
    fabric_width_mm: float         # echo back actual strip width used
    piece_count: int
    bundle_count: int
    ratio_str: str                 # e.g., "0-0-1-0-1-0-0"
    computation_time_ms: float
    svg_preview: Optional[str] = None  # SVG string of the marker layout
    exploration_time_s: Optional[int] = None
    compression_time_s: Optional[int] = None
    use_cloud: bool = False
    seed_used: Optional[int] = None
    seed_screening: bool = False


class TestMarkerResultResponse(BaseModel):
    """Full test marker result from DB."""
    id: str
    pattern_id: str
    order_id: Optional[str] = None
    created_by: str
    ratio_str: str
    size_bundles: Dict[str, int]
    bundle_count: int
    material: Optional[str] = None
    efficiency: float
    length_mm: float
    length_yards: float
    fabric_width_mm: float
    piece_count: int
    computation_time_ms: float
    svg_preview: Optional[str] = None
    time_limit_s: float
    quadtree_depth: int
    early_termination: bool
    piece_buffer_mm: float
    edge_buffer_mm: float
    orientation: str
    exploration_time_s: Optional[int] = None
    compression_time_s: Optional[int] = None
    use_cloud: bool = False
    seed_used: Optional[int] = None
    seed_screening: bool = False
    notes: Optional[str] = None
    created_at: datetime

    class Config:
        from_attributes = True


class TestMarkerResultListItem(BaseModel):
    """Lightweight test marker result without SVG for list endpoints."""
    id: str
    pattern_id: str
    order_id: Optional[str] = None
    ratio_str: str
    size_bundles: Dict[str, int]
    bundle_count: int
    material: Optional[str] = None
    efficiency: float
    length_mm: float
    length_yards: float
    fabric_width_mm: float
    piece_count: int
    computation_time_ms: float
    time_limit_s: float
    quadtree_depth: int
    early_termination: bool
    piece_buffer_mm: float
    edge_buffer_mm: float
    orientation: str
    exploration_time_s: Optional[int] = None
    compression_time_s: Optional[int] = None
    use_cloud: bool = False
    seed_used: Optional[int] = None
    seed_screening: bool = False
    notes: Optional[str] = None
    created_at: datetime

    class Config:
        from_attributes = True


class TestMarkerResultUpdate(BaseModel):
    """Update fields for a saved test marker result."""
    notes: Optional[str] = None
