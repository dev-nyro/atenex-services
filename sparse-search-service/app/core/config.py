# sparse-search-service/app/core/config.py
import logging
import os
import sys
import json
from typing import Optional, Dict, Any
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field, field_validator, SecretStr, ValidationInfo, ValidationError

# --- Default Values ---
POSTGRES_K8S_HOST_DEFAULT = "postgresql-service.nyro-develop.svc.cluster.local"
POSTGRES_K8S_PORT_DEFAULT = 5432
POSTGRES_K8S_DB_DEFAULT = "atenex"
POSTGRES_K8S_USER_DEFAULT = "postgres"

DEFAULT_SERVICE_PORT = 8004
DEFAULT_GCS_INDEX_BUCKET = "atenex-sparse-indices" 
DEFAULT_INDEX_CACHE_MAX_ITEMS = 50
DEFAULT_INDEX_CACHE_TTL_SECONDS = 3600 

class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file='.env',
        env_prefix='SPARSE_',
        env_file_encoding='utf-8',
        case_sensitive=False,
        extra='ignore'
    )

    # --- General ---
    PROJECT_NAME: str = "Atenex Sparse Search Service"
    API_V1_STR: str = "/api/v1"
    LOG_LEVEL: str = Field(default="INFO")
    PORT: int = Field(default=DEFAULT_SERVICE_PORT)
    SERVICE_VERSION: str = "1.0.0" 

    # --- Database (PostgreSQL) ---
    POSTGRES_USER: str = Field(default=POSTGRES_K8S_USER_DEFAULT)
    POSTGRES_PASSWORD: SecretStr
    POSTGRES_SERVER: str = Field(default=POSTGRES_K8S_HOST_DEFAULT)
    POSTGRES_PORT: int = Field(default=POSTGRES_K8S_PORT_DEFAULT)
    POSTGRES_DB: str = Field(default=POSTGRES_K8S_DB_DEFAULT)
    DB_POOL_MIN_SIZE: int = Field(default=2)
    DB_POOL_MAX_SIZE: int = Field(default=10)
    DB_CONNECT_TIMEOUT: int = Field(default=30) 
    DB_COMMAND_TIMEOUT: int = Field(default=60) 

    # --- GCS Index Storage ---
    SPARSE_INDEX_GCS_BUCKET_NAME: str = Field(
        default=DEFAULT_GCS_INDEX_BUCKET,
        description="GCS bucket name for storing precomputed BM25 indexes."
    )

    # --- LRU/TTL Cache for BM25 Instances ---
    SPARSE_INDEX_CACHE_MAX_ITEMS: int = Field(
        default=DEFAULT_INDEX_CACHE_MAX_ITEMS,
        description="Maximum number of company BM25 indexes to keep in the LRU cache."
    )
    SPARSE_INDEX_CACHE_TTL_SECONDS: int = Field(
        default=DEFAULT_INDEX_CACHE_TTL_SECONDS,
        description="Time-to-live in seconds for items in the BM25 index cache."
    )
    
    # --- BM25 Parameters (Opcional, bm2s usa sus propios defaults) ---
    # SPARSE_BM2S_K1: float = Field(default=1.5)
    # SPARSE_BM2S_B: float = Field(default=0.75)


    @field_validator('LOG_LEVEL', mode='before')
    @classmethod
    def normalize_log_level(cls, v: str) -> str:
        return v.upper()

    @field_validator('LOG_LEVEL')
    @classmethod
    def check_log_level(cls, v: str) -> str:
        valid_levels = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
        if v not in valid_levels:
            raise ValueError(f"Invalid LOG_LEVEL '{v}'. Must be one of {valid_levels}")
        return v

    @field_validator('POSTGRES_PASSWORD', mode='before')
    @classmethod
    def check_postgres_password(cls, v: Any, info: ValidationInfo) -> Any:
        if isinstance(v, SecretStr):
            secret_value = v.get_secret_value()
            if secret_value is None or secret_value == "":
                raise ValueError(f"Required secret field 'SPARSE_POSTGRES_PASSWORD' cannot be empty.")
        elif v is None or v == "":
            raise ValueError(f"Required secret field 'SPARSE_POSTGRES_PASSWORD' cannot be empty.")
        return v
    
    @field_validator('SPARSE_INDEX_GCS_BUCKET_NAME', mode='before')
    @classmethod
    def check_gcs_bucket_name(cls, v: Any, info: ValidationInfo) -> Any:
        if v is None or v == "":
            raise ValueError(f"Required field '{info.field_name}' (SPARSE_INDEX_GCS_BUCKET_NAME) cannot be empty.")
        return v

# --- Global Settings Instance ---
temp_log = logging.getLogger("sparse_search_service.config.loader")
if not temp_log.handlers: 
    handler = logging.StreamHandler(sys.stdout)
    formatter = logging.Formatter('%(levelname)s: [%(asctime)s] [%(name)s] %(message)s')
    handler.setFormatter(formatter)
    temp_log.addHandler(handler)
    temp_log.setLevel(logging.INFO) 

try:
    temp_log.info("Loading Sparse Search Service settings...")
    settings = Settings()
    temp_log.info("--- Sparse Search Service Settings Loaded (v1.0.0) ---")

    excluded_fields = {'POSTGRES_PASSWORD'}
    log_data = settings.model_dump(exclude=excluded_fields)

    for key, value in log_data.items():
        temp_log.info(f"  {key.upper()}: {value}")

    pg_pass_status = '*** SET ***' if settings.POSTGRES_PASSWORD and settings.POSTGRES_PASSWORD.get_secret_value() else '!!! NOT SET !!!'
    temp_log.info(f"  POSTGRES_PASSWORD: {pg_pass_status}")
    temp_log.info(f"------------------------------------")

except (ValidationError, ValueError) as e:
    error_details = ""
    if isinstance(e, ValidationError):
        try: error_details = f"\nValidation Errors:\n{json.dumps(e.errors(), indent=2)}"
        except Exception: error_details = f"\nRaw Errors: {e}"
    else: error_details = f"\nError: {e}"

    temp_log.critical(f"!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
    temp_log.critical(f"! FATAL: Sparse Search Service configuration validation failed!{error_details}")
    temp_log.critical(f"! Check environment variables (prefixed with SPARSE_) or .env file.")
    temp_log.critical(f"!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
    sys.exit(1)
except Exception as e:
    temp_log.exception(f"FATAL: Unexpected error loading Sparse Search Service settings: {e}")
    sys.exit(1)