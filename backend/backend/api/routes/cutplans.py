from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from ...database import get_db
from ...schemas.cutplan import (
    CutplanOptimizeRequest, CutplanResponse,
    CutplanMarkerResponse, CostBreakdownResponse
)
from ...models import User, Cutplan, CutplanMarker, Order, MarkerBank
from ...services.cutplan_service import CutplanService
from ..deps import get_current_user

router = APIRouter(prefix="/cutplans", tags=["cutplans"])
cutplan_service = CutplanService()


@router.post("/optimize", response_model=List[CutplanResponse])
async def optimize_cutplan(
    request: CutplanOptimizeRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Run ILP optimization to generate cutplan options."""
    # Verify order exists
    order = db.query(Order).filter(
        Order.id == request.order_id,
        Order.customer_id == current_user.customer_id
    ).first()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    if not order.pattern_id:
        raise HTTPException(status_code=400, detail="Order has no pattern linked")

    # Get fabric_id from first order line
    fabric_id = None
    if order.order_lines:
        first_line = order.order_lines[0]
        fabric_id = first_line.fabric_id
        # If no fabric_id, try to find by fabric_code
        if not fabric_id and first_line.fabric_code:
            from ...models import Fabric
            fabric = db.query(Fabric).filter(
                Fabric.code == first_line.fabric_code,
                Fabric.customer_id == current_user.customer_id
            ).first()
            if fabric:
                fabric_id = fabric.id

    if not fabric_id:
        raise HTTPException(status_code=400, detail="Order has no fabric configured")

    # Map strategy names
    strategy_map = {
        "max_efficiency": "max_efficiency",
        "efficiency": "max_efficiency",
        "balanced": "balanced",
        "min_markers": "min_markers",
        "min_plies": "min_plies",
        "min_bundle_cuts": "min_bundle_cuts",
    }

    strategies = []
    for opt in (request.generate_options or ["max_efficiency", "balanced", "min_markers"]):
        mapped = strategy_map.get(opt.lower(), opt.lower())
        if mapped not in strategies:
            strategies.append(mapped)

    try:
        cutplans = cutplan_service.run_multi_strategy_optimization(
            db=db,
            order_id=request.order_id,
            pattern_id=order.pattern_id,
            fabric_id=fabric_id,
            customer_id=current_user.customer_id,
            strategies=strategies,
            penalty=request.penalty or 5.0,
        )
        return cutplans
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/{cutplan_id}", response_model=CutplanResponse)
async def get_cutplan(
    cutplan_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Get cutplan by ID."""
    cutplan = db.query(Cutplan).join(Order).filter(
        Cutplan.id == cutplan_id,
        Order.customer_id == current_user.customer_id
    ).first()
    if not cutplan:
        raise HTTPException(status_code=404, detail="Cutplan not found")
    return cutplan


@router.get("", response_model=List[CutplanResponse])
async def list_cutplans(
    order_id: Optional[str] = None,
    status: Optional[str] = None,
    skip: int = 0,
    limit: int = 100,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """List cutplans."""
    query = db.query(Cutplan).join(Order).filter(
        Order.customer_id == current_user.customer_id
    )
    if order_id:
        query = query.filter(Cutplan.order_id == order_id)
    if status:
        query = query.filter(Cutplan.status == status)

    cutplans = query.order_by(Cutplan.created_at.desc()).offset(skip).limit(limit).all()
    return cutplans


@router.post("/{cutplan_id}/approve", response_model=CutplanResponse)
async def approve_cutplan(
    cutplan_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Approve a cutplan for production."""
    cutplan = db.query(Cutplan).join(Order).filter(
        Cutplan.id == cutplan_id,
        Order.customer_id == current_user.customer_id
    ).first()
    if not cutplan:
        raise HTTPException(status_code=404, detail="Cutplan not found")

    if cutplan.status != "ready":
        raise HTTPException(status_code=400, detail="Cutplan must be in ready status")

    cutplan = cutplan_service.approve_cutplan(db, cutplan_id)
    return cutplan


@router.delete("/{cutplan_id}")
async def delete_cutplan(
    cutplan_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Delete a cutplan."""
    cutplan = db.query(Cutplan).join(Order).filter(
        Cutplan.id == cutplan_id,
        Order.customer_id == current_user.customer_id
    ).first()
    if not cutplan:
        raise HTTPException(status_code=404, detail="Cutplan not found")

    if cutplan.status in ["approved", "in_production", "completed"]:
        raise HTTPException(status_code=400, detail="Cannot delete approved/in-production cutplan")

    db.delete(cutplan)
    db.commit()
    return {"message": "Cutplan deleted"}


@router.get("/{cutplan_id}/cost-analysis", response_model=CostBreakdownResponse)
async def get_cost_analysis(
    cutplan_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Get detailed cost breakdown for a cutplan."""
    cutplan = db.query(Cutplan).join(Order).filter(
        Cutplan.id == cutplan_id,
        Order.customer_id == current_user.customer_id
    ).first()
    if not cutplan:
        raise HTTPException(status_code=404, detail="Cutplan not found")

    return CostBreakdownResponse(
        total_cost=cutplan.total_cost or 0,
        fabric_cost=cutplan.fabric_cost or 0,
        spreading_cost=cutplan.spreading_cost or 0,
        cutting_cost=cutplan.cutting_cost or 0,
        prep_cost=cutplan.prep_cost or 0,
        fabric_yards=cutplan.total_yards or 0,
        total_plies=cutplan.total_plies or 0,
        unique_markers=cutplan.unique_markers or 0,
    )
