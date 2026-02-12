from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ...database import get_db
from ...schemas.cost import CostConfigUpdate, CostConfigResponse
from ...models import User, CostConfig
from ..deps import get_current_user

router = APIRouter(prefix="/costs", tags=["costs"])


@router.get("", response_model=CostConfigResponse)
async def get_cost_config(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Get cost config for current customer. Auto-creates default if none exists."""
    config = db.query(CostConfig).filter(
        CostConfig.customer_id == current_user.customer_id
    ).first()

    if not config:
        config = CostConfig(customer_id=current_user.customer_id)
        db.add(config)
        db.commit()
        db.refresh(config)

    return config


@router.put("", response_model=CostConfigResponse)
async def update_cost_config(
    data: CostConfigUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Update cost config for current customer."""
    config = db.query(CostConfig).filter(
        CostConfig.customer_id == current_user.customer_id
    ).first()

    if not config:
        config = CostConfig(customer_id=current_user.customer_id)
        db.add(config)
        db.commit()
        db.refresh(config)

    update_data = data.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(config, field, value)

    db.commit()
    db.refresh(config)
    return config
