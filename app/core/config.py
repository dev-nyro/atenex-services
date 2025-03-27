import logging
from typing import Optional
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import RedisDsn, PostgresDsn, AnyHttpUrl, SecretStr

# Define niveles de log para que Pydantic los reconozca
LogLevel = logging.getLevelName # type: ignore

class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file='.env',
        env_file_encoding='utf-8',
        case_sensitive=False,
        extra='ignore' # Ignorar variables de entorno extra
    )

    # --- General ---
    PROJECT_NAME: str = "Ingest Service"
    API_V1_STR: str = "/api/v1"
    LOG_LEVEL: LogLevel = logging.INFO # type: ignore

    # --- JWT (Aunque lo valide el Gateway, puede ser útil tener el secret) ---
    # SECRET_KEY: SecretStr = SecretStr("super-secret-key") # Cambiar en producción
    # ALGORITHM: str = "HS256"

    # --- Celery ---
    CELERY_BROKER_URL: RedisDsn = RedisDsn("redis://localhost:6379/0") # type: ignore
    CELERY_RESULT_BACKEND: RedisDsn = RedisDsn("redis://localhost:6379/1") # type: ignore

    # --- Databases ---
    POSTGRES_DSN: PostgresDsn = PostgresDsn("postgresql+asyncpg://user:pass@host:port/db") # type: ignore
    MILVUS_URI: str = "http://localhost:19530"
    MILVUS_COLLECTION_NAME: str = "document_chunks"
    MILVUS_DIMENSION: int = 768 # Ajustar según el modelo de embedding

    # --- External Services ---
    STORAGE_SERVICE_URL: AnyHttpUrl = AnyHttpUrl("http://localhost:8001/api/v1/storage") # type: ignore
    OCR_SERVICE_URL: AnyHttpUrl = AnyHttpUrl("http://localhost:8002/api/v1/ocr") # type: ignore
    CHUNKING_SERVICE_URL: AnyHttpUrl = AnyHttpUrl("http://localhost:8003/api/v1/chunking") # type: ignore
    EMBEDDING_SERVICE_URL: AnyHttpUrl = AnyHttpUrl("http://localhost:8004/api/v1/embedding") # type: ignore

    # --- Service Client Config ---
    HTTP_CLIENT_TIMEOUT: int = 30 # Timeout en segundos para llamadas HTTP
    HTTP_CLIENT_MAX_RETRIES: int = 3
    HTTP_CLIENT_BACKOFF_FACTOR: float = 0.5

    # --- File Processing ---
    SUPPORTED_CONTENT_TYPES: list[str] = [
        "application/pdf",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document", # docx
        "text/plain",
        "image/jpeg",
        "image/png",
    ]
    OCR_REQUIRED_CONTENT_TYPES: list[str] = [
        "image/jpeg",
        "image/png",
        # Podríamos añadir PDF aquí y detectar si es escaneado en el servicio OCR o aquí
    ]
    # Podríamos añadir configuraciones de chunking aquí si no las pasamos al servicio

settings = Settings()