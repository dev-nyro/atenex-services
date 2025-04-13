# ingest-service/app/core/config.py
import logging
import os
from typing import Optional, List, Any, Dict, Union
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import RedisDsn, AnyHttpUrl, SecretStr, Field, validator, ValidationError, HttpUrl
import sys
import json # Para parsear MILVUS_INDEX_PARAMS/SEARCH_PARAMS

# --- Service Names en K8s ---
POSTGRES_K8S_SVC = "postgresql.nyro-develop.svc.cluster.local"           # Namespace: nyro-develop
MINIO_K8S_SVC = "minio-service.nyro-develop.svc.cluster.local"            # Namespace: nyro-develop
# *** CORREGIDO: Apuntar explícitamente al namespace 'default' para Milvus ***
MILVUS_K8S_SVC = "milvus-milvus.default.svc.cluster.local"                # Namespace: default
REDIS_K8S_SVC = "redis-service-master.nyro-develop.svc.cluster.local"     # Namespace: nyro-develop

# --- Defaults ---
POSTGRES_K8S_PORT_DEFAULT = 5432
POSTGRES_K8S_DB_DEFAULT = "atenex" # Base de datos para Atenex
POSTGRES_K8S_USER_DEFAULT = "postgres" # Usuario por defecto
MINIO_K8S_PORT_DEFAULT = 9000
MINIO_BUCKET_DEFAULT = "atenex" # Bucket específico
MILVUS_K8S_PORT_DEFAULT = 19530
REDIS_K8S_PORT_DEFAULT = 6379
MILVUS_DEFAULT_COLLECTION = "atenex_doc_chunks" # Nombre de colección más específico
MILVUS_DEFAULT_INDEX_PARAMS = '{"metric_type": "COSINE", "index_type": "HNSW", "params": {"M": 16, "efConstruction": 256}}'
MILVUS_DEFAULT_SEARCH_PARAMS = '{"metric_type": "COSINE", "params": {"ef": 128}}'
OPENAI_DEFAULT_EMBEDDING_MODEL = "text-embedding-3-small"
DEFAULT_EMBEDDING_DIM = 1536 # Para text-embedding-3-small / ada-002

class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file='.env',
        env_prefix='INGEST_',
        env_file_encoding='utf-8',
        case_sensitive=False,
        extra='ignore'
    )

    # --- General ---
    PROJECT_NAME: str = "Atenex Ingest Service"
    API_V1_STR: str = "/api/v1/ingest" # Prefijo base para este servicio
    LOG_LEVEL: str = "INFO"

    # --- Celery (Usando Redis en K8s 'nyro-develop') ---
    CELERY_BROKER_URL: RedisDsn = RedisDsn(f"redis://{REDIS_K8S_SVC}:{REDIS_K8S_PORT_DEFAULT}/0")
    CELERY_RESULT_BACKEND: RedisDsn = RedisDsn(f"redis://{REDIS_K8S_SVC}:{REDIS_K8S_PORT_DEFAULT}/1")

    # --- Database (PostgreSQL Directo en K8s 'nyro-develop') ---
    POSTGRES_USER: str = POSTGRES_K8S_USER_DEFAULT
    POSTGRES_PASSWORD: SecretStr
    POSTGRES_SERVER: str = POSTGRES_K8S_SVC
    POSTGRES_PORT: int = POSTGRES_K8S_PORT_DEFAULT
    POSTGRES_DB: str = POSTGRES_K8S_DB_DEFAULT

    # --- Milvus (en K8s 'default') ---
    # *** CORREGIDO: Construir URI usando MILVUS_K8S_SVC que apunta a 'default' ***
    MILVUS_URI: str = f"http://{MILVUS_K8S_SVC}:{MILVUS_K8S_PORT_DEFAULT}"
    MILVUS_COLLECTION_NAME: str = MILVUS_DEFAULT_COLLECTION
    MILVUS_INDEX_PARAMS: Dict[str, Any] = Field(default=json.loads(MILVUS_DEFAULT_INDEX_PARAMS))
    MILVUS_SEARCH_PARAMS: Dict[str, Any] = Field(default=json.loads(MILVUS_DEFAULT_SEARCH_PARAMS))
    MILVUS_CONTENT_FIELD: str = "content"
    MILVUS_EMBEDDING_FIELD: str = "embedding"
    MILVUS_METADATA_FIELDS: List[str] = Field(default=[
        "company_id", "document_id", "file_name", "file_type",
    ])

    # --- MinIO Storage (en K8s 'nyro-develop', bucket 'atenex') ---
    MINIO_ENDPOINT: str = f"{MINIO_K8S_SVC}:{MINIO_K8S_PORT_DEFAULT}"
    MINIO_ACCESS_KEY: SecretStr
    MINIO_SECRET_KEY: SecretStr
    MINIO_BUCKET_NAME: str = MINIO_BUCKET_DEFAULT
    MINIO_USE_SECURE: bool = False

    # --- External Services (OpenAI) ---
    OPENAI_API_KEY: SecretStr
    OPENAI_EMBEDDING_MODEL: str = OPENAI_DEFAULT_EMBEDDING_MODEL
    EMBEDDING_DIMENSION: int = DEFAULT_EMBEDDING_DIM

    # --- Service Client Config (Genérico) ---
    HTTP_CLIENT_TIMEOUT: int = 60
    HTTP_CLIENT_MAX_RETRIES: int = 2
    HTTP_CLIENT_BACKOFF_FACTOR: float = 1.0

    # --- File Processing & Haystack ---
    SUPPORTED_CONTENT_TYPES: List[str] = Field(default=[
        "application/pdf",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document", # DOCX
        "text/plain",
        "text/markdown",
        "text/html",
    ])
    SPLITTER_CHUNK_SIZE: int = 500
    SPLITTER_CHUNK_OVERLAP: int = 50
    SPLITTER_SPLIT_BY: str = "word"

    # --- Validadores ---
    @validator("LOG_LEVEL")
    def check_log_level(cls, v):
        valid_levels = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
        if v.upper() not in valid_levels:
            raise ValueError(f"Invalid LOG_LEVEL '{v}'. Must be one of {valid_levels}")
        return v.upper()

    @validator("MILVUS_INDEX_PARAMS", "MILVUS_SEARCH_PARAMS", pre=True)
    def parse_milvus_params(cls, v):
        if isinstance(v, str):
            try:
                return json.loads(v)
            except json.JSONDecodeError:
                raise ValueError("Milvus params must be a valid JSON string if provided as string")
        return v

    @validator('EMBEDDING_DIMENSION', pre=True, always=True)
    def set_embedding_dimension(cls, v: Optional[int], values: Dict[str, Any]) -> int:
        model = values.get('OPENAI_EMBEDDING_MODEL', OPENAI_DEFAULT_EMBEDDING_MODEL)
        if model == "text-embedding-3-large":
            calculated_dim = 3072
        elif model in ["text-embedding-3-small", "text-embedding-ada-002"]:
            calculated_dim = 1536
        else:
            return v if v is not None else DEFAULT_EMBEDDING_DIM

        if v is not None and v != calculated_dim:
             logging.warning(f"Provided EMBEDDING_DIMENSION {v} conflicts with model {model} ({calculated_dim} expected). Using calculated value: {calculated_dim}")
             return calculated_dim
        return calculated_dim

    @validator('POSTGRES_PASSWORD', 'MINIO_ACCESS_KEY', 'MINIO_SECRET_KEY', 'OPENAI_API_KEY')
    def check_secrets_not_empty(cls, v: SecretStr, field): # Corrección nombre field
        field_name = field.alias if field.alias else field.name
        if not v or not v.get_secret_value():
            raise ValueError(f"Secret field '{field_name}' must not be empty.")
        return v

# --- Instancia Global ---
temp_log = logging.getLogger("ingest_service.config.loader")
# ... (resto del código de inicialización y logging de config sin cambios) ...
try:
    temp_log.info("Loading Ingest Service settings...")
    settings = Settings()
    temp_log.info("Ingest Service Settings Loaded Successfully:")
    temp_log.info(f"  PROJECT_NAME: {settings.PROJECT_NAME}")
    temp_log.info(f"  LOG_LEVEL: {settings.LOG_LEVEL}")
    temp_log.info(f"  API_V1_STR: {settings.API_V1_STR}")
    temp_log.info(f"  CELERY_BROKER_URL: {settings.CELERY_BROKER_URL}")
    temp_log.info(f"  CELERY_RESULT_BACKEND: {settings.CELERY_RESULT_BACKEND}")
    temp_log.info(f"  POSTGRES_SERVER: {settings.POSTGRES_SERVER}:{settings.POSTGRES_PORT}")
    temp_log.info(f"  POSTGRES_DB: {settings.POSTGRES_DB}")
    temp_log.info(f"  POSTGRES_USER: {settings.POSTGRES_USER}")
    temp_log.info(f"  POSTGRES_PASSWORD: *** SET ***")
    temp_log.info(f"  MILVUS_URI: {settings.MILVUS_URI} (Points to 'default' namespace service)") # Nota aclaratoria
    temp_log.info(f"  MILVUS_COLLECTION_NAME: {settings.MILVUS_COLLECTION_NAME}")
    temp_log.info(f"  MINIO_ENDPOINT: {settings.MINIO_ENDPOINT}")
    temp_log.info(f"  MINIO_BUCKET_NAME: {settings.MINIO_BUCKET_NAME}")
    temp_log.info(f"  MINIO_ACCESS_KEY: *** SET ***")
    temp_log.info(f"  MINIO_SECRET_KEY: *** SET ***")
    temp_log.info(f"  OPENAI_API_KEY: *** SET ***")
    temp_log.info(f"  OPENAI_EMBEDDING_MODEL: {settings.OPENAI_EMBEDDING_MODEL}")
    temp_log.info(f"  EMBEDDING_DIMENSION: {settings.EMBEDDING_DIMENSION}")
    temp_log.info(f"  SUPPORTED_CONTENT_TYPES: {settings.SUPPORTED_CONTENT_TYPES}")

except (ValidationError, ValueError) as e:
    error_details = ""
    if isinstance(e, ValidationError):
        try: error_details = f"\nValidation Errors:\n{e.json(indent=2)}"
        except Exception: error_details = f"\nRaw Errors: {e.errors()}"
    temp_log.critical(f"!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
    temp_log.critical(f"! FATAL: Ingest Service configuration validation failed:{error_details}")
    temp_log.critical(f"! Check environment variables (prefixed with INGEST_) or .env file.")
    temp_log.critical(f"! Original Error: {e}")
    temp_log.critical(f"!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
    sys.exit(1)
except Exception as e:
    temp_log.exception(f"FATAL: Unexpected error loading Ingest Service settings: {e}")
    sys.exit(1)