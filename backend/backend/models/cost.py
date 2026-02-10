from sqlalchemy import Column, String, ForeignKey, Float, Integer
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
    fabric_cost_per_yard = Column(Float, default=5.0)  # Base fabric cost if not specified
    spreading_cost_per_yard = Column(Float, default=0.10)  # Labor for spreading

    # Per-operation costs
    cutting_cost_per_inch = Column(Float, default=0.01)  # Per linear inch of cut
    prep_cost_per_marker = Column(Float, default=2.0)  # Setup cost per marker

    # Constraints
    max_ply_height = Column(Integer, default=100)  # Max layers per cut
    min_plies_by_bundle = Column(String(255), default="6:50,5:40,4:30,3:10,2:1,1:1")  # e.g., "6:50,5:40,..."

    # Relationships
    customer = relationship("Customer", back_populates="cost_configs")
