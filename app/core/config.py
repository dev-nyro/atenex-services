# ./app/core/config.py (CORREGIDO)
import logging
import os
from typing import Optional, List, Any, Dict # Import Dict
from pydantic_settings import BaseSettings, SettingsConfigDict
# from pydantic import RedisDsn, PostgresDsn, AnyHttpUrl, SecretStr, Field, validator, ValidationError, ValidationInfo # Pydantic v2 style
from pydantic import RedisDsn, AnyHttpUrl, SecretStr, Field, validator, ValidationError, HttpUrl # Pydantic v1 style (HttpUrl instead of AnyHttpUrl sometimes needed)
import sys

# --- Supabase Connection Defaults (Usando Puerto del Pooler por defecto) ---
SUPABASE_DEFAULT_HOST = "db.ymsilkrhstwxikjiqqog.supabase.co"
# *** CORREGIDO: Puerto por defecto al del pooler ***
SUPABASE_DEFAULT_PORT_INT = 6543
SUPABASE_DEFAULT_DB = "postgres"
SUPABASE_DEFAULT_USER = "postgres"

# --- Milvus Kubernetes Defaults ---
# *** CORREGIDO: Usar URI por defecto basado en ConfigMap/K8s service name ***
MILVUS_K8S_DEFAULT_URI = "http://milvus-service.nyro-develop.svc.cluster.local:19530"

# --- Redis Kubernetes Defaults ---
# *** CORREGIDO: Apuntar al servicio master por defecto ***
REDIS_K8S_DEFAULT_HOST = "redis-service-master.nyro-develop.svc.cluster.local"
REDIS_K8S_DEFAULT_PORT = 6379

class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file='.env',
        env_prefix='INGEST_', # Asegúrate que tus variables de entorno usen este prefijo
        env_file_encoding='utf-8',
        case_sensitive=False,
        extra='ignore'
    )

    # --- General ---
    PROJECT_NAME: str = "Ingest Service (Haystack/K8s/Supabase/Pooler)"
    API_V1_STR: str = "/api/v1"
    LOG_LEVEL: str = "INFO"

    # --- Celery ---
    # *** CORREGIDO: Usar defaults basados en K8s service name ***
    CELERY_BROKER_URL: RedisDsn = RedisDsn(f"redis://{REDIS_K8S_DEFAULT_HOST}:{REDIS_K8S_DEFAULT_PORT}/0")
    CELERY_RESULT_BACKEND: RedisDsn = RedisDsn(f"redis://{REDIS_K8S_DEFAULT_HOST}:{REDIS_K8S_DEFAULT_PORT}/1")

    # --- Database (Supabase Transaction Pooler Settings) ---
    # *** CORREGIDO: Eliminado POSTGRES_DSN y su validador ***
    POSTGRES_USER: str = SUPABASE_DEFAULT_USER
    POSTGRES_PASSWORD: SecretStr # Obligatorio desde Secrets
    POSTGRES_SERVER: str = SUPABASE_DEFAULT_HOST
    POSTGRES_PORT: int = SUPABASE_DEFAULT_PORT_INT # Default al puerto del pooler
    POSTGRES_DB: str = SUPABASE_DEFAULT_DB

    # --- Milvus ---
    # *** CORREGIDO: Usar MILVUS_URI en lugar de host/port ***
    MILVUS_URI: AnyHttpUrl = AnyHttpUrl(MILVUS_K8S_DEFAULT_URI) # Usar AnyHttpUrl o HttpUrl
    MILVUS_COLLECTION_NAME: str = "document_chunks_haystack"
    MILVUS_INDEX_PARAMS: Dict[str, Any] = Field(default={ # Usa Dict en lugar de dict para Pydantic v1
        "metric_type": "COSINE",
        "index_type": "HNSW",
        "params": {"M": 16, "efConstruction": 256}
    })
    MILVUS_SEARCH_PARAMS: Dict[str, Any] = Field(default={ # Usa Dict en lugar de dict para Pydantic v1
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
        # Añade aquí otros campos de metadatos que SÍ QUIERES indexar en Milvus
        # "author",
        # "category",
    ])

    # --- MinIO Storage ---
    # Default al servicio K8s, puede ser sobreescrito por ConfigMap
    MINIO_ENDPOINT: str = "minio-service.nyro-develop.svc.cluster.local:9000"
    MINIO_ACCESS_KEY: SecretStr # Obligatorio desde Secrets
    MINIO_SECRET_KEY: SecretStr # Obligatorio desde Secrets
    MINIO_BUCKET_NAME: str = "ingested-documents"
    MINIO_USE_SECURE: bool = False # Pydantic debería interpretar "false" como False

    # --- External Services ---
    OCR_SERVICE_URL: Optional[AnyHttpUrl] = None # Usa AnyHttpUrl o HttpUrl

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
    SPLITTER_SPLIT_BY: str = "word" # O 'sentence', 'passage'

    # --- OpenAI ---
    OPENAI_API_KEY: SecretStr # Obligatorio desde Secrets
    OPENAI_EMBEDDING_MODEL: str = "text-embedding-3-small"
    EMBEDDING_DIMENSION: int = 1536 # Default, será ajustado por validador

    # --- Validators ---
    # Validador de dimensión de embedding (mantener como estaba)
    @validator("EMBEDDING_DIMENSION", pre=True, always=True)
    def set_embedding_dimension(cls, v: Optional[int], values: dict[str, Any]) -> int:
        model = values.get("OPENAI_EMBEDDING_MODEL")
        if model == "text-embedding-3-large": return 3072
        elif model in ["text-embedding-3-small", "text-embedding-ada-002"]: return 1536
        # Fallback si v no es provisto o es 0, basado en el modelo si existe
        if v is None or v == 0:
            if model: # Si el modelo está definido, usar su dimensión
                 if model == "text-embedding-3-large": return 3072
                 if model in ["text-embedding-3-small", "text-embedding-ada-002"]: return 1536
            return 1536 # Default genérico si no hay modelo o v es inválido
        return v # Retornar v si fue provisto y es válido (>0)


# --- Instancia Global ---
try:
    settings = Settings()
    # Log successful loading and key settings (optional)
    print("DEBUG: Settings loaded successfully.")
    print(f"DEBUG: Using Postgres Server: {settings.POSTGRES_SERVER}:{settings.POSTGRES_PORT}")
    print(f"DEBUG: Using Milvus URI: {settings.MILVUS_URI}")
    print(f"DEBUG: Using Redis Broker: {settings.CELERY_BROKER_URL}")
    print(f"DEBUG: Using Minio Endpoint: {settings.MINIO_ENDPOINT}")

except (ValidationError, ValueError) as e:
    error_details = ""
    if isinstance(e, ValidationError):
        try: error_details = f"\nValidation Errors:\n{e.json(indent=2)}" # Pydantic v1 might use e.errors()
        except Exception:
             try: error_details = f"\nRaw Errors: {e.errors()}"
             except Exception: error_details = f"\nError details unavailable: {e}"

    print(f"FATAL: Configuration validation failed:{error_details}\nOriginal Error: {e}")
    # Consider logging the error as well if logging is setup early enough
    # import traceback; traceback.print_exc()
    sys.exit(1) # Salir si la configuración falla
except Exception as e:
    print(f"FATAL: Unexpected error during Settings instantiation:\n{e}")
    import traceback; traceback.print_exc()
    sys.exit(1)