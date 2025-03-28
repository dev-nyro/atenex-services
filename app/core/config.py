# ./app/core/config.py
import logging
import os
from typing import Optional, List, Any
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import RedisDsn, PostgresDsn, AnyHttpUrl, SecretStr, Field, validator, ValidationError

# --- Supabase Connection Defaults (Direct Connection) ---
SUPABASE_DEFAULT_HOST = "db.ymsilkrhstwxikjiqqog.supabase.co" # Reemplaza si tu host es diferente
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
    POSTGRES_PASSWORD: SecretStr # Ensure INGEST_POSTGRES_PASSWORD is set
    POSTGRES_SERVER: str = SUPABASE_DEFAULT_HOST
    POSTGRES_PORT: int = SUPABASE_DEFAULT_PORT
    POSTGRES_DB: str = SUPABASE_DEFAULT_DB
    POSTGRES_DSN: Optional[PostgresDsn] = None # Will be built by validator if not provided

    # --- Milvus ---
    MILVUS_URI: str = "http://milvus-service:19530"
    MILVUS_COLLECTION_NAME: str = "document_chunks_haystack"
    MILVUS_INDEX_PARAMS: dict = Field(default={
        "metric_type": "COSINE",
        "index_type": "HNSW",
        "params": {"M": 16, "efConstruction": 256}
    })
    MILVUS_SEARCH_PARAMS: dict = Field(default={
        "metric_type": "COSINE",
        "params": {"ef": 128}
    })
    MILVUS_CONTENT_FIELD: str = "content"
    MILVUS_EMBEDDING_FIELD: str = "embedding"
    MILVUS_METADATA_FIELDS: List[str] = Field(default=[
        "company_id",
        "document_id",
        "file_name",
        "file_type",
        # Add other needed/desired Milvus metadata fields here
        # "category",
        # "source",
    ])

    # --- MinIO Storage ---
    MINIO_ENDPOINT: str = "minio-service:9000"
    MINIO_ACCESS_KEY: SecretStr # Ensure INGEST_MINIO_ACCESS_KEY is set
    MINIO_SECRET_KEY: SecretStr # Ensure INGEST_MINIO_SECRET_KEY is set
    MINIO_BUCKET_NAME: str = "ingested-documents"
    MINIO_USE_SECURE: bool = False

    # --- External Services ---
    OCR_SERVICE_URL: Optional[AnyHttpUrl] = None

    # --- Service Client Config ---
    HTTP_CLIENT_TIMEOUT: int = 60
    HTTP_CLIENT_MAX_RETRIES: int = 2
    HTTP_CLIENT_BACKOFF_FACTOR: float = 1.0

    # --- File Processing & Haystack ---
    SUPPORTED_CONTENT_TYPES: List[str] = Field(default=[
        "application/pdf",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "text/plain", "text/markdown", "text/html",
        "image/jpeg", "image/png",
    ])
    EXTERNAL_OCR_REQUIRED_CONTENT_TYPES: List[str] = Field(default=["image/jpeg", "image/png"])
    SPLITTER_CHUNK_SIZE: int = 500
    SPLITTER_CHUNK_OVERLAP: int = 50
    SPLITTER_SPLIT_BY: str = "word"

    # --- OpenAI ---
    OPENAI_API_KEY: SecretStr # Ensure INGEST_OPENAI_API_KEY is set
    OPENAI_EMBEDDING_MODEL: str = "text-embedding-3-small"
    EMBEDDING_DIMENSION: int = 1536

    # --- Validators ---
    @validator("POSTGRES_DSN", pre=True, always=True)
    def assemble_postgres_dsn(cls, v: Optional[str], values: dict[str, Any]) -> Any:
        """
        Constructs the DSN string for asyncpg if not provided explicitly.
        Ensures the format is correct for Supabase Direct Connection.
        """
        if isinstance(v, str):
            # If INGEST_POSTGRES_DSN is provided, validate it
            try:
                dsn = PostgresDsn(v)
                # Ensure the scheme is correct for asyncpg
                if dsn.scheme != "postgresql+asyncpg":
                     # Rebuild with the correct scheme if validation passed
                     return dsn.build(scheme="postgresql+asyncpg")
                return str(dsn) # Return the validated DSN string
            except ValidationError as e:
                raise ValueError(f"Invalid INGEST_POSTGRES_DSN provided: {e}") from e
            # Note: We return str(dsn) above, so this 'return v' is technically unreachable
            # if validation succeeds, but kept for logical structure clarity in case of future changes.
            # return v

        # If INGEST_POSTGRES_DSN is not set, build it from parts
        password_obj = values.get("POSTGRES_PASSWORD")
        if not password_obj:
             # This validation might ideally happen before the validator runs if POSTGRES_PASSWORD
             # is not Optional and has no default, but checking here adds robustness.
             raise ValueError("INGEST_POSTGRES_PASSWORD environment variable is required.")

        password_value = password_obj.get_secret_value()

        # Ensure required parts for building are present (Pydantic usually catches this earlier)
        user = values.get("POSTGRES_USER")
        server = values.get("POSTGRES_SERVER")
        port = values.get("POSTGRES_PORT") # Already validated as int by Pydantic
        db_name = values.get("POSTGRES_DB")

        if not all([user, server, port, db_name]):
             missing = [k for k, val in {"user": user, "server": server, "port": port, "db": db_name}.items() if not val]
             raise ValueError(f"Missing required PostgreSQL connection parts: {missing}")

        try:
            # Construct the DSN using pydantic's validation power
            dsn = PostgresDsn.build(
                scheme="postgresql+asyncpg", # Use the asyncpg scheme
                username=user,
                password=password_value,
                host=server,
                port=port,
                # *** CORRECTED LINE BELOW ***
                # Pass only the database name (or None). Let Pydantic handle the '/'.
                path=db_name or None,
            )
            return str(dsn) # Return the built DSN string
        except ValidationError as e:
            # This might catch issues like invalid host characters etc. during build
            raise ValueError(f"Failed to assemble Postgres DSN from parts: {e}") from e

    @validator("EMBEDDING_DIMENSION", pre=True, always=True)
    def set_embedding_dimension(cls, v: Optional[int], values: dict[str, Any]) -> int:
        # Ensure v is treated as Optional[int] coming in
        model = values.get("OPENAI_EMBEDDING_MODEL")
        if model == "text-embedding-3-large":
            return 3072
        elif model in ["text-embedding-3-small", "text-embedding-ada-002"]:
            return 1536

        # Handle case where v might be None or 0 initially
        if v is None or v == 0:
            if model: # If model is known, use its dimension
                 # Recalculate based on model if v was None/0
                if model == "text-embedding-3-large": return 3072
                if model in ["text-embedding-3-small", "text-embedding-ada-002"]: return 1536
            return 1536 # Default dimension if v is None/0 and model is unknown/not matched

        return v # Return existing value if provided and > 0


# Create the settings instance globally
try:
    settings = Settings()
    # Optional: Log successful configuration load
    # log = logging.getLogger(__name__) # Basic logger if structlog not yet configured
    # log.info("Configuration loaded successfully.")
except ValidationError as e:
    # Log this critical error clearly during startup
    print(f"FATAL: Configuration validation failed:\n{e}")
    # Exit if configuration is invalid, essential for app function
    import sys
    sys.exit(1) # Ensure the process exits on configuration failure