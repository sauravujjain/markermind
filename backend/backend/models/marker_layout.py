from sqlalchemy import Column, String, ForeignKey, Float, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
from ..database import Base
from .base import TimestampMixin, generate_uuid


class MarkerLayout(Base, TimestampMixin):
    """Stores the final CPU-nested layout for a cutplan marker."""
    __tablename__ = "marker_layouts"

    id = Column(UUID(as_uuid=False), primary_key=True, default=generate_uuid)
    cutplan_marker_id = Column(UUID(as_uuid=False), ForeignKey("cutplan_markers.id"), nullable=False, unique=True)
    utilization = Column(Float)          # 0-1
    strip_length_mm = Column(Float)
    length_yards = Column(Float)
    computation_time_s = Column(Float)
    svg_preview = Column(Text)           # SVG string for web display
    dxf_file_path = Column(String(500))  # Path to saved DXF file
    piece_buffer_mm = Column(Float)
    edge_buffer_mm = Column(Float)
    time_limit_s = Column(Float)
    rotation_mode = Column(String(20))

    cutplan_marker = relationship("CutplanMarker", back_populates="layout")
