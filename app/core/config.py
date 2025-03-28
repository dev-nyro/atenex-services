import logging
import os
# Add 'Any' to this import
from typing import Optional, List, Any
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import RedisDsn, PostgresDsn, AnyHttpUrl, SecretStr, Field, validator

# Define niveles de log para que Pydantic los reconozca
LogLevel = logging.getLevelName # type: ignore

class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file='.env',        # Still useful for local dev, K8s uses env vars
        env_prefix='INGEST_',   # Optional: Add prefix to env vars
        env_file_encoding='utf-8',
        case_sensitive=False,
        extra='ignore'
    )

    # --- General ---
    PROJECT_NAME: str = "Ingest Service (Haystack/K8s)"
    API_V1_STR: str = "/api/v1"
    LOG_LEVEL: LogLevel = logging.INFO # type: ignore

    # --- Celery (using K8s service names) ---
    CELERY_BROKER_URL: RedisDsn = RedisDsn("redis://redis-service:6379/0") # type: ignore
    CELERY_RESULT_BACKEND: RedisDsn = RedisDsn("redis://redis-service:6379/1") # type: ignore

    # --- Databases (using K8s service names) ---
    # Format: postgresql+asyncpg://<user>:<password>@<host>:<port>/<dbname>
    POSTGRES_USER: str = "user"
    POSTGRES_PASSWORD: SecretStr = SecretStr("password") # Load from Secret
    POSTGRES_SERVER: str = "postgres-service"
    POSTGRES_PORT: str = "5432"
    POSTGRES_DB: str = "mydatabase"
    POSTGRES_DSN: Optional[PostgresDsn] = None # Will be constructed

    MILVUS_URI: str = "http://milvus-service:19530" # K8s service name
    MILVUS_COLLECTION_NAME: str = "document_chunks_haystack"
    MILVUS_INDEX_PARAMS: dict = Field(default={
        "metric_type": "COSINE",
        "index_type": "HNSW",
        "params": {"M": 16, "efConstruction": 256}
    })
    MILVUS_SEARCH_PARAMS: dict = Field(default={"metric_type": "COSINE", "params": {"ef": 128}})

    # --- MinIO Storage (using K8s service name) ---
    MINIO_ENDPOINT: str = "minio-service:9000" # K8s service name and port
    MINIO_ACCESS_KEY: SecretStr = SecretStr("minioadmin") # Load from Secret
    MINIO_SECRET_KEY: SecretStr = SecretStr("minioadmin") # Load from Secret
    MINIO_BUCKET_NAME: str = "ingested-documents"
    MINIO_USE_SECURE: bool = False # Use HTTP within cluster by default

    # --- External Services (Only OCR if needed) ---
    # Optional: Keep OCR service if needed for images/scanned PDFs
    OCR_SERVICE_URL: Optional[AnyHttpUrl] = None # type: ignore # Example: "http://ocr-service:8002/api/v1/ocr"

    # --- Service Client Config ---
    HTTP_CLIENT_TIMEOUT: int = 60
    HTTP_CLIENT_MAX_RETRIES: int = 2
    HTTP_CLIENT_BACKOFF_FACTOR: float = 1.0

    # --- File Processing & Haystack ---
    SUPPORTED_CONTENT_TYPES: List[str] = Field(default=[
        "application/pdf",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document", # docx
        "text/plain", "text/markdown", "text/html",
        "image/jpeg", "image/png", # Keep if OCR service is used
    ])
    EXTERNAL_OCR_REQUIRED_CONTENT_TYPES: List[str] = Field(default=["image/jpeg", "image/png"])
    SPLITTER_CHUNK_SIZE: int = 500
    SPLITTER_CHUNK_OVERLAP: int = 50
    SPLITTER_SPLIT_BY: str = "word"

    # --- OpenAI ---
    OPENAI_API_KEY: SecretStr = SecretStr("sk-...") # Loaded from Secret
    OPENAI_EMBEDDING_MODEL: str = "text-embedding-3-small"
    EMBEDDING_DIMENSION: int = 1536

    # --- MilvusDocumentStore Fields ---
    MILVUS_CONTENT_FIELD: str = "content"
    MILVUS_EMBEDDING_FIELD: str = "embedding"
    MILVUS_METADATA_FIELDS: List[str] = Field(default=["company_id", "document_id", "file_name", "file_type"])

    # --- Validators ---
    # Make sure 'Any' comes from typing import above
    @validator("POSTGRES_DSN", pre=True, always=True)
    def assemble_postgres_dsn(cls, v: Optional[str], values: dict[str, Any]) -> Any:
        if isinstance(v, str):
            return v
        # Ensure password handling is correct
        password = values.get("POSTGRES_PASSWORD")
        password_value = password.get_secret_value() if password else None
        return PostgresDsn.build(
            scheme="postgresql+asyncpg",
            username=values.get("POSTGRES_USER"),
            password=password_value,
            host=values.get("POSTGRES_SERVER"),
            port=int(values.get("POSTGRES_PORT", 5432)), # type: ignore
            path=f"/{values.get('POSTGRES_DB') or ''}", # Add leading slash for path
        )

    @validator("EMBEDDING_DIMENSION", pre=True, always=True)
    def set_embedding_dimension(cls, v: int, values: dict[str, Any]) -> int:
        model = values.get("OPENAI_EMBEDDING_MODEL")
        if model == "text-embedding-3-large":
            return 3072
        elif model in ["text-embedding-3-small", "text-embedding-ada-002"]:
            return 1536
        # Add other models if needed
        return v # Return existing or default if model not matched

settings = Settings()