from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from ...database import get_db
from ...schemas.auth import UserCreate, UserLogin, UserResponse, TokenResponse
from ...services.auth_service import AuthService
from ..deps import get_current_user
from ...models import User

router = APIRouter(prefix="/auth", tags=["auth"])
security = HTTPBearer()
auth_service = AuthService()


@router.post("/register", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
async def register(user_data: UserCreate, db: Session = Depends(get_db)):
    """Register a new user."""
    user = auth_service.register_user(db, user_data)
    return UserResponse(
        id=user.id,
        email=user.email,
        name=user.name,
        role=user.role.value,
        customer_id=user.customer_id,
        created_at=user.created_at,
    )


@router.post("/login", response_model=TokenResponse)
async def login(login_data: UserLogin, db: Session = Depends(get_db)):
    """Login and get access token."""
    return auth_service.login(db, login_data)


@router.post("/logout")
async def logout(
    credentials: HTTPAuthorizationCredentials = Depends(security),
    db: Session = Depends(get_db)
):
    """Logout and invalidate token."""
    auth_service.logout(db, credentials.credentials)
    return {"message": "Logged out successfully"}


@router.get("/me", response_model=UserResponse)
async def get_me(current_user: User = Depends(get_current_user)):
    """Get current user profile."""
    return UserResponse(
        id=current_user.id,
        email=current_user.email,
        name=current_user.name,
        role=current_user.role.value,
        customer_id=current_user.customer_id,
        created_at=current_user.created_at,
    )
