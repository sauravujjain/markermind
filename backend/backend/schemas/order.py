from pydantic import BaseModel
from typing import Optional, List, Dict
from datetime import datetime


class SizeQuantityCreate(BaseModel):
    size_code: str
    quantity: int


class SizeQuantityResponse(BaseModel):
    id: str
    order_line_id: str
    size_code: str
    quantity: int

    class Config:
        from_attributes = True


class OrderLineCreate(BaseModel):
    """
    Create an order line (fabric + color combination).

    Maps to Excel columns:
    - fabric_code: "Fabric" column (SO1, SO2, FO1, Shell)
    - color_code: "Order Color" column (8320, 8535, Red)
    - extra_percent: "Extra %" column (0, 3, etc.)
    """
    fabric_code: str
    color_code: str
    extra_percent: float = 0.0
    fabric_id: Optional[str] = None
    quantities: List[SizeQuantityCreate] = []


class OrderLineResponse(BaseModel):
    id: str
    order_id: str
    fabric_code: str
    color_code: str
    extra_percent: float
    fabric_id: Optional[str]
    size_quantities: List[SizeQuantityResponse] = []

    class Config:
        from_attributes = True


# Backward compatibility aliases
OrderColorCreate = OrderLineCreate
OrderColorResponse = OrderLineResponse


class OrderCreate(BaseModel):
    """
    Create an order.

    Maps to Excel:
    - order_number: "Order No." column
    - style_number: "Style No." column
    """
    order_number: str
    style_number: Optional[str] = None
    style_id: Optional[str] = None
    pattern_id: Optional[str] = None
    piece_buffer_mm: float = 0.0
    edge_buffer_mm: float = 0.0
    rotation_mode: str = "free"
    lines: List[OrderLineCreate] = []


class OrderUpdate(BaseModel):
    order_number: Optional[str] = None
    style_number: Optional[str] = None
    style_id: Optional[str] = None
    pattern_id: Optional[str] = None
    piece_buffer_mm: Optional[float] = None
    edge_buffer_mm: Optional[float] = None
    rotation_mode: Optional[str] = None
    status: Optional[str] = None


class OrderResponse(BaseModel):
    id: str
    customer_id: str
    order_number: str
    style_number: Optional[str]
    style_id: Optional[str]
    pattern_id: Optional[str]
    status: str
    piece_buffer_mm: float
    edge_buffer_mm: float
    rotation_mode: str
    order_lines: List[OrderLineResponse] = []
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class OrderImportRow(BaseModel):
    """
    Single row from Excel/CSV import.

    Excel columns:
    - Order No. → order_number
    - Style No. → style_number
    - Fabric → fabric_code
    - Order Color → color_code
    - Extra % → extra_percent
    - [Size columns] → sizes dict
    """
    order_number: str
    style_number: str
    fabric_code: str
    color_code: str
    extra_percent: float = 0.0
    sizes: Dict[str, int]  # {"46": 74, "48": 244, ...} or {"S": 200, "M": 250, ...}


class OrderImportRequest(BaseModel):
    """Request to import multiple order rows from Excel."""
    rows: List[OrderImportRow]


class OrderSummary(BaseModel):
    """Summary statistics for an order."""
    id: str
    order_number: str
    style_number: Optional[str]
    status: str
    fabric_count: int
    color_count: int
    total_garments: int
    created_at: datetime

    class Config:
        from_attributes = True
