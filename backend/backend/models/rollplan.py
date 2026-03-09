from sqlalchemy import Column, String, ForeignKey, Integer, Float, Boolean, Enum as SQLEnum, JSON, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
from ..database import Base
from .base import TimestampMixin, generate_uuid
import enum


class RollPlanStatus(str, enum.Enum):
    pending = "pending"
    running = "running"
    completed = "completed"
    failed = "failed"
    cancelled = "cancelled"


class RollPlanMode(str, enum.Enum):
    monte_carlo = "monte_carlo"
    ga = "ga"
    both = "both"


class RollInputType(str, enum.Enum):
    pseudo = "pseudo"
    real = "real"
    mixed = "mixed"


class RollPlan(Base, TimestampMixin):
    """Roll plan: simulation config + results for a cutplan color."""
    __tablename__ = "roll_plans"

    id = Column(UUID(as_uuid=False), primary_key=True, default=generate_uuid)
    cutplan_id = Column(UUID(as_uuid=False), ForeignKey("cutplans.id"), nullable=False)
    name = Column(String(200))
    color_code = Column(String(50), nullable=True)

    # Status
    status = Column(
        SQLEnum(RollPlanStatus, name="rollplanstatus", create_type=False),
        default=RollPlanStatus.pending,
    )
    mode = Column(
        SQLEnum(RollPlanMode, name="rollplanmode", create_type=False),
        default=RollPlanMode.both,
    )

    # Simulation config
    num_simulations = Column(Integer, default=100)
    min_reuse_length_yards = Column(Float, default=0.5)
    input_type = Column(
        SQLEnum(RollInputType, name="rollinputtype", create_type=False),
        default=RollInputType.pseudo,
    )
    pseudo_roll_avg_yards = Column(Float, default=100.0)
    pseudo_roll_delta_yards = Column(Float, default=20.0)

    # Progress
    progress = Column(Integer, default=0)
    progress_message = Column(String(500), default="")
    error_message = Column(Text, nullable=True)

    # Results: Monte Carlo — waste breakdown per category (avg across runs)
    total_fabric_required = Column(Float, nullable=True)
    # Type 1: unusable scraps (< yield per garment)
    mc_unusable_avg = Column(Float, nullable=True)
    mc_unusable_std = Column(Float, nullable=True)
    mc_unusable_p95 = Column(Float, nullable=True)
    # Type 2: end-bit waste (optimization target)
    mc_endbit_avg = Column(Float, nullable=True)
    mc_endbit_std = Column(Float, nullable=True)
    mc_endbit_p95 = Column(Float, nullable=True)
    # Type 3: returnable to warehouse (>= longest marker)
    mc_returnable_avg = Column(Float, nullable=True)
    mc_returnable_std = Column(Float, nullable=True)
    mc_returnable_p95 = Column(Float, nullable=True)
    # Real waste (Type 1 + Type 2) combined
    mc_real_waste_avg = Column(Float, nullable=True)
    mc_real_waste_std = Column(Float, nullable=True)
    mc_real_waste_p95 = Column(Float, nullable=True)
    mc_simulation_runs = Column(JSON, nullable=True)       # Summary per run
    mc_best_run_dockets = Column(JSON, nullable=True)      # CutDockets from best run

    # Results: GA Optimizer — waste breakdown
    ga_unusable_yards = Column(Float, nullable=True)
    ga_endbit_yards = Column(Float, nullable=True)
    ga_returnable_yards = Column(Float, nullable=True)
    ga_real_waste_yards = Column(Float, nullable=True)
    ga_generations_run = Column(Integer, nullable=True)
    ga_dockets = Column(JSON, nullable=True)               # CutDockets from GA
    preflight_warnings = Column(JSON, nullable=True)      # Pre-flight validation warnings

    # Relationships
    cutplan = relationship("Cutplan", back_populates="roll_plans")
    rolls = relationship("FabricRoll", back_populates="roll_plan", cascade="all, delete-orphan")


class FabricRoll(Base, TimestampMixin):
    """Uploaded fabric roll inventory."""
    __tablename__ = "fabric_rolls"

    id = Column(UUID(as_uuid=False), primary_key=True, default=generate_uuid)
    roll_plan_id = Column(UUID(as_uuid=False), ForeignKey("roll_plans.id"), nullable=False)
    roll_number = Column(String(100), nullable=False)
    length_yards = Column(Float, nullable=False)
    is_pseudo = Column(Boolean, default=False)

    # Future fields (stored but not used in V1)
    width_inches = Column(Float, nullable=True)
    shrinkage_x_pct = Column(Float, nullable=True)
    shrinkage_y_pct = Column(Float, nullable=True)
    shade_group = Column(String(50), nullable=True)

    # Relationships
    roll_plan = relationship("RollPlan", back_populates="rolls")
