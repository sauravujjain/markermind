from sqlalchemy import Column, String, Integer, ForeignKey, Float, Text, Boolean, JSON, LargeBinary
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
from ..database import Base
from .base import TimestampMixin, generate_uuid


class TestMarkerResult(Base, TimestampMixin):
    """Persisted test marker nesting result for experiment comparison."""
    __tablename__ = "test_marker_results"

    id = Column(UUID(as_uuid=False), primary_key=True, default=generate_uuid)
    pattern_id = Column(UUID(as_uuid=False), ForeignKey("patterns.id"), nullable=False)
    order_id = Column(UUID(as_uuid=False), ForeignKey("orders.id"), nullable=True)
    created_by = Column(UUID(as_uuid=False), ForeignKey("users.id"), nullable=False)

    # Ratio
    ratio_str = Column(String(100), nullable=False)           # e.g. "0-2-1-0-1-0-0"
    size_bundles = Column(JSON, nullable=False)                # {"S": 2, "M": 1, "XL": 1}
    bundle_count = Column(Integer, nullable=False)
    material = Column(String(50), nullable=True)

    # Results
    efficiency = Column(Float, nullable=False)                 # 0-1
    length_mm = Column(Float, nullable=False)
    length_yards = Column(Float, nullable=False)
    fabric_width_mm = Column(Float, nullable=False)
    piece_count = Column(Integer, nullable=False)
    computation_time_ms = Column(Float, nullable=False)
    svg_preview = Column(Text, nullable=True)
    dxf_data = Column(LargeBinary, nullable=True)

    # Nesting params snapshot
    time_limit_s = Column(Float, nullable=False)
    quadtree_depth = Column(Integer, nullable=False)
    early_termination = Column(Boolean, nullable=False)
    piece_buffer_mm = Column(Float, nullable=False)
    edge_buffer_mm = Column(Float, nullable=False)
    orientation = Column(String(20), nullable=False)
    exploration_time_s = Column(Integer, nullable=True)        # custom split
    compression_time_s = Column(Integer, nullable=True)        # custom split
    use_cloud = Column(Boolean, nullable=False, server_default='false')
    seed_used = Column(Integer, nullable=True)
    seed_screening = Column(Boolean, nullable=False, server_default='false')

    # User annotation
    notes = Column(Text, nullable=True)

    # Relationships
    pattern = relationship("Pattern")
    order = relationship("Order")
    user = relationship("User")
