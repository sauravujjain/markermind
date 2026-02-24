from pydantic_settings import BaseSettings
from typing import List
import os


class Settings(BaseSettings):
    # Database (using psycopg3)
    database_url: str = "postgresql+psycopg://markermind:markermind_dev@localhost:5432/markermind"

    # Redis
    redis_url: str = "redis://localhost:6379/0"

    # JWT
    jwt_secret_key: str = "your-secret-key-change-in-production"
    jwt_algorithm: str = "HS256"
    jwt_access_token_expire_minutes: int = 60

    # App
    debug: bool = True
    cors_origins: List[str] = ["*"]  # Accepts any origin; restrict in production via .env

    # File Upload
    upload_dir: str = "./uploads"
    max_upload_size_mb: int = 100

    # Nesting Engine Path (relative to project root)
    nesting_engine_path: str = "../nesting_engine"
    scripts_path: str = "../scripts"

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


settings = Settings()

# Resolve base directory (where backend package lives)
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

def resolve_path(path: str) -> str:
    """
    Resolve a path that may be relative to absolute.
    Works locally and in GCP deployments.

    For GCP: Set UPLOAD_BASE_DIR env var to the mounted storage path.
    """
    if os.path.isabs(path):
        return path

    # Check for GCP/production override
    upload_base = os.environ.get('UPLOAD_BASE_DIR', BASE_DIR)
    return os.path.normpath(os.path.join(upload_base, path))

# Ensure upload directory exists
os.makedirs(resolve_path(settings.upload_dir), exist_ok=True)
