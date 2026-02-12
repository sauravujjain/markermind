from sqlalchemy import Column, String, ForeignKey, Float, Integer, Boolean
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
from ..database import Base
from .base import TimestampMixin, generate_uuid


class CostConfig(Base, TimestampMixin):
    """Customer-specific cost configuration."""
    __tablename__ = "cost_configs"

    id = Column(UUID(as_uuid=False), primary_key=True, default=generate_uuid)
    customer_id = Column(UUID(as_uuid=False), ForeignKey("customers.id"), nullable=False)
    name = Column(String(100), default="Default")  # Config name

    # Per-yard costs
    fabric_cost_per_yard = Column(Float, default=3.0)  # Base fabric cost per yard
    spreading_cost_per_yard = Column(Float, default=0.00122)  # Spreading cost per yard of fabric spread
    spreading_cost_per_ply = Column(Float, default=0.013)  # Spreading cost per ply (layer)

    # Spreading input parameters (used to calculate spreading_cost_per_yard and spreading_cost_per_ply)
    spreading_labor_cost_per_hour = Column(Float, default=1.0)  # USD per hour
    spreading_speed_m_per_min = Column(Float, default=20.0)  # meters per minute
    spreading_prep_buffer_pct = Column(Float, default=20.0)  # preparation/wait buffer %
    spreading_workers_per_lay = Column(Integer, default=2)  # number of workers per lay
    ply_end_cut_time_s = Column(Float, default=20.0)  # seconds per ply end cut

    # Per-operation costs
    cutting_cost_per_inch = Column(Float, default=0.000424)  # Per linear inch of perimeter per cut
    prep_cost_per_marker = Column(Float, default=0.03)  # Paper/print cost per unique marker

    # Cutting input parameters (used to calculate cutting_cost_per_inch)
    cutting_speed_cm_per_s = Column(Float, default=10.0)  # cm per second
    cutting_labor_cost_per_hour = Column(Float, default=1.0)  # USD per hour
    cutting_workers_per_cut = Column(Integer, default=1)  # number of workers per cut

    # Preparatory cost input parameters (per meter of marker)
    prep_cost_per_meter = Column(Float, default=0.25)  # Calculated total prep cost per meter
    prep_perf_paper_cost_per_m = Column(Float, default=0.1)  # Perforated paper for garment (Auto CNC)
    prep_perf_paper_enabled = Column(Boolean, default=True)
    prep_underlayer_cost_per_m = Column(Float, default=0.1)  # Perforated underlayer paper (Auto Cutter)
    prep_underlayer_enabled = Column(Boolean, default=True)
    prep_top_layer_cost_per_m = Column(Float, default=0.05)  # Top layer paper (Auto Cutter)
    prep_top_layer_enabled = Column(Boolean, default=True)

    # Constraints
    max_ply_height = Column(Integer, default=100)  # Max layers per cut
    min_plies_by_bundle = Column(String(255), default="6:50,5:40,4:30,3:10,2:1,1:1")  # e.g., "6:50,5:40,..."

    # Relationships
    customer = relationship("Customer", back_populates="cost_configs")
