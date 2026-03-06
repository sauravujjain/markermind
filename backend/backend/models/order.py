from sqlalchemy import Column, String, ForeignKey, Integer, Float, Enum as SQLEnum
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
from ..database import Base
from .base import TimestampMixin, generate_uuid
import enum


class OrderStatus(str, enum.Enum):
    DRAFT = "draft"
    PENDING_PATTERN = "pending_pattern"
    PENDING_NESTING = "pending_nesting"
    NESTING_IN_PROGRESS = "nesting_in_progress"
    PENDING_CUTPLAN = "pending_cutplan"
    CUTPLAN_READY = "cutplan_ready"
    APPROVED = "approved"
    COMPLETED = "completed"


class RotationMode(str, enum.Enum):
    FREE = "free"  # 0° or 180° per piece
    NAP_SAFE = "nap_safe"  # All pieces same direction
    GARMENT_LINKED = "garment_linked"  # Pieces of same garment rotate together


class Order(Base, TimestampMixin):
    """Customer order with size/color quantities."""
    __tablename__ = "orders"

    id = Column(UUID(as_uuid=False), primary_key=True, default=generate_uuid)
    customer_id = Column(UUID(as_uuid=False), ForeignKey("customers.id"), nullable=False)
    order_number = Column(String(100), nullable=False, index=True)
    style_number = Column(String(100))  # Style No. from Excel (e.g., "style1")
    style_id = Column(UUID(as_uuid=False), ForeignKey("styles.id"))
    pattern_id = Column(UUID(as_uuid=False), ForeignKey("patterns.id"))
    status = Column(SQLEnum(OrderStatus, values_callable=lambda x: [e.value for e in x]), default=OrderStatus.DRAFT, nullable=False)

    # Nesting parameters
    piece_buffer_mm = Column(Float, default=0.0)  # Gap between pieces
    edge_buffer_mm = Column(Float, default=0.0)  # Gap from fabric edge
    rotation_mode = Column(SQLEnum(RotationMode, values_callable=lambda x: [e.value for e in x]), default=RotationMode.FREE)

    # Relationships
    customer = relationship("Customer", back_populates="orders")
    style = relationship("Style", back_populates="orders")
    pattern = relationship("Pattern", back_populates="orders")
    order_lines = relationship("OrderLine", back_populates="order", cascade="all, delete-orphan")
    nesting_jobs = relationship("NestingJob", back_populates="order", cascade="all, delete-orphan")
    cutplans = relationship("Cutplan", back_populates="order", cascade="all, delete-orphan")


class OrderLine(Base, TimestampMixin):
    """
    Each line in the order Excel - unique (fabric, color) combination.

    Maps to Excel columns:
    - fabric_code: "Fabric" column (SO1, SO2, FO1, Shell)
    - color_code: "Order Color" column (8320, 8535, Red)
    - extra_percent: "Extra %" column (0, 3, etc.)
    """
    __tablename__ = "order_lines"

    id = Column(UUID(as_uuid=False), primary_key=True, default=generate_uuid)
    order_id = Column(UUID(as_uuid=False), ForeignKey("orders.id"), nullable=False)

    # From Excel columns
    fabric_code = Column(String(50), nullable=False)  # "Fabric" column: SO1, SO2, FO1, Shell
    color_code = Column(String(50), nullable=False)   # "Order Color" column: 8320, 8535, Red
    extra_percent = Column(Float, default=0.0)        # "Extra %" column: 0, 3, etc.

    # Optional: link to master data
    fabric_id = Column(UUID(as_uuid=False), ForeignKey("fabrics.id"))

    # Relationships
    order = relationship("Order", back_populates="order_lines")
    fabric = relationship("Fabric", back_populates="order_lines")
    size_quantities = relationship("SizeQuantity", back_populates="order_line", cascade="all, delete-orphan", order_by="SizeQuantity.sort_order")


class SizeQuantity(Base, TimestampMixin):
    """Quantity per size within an order line."""
    __tablename__ = "size_quantities"

    id = Column(UUID(as_uuid=False), primary_key=True, default=generate_uuid)
    order_line_id = Column(UUID(as_uuid=False), ForeignKey("order_lines.id"), nullable=False)
    size_code = Column(String(20), nullable=False)  # e.g., "M", "L", "42", "46"
    quantity = Column(Integer, default=0, nullable=False)
    sort_order = Column(Integer, default=0, nullable=False)  # Preserves column order from Excel import

    # Relationships
    order_line = relationship("OrderLine", back_populates="size_quantities")


# Keep old name as alias for backward compatibility during migration
OrderColor = OrderLine
