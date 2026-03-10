from sqlalchemy import Column, String, ForeignKey, Float, Enum as SQLEnum, JSON, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
from ..database import Base
from .base import TimestampMixin, generate_uuid
import enum


class MarkerSourceType(str, enum.Enum):
    gpu_nesting = "gpu_nesting"  # From GPU raster nesting
    spyrrow = "spyrrow"  # Refined with Spyrrow CPU solver
    manual = "manual"  # Manually entered
    imported = "imported"  # Imported from external system


class MarkerBank(Base, TimestampMixin):
    """Bank of evaluated markers for ILP selection."""
    __tablename__ = "marker_bank"

    id = Column(UUID(as_uuid=False), primary_key=True, default=generate_uuid)
    pattern_id = Column(UUID(as_uuid=False), ForeignKey("patterns.id"), nullable=False)
    fabric_id = Column(UUID(as_uuid=False), ForeignKey("fabrics.id"), nullable=True)  # Nullable for general markers
    ratio_str = Column(String(50), nullable=False)  # e.g., "1-2-1-0-0-1-0"
    efficiency = Column(Float, nullable=False)  # 0-100%
    length_yards = Column(Float, nullable=False)
    length_mm = Column(Float)
    fabric_width_inches = Column(Float, nullable=True)  # Fabric width this marker was evaluated at
    source_type = Column(SQLEnum(MarkerSourceType, name="markersourcetype", create_type=False), default=MarkerSourceType.gpu_nesting)
    extra_data = Column(JSON, default=dict)  # Additional data (sorting strategy, etc.)

    # Relationships
    pattern = relationship("Pattern", back_populates="markers")
    fabric = relationship("Fabric", back_populates="markers")
    cutplan_markers = relationship("CutplanMarker", back_populates="marker")
