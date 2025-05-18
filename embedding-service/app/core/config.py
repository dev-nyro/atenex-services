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

# FastEmbed specific defaults (kept for potential future use, but not primary)
DEFAULT_FASTEMBED_MODEL_NAME = "sentence-transformers/all-MiniLM-L6-v2"
DEFAULT_FASTEMBED_EMBEDDING_DIMENSION = 384 # Default for all-MiniLM-L6-v2
DEFAULT_FASTEMBED_MAX_LENGTH = 512


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

    # --- Active Embedding Provider ---
    # This will determine which adapter is primarily used.
    # For now, we'll hardcode it to OpenAI in main.py logic,
    # but this could be a config option in a more advanced setup.

    # --- OpenAI Embedding Model ---
    OPENAI_API_KEY: Optional[SecretStr] = Field(default=None, description="OpenAI API Key. Required if using OpenAI.")
    OPENAI_EMBEDDING_MODEL_NAME: str = Field(default=DEFAULT_OPENAI_EMBEDDING_MODEL_NAME, description="Name of the OpenAI embedding model to use.")
    OPENAI_EMBEDDING_DIMENSIONS_OVERRIDE: Optional[int] = Field(default=None, gt=0, description="Optional: Override embedding dimensions for OpenAI. Supported by text-embedding-3 models.")
    EMBEDDING_DIMENSION: int = Field(default=DEFAULT_OPENAI_EMBEDDING_DIMENSIONS_SMALL, description="Actual dimension of the embeddings that will be produced by the active provider.")
    OPENAI_API_BASE: Optional[str] = Field(default=None, description="Optional: Base URL for OpenAI API, e.g., for Azure OpenAI.")
    OPENAI_TIMEOUT_SECONDS: int = Field(default=DEFAULT_OPENAI_TIMEOUT_SECONDS, gt=0)
    OPENAI_MAX_RETRIES: int = Field(default=DEFAULT_OPENAI_MAX_RETRIES, ge=0)

    # --- FastEmbed Model (Optional, for fallback or specific use cases if retained) ---
    FASTEMBED_MODEL_NAME: str = Field(default=DEFAULT_FASTEMBED_MODEL_NAME)
    FASTEMBED_CACHE_DIR: Optional[str] = Field(default=None)
    FASTEMBED_THREADS: Optional[int] = Field(default=None)
    FASTEMBED_MAX_LENGTH: int = Field(default=DEFAULT_FASTEMBED_MAX_LENGTH)


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
        # This validation assumes OpenAI is the primary provider for EMBEDDING_DIMENSION.
        # If FastEmbed were primary, this logic would need to adapt or be conditional.
        if v <= 0:
            raise ValueError("EMBEDDING_DIMENSION must be a positive integer.")

        # Values from context (already parsed or default)
        openai_model_name = info.data.get('OPENAI_EMBEDDING_MODEL_NAME', DEFAULT_OPENAI_EMBEDDING_MODEL_NAME)
        openai_dimensions_override = info.data.get('OPENAI_EMBEDDING_DIMENSIONS_OVERRIDE')

        expected_dimension_openai = None
        if openai_model_name == "text-embedding-3-small":
            expected_dimension_openai = DEFAULT_OPENAI_EMBEDDING_DIMENSIONS_SMALL
        elif openai_model_name == "text-embedding-3-large":
            expected_dimension_openai = DEFAULT_OPENAI_EMBEDDING_DIMENSIONS_LARGE
        elif openai_model_name == "text-embedding-ada-002":
             expected_dimension_openai = 1536
        
        # If an override is provided for OpenAI, EMBEDDING_DIMENSION must match it.
        if openai_dimensions_override is not None:
            if v != openai_dimensions_override:
                raise ValueError(
                    f"EMBEDDING_DIMENSION ({v}) must match OPENAI_EMBEDDING_DIMENSIONS_OVERRIDE ({openai_dimensions_override}) when override is set for OpenAI."
                )
            logging.info(f"Using overridden OpenAI embedding dimension: {v} for model {openai_model_name}")
        # If no override, and we have an expected dimension for the selected OpenAI model, it must match.
        elif expected_dimension_openai is not None:
            if v != expected_dimension_openai:
                raise ValueError(
                    f"EMBEDDING_DIMENSION ({v}) does not match the default dimension ({expected_dimension_openai}) for OpenAI model '{openai_model_name}'. "
                    f"If you intend to use a different dimension with this OpenAI model, set OPENAI_EMBEDDING_DIMENSIONS_OVERRIDE."
                )
            logging.info(f"Using default OpenAI embedding dimension: {v} for model {openai_model_name}")
        # If it's a different OpenAI model or some other provider is implicitly active
        else:
            logging.warning(
                f"Could not determine a default OpenAI dimension for model '{openai_model_name}'. "
                f"Using configured EMBEDDING_DIMENSION: {v}. Ensure this is correct for the active embedding provider."
            )
        return v
    
    @field_validator('OPENAI_API_KEY', mode='before')
    @classmethod
    def check_openai_api_key(cls, v: Optional[str], info: ValidationInfo) -> Optional[SecretStr]:
        # This validator primarily ensures that if OpenAI is intended, the key should be present.
        # The actual decision to use OpenAI vs FastEmbed will be in main.py for now.
        # If we were to make it configurable via an 'ACTIVE_PROVIDER' env var, this would change.
        if v is None:
            # Allow None if, for example, FastEmbed was the intended active provider.
            # However, for the current goal of making OpenAI primary, we might want it to be stricter.
            # For now, let's log a warning if it's not set, as OpenAIAdapter will fail later if it's needed.
            logging.warning("OPENAI_API_KEY is not set. OpenAI embeddings will not be available.")
            return None
        return SecretStr(v)


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
        display_value = "********" if isinstance(value, SecretStr) and "API_KEY" in key.upper() else value
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