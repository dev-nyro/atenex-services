# ./app/core/config.py (CORREGIDO - Defaults restaurados, prints eliminados)
import logging
import os
from typing import Optional, List, Any
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import RedisDsn, PostgresDsn, AnyHttpUrl, SecretStr, Field, validator, ValidationError
import sys

# --- Supabase Connection Defaults ---
SUPABASE_DEFAULT_HOST = "db.ymsilkrhstwxikjiqqog.supabase.co"
SUPABASE_DEFAULT_PORT = 5432
SUPABASE_DEFAULT_DB = "postgres"
SUPABASE_DEFAULT_USER = "postgres"

class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file='.env',
        env_prefix='INGEST_',
        env_file_encoding='utf-8',
        case_sensitive=False,
        extra='ignore'
    )

    # --- General ---
    PROJECT_NAME: str = "Ingest Service (Haystack/K8s/Supabase)"
    API_V1_STR: str = "/api/v1"
    LOG_LEVEL: str = "INFO"

    # --- Celery ---
    CELERY_BROKER_URL: RedisDsn = RedisDsn("redis://redis-service:6379/0")
    CELERY_RESULT_BACKEND: RedisDsn = RedisDsn("redis://redis-service:6379/1")

    # --- Database (Supabase Direct Connection Settings) ---
    POSTGRES_USER: str = SUPABASE_DEFAULT_USER
    POSTGRES_PASSWORD: SecretStr
    POSTGRES_SERVER: str = SUPABASE_DEFAULT_HOST
    POSTGRES_PORT: int = SUPABASE_DEFAULT_PORT
    POSTGRES_DB: str = SUPABASE_DEFAULT_DB
    POSTGRES_DSN: Optional[PostgresDsn] = None

    # --- Milvus ---
    MILVUS_URI: str = "http://milvus-service:19530"
    MILVUS_COLLECTION_NAME: str = "document_chunks_haystack"
    # *** CORREGIDO: Default completo ***
    MILVUS_INDEX_PARAMS: dict = Field(default={
        "metric_type": "COSINE",
        "index_type": "HNSW",
        "params": {"M": 16, "efConstruction": 256}
    })
    # *** CORREGIDO: Default completo ***
    MILVUS_SEARCH_PARAMS: dict = Field(default={
        "metric_type": "COSINE",
        "params": {"ef": 128}
    })
    MILVUS_CONTENT_FIELD: str = "content"
    MILVUS_EMBEDDING_FIELD: str = "embedding"
    # *** CORREGIDO: Default completo ***
    # Ajusta esta lista según los metadatos REALES que necesites en Milvus
    MILVUS_METADATA_FIELDS: List[str] = Field(default=[
        "company_id",
        "document_id",
        "file_name",
        "file_type",
        # "category", # Ejemplo
        # "source",   # Ejemplo
    ])

    # --- MinIO Storage ---
    MINIO_ENDPOINT: str = "minio-service:9000"
    MINIO_ACCESS_KEY: SecretStr
    MINIO_SECRET_KEY: SecretStr
    MINIO_BUCKET_NAME: str = "ingested-documents"
    MINIO_USE_SECURE: bool = False

    # --- External Services ---
    OCR_SERVICE_URL: Optional[AnyHttpUrl] = None

    # --- Service Client Config ---
    HTTP_CLIENT_TIMEOUT: int = 60
    HTTP_CLIENT_MAX_RETRIES: int = 2
    HTTP_CLIENT_BACKOFF_FACTOR: float = 1.0

    # --- File Processing & Haystack ---
    # *** CORREGIDO: Default completo ***
    SUPPORTED_CONTENT_TYPES: List[str] = Field(default=[
        "application/pdf",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document", # docx
        "text/plain",
        "text/markdown",
        "text/html",
        "image/jpeg",
        "image/png",
    ])
    # *** CORREGIDO: Default completo ***
    EXTERNAL_OCR_REQUIRED_CONTENT_TYPES: List[str] = Field(default=[
        "image/jpeg",
        "image/png"
    ])
    SPLITTER_CHUNK_SIZE: int = 500
    SPLITTER_CHUNK_OVERLAP: int = 50
    SPLITTER_SPLIT_BY: str = "word"

    # --- OpenAI ---
    OPENAI_API_KEY: SecretStr
    OPENAI_EMBEDDING_MODEL: str = "text-embedding-3-small"
    EMBEDDING_DIMENSION: int = 1536

    # --- Validators ---
    @validator("POSTGRES_DSN", pre=True, always=True)
    def assemble_postgres_dsn(cls, v: Optional[str], values: dict[str, Any]) -> Any:
        """
        Constructs the DSN string for asyncpg if not provided explicitly.
        Ensures the format is correct for Supabase Direct Connection.
        (Lógica corregida en respuesta anterior, sin prints ahora)
        """
        if isinstance(v, str):
            try:
                dsn = PostgresDsn(v)
                if dsn.scheme != "postgresql+asyncpg":
                     return dsn.build(scheme="postgresql+asyncpg")
                return str(dsn)
            except ValidationError as e:
                raise ValueError(f"Invalid INGEST_POSTGRES_DSN provided: {e}") from e

        password_obj = values.get("POSTGRES_PASSWORD")
        if not password_obj:
             raise ValueError("INGEST_POSTGRES_PASSWORD environment variable is required.")
        password_value = password_obj.get_secret_value()
        user = values.get("POSTGRES_USER")
        server = values.get("POSTGRES_SERVER")
        port = values.get("POSTGRES_PORT")
        db_name = values.get("POSTGRES_DB")
        if not all([user, server, port, db_name]):
             missing = [k for k, val in {"user": user, "server": server, "port": port, "db": db_name}.items() if not val]
             raise ValueError(f"Missing required PostgreSQL connection parts: {missing}")
        try:
            dsn = PostgresDsn.build(
                scheme="postgresql+asyncpg",
                username=user,
                password=password_value,
                host=server,
                port=port,
                path=db_name or None, # Correct path logic
            )
            return str(dsn)
        except ValidationError as e:
            raise ValueError(f"Failed to assemble Postgres DSN from parts: {e}") from e

    # --- set_embedding_dimension validator (sin cambios) ---
    @validator("EMBEDDING_DIMENSION", pre=True, always=True)
    def set_embedding_dimension(cls, v: Optional[int], values: dict[str, Any]) -> int:
        model = values.get("OPENAI_EMBEDDING_MODEL")
        if model == "text-embedding-3-large":
            return 3072
        elif model in ["text-embedding-3-small", "text-embedding-ada-002"]:
            return 1536
        if v is None or v == 0:
            if model:
                if model == "text-embedding-3-large": return 3072
                if model in ["text-embedding-3-small", "text-embedding-ada-002"]: return 1536
            return 1536
        return v

# Create the settings instance globally
try:
    settings = Settings()
except (ValidationError, ValueError) as e: # Catch both Pydantic and our ValueErrors
    # Log critical error clearly
    print(f"FATAL: Configuration validation failed:\n{e}")
    sys.exit(1) # Ensure exit on any validation failure
except Exception as e: # Catch any other unexpected error
    print(f"FATAL: Unexpected error during Settings instantiation:\n{e}")
    sys.exit(1)