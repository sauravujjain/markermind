from pydantic import BaseModel
from typing import Optional, List, Dict, Any
from datetime import datetime


class PatternCreate(BaseModel):
    name: str
    file_type: str = "aama"


class PatternFabricMappingCreate(BaseModel):
    material_name: str
    fabric_id: Optional[str] = None


class PatternFabricMappingResponse(BaseModel):
    id: str
    pattern_id: str
    material_name: str
    fabric_id: Optional[str]

    class Config:
        from_attributes = True


class PatternResponse(BaseModel):
    id: str
    customer_id: str
    name: str
    file_type: str
    dxf_file_path: Optional[str]
    rul_file_path: Optional[str]
    is_parsed: bool
    available_sizes: List[str]
    available_materials: List[str]
    parse_metadata: Dict[str, Any]
    fabric_mappings: List[PatternFabricMappingResponse] = []
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class PatternParseResult(BaseModel):
    success: bool
    sizes: List[str]
    materials: List[str]
    piece_count: int
    metadata: Dict[str, Any]
    error: Optional[str] = None
