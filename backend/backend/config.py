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
    cors_origins: List[str] = ["http://localhost:3000"]

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

# Ensure upload directory exists
os.makedirs(settings.upload_dir, exist_ok=True)
