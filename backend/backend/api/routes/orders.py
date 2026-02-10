from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, status, UploadFile, File
from sqlalchemy.orm import Session
import pandas as pd
import io

from ...database import get_db
from ...schemas.order import (
    OrderCreate, OrderUpdate, OrderResponse,
    OrderLineCreate, OrderLineResponse,
    SizeQuantityCreate, SizeQuantityResponse,
    OrderImportRequest, OrderImportRow
)
from ...models import User, Order, OrderLine, SizeQuantity
from ..deps import get_current_user

router = APIRouter(prefix="/orders", tags=["orders"])


@router.get("", response_model=List[OrderResponse])
async def list_orders(
    status: Optional[str] = None,
    skip: int = 0,
    limit: int = 100,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """List all orders for the current customer."""
    query = db.query(Order).filter(Order.customer_id == current_user.customer_id)
    if status:
        query = query.filter(Order.status == status)
    orders = query.offset(skip).limit(limit).all()
    return orders


@router.post("", response_model=OrderResponse, status_code=status.HTTP_201_CREATED)
async def create_order(
    order_data: OrderCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Create a new order."""
    # Create order
    order = Order(
        customer_id=current_user.customer_id,
        order_number=order_data.order_number,
        style_number=order_data.style_number,
        style_id=order_data.style_id,
        pattern_id=order_data.pattern_id,
        piece_buffer_mm=order_data.piece_buffer_mm,
        edge_buffer_mm=order_data.edge_buffer_mm,
        rotation_mode=order_data.rotation_mode,
    )
    db.add(order)
    db.flush()

    # Create lines and quantities
    for line_data in order_data.lines:
        line = OrderLine(
            order_id=order.id,
            fabric_code=line_data.fabric_code,
            color_code=line_data.color_code,
            extra_percent=line_data.extra_percent,
            fabric_id=line_data.fabric_id,
        )
        db.add(line)
        db.flush()

        for qty_data in line_data.quantities:
            qty = SizeQuantity(
                order_line_id=line.id,
                size_code=qty_data.size_code,
                quantity=qty_data.quantity,
            )
            db.add(qty)

    db.commit()
    db.refresh(order)
    return order


@router.get("/{order_id}", response_model=OrderResponse)
async def get_order(
    order_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Get order by ID."""
    order = db.query(Order).filter(
        Order.id == order_id,
        Order.customer_id == current_user.customer_id
    ).first()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    return order


@router.put("/{order_id}", response_model=OrderResponse)
async def update_order(
    order_id: str,
    order_data: OrderUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Update an order."""
    order = db.query(Order).filter(
        Order.id == order_id,
        Order.customer_id == current_user.customer_id
    ).first()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    update_data = order_data.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(order, field, value)

    db.commit()
    db.refresh(order)
    return order


@router.delete("/{order_id}")
async def delete_order(
    order_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Delete an order."""
    order = db.query(Order).filter(
        Order.id == order_id,
        Order.customer_id == current_user.customer_id
    ).first()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    db.delete(order)
    db.commit()
    return {"message": "Order deleted"}


@router.post("/import-batch", response_model=List[OrderResponse])
async def import_orders_batch(
    request: OrderImportRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    Import multiple orders from parsed Excel data.

    The frontend parses the Excel and sends structured JSON data.
    This allows batch import of all orders at once.
    """
    if not request.rows:
        raise HTTPException(status_code=400, detail="No order data provided")

    # Group rows by order number
    orders_data = {}
    for row in request.rows:
        order_key = row.order_number.lower()
        if order_key not in orders_data:
            orders_data[order_key] = {
                "order_number": row.order_number,
                "style_number": row.style_number,
                "lines": []
            }
        orders_data[order_key]["lines"].append(row)

    # Create each order
    created_orders = []
    for order_data in orders_data.values():
        # Check if order already exists
        existing = db.query(Order).filter(
            Order.customer_id == current_user.customer_id,
            Order.order_number == order_data["order_number"]
        ).first()
        if existing:
            raise HTTPException(
                status_code=400,
                detail=f"Order '{order_data['order_number']}' already exists"
            )

        # Create order
        order = Order(
            customer_id=current_user.customer_id,
            order_number=order_data["order_number"],
            style_number=order_data["style_number"],
        )
        db.add(order)
        db.flush()

        # Create lines and quantities
        for row in order_data["lines"]:
            line = OrderLine(
                order_id=order.id,
                fabric_code=row.fabric_code,
                color_code=row.color_code,
                extra_percent=row.extra_percent,
            )
            db.add(line)
            db.flush()

            for size_code, qty in row.sizes.items():
                if qty > 0:
                    sq = SizeQuantity(
                        order_line_id=line.id,
                        size_code=size_code,
                        quantity=qty,
                    )
                    db.add(sq)

        created_orders.append(order)

    db.commit()

    # Refresh all orders to get relationships
    for order in created_orders:
        db.refresh(order)

    return created_orders


@router.post("/import", response_model=OrderResponse)
async def import_order(
    file: UploadFile = File(...),
    order_number: str = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Import single order from Excel/CSV file (legacy endpoint)."""
    # Read file
    content = await file.read()

    try:
        if file.filename.endswith('.csv'):
            df = pd.read_csv(io.BytesIO(content))
        else:
            df = pd.read_excel(io.BytesIO(content))
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to parse file: {str(e)}")

    # Expected format: Color, Size1, Size2, Size3, ...
    # First column is color code, remaining columns are sizes
    if len(df.columns) < 2:
        raise HTTPException(status_code=400, detail="File must have at least 2 columns (color + sizes)")

    color_col = df.columns[0]
    size_cols = list(df.columns[1:])

    # Create order
    if not order_number:
        order_number = file.filename.rsplit('.', 1)[0]

    order = Order(
        customer_id=current_user.customer_id,
        order_number=order_number,
    )
    db.add(order)
    db.flush()

    # Create lines and quantities
    for _, row in df.iterrows():
        color_code = str(row[color_col]).strip()
        if not color_code or pd.isna(color_code):
            continue

        line = OrderLine(
            order_id=order.id,
            fabric_code="DEFAULT",  # Legacy format doesn't have fabric column
            color_code=color_code,
        )
        db.add(line)
        db.flush()

        for size_col in size_cols:
            qty = row[size_col]
            if pd.notna(qty) and int(qty) > 0:
                sq = SizeQuantity(
                    order_line_id=line.id,
                    size_code=str(size_col),
                    quantity=int(qty),
                )
                db.add(sq)

    db.commit()
    db.refresh(order)
    return order


@router.get("/{order_id}/lines", response_model=List[OrderLineResponse])
async def list_order_lines(
    order_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """List lines for an order."""
    order = db.query(Order).filter(
        Order.id == order_id,
        Order.customer_id == current_user.customer_id
    ).first()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    return order.order_lines


@router.post("/{order_id}/lines", response_model=OrderLineResponse, status_code=status.HTTP_201_CREATED)
async def add_order_line(
    order_id: str,
    line_data: OrderLineCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Add a line (fabric + color) to an order."""
    order = db.query(Order).filter(
        Order.id == order_id,
        Order.customer_id == current_user.customer_id
    ).first()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    line = OrderLine(
        order_id=order.id,
        fabric_code=line_data.fabric_code,
        color_code=line_data.color_code,
        extra_percent=line_data.extra_percent,
        fabric_id=line_data.fabric_id,
    )
    db.add(line)
    db.flush()

    for qty_data in line_data.quantities:
        qty = SizeQuantity(
            order_line_id=line.id,
            size_code=qty_data.size_code,
            quantity=qty_data.quantity,
        )
        db.add(qty)

    db.commit()
    db.refresh(line)
    return line


@router.put("/{order_id}/lines/{line_id}", response_model=OrderLineResponse)
async def update_order_line(
    order_id: str,
    line_id: str,
    quantities: List[SizeQuantityCreate],
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Update quantities for an order line."""
    line = db.query(OrderLine).join(Order).filter(
        OrderLine.id == line_id,
        Order.id == order_id,
        Order.customer_id == current_user.customer_id
    ).first()
    if not line:
        raise HTTPException(status_code=404, detail="Line not found")

    # Delete existing quantities
    db.query(SizeQuantity).filter(SizeQuantity.order_line_id == line.id).delete()

    # Add new quantities
    for qty_data in quantities:
        qty = SizeQuantity(
            order_line_id=line.id,
            size_code=qty_data.size_code,
            quantity=qty_data.quantity,
        )
        db.add(qty)

    db.commit()
    db.refresh(line)
    return line
