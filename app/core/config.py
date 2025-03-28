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
    POSTGRES_PASSWORD: SecretStr
    POSTGRES_SERVER: str = SUPABASE_DEFAULT_HOST
    POSTGRES_PORT: int = SUPABASE_DEFAULT_PORT
    POSTGRES_DB: str = SUPABASE_DEFAULT_DB
    POSTGRES_DSN: Optional[PostgresDsn] = None

    # --- Milvus ---
    MILVUS_URI: str = "http://milvus-service:19530" # K8s service name for Milvus
    MILVUS_COLLECTION_NAME: str = "document_chunks_haystack"
    MILVUS_INDEX_PARAMS: dict = Field(default={
        "metric_type": "COSINE", # COSINE is common for text embeddings
        "index_type": "HNSW",    # HNSW is a good balance of speed/accuracy
        "params": {"M": 16, "efConstruction": 256} # Adjust based on Milvus docs/tuning
    })
    MILVUS_SEARCH_PARAMS: dict = Field(default={
        "metric_type": "COSINE",
        "params": {"ef": 128} # Adjust based on Milvus docs/tuning
    })

    # --- MilvusDocumentStore Fields ---
    MILVUS_CONTENT_FIELD: str = "content" # Field in Milvus to store the chunk text
    MILVUS_EMBEDDING_FIELD: str = "embedding" # Field in Milvus to store the vector

    # *** CORRECCIÓN IMPORTANTE ***
    # Define ALL metadata fields you want stored AND FILTERABLE in Milvus.
    # These MUST match the schema you create in your Milvus collection.
    # Add fields from 'original_metadata' that you care about here.
    MILVUS_METADATA_FIELDS: List[str] = Field(default=[
        "company_id",     # For multi-tenant filtering
        "document_id",    # To link back to the original document
        "file_name",      # Original file name
        "file_type",      # Original content type
        # --- Añade aquí campos adicionales importantes de 'original_metadata' ---
        # "category",       # Ejemplo: 'report', 'email', 'article'
        # "source",         # Ejemplo: 'upload', 'web_scrape'
        # "author",         # Ejemplo
        # "language"        # Ejemplo: si lo extraes o viene en metadata
        # "department"      # Ejemplo: 'finance', 'legal'
        # ----------------------------------------------------------------------
    ])

    # --- MinIO Storage ---
    MINIO_ENDPOINT: str = "minio-service:9000" # K8s service name and port
    MINIO_ACCESS_KEY: SecretStr
    MINIO_SECRET_KEY: SecretStr
    MINIO_BUCKET_NAME: str = "ingested-documents"
    MINIO_USE_SECURE: bool = False # HTTP within cluster

    # --- External Services ---
    OCR_SERVICE_URL: Optional[AnyHttpUrl] = None

    # --- Service Client Config ---
    HTTP_CLIENT_TIMEOUT: int = 60
    HTTP_CLIENT_MAX_RETRIES: int = 2
    HTTP_CLIENT_BACKOFF_FACTOR: float = 1.0

    # --- File Processing & Haystack ---
    SUPPORTED_CONTENT_TYPES: List[str] = Field(default=[
        "application/pdf",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document", # docx
        "text/plain", "text/markdown", "text/html",
        "image/jpeg", "image/png",
    ])
    EXTERNAL_OCR_REQUIRED_CONTENT_TYPES: List[str] = Field(default=["image/jpeg", "image/png"])
    SPLITTER_CHUNK_SIZE: int = 500
    SPLITTER_CHUNK_OVERLAP: int = 50
    SPLITTER_SPLIT_BY: str = "word" # or "sentence", "passage"

    # --- OpenAI ---
    OPENAI_API_KEY: SecretStr
    OPENAI_EMBEDDING_MODEL: str = "text-embedding-3-small"
    EMBEDDING_DIMENSION: int = 1536 # Default for text-embedding-3-small/ada-002

    # --- Validators ---
    @validator("POSTGRES_DSN", pre=True, always=True)
    def assemble_postgres_dsn(cls, v: Optional[str], values: dict[str, Any]) -> Any:
        if isinstance(v, str):
            try:
                dsn = PostgresDsn(v)
                if dsn.scheme != "postgresql+asyncpg":
                     return dsn.build(scheme="postgresql+asyncpg") # Force asyncpg scheme
                return str(dsn)
            except ValidationError as e:
                raise ValueError(f"Invalid INGEST_POSTGRES_DSN provided: {e}") from e
        password_obj = values.get("POSTGRES_PASSWORD")
        if not password_obj:
             raise ValueError("INGEST_POSTGRES_PASSWORD environment variable is required.")
        password_value = password_obj.get_secret_value()
        try:
            dsn = PostgresDsn.build(
                scheme="postgresql+asyncpg",
                username=values.get("POSTGRES_USER"),
                password=password_value,
                host=values.get("POSTGRES_SERVER"),
                port=values.get("POSTGRES_PORT"),
                path=f"/{values.get('POSTGRES_DB') or ''}",
            )
            return str(dsn)
        except ValidationError as e:
            raise ValueError(f"Failed to assemble Postgres DSN from parts: {e}") from e

    @validator("EMBEDDING_DIMENSION", pre=True, always=True)
    def set_embedding_dimension(cls, v: int, values: dict[str, Any]) -> int:
        model = values.get("OPENAI_EMBEDDING_MODEL")
        if model == "text-embedding-3-large":
            return 3072
        elif model in ["text-embedding-3-small", "text-embedding-ada-002"]:
            return 1536
        if v == 0 and not model:
             return 1536 # Default if not set
        return v

# Create the settings instance globally
try:
    settings = Settings()
except ValidationError as e:
    # Log this critical error clearly during startup
    print(f"FATAL: Configuration validation failed:\n{e}")
    # Exit if configuration is invalid, essential for app function
    import sys
    sys.exit(1)