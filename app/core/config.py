# ./app/core/config.py (CON LOGGING DE DEPURACIÓN EN EL VALIDADOR)
import logging
import os
from typing import Optional, List, Any
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import RedisDsn, PostgresDsn, AnyHttpUrl, SecretStr, Field, validator, ValidationError
import sys # Import sys for exit

# --- Supabase Connection Defaults ---
# (Estos no cambian)
SUPABASE_DEFAULT_HOST = "db.ymsilkrhstwxikjiqqog.supabase.co"
SUPABASE_DEFAULT_PORT = 5432
SUPABASE_DEFAULT_DB = "postgres"
SUPABASE_DEFAULT_USER = "postgres"

# Get a basic logger for early debugging if needed
# Note: This runs *before* structlog might be fully configured by setup_logging()
# Using print might be more reliable at this very early stage.
print("DEBUG: Loading config.py...")

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
    # (Configuración de Milvus sin cambios)
    MILVUS_URI: str = "http://milvus-service:19530"
    MILVUS_COLLECTION_NAME: str = "document_chunks_haystack"
    MILVUS_INDEX_PARAMS: dict = Field(default={...}) # Keep details
    MILVUS_SEARCH_PARAMS: dict = Field(default={...}) # Keep details
    MILVUS_CONTENT_FIELD: str = "content"
    MILVUS_EMBEDDING_FIELD: str = "embedding"
    MILVUS_METADATA_FIELDS: List[str] = Field(default=[...]) # Keep details

    # --- MinIO Storage ---
    # (Configuración de MinIO sin cambios)
    MINIO_ENDPOINT: str = "minio-service:9000"
    MINIO_ACCESS_KEY: SecretStr
    MINIO_SECRET_KEY: SecretStr
    MINIO_BUCKET_NAME: str = "ingested-documents"
    MINIO_USE_SECURE: bool = False

    # --- External Services ---
    OCR_SERVICE_URL: Optional[AnyHttpUrl] = None

    # --- Service Client Config ---
    # (Configuración del cliente HTTP sin cambios)
    HTTP_CLIENT_TIMEOUT: int = 60
    HTTP_CLIENT_MAX_RETRIES: int = 2
    HTTP_CLIENT_BACKOFF_FACTOR: float = 1.0

    # --- File Processing & Haystack ---
    # (Configuración de procesamiento sin cambios)
    SUPPORTED_CONTENT_TYPES: List[str] = Field(default=[...]) # Keep details
    EXTERNAL_OCR_REQUIRED_CONTENT_TYPES: List[str] = Field(default=[...]) # Keep details
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
        Includes debugging output.
        """
        print(f"DEBUG: assemble_postgres_dsn called. Provided v='{v}'")
        print(f"DEBUG: values dict received by validator: {values}") # Log the whole dict

        if isinstance(v, str):
            print(f"DEBUG: INGEST_POSTGRES_DSN provided directly: '{v}'")
            try:
                dsn = PostgresDsn(v)
                if dsn.scheme != "postgresql+asyncpg":
                     print("DEBUG: Forcing scheme to postgresql+asyncpg")
                     built_dsn = dsn.build(scheme="postgresql+asyncpg")
                     print(f"DEBUG: Built DSN from provided string: '{built_dsn}'")
                     return built_dsn
                print(f"DEBUG: Returning validated provided DSN: '{str(dsn)}'")
                return str(dsn)
            except ValidationError as e:
                print(f"ERROR: Validation failed for provided INGEST_POSTGRES_DSN: {e}")
                raise ValueError(f"Invalid INGEST_POSTGRES_DSN provided: {e}") from e

        # --- Building DSN from parts ---
        print("DEBUG: INGEST_POSTGRES_DSN not provided, attempting to build from parts...")
        password_obj = values.get("POSTGRES_PASSWORD")
        password_value = password_obj.get_secret_value() if password_obj else None
        user = values.get("POSTGRES_USER")
        server = values.get("POSTGRES_SERVER")
        port = values.get("POSTGRES_PORT")
        db_name = values.get("POSTGRES_DB")

        # Log the parts being used for building
        print(f"DEBUG: Building DSN with:")
        print(f"  - User: {user}")
        print(f"  - Password Provided: {bool(password_value)}") # Don't log the password itself
        print(f"  - Server: {server}")
        print(f"  - Port: {port}")
        print(f"  - DB Name: {db_name}")

        if not password_obj:
             print("ERROR: INGEST_POSTGRES_PASSWORD environment variable is required.")
             raise ValueError("INGEST_POSTGRES_PASSWORD environment variable is required.")
        if not all([user, server, port, db_name]):
             missing = [k for k, val in {"user": user, "server": server, "port": port, "db": db_name}.items() if not val]
             print(f"ERROR: Missing required PostgreSQL connection parts: {missing}")
             raise ValueError(f"Missing required PostgreSQL connection parts: {missing}")

        try:
            # Construct the DSN (logic remains the same - path correction applied previously)
            dsn = PostgresDsn.build(
                scheme="postgresql+asyncpg",
                username=user,
                password=password_value,
                host=server,
                port=port,
                path=db_name or None, # Corrected logic
            )
            built_dsn_str = str(dsn)
            print(f"DEBUG: Successfully built DSN from parts: '{built_dsn_str}'")
            return built_dsn_str
        except ValidationError as e:
            print(f"ERROR: Pydantic validation failed during DSN build from parts: {e}")
            # It's helpful to see the internal error details here
            raise ValueError(f"Failed to assemble Postgres DSN from parts: {e}") from e
        except Exception as e:
            # Catch any other unexpected error during build
            print(f"ERROR: Unexpected error during DSN build from parts: {e}")
            raise ValueError(f"Unexpected error assembling Postgres DSN: {e}") from e


    # --- set_embedding_dimension validator remains the same ---
    @validator("EMBEDDING_DIMENSION", pre=True, always=True)
    def set_embedding_dimension(cls, v: Optional[int], values: dict[str, Any]) -> int:
        # ... (logic from previous version) ...
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

# --- Global Settings Initialization ---
try:
    print("DEBUG: Initializing Settings instance...")
    settings = Settings()
    print("DEBUG: Settings instance initialized successfully.")
    # Optional: Log DSN after successful validation
    # print(f"INFO: Final POSTGRES_DSN='{settings.POSTGRES_DSN}'")
except ValidationError as e:
    print(f"FATAL: Configuration validation failed during Settings instantiation:\n{e}")
    sys.exit(1)
except ValueError as e: # Catch ValueErrors raised explicitly from validator
    print(f"FATAL: Configuration validation failed due to ValueError:\n{e}")
    sys.exit(1)
except Exception as e: # Catch any other unexpected error during init
    print(f"FATAL: Unexpected error during Settings instantiation:\n{e}")
    sys.exit(1)

print("DEBUG: config.py loaded.")