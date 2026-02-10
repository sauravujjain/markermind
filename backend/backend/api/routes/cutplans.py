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

    # Get demand
    demand = cutplan_service.get_order_demand(db, request.order_id)
    if not demand:
        raise HTTPException(status_code=400, detail="Order has no quantities")

    # Get cost config
    cost_config = cutplan_service.get_cost_config(db, current_user.customer_id)

    # Get available markers
    markers = cutplan_service.get_available_markers(
        db,
        pattern_id=order.pattern_id,
        fabric_id=order.order_lines[0].fabric_id if order.order_lines else None
    )
    if not markers:
        raise HTTPException(status_code=400, detail="No markers available. Run nesting first.")

    cutplans = []

    # Generate cutplan options based on request
    solver_config = {
        "penalty": request.penalty,
    }

    for option in request.generate_options:
        name = f"Option - {option.title()}"

        cutplan = cutplan_service.create_cutplan(
            db=db,
            order_id=request.order_id,
            name=name,
            solver_type=request.solver_type,
        )

        try:
            # Run optimization based on solver type
            if request.solver_type == "single_color":
                selected = cutplan_service.run_ilp_optimization(
                    db, cutplan, demand, markers, solver_config
                )
            elif request.solver_type == "multicolor_joint":
                # For multicolor, we need markers by color
                markers_by_color = {}
                for color in order.order_lines:
                    color_markers = cutplan_service.get_available_markers(
                        db, order.pattern_id, color.fabric_id
                    )
                    markers_by_color[color.color_code] = color_markers
                selected = cutplan_service.run_multicolor_optimization(
                    db, cutplan, demand, markers_by_color, solver_config
                )
            else:  # two_stage
                selected = cutplan_service.run_two_stage_optimization(
                    db, cutplan, demand, markers, solver_config
                )

            # Save markers and calculate costs
            if selected:
                cutplan_service.save_cutplan_markers(db, cutplan, selected)
                costs = cutplan_service.calculate_costs(cutplan, cost_config, selected)

                # Update cutplan with summary
                cutplan.unique_markers = costs["unique_markers"]
                cutplan.total_cuts = costs["total_cuts"]
                cutplan.total_plies = costs["total_plies"]
                cutplan.total_yards = costs["total_yards"]
                cutplan.total_cost = costs["total_cost"]
                cutplan.fabric_cost = costs["fabric_cost"]
                cutplan.spreading_cost = costs["spreading_cost"]
                cutplan.cutting_cost = costs["cutting_cost"]
                cutplan.prep_cost = costs["prep_cost"]
                db.commit()

            cutplans.append(cutplan)

        except Exception as e:
            cutplan.status = "draft"
            db.commit()
            # Continue to next option even if one fails

    db.refresh(order)
    return [cp for cp in cutplans]


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
