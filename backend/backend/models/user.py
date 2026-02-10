from sqlalchemy import Column, String, ForeignKey, DateTime, JSON, Enum as SQLEnum
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
from ..database import Base
from .base import TimestampMixin, generate_uuid
import enum


class UserRole(str, enum.Enum):
    ADMIN = "admin"
    MANAGER = "manager"
    OPERATOR = "operator"


class Customer(Base, TimestampMixin):
    """Customer/Tenant table for multi-tenancy."""
    __tablename__ = "customers"

    id = Column(UUID(as_uuid=False), primary_key=True, default=generate_uuid)
    name = Column(String(255), nullable=False)
    code = Column(String(50), unique=True, nullable=False)  # Short code like "ACME"
    settings = Column(JSON, default=dict)  # Customer-specific settings

    # Relationships
    users = relationship("User", back_populates="customer", cascade="all, delete-orphan")
    fabrics = relationship("Fabric", back_populates="customer", cascade="all, delete-orphan")
    styles = relationship("Style", back_populates="customer", cascade="all, delete-orphan")
    patterns = relationship("Pattern", back_populates="customer", cascade="all, delete-orphan")
    orders = relationship("Order", back_populates="customer", cascade="all, delete-orphan")
    cost_configs = relationship("CostConfig", back_populates="customer", cascade="all, delete-orphan")


class User(Base, TimestampMixin):
    """User table for authentication."""
    __tablename__ = "users"

    id = Column(UUID(as_uuid=False), primary_key=True, default=generate_uuid)
    customer_id = Column(UUID(as_uuid=False), ForeignKey("customers.id"), nullable=False)
    email = Column(String(255), unique=True, nullable=False, index=True)
    password_hash = Column(String(255), nullable=False)
    name = Column(String(255), nullable=False)
    role = Column(SQLEnum(UserRole, values_callable=lambda x: [e.value for e in x]), default=UserRole.OPERATOR, nullable=False)
    is_active = Column(String(1), default="Y", nullable=False)  # Y/N for active status

    # Relationships
    customer = relationship("Customer", back_populates="users")
    sessions = relationship("Session", back_populates="user", cascade="all, delete-orphan")


class Session(Base, TimestampMixin):
    """Session table for token management."""
    __tablename__ = "sessions"

    id = Column(UUID(as_uuid=False), primary_key=True, default=generate_uuid)
    user_id = Column(UUID(as_uuid=False), ForeignKey("users.id"), nullable=False)
    token_hash = Column(String(255), nullable=False, index=True)
    expires_at = Column(DateTime(timezone=True), nullable=False)

    # Relationships
    user = relationship("User", back_populates="sessions")
