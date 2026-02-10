from sqlalchemy import Column, String, ForeignKey
from sqlalchemy.dialects.postgresql import UUID, ARRAY
from sqlalchemy.orm import relationship
from ..database import Base
from .base import TimestampMixin, generate_uuid


class Style(Base, TimestampMixin):
    """Style/Garment reference data."""
    __tablename__ = "styles"

    id = Column(UUID(as_uuid=False), primary_key=True, default=generate_uuid)
    customer_id = Column(UUID(as_uuid=False), ForeignKey("customers.id"), nullable=False)
    style_number = Column(String(100), nullable=False)
    name = Column(String(255))
    description = Column(String(500))
    size_range = Column(ARRAY(String), default=list)  # e.g., ["XS", "S", "M", "L", "XL"]

    # Relationships
    customer = relationship("Customer", back_populates="styles")
    orders = relationship("Order", back_populates="style")
