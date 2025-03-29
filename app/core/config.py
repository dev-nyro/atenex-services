# ./app/core/config.py (CORREGIDO - URLs de Celery apuntan a redis-service-master)
import logging
import os
from typing import Optional, List, Any
from pydantic_settings import BaseSettings, SettingsConfigDict
# from pydantic import RedisDsn, PostgresDsn, AnyHttpUrl, SecretStr, Field, validator, ValidationError, ValidationInfo # Pydantic v2
from pydantic import RedisDsn, PostgresDsn, AnyHttpUrl, SecretStr, Field, validator, ValidationError # Pydantic v1 style
import sys
import traceback # Importar traceback para logs más detallados

# --- Supabase Connection Defaults ---
SUPABASE_DEFAULT_HOST = "db.ymsilkrhstwxikjiqqog.supabase.co"
SUPABASE_DEFAULT_PORT_INT = 5432
SUPABASE_DEFAULT_DB = "postgres"
SUPABASE_DEFAULT_USER = "postgres"

# --- Milvus Kubernetes Defaults ---
MILVUS_K8S_HOST = "milvus-milvus.default.svc.cluster.local" # Asume que Milvus está en el namespace 'default', ajustar si es necesario
MILVUS_K8S_PORT = 19530

# --- Redis Kubernetes Defaults ---
# *** CORRECCIÓN: Usar el nombre de servicio probable dentro del mismo namespace ***
# Asume que el servicio se llama 'redis-service-master' y está en el mismo namespace que el worker/api
REDIS_K8S_SERVICE = "redis-service-master"
REDIS_K8S_PORT = 6379

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
    # *** CORREGIDO: Apuntar al nombre de servicio correcto (sin namespace si está en el mismo) ***
    CELERY_BROKER_URL: RedisDsn = RedisDsn(f"redis://{REDIS_K8S_SERVICE}:{REDIS_K8S_PORT}/0")
    CELERY_RESULT_BACKEND: RedisDsn = RedisDsn(f"redis://{REDIS_K8S_SERVICE}:{REDIS_K8S_PORT}/1")

    # --- Database (Supabase Direct Connection Settings) ---
    POSTGRES_USER: str = SUPABASE_DEFAULT_USER
    POSTGRES_PASSWORD: SecretStr
    POSTGRES_SERVER: str = SUPABASE_DEFAULT_HOST
    POSTGRES_PORT: int = SUPABASE_DEFAULT_PORT_INT # Correcto: int
    POSTGRES_DB: str = SUPABASE_DEFAULT_DB
    POSTGRES_DSN: Optional[PostgresDsn] = None # Se ensamblará en el validador

    # --- Milvus ---
    MILVUS_HOST: str = MILVUS_K8S_HOST
    MILVUS_PORT: int = MILVUS_K8S_PORT
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
        # Añadir aquí CUALQUIER otro campo de metadata que quieras almacenar en Milvus
    ])

    # --- MinIO Storage ---
    MINIO_ENDPOINT: str = "minio-service:9000" # Asume que minio-service está en el mismo namespace
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
        "image/jpeg", # Requiere OCR
        "image/png",  # Requiere OCR
    ])
    EXTERNAL_OCR_REQUIRED_CONTENT_TYPES: List[str] = Field(default=[
        "image/jpeg",
        "image/png"
    ])
    SPLITTER_CHUNK_SIZE: int = 500
    SPLITTER_CHUNK_OVERLAP: int = 50
    SPLITTER_SPLIT_BY: str = "word" # O 'sentence', 'passage'

    # --- OpenAI ---
    OPENAI_API_KEY: SecretStr
    OPENAI_EMBEDDING_MODEL: str = "text-embedding-3-small"
    EMBEDDING_DIMENSION: int = 1536

    # --- Validators ---
    # Validador de DSN de Postgres (mantener como estaba)
    @validator("POSTGRES_DSN", pre=True, always=True)
    def assemble_postgres_dsn(cls, v: Optional[str], values: dict[str, Any]) -> Any:
        # La lógica existente aquí es para validar/ensamblar si se *proporciona* un DSN
        # o para construir uno a partir de las partes. Dado que postgres_client.py
        # usa los argumentos directamente, esta validación principalmente asegura
        # que las variables de entorno individuales están presentes si no se da un DSN.
        # Mantenemos la lógica original para esa validación y los logs de debug.
        final_dsn_str: Optional[str] = None
        if isinstance(v, str) and v.startswith("postgres"):
             print(f"DEBUG: Attempting to use provided DSN: {v!r}")
             try:
                 dsn = PostgresDsn(v)
                 if dsn.scheme == "postgresql+asyncpg": final_dsn_str = str(dsn)
                 elif dsn.scheme == "postgresql":
                      user_part = f"{dsn.user}:{dsn.password}@" if dsn.user and dsn.password else (f"{dsn.user}@" if dsn.user else "")
                      host_part = dsn.host or ""
                      port_part = f":{dsn.port}" if dsn.port is not None else ""
                      db_part = dsn.path or "/postgres"
                      final_dsn_str = f"postgresql+asyncpg://{user_part}{host_part}{port_part}{db_part}"
                      print(f"DEBUG: Rebuilt DSN with asyncpg scheme: {final_dsn_str!r}")
                 else: raise ValueError(f"Unsupported scheme in provided DSN: {dsn.scheme}")
                 validated_dsn = PostgresDsn(final_dsn_str)
                 print(f"DEBUG: Successfully validated provided/rebuilt DSN: {str(validated_dsn)!r}")
                 return str(validated_dsn)
             except ValidationError as e:
                 print(f"ERROR: Validation failed for provided DSN {v!r}: {e}")
                 raise ValueError(f"Invalid INGEST_POSTGRES_DSN provided: {e}") from e
             except Exception as e:
                 print(f"ERROR: Unexpected error processing provided DSN {v!r}: {e}")
                 traceback.print_exc()
                 raise ValueError(f"Unexpected error processing provided DSN: {e}") from e
        print("DEBUG: Provided value 'v' is not a DSN string or check failed. Building DSN from parts.")
        password_obj = values.get("POSTGRES_PASSWORD")
        if not password_obj: raise ValueError("INGEST_POSTGRES_PASSWORD environment variable is required.")
        password_value = password_obj.get_secret_value() if hasattr(password_obj, 'get_secret_value') else str(password_obj)
        user = values.get("POSTGRES_USER")
        server = values.get("POSTGRES_SERVER")
        port_int = values.get("POSTGRES_PORT")
        db_name = values.get("POSTGRES_DB")
        if not all([user, server, port_int is not None, db_name]):
             missing = [k for k, val in {"user": user, "server": server, "port": port_int, "db": db_name}.items() if val is None or str(val).strip() == '']
             if port_int is None: missing.append("port")
             print(f"DEBUG: Missing parts check failed. Values: user={user!r}, server={server!r}, port={port_int!r}, db={db_name!r}, password_present={bool(password_obj)}")
             raise ValueError(f"Missing required PostgreSQL connection parts: {missing}")
        print(f"DEBUG: Assembling DSN string from parts: user={user!r}, server={server!r}, port={port_int!r}, db={db_name!r}")
        try:
            # Construye un DSN sólo para validación interna y debug logging
            dsn_str = f"postgresql+asyncpg://{user}:{password_value}@{server}:{port_int}/{db_name}"
            print(f"DEBUG: Assembled DSN string: {dsn_str!r}")
            validated_dsn = PostgresDsn(dsn_str)
            print(f"DEBUG: Successfully validated assembled DSN: {str(validated_dsn)!r}")
            # Devolvemos el DSN validado, aunque no se use directamente en create_pool
            return str(validated_dsn)
        except ValidationError as e:
            print(f"ERROR: Validation failed for assembled DSN string {dsn_str!r}: {e}")
            raise ValueError(f"Failed to validate assembled Postgres DSN string: {e}") from e
        except Exception as e:
             print(f"ERROR: Unexpected error assembling/validating DSN from parts. Error: {e}")
             traceback.print_exc()
             raise ValueError(f"Unexpected error assembling/validating Postgres DSN from parts: {e}") from e


    # Validador de dimensión de embedding (sin cambios necesarios)
    @validator("EMBEDDING_DIMENSION", pre=True, always=True)
    def set_embedding_dimension(cls, v: Optional[int], values: dict[str, Any]) -> int:
        # La lógica existente aquí es correcta
        model = values.get("OPENAI_EMBEDDING_MODEL")
        if model == "text-embedding-3-large": return 3072
        elif model in ["text-embedding-3-small", "text-embedding-ada-002"]: return 1536
        # Fallback si v no está o es 0, o si el modelo no coincide
        if v is None or v <= 0:
             print(f"WARN: INGEST_EMBEDDING_DIMENSION not set or invalid ({v}), defaulting based on model ({model}) or to 1536.")
             if model == "text-embedding-3-large": return 3072
             # Para small, ada, u otros no reconocidos, usar 1536 por defecto
             return 1536
        return v

# --- Instancia Global ---
try:
    settings = Settings()
except (ValidationError, ValueError) as e:
    error_details = ""
    if isinstance(e, ValidationError):
        try: error_details = f"\nValidation Errors:\n{e.json(indent=2)}"
        except Exception: error_details = f"\nRaw Errors: {e.errors()}"
    print(f"FATAL: Configuration validation failed:{error_details}\nOriginal Error: {e}")
    sys.exit(1)
except Exception as e:
    print(f"FATAL: Unexpected error during Settings instantiation:\n{e}")
    traceback.print_exc()
    sys.exit(1)