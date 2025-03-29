# ./app/core/config.py (CORREGIDO - POSTGRES_PORT como int)
import logging
import os
from typing import Optional, List, Any
from pydantic_settings import BaseSettings, SettingsConfigDict
# Asegúrate de importar ValidationInfo si usas Pydantic v2 y @field_validator
# from pydantic import RedisDsn, PostgresDsn, AnyHttpUrl, SecretStr, Field, validator, ValidationError, ValidationInfo
from pydantic import RedisDsn, PostgresDsn, AnyHttpUrl, SecretStr, Field, validator, ValidationError
import sys

# --- Supabase Connection Defaults ---
SUPABASE_DEFAULT_HOST = "db.ymsilkrhstwxikjiqqog.supabase.co"
# SUPABASE_DEFAULT_PORT_STR = "5432" # Ya no se necesita como string por defecto
SUPABASE_DEFAULT_PORT_INT = 5432 # <--- Usar entero por defecto
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
    # *** CORREGIDO: Definir como entero ***
    POSTGRES_PORT: int = SUPABASE_DEFAULT_PORT_INT # <--- TIPO CAMBIADO A INT y default a entero
    POSTGRES_DB: str = SUPABASE_DEFAULT_DB
    POSTGRES_DSN: Optional[PostgresDsn] = None # Se ensamblará en el validador

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
        # "category",
        # "source",
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
    SUPPORTED_CONTENT_TYPES: List[str] = Field(default=[
        "application/pdf",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document", # docx
        "text/plain",
        "text/markdown",
        "text/html",
        "image/jpeg",
        "image/png",
    ])
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
    # Usando @validator (estilo Pydantic v1 como en tu original)
    @validator("POSTGRES_DSN", pre=True, always=True)
    def assemble_postgres_dsn(cls, v: Optional[str], values: dict[str, Any]) -> Any:
        if isinstance(v, str):
            # Si se proporciona un DSN completo, intentar usarlo directamente
            try:
                dsn = PostgresDsn(v)
                # Asegurarse de que usa asyncpg
                if dsn.scheme != "postgresql+asyncpg":
                     # Intentar construir con el esquema correcto usando build (Pydantic v1)
                     built_dsn = dsn.build(
                         scheme="postgresql+asyncpg",
                         user=dsn.user or '',
                         password=dsn.password or '',
                         host=dsn.host or '',
                         # Pasar puerto como int si existe, build debería manejarlo
                         port=dsn.port if dsn.port is not None else None,
                         path=dsn.path or ''
                     )
                     validated_built_dsn = PostgresDsn(built_dsn)
                     return str(validated_built_dsn)

                validated_dsn = PostgresDsn(str(dsn))
                return str(validated_dsn)
            except ValidationError as e:
                raise ValueError(f"Invalid INGEST_POSTGRES_DSN provided or failed to rebuild: {e}") from e
            except Exception as e:
                 raise ValueError(f"Unexpected error processing provided DSN: {e}") from e

        # --- Construir DSN desde las partes si no se proporcionó uno completo ---
        password_obj = values.get("POSTGRES_PASSWORD")
        if not password_obj:
             raise ValueError("INGEST_POSTGRES_PASSWORD environment variable is required.")
        password_value = password_obj.get_secret_value()
        user = values.get("POSTGRES_USER")
        server = values.get("POSTGRES_SERVER")
        # 'port' ya es un entero gracias a la definición del campo
        port_int = values.get("POSTGRES_PORT")
        db_name = values.get("POSTGRES_DB")

        if not all([user, server, port_int is not None, db_name]):
             missing = [k for k, val in {"user": user, "server": server, "port": port_int, "db": db_name}.items() if val is None or val == '']
             if port_int is None: missing.append("port")
             raise ValueError(f"Missing required PostgreSQL connection parts: {missing}")

        try:
            # *** USAR PostgresDsn.build PASANDO EL ENTERO DIRECTAMENTE ***
            dsn_built = PostgresDsn.build(
                scheme="postgresql+asyncpg",
                username=user,
                password=password_value,
                host=server,
                port=port_int, # <--- PASAR EL ENTERO DIRECTAMENTE
                path=f"/{db_name}"
            )
            # Validar el DSN construido
            validated_dsn = PostgresDsn(dsn_built)
            return str(validated_dsn)

        except ValidationError as e:
            print(f"DEBUG: Failed DSN build/validation. Parts used: user={user!r}, server={server!r}, port={port_int!r}, db={db_name!r}")
            raise ValueError(f"Failed to assemble or validate Postgres DSN from parts using build: {e}") from e
        except Exception as e:
             print(f"DEBUG: Unexpected error during DSN build. Parts used: user={user!r}, server={server!r}, port={port_int!r}, db={db_name!r}")
             # Mostrar el traceback específico de este error puede ser útil
             import traceback
             print("Traceback for unexpected error during build:")
             traceback.print_exc()
             # El error original ya indica str vs int, pero mantenemos la estructura por si cambia
             raise ValueError(f"Unexpected error assembling Postgres DSN using build: {e}") from e

    # --- set_embedding_dimension validator (sin cambios) ---
    @validator("EMBEDDING_DIMENSION", pre=True, always=True)
    def set_embedding_dimension(cls, v: Optional[int], values: dict[str, Any]) -> int:
        model = values.get("OPENAI_EMBEDDING_MODEL")
        # Lógica existente para determinar la dimensión basada en el modelo
        if model == "text-embedding-3-large":
            return 3072
        elif model in ["text-embedding-3-small", "text-embedding-ada-002"]:
            return 1536
        # Fallback si no hay modelo o si se proporciona un valor explícito
        if v is None or v == 0:
            # Si no hay valor explícito, intentar inferir de nuevo (redundante pero seguro)
            if model:
                if model == "text-embedding-3-large": return 3072
                if model in ["text-embedding-3-small", "text-embedding-ada-002"]: return 1536
            # Default final si no se puede inferir y no se proporcionó valor
            return 1536 # O el default que consideres más apropiado
        # Si se proporcionó un valor explícito (v), usarlo
        return v

# Create the settings instance globally
try:
    settings = Settings()
except (ValidationError, ValueError) as e:
    # Mejorar el mensaje de error para incluir detalles de validación si están disponibles
    error_details = ""
    if isinstance(e, ValidationError):
        try:
            # Intentar formatear los errores de Pydantic para mayor claridad
            error_details = f"\nValidation Errors:\n{e.json(indent=2)}"
        except Exception: # Por si json() falla
             error_details = f"\nRaw Errors: {e.errors()}"

    print(f"FATAL: Configuration validation failed:{error_details}\nOriginal Error: {e}")
    sys.exit(1)
except Exception as e:
    print(f"FATAL: Unexpected error during Settings instantiation:\n{e}")
    # Considerar imprimir el traceback completo en caso de errores inesperados
    import traceback
    traceback.print_exc()
    sys.exit(1)