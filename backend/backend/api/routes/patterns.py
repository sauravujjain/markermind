from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, status, UploadFile, File, Form
from fastapi.responses import Response
from sqlalchemy.orm import Session

from ...database import get_db
from ...schemas.pattern import (
    PatternCreate, PatternResponse,
    PatternFabricMappingCreate, PatternFabricMappingResponse, PatternParseResult
)
from ...models import User, Pattern, PatternFabricMapping
from ...services.pattern_service import PatternService
from ..deps import get_current_user

router = APIRouter(prefix="/patterns", tags=["patterns"])
pattern_service = PatternService()


@router.get("", response_model=List[PatternResponse])
async def list_patterns(
    skip: int = 0,
    limit: int = 100,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """List all patterns for the current customer."""
    patterns = db.query(Pattern).filter(
        Pattern.customer_id == current_user.customer_id
    ).offset(skip).limit(limit).all()
    return patterns


@router.post("/upload", response_model=PatternResponse, status_code=status.HTTP_201_CREATED)
async def upload_pattern(
    name: str = Form(...),
    file_type: str = Form("aama"),
    size_names: Optional[str] = Form(None),
    dxf_file: UploadFile = File(...),
    rul_file: Optional[UploadFile] = File(None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Upload a new pattern (DXF + optional RUL files)."""
    # Generate unique name if this one already exists
    base_name = name
    unique_name = name
    suffix = 1

    while True:
        existing = db.query(Pattern).filter(
            Pattern.customer_id == current_user.customer_id,
            Pattern.name == unique_name
        ).first()
        if not existing:
            break
        suffix += 1
        unique_name = f"{base_name} ({suffix})"

    # Create pattern record with unique name
    pattern = pattern_service.create_pattern(
        db=db,
        customer_id=current_user.customer_id,
        name=unique_name,
        file_type=file_type,
    )

    # Read and save files
    dxf_content = await dxf_file.read()
    rul_content = await rul_file.read() if rul_file else None

    dxf_path, rul_path = pattern_service.save_uploaded_files(
        pattern=pattern,
        dxf_content=dxf_content,
        rul_content=rul_content,
    )

    # Update pattern with file paths
    pattern.dxf_file_path = dxf_path
    pattern.rul_file_path = rul_path
    db.commit()
    db.refresh(pattern)

    # Auto-parse pattern after upload
    try:
        pattern_service.parse_pattern(db, pattern, size_names=size_names)
    except Exception as e:
        # Log but don't fail the upload if parsing fails
        print(f"Pattern parsing failed: {e}")

    return pattern


@router.get("/{pattern_id}", response_model=PatternResponse)
async def get_pattern(
    pattern_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Get pattern by ID."""
    pattern = db.query(Pattern).filter(
        Pattern.id == pattern_id,
        Pattern.customer_id == current_user.customer_id
    ).first()
    if not pattern:
        raise HTTPException(status_code=404, detail="Pattern not found")
    return pattern


@router.delete("/{pattern_id}")
async def delete_pattern(
    pattern_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Delete a pattern."""
    pattern = db.query(Pattern).filter(
        Pattern.id == pattern_id,
        Pattern.customer_id == current_user.customer_id
    ).first()
    if not pattern:
        raise HTTPException(status_code=404, detail="Pattern not found")

    db.delete(pattern)
    db.commit()
    return {"message": "Pattern deleted"}


@router.post("/{pattern_id}/parse", response_model=PatternParseResult)
async def parse_pattern(
    pattern_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Trigger pattern parsing to extract sizes and materials."""
    pattern = db.query(Pattern).filter(
        Pattern.id == pattern_id,
        Pattern.customer_id == current_user.customer_id
    ).first()
    if not pattern:
        raise HTTPException(status_code=404, detail="Pattern not found")

    if not pattern.dxf_file_path:
        raise HTTPException(status_code=400, detail="Pattern has no DXF file")

    result = pattern_service.parse_pattern(db, pattern)
    return result


@router.post("/{pattern_id}/fabric-mapping", response_model=List[PatternFabricMappingResponse])
async def update_fabric_mappings(
    pattern_id: str,
    mappings: List[PatternFabricMappingCreate],
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Update fabric mappings for a pattern."""
    pattern = db.query(Pattern).filter(
        Pattern.id == pattern_id,
        Pattern.customer_id == current_user.customer_id
    ).first()
    if not pattern:
        raise HTTPException(status_code=404, detail="Pattern not found")

    results = []
    for mapping in mappings:
        result = pattern_service.update_fabric_mapping(
            db=db,
            pattern_id=pattern_id,
            material_name=mapping.material_name,
            fabric_id=mapping.fabric_id,
        )
        results.append(result)

    return results


@router.get("/{pattern_id}/pieces")
async def get_pattern_pieces(
    pattern_id: str,
    material: Optional[str] = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Get list of pieces from a parsed pattern."""
    pattern = db.query(Pattern).filter(
        Pattern.id == pattern_id,
        Pattern.customer_id == current_user.customer_id
    ).first()
    if not pattern:
        raise HTTPException(status_code=404, detail="Pattern not found")

    if not pattern.is_parsed:
        raise HTTPException(status_code=400, detail="Pattern not parsed yet")

    pieces = pattern_service.get_pattern_pieces(pattern)

    # Filter by material if specified
    if material:
        pieces = [p for p in pieces if p.get("material") == material]

    return {
        "pieces": pieces,
        "total_count": len(pieces),
        "pieces_by_material": pattern.parse_metadata.get("pieces_by_material", {}),
    }


@router.post("/{pattern_id}/merge-materials")
async def merge_materials(
    pattern_id: str,
    request: dict,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Merge multiple pattern materials into one for combined nesting."""
    pattern = db.query(Pattern).filter(
        Pattern.id == pattern_id,
        Pattern.customer_id == current_user.customer_id,
    ).first()
    if not pattern:
        raise HTTPException(status_code=404, detail="Pattern not found")
    if not pattern.is_parsed:
        raise HTTPException(status_code=400, detail="Pattern not parsed yet")

    source_materials = request.get("source_materials", [])
    target_name = request.get("target_name", "")

    if len(source_materials) < 2:
        raise HTTPException(status_code=400, detail="Need at least 2 materials to merge")
    if not target_name:
        raise HTTPException(status_code=400, detail="Target name is required")

    result = pattern_service.merge_materials(db, pattern, source_materials, target_name)

    if not result.get("success"):
        raise HTTPException(status_code=400, detail=result.get("error", "Merge failed"))

    return result


@router.get("/{pattern_id}/preview")
async def get_pattern_preview(
    pattern_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Get SVG preview of pattern pieces."""
    pattern = db.query(Pattern).filter(
        Pattern.id == pattern_id,
        Pattern.customer_id == current_user.customer_id
    ).first()
    if not pattern:
        raise HTTPException(status_code=404, detail="Pattern not found")

    svg_content = pattern_service.get_pattern_svg(pattern)
    if not svg_content:
        raise HTTPException(status_code=404, detail="Preview not available")

    return Response(content=svg_content, media_type="image/svg+xml")
