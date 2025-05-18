# embedding-service/app/core/config.py
import logging
import os
from typing import Optional, List, Any, Dict
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field, field_validator, ValidationError, ValidationInfo, SecretStr
import sys
import json

# --- Defaults ---
DEFAULT_PROJECT_NAME = "Atenex Embedding Service"
DEFAULT_LOG_LEVEL = "INFO"
DEFAULT_API_V1_STR = "/api/v1"

# OpenAI specific defaults
DEFAULT_OPENAI_EMBEDDING_MODEL_NAME = "text-embedding-3-small"
DEFAULT_OPENAI_EMBEDDING_DIMENSIONS_SMALL = 1536 # for text-embedding-3-small
DEFAULT_OPENAI_EMBEDDING_DIMENSIONS_LARGE = 3072 # for text-embedding-3-large
DEFAULT_OPENAI_TIMEOUT_SECONDS = 30
DEFAULT_OPENAI_MAX_RETRIES = 3

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
    PORT: int = Field(default=8003, description="Port the service will listen on.")

    # --- OpenAI Embedding Model ---
    OPENAI_API_KEY: SecretStr = Field(..., description="OpenAI API Key.")
    OPENAI_EMBEDDING_MODEL_NAME: str = Field(default=DEFAULT_OPENAI_EMBEDDING_MODEL_NAME, description="Name of the OpenAI embedding model to use.")
    OPENAI_EMBEDDING_DIMENSIONS_OVERRIDE: Optional[int] = Field(default=None, gt=0, description="Optional: Override embedding dimensions. Supported by text-embedding-3 models.")
    EMBEDDING_DIMENSION: int = Field(description="Actual dimension of the embeddings that will be produced.") # This will be validated
    OPENAI_API_BASE: Optional[str] = Field(default=None, description="Optional: Base URL for OpenAI API, e.g., for Azure OpenAI.")
    OPENAI_TIMEOUT_SECONDS: int = Field(default=DEFAULT_OPENAI_TIMEOUT_SECONDS, gt=0)
    OPENAI_MAX_RETRIES: int = Field(default=DEFAULT_OPENAI_MAX_RETRIES, ge=0)

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
    def validate_embedding_dimension(cls, v: int, info: ValidationInfo) -> int:
        if v <= 0:
            raise ValueError("EMBEDDING_DIMENSION must be a positive integer.")

        model_name = info.data.get('OPENAI_EMBEDDING_MODEL_NAME', DEFAULT_OPENAI_EMBEDDING_MODEL_NAME)
        dimensions_override = info.data.get('OPENAI_EMBEDDING_DIMENSIONS_OVERRIDE')

        expected_dimension = None
        if model_name == "text-embedding-3-small":
            expected_dimension = DEFAULT_OPENAI_EMBEDDING_DIMENSIONS_SMALL
        elif model_name == "text-embedding-3-large":
            expected_dimension = DEFAULT_OPENAI_EMBEDDING_DIMENSIONS_LARGE
        elif model_name == "text-embedding-ada-002": # Older model, different dimension
             expected_dimension = 1536 # OpenAI 'text-embedding-ada-002' dimension
        # Add other OpenAI models and their default dimensions if needed

        if dimensions_override is not None:
            if v != dimensions_override:
                raise ValueError(
                    f"EMBEDDING_DIMENSION ({v}) must match OPENAI_EMBEDDING_DIMENSIONS_OVERRIDE ({dimensions_override}) when override is set."
                )
            # Further validation could check if override is valid for the model, but OpenAI API handles this.
            logging.info(f"Using overridden embedding dimension: {v} for model {model_name}")
        elif expected_dimension is not None:
            if v != expected_dimension:
                raise ValueError(
                    f"EMBEDDING_DIMENSION ({v}) does not match the default dimension ({expected_dimension}) for model '{model_name}'. "
                    f"If you intend to use a different dimension, set OPENAI_EMBEDDING_DIMENSIONS_OVERRIDE."
                )
            logging.info(f"Using default embedding dimension: {v} for model {model_name}")
        else:
            logging.warning(
                f"Could not determine default dimension for model '{model_name}'. "
                f"Using configured EMBEDDING_DIMENSION: {v}. Ensure this is correct."
            )
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
        display_value = "********" if isinstance(value, SecretStr) and key == "OPENAI_API_KEY" else value
        temp_log_config.info(f"  {key.upper()}: {display_value}")
    temp_log_config.info("------------------------------------")

except (ValidationError, ValueError) as e_config:
    error_details_config = ""
    if isinstance(e_config, ValidationError):
        try: error_details_config = f"\nValidation Errors:\n{json.dumps(e_config.errors(), indent=2)}"
        except Exception: error_details_config = f"\nRaw Errors: {e_config}"
    else: error_details_config = f"\nError: {e_config}"
    temp_log_config.critical("!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
    temp_log_config.critical(f"! FATAL: Embedding Service configuration validation failed!{error_details_config}")
    temp_log_config.critical(f"! Check environment variables (prefixed with EMBEDDING_) or .env file.")
    temp_log_config.critical("!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
    sys.exit(1)
except Exception as e_config_unhandled:
    temp_log_config.exception(f"FATAL: Unexpected error loading Embedding Service settings: {e_config_unhandled}")
    sys.exit(1)