# embedding-service/app/core/config.py
import logging
import os
from typing import Optional, List, Any, Dict
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field, field_validator, ValidationError, ValidationInfo
import sys
import json

# --- Defaults ---
DEFAULT_PROJECT_NAME = "Atenex Embedding Service"
DEFAULT_LOG_LEVEL = "INFO"
DEFAULT_API_V1_STR = "/api/v1"
DEFAULT_EMBEDDING_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
DEFAULT_EMBEDDING_DIMENSION = 384
DEFAULT_FASTEMBED_CACHE_DIR: Optional[str] = None # Set to a path like "/app/models" in Docker for persistence
DEFAULT_FASTEMBED_THREADS: Optional[int] = None # None uses FastEmbed default (usually num CPU cores)
DEFAULT_FASTEMBED_MAX_LENGTH: int = 512 # Max sequence length for the model

class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file='.env',
        env_prefix='EMBEDDING_', # Unique prefix for this service
        env_file_encoding='utf-8',
        case_sensitive=False,
        extra='ignore'
    )

    # --- General ---
    PROJECT_NAME: str = Field(default=DEFAULT_PROJECT_NAME)
    API_V1_STR: str = Field(default=DEFAULT_API_V1_STR)
    LOG_LEVEL: str = Field(default=DEFAULT_LOG_LEVEL)
    PORT: int = Field(default=8003, description="Port the service will listen on.") # Example port

    # --- Embedding Model (FastEmbed) ---
    FASTEMBED_MODEL_NAME: str = Field(default=DEFAULT_EMBEDDING_MODEL)
    EMBEDDING_DIMENSION: int = Field(default=DEFAULT_EMBEDDING_DIMENSION)
    # Optional: For query/document specific prefixes if your chosen model supports/recommends them
    # FASTEMBED_QUERY_PREFIX: Optional[str] = Field(default=None)
    # FASTEMBED_DOCUMENT_PREFIX: Optional[str] = Field(default=None)
    FASTEMBED_CACHE_DIR: Optional[str] = Field(default=DEFAULT_FASTEMBED_CACHE_DIR, description="Directory to cache downloaded FastEmbed models.")
    FASTEMBED_THREADS: Optional[int] = Field(default=DEFAULT_FASTEMBED_THREADS, gt=0, description="Number of threads for FastEmbed tokenization.")
    FASTEMBED_MAX_LENGTH: int = Field(default=DEFAULT_FASTEMBED_MAX_LENGTH, gt=0, description="Max sequence length for the embedding model.")


    # --- Validators ---
    @field_validator('LOG_LEVEL')
    @classmethod
    def check_log_level(cls, v: str) -> str:
        valid_levels = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
        normalized_v = v.upper()
        if normalized_v not in valid_levels:
            raise ValueError(f"Invalid LOG_LEVEL '{v}'. Must be one of {valid_levels}")
        return normalized_v

    @field_validator('EMBEDDING_DIMENSION')
    @classmethod
    def check_embedding_dimension(cls, v: int, info: ValidationInfo) -> int:
        if v <= 0:
            raise ValueError("EMBEDDING_DIMENSION must be a positive integer.")
        model_name = info.data.get('FASTEMBED_MODEL_NAME', DEFAULT_EMBEDDING_MODEL)

        # Basic check for known models
        expected_dim = -1
        if 'all-MiniLM-L6-v2' in model_name: expected_dim = 384
        elif 'bge-small-en-v1.5' in model_name: expected_dim = 384
        elif 'bge-base-en-v1.5' in model_name: expected_dim = 768
        elif 'bge-large-en-v1.5' in model_name: expected_dim = 1024

        if expected_dim != -1 and v != expected_dim:
            logging.warning(
                f"Configured EMBEDDING_DIMENSION ({v}) differs from standard dimension ({expected_dim}) "
                f"for model '{model_name}'. Ensure this is intentional and matches the actual model output."
            )
        elif expected_dim == -1:
             logging.warning(
                f"Unknown standard embedding dimension for model '{model_name}'. "
                f"Using configured dimension {v}. Verify this matches the actual model output."
            )
        logging.debug(f"Using EMBEDDING_DIMENSION: {v} for model: {model_name}")
        return v

# --- Global Settings Instance ---
temp_log_config = logging.getLogger("embedding_service.config.loader")
if not temp_log_config.handlers:
    _handler = logging.StreamHandler(sys.stdout)
    _formatter = logging.Formatter('%(levelname)s: [%(name)s] %(message)s')
    _handler.setFormatter(_formatter)
    temp_log_config.addHandler(_handler)
    temp_log_config.setLevel(logging.INFO)

try:
    temp_log_config.info("Loading Embedding Service settings...")
    settings = Settings()
    temp_log_config.info("--- Embedding Service Settings Loaded ---")
    for key, value in settings.model_dump().items():
        temp_log_config.info(f"  {key.upper()}: {value}")
    temp_log_config.info("------------------------------------")

except (ValidationError, ValueError) as e_config:
    error_details_config = ""
    if isinstance(e_config, ValidationError):
        try: error_details_config = f"\nValidation Errors:\n{json.dumps(e_config.errors(), indent=2)}"
        except Exception: error_details_config = f"\nRaw Errors: {e_config}" # Ensure e_config is used
    else: error_details_config = f"\nError: {e_config}" # Ensure e_config is used
    temp_log_config.critical("!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
    temp_log_config.critical(f"! FATAL: Embedding Service configuration validation failed!{error_details_config}")
    temp_log_config.critical(f"! Check environment variables (prefixed with EMBEDDING_) or .env file.")
    temp_log_config.critical("!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
    sys.exit(1)
except Exception as e_config_unhandled:
    temp_log_config.exception(f"FATAL: Unexpected error loading Embedding Service settings: {e_config_unhandled}")
    sys.exit(1)