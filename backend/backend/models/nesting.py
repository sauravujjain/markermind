from sqlalchemy import Column, String, ForeignKey, Integer, Float, Enum as SQLEnum, Text, Boolean, JSON
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
from ..database import Base
from .base import TimestampMixin, generate_uuid
import enum


class JobStatus(str, enum.Enum):
    pending = "pending"
    running = "running"
    completed = "completed"
    failed = "failed"
    cancelled = "cancelled"


class NestingJob(Base, TimestampMixin):
    """GPU nesting job for marker evaluation."""
    __tablename__ = "nesting_jobs"

    id = Column(UUID(as_uuid=False), primary_key=True, default=generate_uuid)
    order_id = Column(UUID(as_uuid=False), ForeignKey("orders.id"), nullable=False)
    pattern_id = Column(UUID(as_uuid=False), ForeignKey("patterns.id"), nullable=False)
    status = Column(SQLEnum(JobStatus, name="jobstatus", create_type=False), default=JobStatus.pending, nullable=False)
    progress = Column(Integer, default=0)  # 0-100
    progress_message = Column(String(255))
    error_message = Column(Text)
    celery_task_id = Column(String(100))  # For tracking Celery task

    # Job parameters (stored for reproducibility)
    fabric_width_inches = Column(Float)
    fabric_widths = Column(JSON, nullable=True)  # Multi-width: list of widths in inches (e.g., [54, 58, 62])
    max_bundle_count = Column(Integer, default=6)
    top_n_results = Column(Integer, default=10)  # Top N results per bundle count
    full_coverage = Column(Boolean, default=False)  # If True, evaluate ALL ratios (brute force)
    gpu_scale = Column(Float, default=0.15)  # Rasterization resolution (px/mm). 0.15=fast, 0.3=demo quality
    selected_sizes = Column(JSON, nullable=True)  # Optional subset of pattern sizes to nest (list of strings)
    strategy = Column(String(20), default="auto")  # "auto", "brute_force", "lhs_predict"

    # Relationships
    order = relationship("Order", back_populates="nesting_jobs")
    pattern = relationship("Pattern", back_populates="nesting_jobs")
    results = relationship("NestingJobResult", back_populates="job", cascade="all, delete-orphan")


class NestingJobResult(Base, TimestampMixin):
    """Individual marker result from GPU nesting."""
    __tablename__ = "nesting_job_results"

    id = Column(UUID(as_uuid=False), primary_key=True, default=generate_uuid)
    nesting_job_id = Column(UUID(as_uuid=False), ForeignKey("nesting_jobs.id"), nullable=False)
    bundle_count = Column(Integer, nullable=False)  # 1-6 bundles
    rank = Column(Integer, nullable=False)  # Rank within bundle count (1 = best)
    ratio_str = Column(String(50), nullable=False)  # e.g., "1-2-1-0-0-1-0"
    efficiency = Column(Float, nullable=False)  # 0-100%
    length_yards = Column(Float, nullable=False)
    length_mm = Column(Float)
    fabric_width_inches = Column(Float, nullable=True)  # Width this result was evaluated at
    svg_preview = Column(Text)  # SVG string of marker layout (vector preview)

    # Relationships
    job = relationship("NestingJob", back_populates="results")
