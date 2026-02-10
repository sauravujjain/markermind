from pydantic import BaseModel
from typing import Optional
from datetime import datetime


class FabricCreate(BaseModel):
    name: str
    code: str
    width_inches: float
    cost_per_yard: Optional[float] = 0.0
    description: Optional[str] = None


class FabricUpdate(BaseModel):
    name: Optional[str] = None
    code: Optional[str] = None
    width_inches: Optional[float] = None
    cost_per_yard: Optional[float] = None
    description: Optional[str] = None


class FabricResponse(BaseModel):
    id: str
    customer_id: str
    name: str
    code: str
    width_inches: float
    cost_per_yard: float
    description: Optional[str]
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True
