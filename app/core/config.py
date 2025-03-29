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
        final_dsn_str: Optional[str] = None

        if isinstance(v, str) and v.startswith("postgres"): # Verificar si se pasó un DSN válido
             # Si se proporciona un DSN completo, intentar usarlo directamente pero asegurar esquema
             print(f"DEBUG: Attempting to use provided DSN: {v!r}")
             try:
                 dsn = PostgresDsn(v)
                 # Asegurarse de que usa asyncpg
                 if dsn.scheme == "postgresql+asyncpg":
                      final_dsn_str = str(dsn)
                 elif dsn.scheme == "postgresql":
                      # Reconstruir la cadena con el esquema correcto
                      # Nota: Acceder a los atributos puede dar None si no están en el DSN original
                      user_part = f"{dsn.user}:{dsn.password}@" if dsn.user and dsn.password else (f"{dsn.user}@" if dsn.user else "")
                      host_part = dsn.host or ""
                      port_part = f":{dsn.port}" if dsn.port is not None else ""
                      db_part = dsn.path or "/postgres" # Default a /postgres si no hay path
                      final_dsn_str = f"postgresql+asyncpg://{user_part}{host_part}{port_part}{db_part}"
                      print(f"DEBUG: Rebuilt DSN with asyncpg scheme: {final_dsn_str!r}")
                 else:
                      raise ValueError(f"Unsupported scheme in provided DSN: {dsn.scheme}")

                 # Validar el DSN final (ya sea el original o el reconstruido)
                 validated_dsn = PostgresDsn(final_dsn_str)
                 print(f"DEBUG: Successfully validated provided/rebuilt DSN: {str(validated_dsn)!r}")
                 return str(validated_dsn)

             except ValidationError as e:
                 print(f"ERROR: Validation failed for provided DSN {v!r}: {e}")
                 raise ValueError(f"Invalid INGEST_POSTGRES_DSN provided: {e}") from e
             except Exception as e:
                 print(f"ERROR: Unexpected error processing provided DSN {v!r}: {e}")
                 # Mostrar traceback para errores inesperados
                 import traceback
                 traceback.print_exc()
                 raise ValueError(f"Unexpected error processing provided DSN: {e}") from e

        # --- Construir DSN desde las partes si no se proporcionó uno completo o si 'v' no era DSN ---
        print("DEBUG: Provided value 'v' is not a DSN string or check failed. Building DSN from parts.")
        password_obj = values.get("POSTGRES_PASSWORD")
        if not password_obj:
             raise ValueError("INGEST_POSTGRES_PASSWORD environment variable is required.")
        # Asegurarse de que get_secret_value() no falle si password_obj no es SecretStr (aunque debería serlo)
        password_value = password_obj.get_secret_value() if hasattr(password_obj, 'get_secret_value') else str(password_obj)

        user = values.get("POSTGRES_USER")
        server = values.get("POSTGRES_SERVER")
        # 'port' ya es un entero gracias a la definición del campo
        port_int = values.get("POSTGRES_PORT")
        db_name = values.get("POSTGRES_DB")

        # Verificar que todas las partes necesarias están presentes
        if not all([user, server, port_int is not None, db_name]):
             missing = [k for k, val in {"user": user, "server": server, "port": port_int, "db": db_name}.items() if val is None or str(val).strip() == '']
             # Asegurarse de añadir 'port' si es None
             if port_int is None: missing.append("port")
             # Imprimir valores para depuración
             print(f"DEBUG: Missing parts check failed. Values: user={user!r}, server={server!r}, port={port_int!r}, db={db_name!r}, password_present={bool(password_obj)}")
             raise ValueError(f"Missing required PostgreSQL connection parts: {missing}")

        print(f"DEBUG: Assembling DSN string from parts: user={user!r}, server={server!r}, port={port_int!r}, db={db_name!r}")
        try:
            # *** CONSTRUIR LA CADENA DSN MANUALMENTE ***
            dsn_str = f"postgresql+asyncpg://{user}:{password_value}@{server}:{port_int}/{db_name}"
            print(f"DEBUG: Assembled DSN string: {dsn_str!r}") # Log importante

            # *** VALIDAR LA CADENA CONSTRUIDA CON PostgresDsn() ***
            validated_dsn = PostgresDsn(dsn_str)
            print(f"DEBUG: Successfully validated assembled DSN: {str(validated_dsn)!r}")
            return str(validated_dsn) # Devolver como string

        except ValidationError as e:
            # Error durante la validación de la cadena construida
            print(f"ERROR: Validation failed for assembled DSN string {dsn_str!r}: {e}")
            raise ValueError(f"Failed to validate assembled Postgres DSN string: {e}") from e
        except Exception as e:
             # Otros errores inesperados durante la construcción o validación
             print(f"ERROR: Unexpected error assembling/validating DSN from parts. String was: {'Not assembled'}. Error: {e}")
             import traceback
             traceback.print_exc()
             raise ValueError(f"Unexpected error assembling/validating Postgres DSN from parts: {e}") from e

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