# ./app/core/config.py
import logging
import os
# Add 'Any' to this import
from typing import Optional, List, Any
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import RedisDsn, PostgresDsn, AnyHttpUrl, SecretStr, Field, validator, ValidationError # Import ValidationError

# --- Supabase Connection Defaults (Direct Connection) ---
# Replace with your actual Supabase project details if different
SUPABASE_DEFAULT_HOST = "db.ymsilkrhstwxikjiqqog.supabase.co" # Reemplaza si tu host es diferente
SUPABASE_DEFAULT_PORT = 5432
SUPABASE_DEFAULT_DB = "postgres"
SUPABASE_DEFAULT_USER = "postgres"

class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file='.env',        # Still useful for local dev, K8s uses env vars
        env_prefix='INGEST_',   # Prefix for environment variables (e.g., INGEST_POSTGRES_USER)
        env_file_encoding='utf-8',
        case_sensitive=False,
        extra='ignore'
    )

    # --- General ---
    PROJECT_NAME: str = "Ingest Service (Haystack/K8s/Supabase)"
    API_V1_STR: str = "/api/v1"
    # Keep LOG_LEVEL as str from previous correction
    LOG_LEVEL: str = "INFO"

    # --- Celery (using K8s service names - Adjust if needed for Supabase context) ---
    # Ensure Redis is accessible from your deployment environment
    CELERY_BROKER_URL: RedisDsn = RedisDsn("redis://redis-service:6379/0")
    CELERY_RESULT_BACKEND: RedisDsn = RedisDsn("redis://redis-service:6379/1")

    # --- Database (Supabase Direct Connection Settings) ---
    # These will be loaded from environment variables primarily,
    # but defaults point to Supabase direct connection structure.
    POSTGRES_USER: str = SUPABASE_DEFAULT_USER
    POSTGRES_PASSWORD: SecretStr # MUST be provided via INGEST_POSTGRES_PASSWORD env var
    POSTGRES_SERVER: str = SUPABASE_DEFAULT_HOST
    POSTGRES_PORT: int = SUPABASE_DEFAULT_PORT # Use int directly
    POSTGRES_DB: str = SUPABASE_DEFAULT_DB
    # POSTGRES_DSN allows overriding with a full DSN string if needed,
    # otherwise it will be constructed by the validator below.
    POSTGRES_DSN: Optional[PostgresDsn] = None

    # --- Milvus (Keep as is, assuming it's separate) ---
    MILVUS_URI: str = "http://milvus-service:19530" # K8s service name
    MILVUS_COLLECTION_NAME: str = "document_chunks_haystack"
    MILVUS_INDEX_PARAMS: dict = Field(default={
        "metric_type": "COSINE",
        "index_type": "HNSW",
        "params": {"M": 16, "efConstruction": 256}
    })
    MILVUS_SEARCH_PARAMS: dict = Field(default={"metric_type": "COSINE", "params": {"ef": 128}})

    # --- MinIO Storage (Keep as is or adapt if using Supabase Storage) ---
    # If using Supabase Storage, you'd replace MinIO settings/client
    # with Supabase Storage configurations and relevant client library.
    MINIO_ENDPOINT: str = "minio-service:9000" # K8s service name and port
    MINIO_ACCESS_KEY: SecretStr = SecretStr("minioadmin") # Load from Secret
    MINIO_SECRET_KEY: SecretStr = SecretStr("minioadmin") # Load from Secret
    MINIO_BUCKET_NAME: str = "ingested-documents"
    MINIO_USE_SECURE: bool = False # Use HTTP within cluster by default

    # --- External Services (Only OCR if needed) ---
    OCR_SERVICE_URL: Optional[AnyHttpUrl] = None # type: ignore

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
    OPENAI_API_KEY: SecretStr # MUST be provided via INGEST_OPENAI_API_KEY env var
    OPENAI_EMBEDDING_MODEL: str = "text-embedding-3-small"
    EMBEDDING_DIMENSION: int = 1536

    # --- MilvusDocumentStore Fields ---
    MILVUS_CONTENT_FIELD: str = "content"
    MILVUS_EMBEDDING_FIELD: str = "embedding"
    MILVUS_METADATA_FIELDS: List[str] = Field(default=["company_id", "document_id", "file_name", "file_type"]) # Matches schema/usage

    # --- Validators ---
    @validator("POSTGRES_DSN", pre=True, always=True)
    def assemble_postgres_dsn(cls, v: Optional[str], values: dict[str, Any]) -> Any:
        """
        Constructs the DSN string for asyncpg if not provided explicitly.
        Ensures the format is correct for Supabase Direct Connection.
        """
        if isinstance(v, str):
            # If INGEST_POSTGRES_DSN is provided, validate it.
            # Pydantic's PostgresDsn will handle basic format validation.
            # Ensure it uses postgresql+asyncpg scheme if you rely on asyncpg.
            try:
                # Basic check to ensure it's a valid PostgresDsn
                dsn = PostgresDsn(v)
                # Important: Replace scheme if necessary for asyncpg
                if dsn.scheme != "postgresql+asyncpg":
                    # Rebuild with the correct scheme if validation passed
                     return dsn.build(scheme="postgresql+asyncpg")
                return str(dsn) # Return the validated DSN string
            except ValidationError as e:
                raise ValueError(f"Invalid INGEST_POSTGRES_DSN provided: {e}") from e
            return v # Return the provided string

        # If INGEST_POSTGRES_DSN is not set, build it from parts
        password_obj = values.get("POSTGRES_PASSWORD")
        if not password_obj:
             raise ValueError("INGEST_POSTGRES_PASSWORD environment variable is required.")

        password_value = password_obj.get_secret_value()

        try:
            # Construct the DSN using pydantic's validation power
            dsn = PostgresDsn.build(
                scheme="postgresql+asyncpg", # Use the asyncpg scheme
                username=values.get("POSTGRES_USER"),
                password=password_value,
                host=values.get("POSTGRES_SERVER"),
                port=values.get("POSTGRES_PORT"), # Already validated as int
                path=f"/{values.get('POSTGRES_DB') or ''}",
            )
            return str(dsn) # Return the built DSN string
        except ValidationError as e:
            # This might catch issues like invalid host, port, etc. during build
            raise ValueError(f"Failed to assemble Postgres DSN from parts: {e}") from e

    @validator("EMBEDDING_DIMENSION", pre=True, always=True)
    def set_embedding_dimension(cls, v: int, values: dict[str, Any]) -> int:
        # This validator remains the same
        model = values.get("OPENAI_EMBEDDING_MODEL")
        if model == "text-embedding-3-large":
            return 3072
        elif model in ["text-embedding-3-small", "text-embedding-ada-002"]:
            return 1536
        # Add other models if needed
        if v == 0 and not model: # Avoid setting to 0 if no model specified and no value given
             return 1536 # Default to small/ada if not set
        return v # Return existing or default if model not matched

# Create the settings instance, this will trigger validation
try:
    settings = Settings()
except ValidationError as e:
    print(f"FATAL: Configuration validation failed:\n{e}")
    # Optionally, exit here if running in a script context where settings are crucial
    # import sys
    # sys.exit(1)