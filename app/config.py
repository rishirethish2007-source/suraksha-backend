"""
Configuration management for the Suraksha SOS backend.
"""
from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import List

class Settings(BaseSettings):
    """
    Application settings, loaded from environment variables with sensible defaults.
    """
    DATABASE_URL: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/suraksha_sos"
    REDIS_URL: str = "redis://localhost:6379/0"
    
    JWT_SECRET_KEY: str = ""
    JWT_ALGORITHM: str = "HS256"
    JWT_ISSUER: str = "suraksha"
    JWT_AUDIENCE: str = "suraksha-api"
    AUTO_CREATE_TABLES: bool = False
    
    CORS_ORIGINS: List[str] = ["*"]
    
    SOS_MAX_HOP_COUNT: int = 15
    SOS_TTL_SECONDS: int = 3600
    SOS_RATE_LIMIT_PER_MINUTE: int = 5
    
    MEDIA_UPLOAD_MAX_SIZE_MB: int = 10
    
    WS_HEARTBEAT_INTERVAL: int = 30
    
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

settings = Settings()
