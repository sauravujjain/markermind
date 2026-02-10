from sqlalchemy import Column, String, ForeignKey, Float
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
from ..database import Base
from .base import TimestampMixin, generate_uuid


class Fabric(Base, TimestampMixin):
    """Fabric reference data."""
    __tablename__ = "fabrics"

    id = Column(UUID(as_uuid=False), primary_key=True, default=generate_uuid)
    customer_id = Column(UUID(as_uuid=False), ForeignKey("customers.id"), nullable=False)
    name = Column(String(255), nullable=False)
    code = Column(String(50), nullable=False)  # Fabric code like "DENIM-001"
    width_inches = Column(Float, nullable=False)  # Usable width in inches
    cost_per_yard = Column(Float, default=0.0)  # Cost per linear yard
    description = Column(String(500))

    # Relationships
    customer = relationship("Customer", back_populates="fabrics")
    pattern_mappings = relationship("PatternFabricMapping", back_populates="fabric")
    order_lines = relationship("OrderLine", back_populates="fabric")
    markers = relationship("MarkerBank", back_populates="fabric")
