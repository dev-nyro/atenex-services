# ingest-service/app/core/config.py
import logging
import os
from typing import Optional, List, Any, Dict, Union
from pydantic_settings import BaseSettings, SettingsConfigDict
# LLM_COMMENT: Use Pydantic v2 imports
from pydantic import (
    RedisDsn, AnyHttpUrl, SecretStr, Field, field_validator, ValidationError,
    ValidationInfo
)
import sys
import json

# --- Service Names en K8s ---
POSTGRES_K8S_SVC = "postgresql.nyro-develop.svc.cluster.local"
MINIO_K8S_SVC = "minio-service.nyro-develop.svc.cluster.local"
MILVUS_K8S_SVC = "milvus-milvus.default.svc.cluster.local" # Milvus en default ns
REDIS_K8S_SVC = "redis-service-master.nyro-develop.svc.cluster.local"

# --- Defaults ---
POSTGRES_K8S_PORT_DEFAULT = 5432
POSTGRES_K8S_DB_DEFAULT = "atenex"
POSTGRES_K8S_USER_DEFAULT = "postgres"
MINIO_K8S_PORT_DEFAULT = 9000
MINIO_BUCKET_DEFAULT = "atenex" # LLM_COMMENT: Ensure bucket name matches architecture diagram
MILVUS_K8S_PORT_DEFAULT = 19530
REDIS_K8S_PORT_DEFAULT = 6379
MILVUS_DEFAULT_COLLECTION = "atenex_doc_chunks" # LLM_COMMENT: Ensure collection name consistency
MILVUS_DEFAULT_INDEX_PARAMS = '{"metric_type": "COSINE", "index_type": "HNSW", "params": {"M": 16, "efConstruction": 256}}'
MILVUS_DEFAULT_SEARCH_PARAMS = '{"metric_type": "COSINE", "params": {"ef": 128}}'
# LLM_COMMENT: Keep OpenAI embedding settings for ingest service as per current design
OPENAI_DEFAULT_EMBEDDING_MODEL = "text-embedding-3-small"
# LLM_COMMENT: Ensure dimension matches the selected OpenAI model
DEFAULT_EMBEDDING_DIM = 1536 # Dimension for text-embedding-3-small & ada-002

class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file='.env', env_prefix='INGEST_', env_file_encoding='utf-8',
        case_sensitive=False, extra='ignore'
    )

    # --- General ---
    PROJECT_NAME: str = "Atenex Ingest Service"
    # ¡¡¡¡NUNCA MODIFICAR ESTA RUTA!!!
    # Esta ruta DEBE ser exactamente '/api/v1/ingest' para que el API Gateway funcione correctamente.
    # Si la cambias, romperás la integración y el proxy de rutas. Si tienes dudas, consulta con el equipo de plataforma.
    API_V1_STR: str = "/api/v1/ingest"
    LOG_LEVEL: str = "INFO"

    # --- Celery ---
    CELERY_BROKER_URL: RedisDsn = Field(default=RedisDsn(f"redis://{REDIS_K8S_SVC}:{REDIS_K8S_PORT_DEFAULT}/0"))
    CELERY_RESULT_BACKEND: RedisDsn = Field(default=RedisDsn(f"redis://{REDIS_K8S_SVC}:{REDIS_K8S_PORT_DEFAULT}/1"))

    # --- Database ---
    POSTGRES_USER: str = POSTGRES_K8S_USER_DEFAULT
    POSTGRES_PASSWORD: SecretStr # LLM_COMMENT: Loaded from secrets
    POSTGRES_SERVER: str = POSTGRES_K8S_SVC
    POSTGRES_PORT: int = POSTGRES_K8S_PORT_DEFAULT
    POSTGRES_DB: str = POSTGRES_K8S_DB_DEFAULT

    # --- Milvus ---
    MILVUS_URI: str = Field(default=f"http://{MILVUS_K8S_SVC}:{MILVUS_K8S_PORT_DEFAULT}")
    MILVUS_COLLECTION_NAME: str = MILVUS_DEFAULT_COLLECTION
    # LLM_COMMENT: Ensure metadata fields include filtering keys and display keys
    MILVUS_METADATA_FIELDS: List[str] = Field(default=["company_id", "document_id", "file_name", "file_type"])
    MILVUS_CONTENT_FIELD: str = "content" # LLM_COMMENT: Field name for text content in Milvus
    MILVUS_EMBEDDING_FIELD: str = "embedding" # LLM_COMMENT: Field name for vectors in Milvus
    MILVUS_INDEX_PARAMS: Dict[str, Any] = Field(default_factory=lambda: json.loads(MILVUS_DEFAULT_INDEX_PARAMS))
    MILVUS_SEARCH_PARAMS: Dict[str, Any] = Field(default_factory=lambda: json.loads(MILVUS_DEFAULT_SEARCH_PARAMS))

    # --- MinIO ---
    MINIO_ENDPOINT: str = Field(default=f"{MINIO_K8S_SVC}:{MINIO_K8S_PORT_DEFAULT}")
    MINIO_ACCESS_KEY: SecretStr # LLM_COMMENT: Loaded from secrets
    MINIO_SECRET_KEY: SecretStr # LLM_COMMENT: Loaded from secrets
    MINIO_BUCKET_NAME: str = MINIO_BUCKET_DEFAULT
    MINIO_USE_SECURE: bool = False # LLM_COMMENT: Typically false for internal k8s communication

    # --- Embeddings (OpenAI for Ingestion) ---
    OPENAI_API_KEY: SecretStr # LLM_COMMENT: Loaded from secrets
    OPENAI_EMBEDDING_MODEL: str = OPENAI_DEFAULT_EMBEDDING_MODEL
    # LLM_COMMENT: Dimension must match the chosen OpenAI model
    EMBEDDING_DIMENSION: int = DEFAULT_EMBEDDING_DIM

    # --- Clients ---
    HTTP_CLIENT_TIMEOUT: int = 60
    HTTP_CLIENT_MAX_RETRIES: int = 2
    HTTP_CLIENT_BACKOFF_FACTOR: float = 1.0

    # --- Processing ---
    SUPPORTED_CONTENT_TYPES: List[str] = Field(default=[
        "application/pdf",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document", # DOCX
        "application/msword", # DOC (handled by DOCX converter often)
        "text/plain",
        "text/markdown",
        "text/html"
    ])
    SPLITTER_CHUNK_SIZE: int = 500
    SPLITTER_CHUNK_OVERLAP: int = 50
    SPLITTER_SPLIT_BY: str = "word" # Other options: "sentence", "passage"

    # --- Validators (Pydantic V2 Style) ---
    # LLM_COMMENT: Keep validators for log level and secret presence

    @field_validator("LOG_LEVEL")
    @classmethod
    def check_log_level(cls, v: str) -> str:
        valid_levels = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
        normalized_v = v.upper()
        if normalized_v not in valid_levels: raise ValueError(f"Invalid LOG_LEVEL '{v}'. Must be one of {valid_levels}")
        return normalized_v

    @field_validator('EMBEDDING_DIMENSION', mode='before', check_fields=False)
    @classmethod
    def set_embedding_dimension(cls, v: Optional[int], info: ValidationInfo) -> int:
        # LLM_COMMENT: Ensure dimension calculation matches the OpenAI model used here
        config_values = info.data
        model = config_values.get('OPENAI_EMBEDDING_MODEL', OPENAI_DEFAULT_EMBEDDING_MODEL)
        calculated_dim = DEFAULT_EMBEDDING_DIM # Default from constants
        # Update calculated_dim based on known OpenAI models
        if model == "text-embedding-3-large": calculated_dim = 3072
        elif model in ["text-embedding-3-small", "text-embedding-ada-002"]: calculated_dim = 1536
        # Add other models if needed

        if v is not None and v != calculated_dim:
             logging.warning(f"Provided INGEST_EMBEDDING_DIMENSION {v} conflicts with INGEST_OPENAI_EMBEDDING_MODEL {model} ({calculated_dim} expected). Using calculated value: {calculated_dim}")
             return calculated_dim
        elif v is None:
             # LLM_COMMENT: Log which dimension is being set if not provided
             logging.debug(f"EMBEDDING_DIMENSION not set, defaulting to {calculated_dim} based on model {model}")
             return calculated_dim
        else:
             # LLM_COMMENT: Log if provided dimension matches calculated
             if v == calculated_dim:
                 logging.debug(f"Provided EMBEDDING_DIMENSION {v} matches model {model}")
             return v # Return user-provided value if it matches

    @field_validator('POSTGRES_PASSWORD', 'MINIO_ACCESS_KEY', 'MINIO_SECRET_KEY', 'OPENAI_API_KEY', mode='before')
    @classmethod
    def check_secret_value_present(cls, v: Any, info: ValidationInfo) -> Any:
        """Valida que el valor para un campo secreto no esté vacío antes de convertir a SecretStr."""
        if v is None or v == "":
             field_name = info.field_name if info.field_name else "Unknown Secret Field"
             raise ValueError(f"Required secret field '{field_name}' cannot be empty.")
        return v

# --- Instancia Global ---
# LLM_COMMENT: Keep global settings instance logic, update logging format
temp_log = logging.getLogger("ingest_service.config.loader")
if not temp_log.handlers:
    handler = logging.StreamHandler(sys.stdout)
    # LLM_COMMENT: Use a more informative startup log format
    formatter = logging.Formatter('%(levelname)-8s [%(asctime)s] [%(name)s] %(message)s', datefmt='%Y-%m-%d %H:%M:%S')
    handler.setFormatter(formatter)
    temp_log.addHandler(handler)
    temp_log.setLevel(logging.INFO) # Set level for startup logs

try:
    temp_log.info("Loading Ingest Service settings...")
    settings = Settings()
    temp_log.info("--- Ingest Service Settings Loaded ---")
    temp_log.info(f"  PROJECT_NAME:             {settings.PROJECT_NAME}")
    temp_log.info(f"  LOG_LEVEL:                {settings.LOG_LEVEL}")
    temp_log.info(f"  API_V1_STR:               {settings.API_V1_STR}")
    temp_log.info(f"  CELERY_BROKER_URL:        {settings.CELERY_BROKER_URL}")
    temp_log.info(f"  CELERY_RESULT_BACKEND:    {settings.CELERY_RESULT_BACKEND}")
    temp_log.info(f"  POSTGRES_SERVER:          {settings.POSTGRES_SERVER}:{settings.POSTGRES_PORT}")
    temp_log.info(f"  POSTGRES_DB:              {settings.POSTGRES_DB}")
    temp_log.info(f"  POSTGRES_USER:            {settings.POSTGRES_USER}")
    temp_log.info(f"  POSTGRES_PASSWORD:        *** SET ***")
    temp_log.info(f"  MILVUS_URI:               {settings.MILVUS_URI}")
    temp_log.info(f"  MILVUS_COLLECTION_NAME:   {settings.MILVUS_COLLECTION_NAME}")
    temp_log.info(f"  MINIO_ENDPOINT:           {settings.MINIO_ENDPOINT}")
    temp_log.info(f"  MINIO_BUCKET_NAME:        {settings.MINIO_BUCKET_NAME}")
    temp_log.info(f"  MINIO_ACCESS_KEY:         *** SET ***")
    temp_log.info(f"  MINIO_SECRET_KEY:         *** SET ***")
    temp_log.info(f"  OPENAI_API_KEY:           *** SET ***")
    temp_log.info(f"  OPENAI_EMBEDDING_MODEL:   {settings.OPENAI_EMBEDDING_MODEL}")
    temp_log.info(f"  EMBEDDING_DIMENSION:      {settings.EMBEDDING_DIMENSION}")
    temp_log.info(f"  SUPPORTED_CONTENT_TYPES:  {settings.SUPPORTED_CONTENT_TYPES}")
    temp_log.info(f"  SPLITTER_CHUNK_SIZE:      {settings.SPLITTER_CHUNK_SIZE}")
    temp_log.info(f"  SPLITTER_CHUNK_OVERLAP:   {settings.SPLITTER_CHUNK_OVERLAP}")
    temp_log.info(f"------------------------------------")

except (ValidationError, ValueError) as e:
    error_details = ""
    if isinstance(e, ValidationError):
        try: error_details = f"\nValidation Errors:\n{e.json(indent=2)}"
        except Exception: error_details = f"\nRaw Errors: {e.errors()}"
    temp_log.critical("!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
    temp_log.critical(f"! FATAL: Ingest Service configuration validation failed:{error_details}")
    temp_log.critical(f"! Check environment variables (prefixed with INGEST_) or .env file.")
    temp_log.critical(f"! Original Error: {e}")
    temp_log.critical("!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
    sys.exit(1)
except Exception as e:
    temp_log.exception(f"FATAL: Unexpected error loading Ingest Service settings: {e}")
    sys.exit(1)