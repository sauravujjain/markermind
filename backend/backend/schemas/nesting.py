from pydantic import BaseModel
from typing import Optional, List
from datetime import datetime


class NestingJobCreate(BaseModel):
    order_id: str
    pattern_id: str
    fabric_width_inches: float
    max_bundle_count: int = 6
    top_n_results: int = 10


class NestingJobResultResponse(BaseModel):
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
    results: List[NestingJobResultResponse] = []
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
