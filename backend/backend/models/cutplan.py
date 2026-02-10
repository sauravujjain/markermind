from sqlalchemy import Column, String, ForeignKey, Integer, Float, Enum as SQLEnum, JSON
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
from ..database import Base
from .base import TimestampMixin, generate_uuid
import enum


class CutplanStatus(str, enum.Enum):
    draft = "draft"
    optimizing = "optimizing"
    ready = "ready"
    approved = "approved"
    in_production = "in_production"
    completed = "completed"


class SolverType(str, enum.Enum):
    single_color = "single_color"  # Option E balanced ILP
    multicolor_joint = "multicolor_joint"  # Joint optimization across colors
    two_stage = "two_stage"  # Two-stage solver


class Cutplan(Base, TimestampMixin):
    """Optimized cutting plan for an order."""
    __tablename__ = "cutplans"

    id = Column(UUID(as_uuid=False), primary_key=True, default=generate_uuid)
    order_id = Column(UUID(as_uuid=False), ForeignKey("orders.id"), nullable=False)
    name = Column(String(100))  # e.g., "Option A - Max Efficiency"
    solver_type = Column(SQLEnum(SolverType, name="solvertype", create_type=False), default=SolverType.single_color)
    status = Column(SQLEnum(CutplanStatus, name="cutplanstatus", create_type=False), default=CutplanStatus.draft)

    # Summary metrics
    unique_markers = Column(Integer)
    total_cuts = Column(Integer)
    bundle_cuts = Column(Integer)
    total_plies = Column(Integer)
    total_yards = Column(Float)
    efficiency = Column(Float)  # Weighted average efficiency

    # Cost breakdown
    total_cost = Column(Float)
    fabric_cost = Column(Float)
    spreading_cost = Column(Float)
    cutting_cost = Column(Float)
    prep_cost = Column(Float)

    # Solver parameters (for reproducibility)
    solver_config = Column(JSON, default=dict)

    # Relationships
    order = relationship("Order", back_populates="cutplans")
    markers = relationship("CutplanMarker", back_populates="cutplan", cascade="all, delete-orphan")


class CutplanMarker(Base, TimestampMixin):
    """Marker selection within a cutplan."""
    __tablename__ = "cutplan_markers"

    id = Column(UUID(as_uuid=False), primary_key=True, default=generate_uuid)
    cutplan_id = Column(UUID(as_uuid=False), ForeignKey("cutplans.id"), nullable=False)
    marker_id = Column(UUID(as_uuid=False), ForeignKey("marker_bank.id"))
    ratio_str = Column(String(50), nullable=False)  # Denormalized for convenience
    efficiency = Column(Float)
    length_yards = Column(Float)

    # Ply assignments by color (JSON: {"NAVY": 50, "BLACK": 30})
    plies_by_color = Column(JSON, default=dict)
    total_plies = Column(Integer)
    cuts = Column(Integer)  # ceil(total_plies / max_ply_height)

    # Relationships
    cutplan = relationship("Cutplan", back_populates="markers")
    marker = relationship("MarkerBank", back_populates="cutplan_markers")
