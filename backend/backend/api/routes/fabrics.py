from typing import List
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from ...database import get_db
from ...schemas.fabric import FabricCreate, FabricUpdate, FabricResponse
from ...models import User, Fabric
from ..deps import get_current_user

router = APIRouter(prefix="/fabrics", tags=["fabrics"])


@router.get("", response_model=List[FabricResponse])
async def list_fabrics(
    skip: int = 0,
    limit: int = 100,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """List all fabrics for the current customer."""
    fabrics = db.query(Fabric).filter(
        Fabric.customer_id == current_user.customer_id
    ).offset(skip).limit(limit).all()
    return fabrics


@router.post("", response_model=FabricResponse, status_code=status.HTTP_201_CREATED)
async def create_fabric(
    fabric_data: FabricCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Create a new fabric."""
    fabric = Fabric(
        customer_id=current_user.customer_id,
        name=fabric_data.name,
        code=fabric_data.code,
        width_inches=fabric_data.width_inches,
        cost_per_yard=fabric_data.cost_per_yard or 0.0,
        description=fabric_data.description,
    )
    db.add(fabric)
    db.commit()
    db.refresh(fabric)
    return fabric


@router.get("/{fabric_id}", response_model=FabricResponse)
async def get_fabric(
    fabric_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Get fabric by ID."""
    fabric = db.query(Fabric).filter(
        Fabric.id == fabric_id,
        Fabric.customer_id == current_user.customer_id
    ).first()
    if not fabric:
        raise HTTPException(status_code=404, detail="Fabric not found")
    return fabric


@router.put("/{fabric_id}", response_model=FabricResponse)
async def update_fabric(
    fabric_id: str,
    fabric_data: FabricUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Update a fabric."""
    fabric = db.query(Fabric).filter(
        Fabric.id == fabric_id,
        Fabric.customer_id == current_user.customer_id
    ).first()
    if not fabric:
        raise HTTPException(status_code=404, detail="Fabric not found")

    update_data = fabric_data.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(fabric, field, value)

    db.commit()
    db.refresh(fabric)
    return fabric


@router.delete("/{fabric_id}")
async def delete_fabric(
    fabric_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Delete a fabric."""
    fabric = db.query(Fabric).filter(
        Fabric.id == fabric_id,
        Fabric.customer_id == current_user.customer_id
    ).first()
    if not fabric:
        raise HTTPException(status_code=404, detail="Fabric not found")

    db.delete(fabric)
    db.commit()
    return {"message": "Fabric deleted"}
