# ./app/core/config.py (CORREGIDO - Defaults para Session Pooler)
import logging
import os
from typing import Optional, List, Any, Dict
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import RedisDsn, AnyHttpUrl, SecretStr, Field, validator, ValidationError, HttpUrl
import sys

# --- Supabase Connection Defaults (Usando Session Pooler por defecto) ---
# *** CORREGIDO: Defaults actualizados a Session Pooler y puerto correcto ***
SUPABASE_SESSION_POOLER_HOST = "aws-0-sa-east-1.pooler.supabase.com"
SUPABASE_SESSION_POOLER_PORT_INT = 6543 # Puerto estándar del Session Pooler
SUPABASE_SESSION_POOLER_USER = "postgres.ymsilkrhstwxikjiqqog" # Cambiar ymsilkrhstwxikjiqqog si tu project-ref es diferente
SUPABASE_DEFAULT_DB = "postgres"

# --- Milvus Kubernetes Defaults ---
MILVUS_K8S_DEFAULT_URI = "http://milvus-service.nyro-develop.svc.cluster.local:19530"

# --- Redis Kubernetes Defaults ---
REDIS_K8S_DEFAULT_HOST = "redis-service-master.nyro-develop.svc.cluster.local"
REDIS_K8S_DEFAULT_PORT = 6379

class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file='.env',
        env_prefix='INGEST_',
        env_file_encoding='utf-8',
        case_sensitive=False,
        extra='ignore'
    )

    # --- General ---
    PROJECT_NAME: str = "Ingest Service (Haystack/K8s/Supabase/SessionPooler)"
    API_V1_STR: str = "/api/v1"
    LOG_LEVEL: str = "INFO"

    # --- Celery ---
    CELERY_BROKER_URL: RedisDsn = RedisDsn(f"redis://{REDIS_K8S_DEFAULT_HOST}:{REDIS_K8S_DEFAULT_PORT}/0")
    CELERY_RESULT_BACKEND: RedisDsn = RedisDsn(f"redis://{REDIS_K8S_DEFAULT_HOST}:{REDIS_K8S_DEFAULT_PORT}/1")

    # --- Database (Supabase Session Pooler Settings) ---
    # *** CORREGIDO: Defaults cambiados a Session Pooler con puerto 6543 ***
    POSTGRES_USER: str = SUPABASE_SESSION_POOLER_USER
    POSTGRES_PASSWORD: SecretStr # Obligatorio desde Secrets
    POSTGRES_SERVER: str = SUPABASE_SESSION_POOLER_HOST
    POSTGRES_PORT: int = SUPABASE_SESSION_POOLER_PORT_INT # Usará 6543 por defecto
    POSTGRES_DB: str = SUPABASE_DEFAULT_DB

    # --- Milvus ---
    MILVUS_URI: AnyHttpUrl = AnyHttpUrl(MILVUS_K8S_DEFAULT_URI)
    MILVUS_COLLECTION_NAME: str = "document_chunks_haystack"
    MILVUS_INDEX_PARAMS: Dict[str, Any] = Field(default={
        "metric_type": "COSINE", "index_type": "HNSW", "params": {"M": 16, "efConstruction": 256}
    })
    MILVUS_SEARCH_PARAMS: Dict[str, Any] = Field(default={
        "metric_type": "COSINE", "params": {"ef": 128}
    })
    MILVUS_CONTENT_FIELD: str = "content"
    MILVUS_EMBEDDING_FIELD: str = "embedding"
    MILVUS_METADATA_FIELDS: List[str] = Field(default=[
        "company_id", "document_id", "file_name", "file_type",
    ])

    # --- MinIO Storage ---
    MINIO_ENDPOINT: str = "minio-service.nyro-develop.svc.cluster.local:9000"
    MINIO_ACCESS_KEY: SecretStr # Obligatorio desde Secrets
    MINIO_SECRET_KEY: SecretStr # Obligatorio desde Secrets
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
        "application/pdf", "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "text/plain", "text/markdown", "text/html", "image/jpeg", "image/png",
    ])
    EXTERNAL_OCR_REQUIRED_CONTENT_TYPES: List[str] = Field(default=["image/jpeg", "image/png"])
    SPLITTER_CHUNK_SIZE: int = 500
    SPLITTER_CHUNK_OVERLAP: int = 50
    SPLITTER_SPLIT_BY: str = "word"

    # --- OpenAI ---
    OPENAI_API_KEY: SecretStr # Obligatorio desde Secrets
    OPENAI_EMBEDDING_MODEL: str = "text-embedding-3-small"
    EMBEDDING_DIMENSION: int = 1536 # Default, ajustado por validador

    # --- Validators ---
    @validator("EMBEDDING_DIMENSION", pre=True, always=True)
    def set_embedding_dimension(cls, v: Optional[int], values: dict[str, Any]) -> int:
        model = values.get("OPENAI_EMBEDDING_MODEL")
        # Ajusta la dimensión según el modelo especificado
        if model == "text-embedding-3-large": return 3072
        elif model in ["text-embedding-3-small", "text-embedding-ada-002"]: return 1536
        # Si no se especifica o es 0, intenta deducir del modelo o usa default
        if v is None or v == 0:
            if model:
                 if model == "text-embedding-3-large": return 3072
                 if model in ["text-embedding-3-small", "text-embedding-ada-002"]: return 1536
            return 1536 # Default general si no se puede determinar
        return v # Devuelve el valor si se proporcionó explícitamente

# --- Instancia Global ---
try:
    settings = Settings()
    # *** CORREGIDO: Mensajes de debug para reflejar la configuración real ***
    print("DEBUG: Settings loaded successfully.")
    print(f"DEBUG: Using Postgres Server: {settings.POSTGRES_SERVER}:{settings.POSTGRES_PORT}") # Reflejará el puerto 6543 si usa default
    print(f"DEBUG: Using Postgres User: {settings.POSTGRES_USER}")
    print(f"DEBUG: Using Milvus URI: {settings.MILVUS_URI}")
    print(f"DEBUG: Using Redis Broker: {settings.CELERY_BROKER_URL}")
    print(f"DEBUG: Using Minio Endpoint: {settings.MINIO_ENDPOINT}")

except (ValidationError, ValueError) as e:
    error_details = ""
    if isinstance(e, ValidationError):
        try: error_details = f"\nValidation Errors:\n{e.json(indent=2)}"
        except Exception:
             try: error_details = f"\nRaw Errors: {e.errors()}"
             except Exception: error_details = f"\nError details unavailable: {e}"
    print(f"FATAL: Configuration validation failed:{error_details}\nOriginal Error: {e}")
    sys.exit(1)
except Exception as e:
    print(f"FATAL: Unexpected error during Settings instantiation:\n{e}")
    import traceback; traceback.print_exc()
    sys.exit(1)