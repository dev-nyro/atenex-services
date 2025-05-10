# ingest-service/app/core/config.py
import logging
import os
from typing import Optional, List, Any, Dict, Union
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import (
    RedisDsn, AnyHttpUrl, SecretStr, Field, field_validator, ValidationError,
    ValidationInfo
)
import sys
import json

# --- Service Names en K8s ---
POSTGRES_K8S_SVC = "postgresql-service.nyro-develop.svc.cluster.local"
MILVUS_K8S_SVC = "milvus-standalone.nyro-develop.svc.cluster.local"
REDIS_K8S_SVC = "redis-service-master.nyro-develop.svc.cluster.local"
EMBEDDING_SERVICE_K8S_SVC = "embedding-service.nyro-develop.svc.cluster.local"
DOCPROC_SERVICE_K8S_SVC = "docproc-service.nyro-develop.svc.cluster.local"

# --- Defaults ---
POSTGRES_K8S_PORT_DEFAULT = 5432
POSTGRES_K8S_DB_DEFAULT = "atenex"
POSTGRES_K8S_USER_DEFAULT = "postgres"
MILVUS_K8S_PORT_DEFAULT = 19530
MILVUS_DEFAULT_COLLECTION = "document_chunks_minilm"
MILVUS_DEFAULT_INDEX_PARAMS = '{"metric_type": "IP", "index_type": "HNSW", "params": {"M": 16, "efConstruction": 256}}'
MILVUS_DEFAULT_SEARCH_PARAMS = '{"metric_type": "IP", "params": {"ef": 128}}'
DEFAULT_EMBEDDING_DIM = 384
DEFAULT_TIKTOKEN_ENCODING = "cl100k_base"

DEFAULT_EMBEDDING_SERVICE_URL = f"http://{EMBEDDING_SERVICE_K8S_SVC}:8003/api/v1/embed"
DEFAULT_DOCPROC_SERVICE_URL = f"http://{DOCPROC_SERVICE_K8S_SVC}:8005/api/v1/process"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file='.env', env_prefix='INGEST_', env_file_encoding='utf-8',
        case_sensitive=False, extra='ignore'
    )

    PROJECT_NAME: str = "Atenex Ingest Service"
    API_V1_STR: str = "/api/v1/ingest"
    LOG_LEVEL: str = "INFO"

    CELERY_BROKER_URL: RedisDsn = Field(default_factory=lambda: RedisDsn(f"redis://{REDIS_K8S_SVC}:6379/0"))
    CELERY_RESULT_BACKEND: RedisDsn = Field(default_factory=lambda: RedisDsn(f"redis://{REDIS_K8S_SVC}:6379/1"))

    POSTGRES_USER: str = POSTGRES_K8S_USER_DEFAULT
    POSTGRES_PASSWORD: SecretStr
    POSTGRES_SERVER: str = POSTGRES_K8S_SVC
    POSTGRES_PORT: int = POSTGRES_K8S_PORT_DEFAULT
    POSTGRES_DB: str = POSTGRES_K8S_DB_DEFAULT

    MILVUS_URI: str = Field(default=f"http://{MILVUS_K8S_SVC}:{MILVUS_K8S_PORT_DEFAULT}")
    MILVUS_COLLECTION_NAME: str = MILVUS_DEFAULT_COLLECTION
    MILVUS_GRPC_TIMEOUT: int = 10
    MILVUS_CONTENT_FIELD: str = "content"
    MILVUS_EMBEDDING_FIELD: str = "embedding"
    MILVUS_CONTENT_FIELD_MAX_LENGTH: int = 20000
    MILVUS_INDEX_PARAMS: Dict[str, Any] = Field(default_factory=lambda: json.loads(MILVUS_DEFAULT_INDEX_PARAMS))
    MILVUS_SEARCH_PARAMS: Dict[str, Any] = Field(default_factory=lambda: json.loads(MILVUS_DEFAULT_SEARCH_PARAMS))

    GCS_BUCKET_NAME: str = Field(default="atenex", description="Name of the Google Cloud Storage bucket for storing original files.")

    EMBEDDING_DIMENSION: int = Field(default=DEFAULT_EMBEDDING_DIM, description="Dimension of embeddings expected from the embedding service, used for Milvus schema.")
    INGEST_EMBEDDING_SERVICE_URL: AnyHttpUrl = Field(default=DEFAULT_EMBEDDING_SERVICE_URL, description="URL of the external embedding service.")
    INGEST_DOCPROC_SERVICE_URL: AnyHttpUrl = Field(default=DEFAULT_DOCPROC_SERVICE_URL, description="URL of the external document processing service.")

    TIKTOKEN_ENCODING_NAME: str = Field(default=DEFAULT_TIKTOKEN_ENCODING, description="Name of the tiktoken encoding to use for token counting.")

    HTTP_CLIENT_TIMEOUT: int = 60
    HTTP_CLIENT_MAX_RETRIES: int = 3
    HTTP_CLIENT_BACKOFF_FACTOR: float = 1.0

    SUPPORTED_CONTENT_TYPES: List[str] = Field(default=[
        "application/pdf",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/msword",
        "text/plain",
        "text/markdown",
        "text/html"
    ])

    @field_validator("LOG_LEVEL")
    @classmethod
    def check_log_level(cls, v: str) -> str:
        valid_levels = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
        normalized_v = v.upper()
        if normalized_v not in valid_levels: raise ValueError(f"Invalid LOG_LEVEL '{v}'. Must be one of {valid_levels}")
        return normalized_v

    @field_validator('EMBEDDING_DIMENSION')
    @classmethod
    def check_embedding_dimension(cls, v: int, info: ValidationInfo) -> int:
        if v <= 0: raise ValueError("EMBEDDING_DIMENSION must be a positive integer.")
        logging.debug(f"Using EMBEDDING_DIMENSION for Milvus schema: {v}")
        return v

    @field_validator('POSTGRES_PASSWORD', mode='before')
    @classmethod
    def check_required_secret_value_present(cls, v: Any, info: ValidationInfo) -> Any:
        if v is None or v == "":
             field_name = info.field_name if info.field_name else "Unknown Secret Field"
             raise ValueError(f"Required secret field '{field_name}' cannot be empty.")
        return v

    @field_validator('MILVUS_URI', mode='before')
    @classmethod
    def validate_milvus_uri(cls, v: str) -> str:
        if not v.startswith("http://") and not v.startswith("https://"):
             if "." not in v: # Assume it's a K8s service name without scheme
                 return f"http://{v}"
             # If it has a dot but no scheme, it's likely an invalid format
             raise ValueError(f"Invalid MILVUS_URI format: '{v}'. Must start with 'http://' or 'https://' or be a valid service name.")
        return v

    @field_validator('INGEST_EMBEDDING_SERVICE_URL', 'INGEST_DOCPROC_SERVICE_URL', mode='before')
    @classmethod
    def assemble_service_url(cls, v: Optional[str], info: ValidationInfo) -> str:
        default_map = {
            "INGEST_EMBEDDING_SERVICE_URL": DEFAULT_EMBEDDING_SERVICE_URL,
            "INGEST_DOCPROC_SERVICE_URL": DEFAULT_DOCPROC_SERVICE_URL
        }
        # Ensure info.field_name is not None before using it as a key
        default_url_key = str(info.field_name) if info.field_name else ""
        default_url = default_map.get(default_url_key, "")
        
        url_to_validate = v or default_url
        if not url_to_validate: # Should not happen if defaults are correctly set
            raise ValueError(f"URL for {default_url_key} cannot be empty.")
            
        return str(AnyHttpUrl(url_to_validate))


temp_log = logging.getLogger("ingest_service.config.loader")
if not temp_log.handlers:
    handler = logging.StreamHandler(sys.stdout)
    formatter = logging.Formatter('%(levelname)-8s [%(asctime)s] [%(name)s] %(message)s', datefmt='%Y-%m-%d %H:%M:%S')
    handler.setFormatter(formatter)
    temp_log.addHandler(handler)
    temp_log.setLevel(logging.INFO)

try:
    temp_log.info("Loading Ingest Service settings...")
    settings = Settings()
    temp_log.info("--- Ingest Service Settings Loaded ---")
    temp_log.info(f"  PROJECT_NAME:                 {settings.PROJECT_NAME}")
    temp_log.info(f"  LOG_LEVEL:                    {settings.LOG_LEVEL}")
    temp_log.info(f"  API_V1_STR:                   {settings.API_V1_STR}")
    temp_log.info(f"  CELERY_BROKER_URL:            {settings.CELERY_BROKER_URL}")
    temp_log.info(f"  CELERY_RESULT_BACKEND:        {settings.CELERY_RESULT_BACKEND}")
    temp_log.info(f"  POSTGRES_SERVER:              {settings.POSTGRES_SERVER}:{settings.POSTGRES_PORT}")
    temp_log.info(f"  POSTGRES_DB:                  {settings.POSTGRES_DB}")
    temp_log.info(f"  POSTGRES_USER:                {settings.POSTGRES_USER}")
    temp_log.info(f"  POSTGRES_PASSWORD:            {'*** SET ***' if settings.POSTGRES_PASSWORD and settings.POSTGRES_PASSWORD.get_secret_value() else '!!! NOT SET !!!'}")
    temp_log.info(f"  MILVUS_URI:                   {settings.MILVUS_URI}")
    temp_log.info(f"  MILVUS_COLLECTION_NAME:       {settings.MILVUS_COLLECTION_NAME}")
    temp_log.info(f"  GCS_BUCKET_NAME:              {settings.GCS_BUCKET_NAME}")
    temp_log.info(f"  EMBEDDING_DIMENSION (Milvus): {settings.EMBEDDING_DIMENSION}")
    temp_log.info(f"  INGEST_EMBEDDING_SERVICE_URL: {settings.INGEST_EMBEDDING_SERVICE_URL}")
    temp_log.info(f"  INGEST_DOCPROC_SERVICE_URL:   {settings.INGEST_DOCPROC_SERVICE_URL}")
    temp_log.info(f"  TIKTOKEN_ENCODING_NAME:       {settings.TIKTOKEN_ENCODING_NAME}")
    temp_log.info(f"  SUPPORTED_CONTENT_TYPES:      {settings.SUPPORTED_CONTENT_TYPES}")
    temp_log.info(f"------------------------------------")

except (ValidationError, ValueError) as e:
    error_details = ""
    if isinstance(e, ValidationError):
        try: error_details = f"\nValidation Errors:\n{e.json(indent=2)}"
        except Exception: error_details = f"\nRaw Errors: {e.errors()}"
    else: # ValueError
        error_details = f"\nError: {str(e)}"
    temp_log.critical("!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
    temp_log.critical(f"! FATAL: Ingest Service configuration validation failed:{error_details}")
    temp_log.critical(f"! Check environment variables (prefixed with INGEST_) or .env file.")
    temp_log.critical(f"! Original Error Type: {type(e).__name__}")
    temp_log.critical("!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
    sys.exit(1)
except Exception as e:
    temp_log.exception(f"FATAL: Unexpected error loading Ingest Service settings: {e}")
    sys.exit(1)