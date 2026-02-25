from sqlalchemy import Column, String, ForeignKey, Boolean, JSON, Enum as SQLEnum
from sqlalchemy.dialects.postgresql import UUID, ARRAY
from sqlalchemy.orm import relationship
from ..database import Base
from .base import TimestampMixin, generate_uuid
import enum


class FileType(str, enum.Enum):
    AAMA = "aama"  # AAMA/ASTM DXF format
    GERBER = "gerber"
    LECTRA = "lectra"
    DXF_ONLY = "dxf_only"  # Pre-sized pieces in DXF, no RUL grading needed


class Pattern(Base, TimestampMixin):
    """Pattern file and parsed data."""
    __tablename__ = "patterns"

    id = Column(UUID(as_uuid=False), primary_key=True, default=generate_uuid)
    customer_id = Column(UUID(as_uuid=False), ForeignKey("customers.id"), nullable=False)
    name = Column(String(255), nullable=False)
    file_type = Column(SQLEnum(FileType, values_callable=lambda x: [e.value for e in x]), default=FileType.AAMA, nullable=False)
    dxf_file_path = Column(String(500))  # Path to uploaded DXF file
    rul_file_path = Column(String(500))  # Path to uploaded RUL file (for AAMA)
    is_parsed = Column(Boolean, default=False)
    available_sizes = Column(ARRAY(String), default=list)  # Sizes found in pattern
    available_materials = Column(ARRAY(String), default=list)  # Materials/fabrics found
    parse_metadata = Column(JSON, default=dict)  # Piece counts, bounding boxes, etc.

    # Relationships
    customer = relationship("Customer", back_populates="patterns")
    fabric_mappings = relationship("PatternFabricMapping", back_populates="pattern", cascade="all, delete-orphan")
    orders = relationship("Order", back_populates="pattern")
    nesting_jobs = relationship("NestingJob", back_populates="pattern")
    markers = relationship("MarkerBank", back_populates="pattern")


class PatternFabricMapping(Base, TimestampMixin):
    """Maps pattern materials to customer fabrics."""
    __tablename__ = "pattern_fabric_mappings"

    id = Column(UUID(as_uuid=False), primary_key=True, default=generate_uuid)
    pattern_id = Column(UUID(as_uuid=False), ForeignKey("patterns.id"), nullable=False)
    material_name = Column(String(100), nullable=False)  # Material name from pattern file
    fabric_id = Column(UUID(as_uuid=False), ForeignKey("fabrics.id"))  # Mapped fabric

    # Relationships
    pattern = relationship("Pattern", back_populates="fabric_mappings")
    fabric = relationship("Fabric", back_populates="pattern_mappings")
