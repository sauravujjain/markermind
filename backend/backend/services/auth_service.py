from datetime import datetime, timedelta, timezone
from typing import Optional
import hashlib
import secrets

from jose import JWTError, jwt
import bcrypt
from sqlalchemy.orm import Session
from fastapi import HTTPException, status

from ..config import settings
from ..models import User, Customer, Session as UserSession
from ..schemas.auth import UserCreate, UserLogin, UserResponse, TokenResponse


class AuthService:
    """Authentication service for user management and JWT."""

    @staticmethod
    def hash_password(password: str) -> str:
        """Hash a password using bcrypt."""
        password_bytes = password.encode('utf-8')
        salt = bcrypt.gensalt()
        return bcrypt.hashpw(password_bytes, salt).decode('utf-8')

    @staticmethod
    def verify_password(plain_password: str, hashed_password: str) -> bool:
        """Verify a password against its hash."""
        password_bytes = plain_password.encode('utf-8')
        hashed_bytes = hashed_password.encode('utf-8')
        return bcrypt.checkpw(password_bytes, hashed_bytes)

    @staticmethod
    def create_access_token(user_id: str, expires_delta: Optional[timedelta] = None) -> str:
        """Create a JWT access token."""
        if expires_delta:
            expire = datetime.now(timezone.utc) + expires_delta
        else:
            expire = datetime.now(timezone.utc) + timedelta(minutes=settings.jwt_access_token_expire_minutes)

        to_encode = {
            "sub": user_id,
            "exp": expire,
            "iat": datetime.now(timezone.utc),
        }
        return jwt.encode(to_encode, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)

    @staticmethod
    def decode_token(token: str) -> Optional[str]:
        """Decode and validate a JWT token, returning the user_id."""
        try:
            payload = jwt.decode(token, settings.jwt_secret_key, algorithms=[settings.jwt_algorithm])
            user_id: str = payload.get("sub")
            if user_id is None:
                return None
            return user_id
        except JWTError:
            return None

    @staticmethod
    def hash_token(token: str) -> str:
        """Hash a token for storage."""
        return hashlib.sha256(token.encode()).hexdigest()

    def register_user(self, db: Session, user_data: UserCreate) -> User:
        """Register a new user."""
        # Check if email already exists
        existing_user = db.query(User).filter(User.email == user_data.email).first()
        if existing_user:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Email already registered"
            )

        # Get or create customer
        if user_data.customer_code:
            customer = db.query(Customer).filter(Customer.code == user_data.customer_code).first()
            if not customer:
                # Create new customer
                customer = Customer(
                    name=user_data.customer_code,
                    code=user_data.customer_code,
                )
                db.add(customer)
                db.flush()
        else:
            # Create default customer for user
            customer_code = f"USER_{secrets.token_hex(4).upper()}"
            customer = Customer(
                name=f"Customer {customer_code}",
                code=customer_code,
            )
            db.add(customer)
            db.flush()

        # Create user
        user = User(
            customer_id=customer.id,
            email=user_data.email,
            password_hash=self.hash_password(user_data.password),
            name=user_data.name,
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        return user

    def authenticate_user(self, db: Session, login_data: UserLogin) -> Optional[User]:
        """Authenticate a user by email and password."""
        user = db.query(User).filter(User.email == login_data.email).first()
        if not user:
            return None
        if not self.verify_password(login_data.password, user.password_hash):
            return None
        if user.is_active != "Y":
            return None
        return user

    def login(self, db: Session, login_data: UserLogin) -> TokenResponse:
        """Login user and return token."""
        user = self.authenticate_user(db, login_data)
        if not user:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Incorrect email or password",
                headers={"WWW-Authenticate": "Bearer"},
            )

        # Create access token
        expires_delta = timedelta(minutes=settings.jwt_access_token_expire_minutes)
        access_token = self.create_access_token(user.id, expires_delta)

        # Store session
        session = UserSession(
            user_id=user.id,
            token_hash=self.hash_token(access_token),
            expires_at=datetime.now(timezone.utc) + expires_delta,
        )
        db.add(session)
        db.commit()

        return TokenResponse(
            access_token=access_token,
            expires_in=settings.jwt_access_token_expire_minutes * 60,
            user=UserResponse(
                id=user.id,
                email=user.email,
                name=user.name,
                role=user.role.value,
                customer_id=user.customer_id,
                created_at=user.created_at,
            ),
        )

    def logout(self, db: Session, token: str) -> bool:
        """Invalidate a user's session."""
        token_hash = self.hash_token(token)
        session = db.query(UserSession).filter(UserSession.token_hash == token_hash).first()
        if session:
            db.delete(session)
            db.commit()
            return True
        return False

    def get_current_user(self, db: Session, token: str) -> Optional[User]:
        """Get the current user from token."""
        user_id = self.decode_token(token)
        if not user_id:
            return None

        # Verify session exists and is not expired
        token_hash = self.hash_token(token)
        session = db.query(UserSession).filter(
            UserSession.token_hash == token_hash,
            UserSession.expires_at > datetime.now(timezone.utc)
        ).first()
        if not session:
            return None

        user = db.query(User).filter(User.id == user_id).first()
        return user
