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
    time_limit: float = 10.0      # seconds (1-60)
    piece_buffer_mm: float = 2.0   # gap between pieces in mm (0-10)
    edge_buffer_mm: float = 5.0    # gap from container edge in mm (0-20)
    orientation: str = "free"      # "free" or "nap_one_way"


class TestMarkerResponse(BaseModel):
    """Response from a quick CPU test marker nest."""
    efficiency: float              # 0.0 - 1.0
    length_mm: float
    length_yards: float
    piece_count: int
    bundle_count: int
    ratio_str: str                 # e.g., "0-0-1-0-1-0-0"
    computation_time_ms: float
    svg_preview: Optional[str] = None  # SVG string of the marker layout
