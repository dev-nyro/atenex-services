# embedding-service/app/core/config.py
import logging
import os
from typing import Optional, List, Any, Dict
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field, field_validator, ValidationError, ValidationInfo, SecretStr
import sys
import json
# LLM_FLAG: CONDITIONAL_IMPORT_START
# Import torch only if sentence_transformer is the active provider
# This will be checked after settings are partially loaded.
_torch_imported = False
try:
    if os.getenv("EMBEDDING_ACTIVE_EMBEDDING_PROVIDER", "openai").lower() == "sentence_transformer":
        import torch
        _torch_imported = True
except ImportError:
    # This case should ideally not happen if the Docker image is built correctly
    # for sentence_transformer provider, but good to have a fallback log.
    logging.getLogger("embedding_service.config.loader").warning(
        "Torch import failed even though sentence_transformer might be active. Ensure torch is installed if using ST."
    )
    _torch_imported = False
# LLM_FLAG: CONDITIONAL_IMPORT_END


# --- Defaults ---
DEFAULT_PROJECT_NAME = "Atenex Embedding Service"
DEFAULT_LOG_LEVEL = "INFO"
DEFAULT_API_V1_STR = "/api/v1"
DEFAULT_ACTIVE_EMBEDDING_PROVIDER = "openai" # "openai" or "sentence_transformer"

# OpenAI specific defaults
DEFAULT_OPENAI_EMBEDDING_MODEL_NAME = "text-embedding-3-small"
DEFAULT_OPENAI_EMBEDDING_DIMENSIONS_SMALL = 1536 # for text-embedding-3-small
DEFAULT_OPENAI_EMBEDDING_DIMENSIONS_LARGE = 3072 # for text-embedding-3-large
DEFAULT_OPENAI_TIMEOUT_SECONDS = 30
DEFAULT_OPENAI_MAX_RETRIES = 3

# SentenceTransformer (ST) specific defaults
DEFAULT_ST_MODEL_NAME = "intfloat/multilingual-e5-base" # Updated default ST model
DEFAULT_ST_EMBEDDING_DIMENSION_E5_BASE_V2 = 768 # For intfloat/e5-base-v2 (English)
DEFAULT_ST_EMBEDDING_DIMENSION_MULTILINGUAL_E5_BASE = 768 # For intfloat/multilingual-e5-base
DEFAULT_ST_EMBEDDING_DIMENSION_E5_LARGE_V2 = 1024 # For intfloat/e5-large-v2 (English)
DEFAULT_ST_MODEL_DEVICE = "cpu" # "cpu" or "cuda"
DEFAULT_ST_BATCH_SIZE_CPU = 64
DEFAULT_ST_BATCH_SIZE_CUDA = 128 # Can be higher on GPU
DEFAULT_ST_NORMALIZE_EMBEDDINGS = True # E5 models recommend normalization
DEFAULT_ST_USE_FP16 = True # Enable by default if CUDA supports it

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
    WORKERS: int = Field(default=2, description="Number of Gunicorn workers.")


    # --- Active Embedding Provider ---
    ACTIVE_EMBEDDING_PROVIDER: str = Field(default=DEFAULT_ACTIVE_EMBEDDING_PROVIDER, description="Active embedding provider: 'openai' or 'sentence_transformer'.")

    # --- OpenAI Embedding Model ---
    OPENAI_API_KEY: Optional[SecretStr] = Field(default=None, description="OpenAI API Key. Required if using OpenAI.")
    OPENAI_EMBEDDING_MODEL_NAME: str = Field(default=DEFAULT_OPENAI_EMBEDDING_MODEL_NAME, description="Name of the OpenAI embedding model to use.")
    OPENAI_EMBEDDING_DIMENSIONS_OVERRIDE: Optional[int] = Field(default=None, gt=0, description="Optional: Override embedding dimensions for OpenAI. Supported by text-embedding-3 models.")
    OPENAI_API_BASE: Optional[str] = Field(default=None, description="Optional: Base URL for OpenAI API, e.g., for Azure OpenAI.")
    OPENAI_TIMEOUT_SECONDS: int = Field(default=DEFAULT_OPENAI_TIMEOUT_SECONDS, gt=0)
    OPENAI_MAX_RETRIES: int = Field(default=DEFAULT_OPENAI_MAX_RETRIES, ge=0)

    # --- SentenceTransformer (ST) Model ---
    ST_MODEL_NAME: str = Field(default=DEFAULT_ST_MODEL_NAME)
    ST_MODEL_DEVICE: str = Field(default=DEFAULT_ST_MODEL_DEVICE, description="Device for ST model: 'cpu', 'cuda', 'cuda:0', etc.")
    ST_HF_CACHE_DIR: Optional[str] = Field(default=None, description="Cache directory for HuggingFace models for ST.")
    ST_BATCH_SIZE: int = Field(default=DEFAULT_ST_BATCH_SIZE_CPU, gt=0)
    ST_NORMALIZE_EMBEDDINGS: bool = Field(default=DEFAULT_ST_NORMALIZE_EMBEDDINGS)
    ST_USE_FP16: bool = Field(default=DEFAULT_ST_USE_FP16, description="Enable FP16 for ST models on CUDA if supported.")


    # --- Embedding Dimension (Crucial, validated based on active provider) ---
    EMBEDDING_DIMENSION: int = Field(
        default=DEFAULT_OPENAI_EMBEDDING_DIMENSIONS_SMALL, 
        description="Actual dimension of the embeddings produced by the active provider."
    )

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
    
    @field_validator('ACTIVE_EMBEDDING_PROVIDER')
    @classmethod
    def check_active_provider(cls, v: str) -> str:
        valid_providers = ["openai", "sentence_transformer"]
        if v.lower() not in valid_providers:
            raise ValueError(f"Invalid ACTIVE_EMBEDDING_PROVIDER '{v}'. Must be one of {valid_providers}")
        return v.lower()

    @field_validator('ST_MODEL_DEVICE')
    @classmethod
    def check_st_model_device(cls, v: str, info: ValidationInfo) -> str:
        # LLM_FLAG: CONDITIONAL_CUDA_CHECK_START
        active_provider = info.data.get('ACTIVE_EMBEDDING_PROVIDER', DEFAULT_ACTIVE_EMBEDDING_PROVIDER)
        if active_provider != "sentence_transformer":
            # If not using ST, this validation is less critical or can be skipped.
            # However, we still validate the format if provided.
            if v.lower() not in ["cpu", "cuda"] and not v.lower().startswith("cuda:"):
                 logging.getLogger("embedding_service.config.validator").warning(f"ST_MODEL_DEVICE '{v}' provided but ST is not active. Value will be ignored.")
            return v # Return as is, will be ignored by ST adapter if ST not active.

        if not _torch_imported:
            logging.getLogger("embedding_service.config.validator").warning("Torch not imported. Cannot perform CUDA checks for ST_MODEL_DEVICE. Assuming 'cpu'.")
            return "cpu"
        # LLM_FLAG: CONDITIONAL_CUDA_CHECK_END

        device_str = v.lower()
        if device_str.startswith("cuda"):
            if not torch.cuda.is_available():
                logging.warning(f"ST_MODEL_DEVICE set to '{v}' but CUDA is not available. Forcing to 'cpu'.")
                return "cpu"
            if ":" in device_str:
                try:
                    idx = int(device_str.split(":")[1])
                    if idx >= torch.cuda.device_count():
                        logging.warning(f"CUDA device index {idx} is invalid. Max available: {torch.cuda.device_count()-1}. Using default CUDA device.")
                        return "cuda" 
                except ValueError:
                    logging.warning(f"Invalid CUDA device format '{device_str}'. Using default CUDA device 'cuda'.")
                    return "cuda"
        elif device_str != "cpu" and device_str != "mps": 
             logging.warning(f"Unsupported ST_MODEL_DEVICE '{v}'. Using 'cpu'. Supported: 'cpu', 'cuda', 'cuda:N', 'mps'.")
             return "cpu"
        return device_str

    @field_validator('WORKERS')
    @classmethod
    def check_workers_gpu(cls, v: int, info: ValidationInfo) -> int:
        active_provider = info.data.get('ACTIVE_EMBEDDING_PROVIDER', DEFAULT_ACTIVE_EMBEDDING_PROVIDER)
        st_device = info.data.get('ST_MODEL_DEVICE', DEFAULT_ST_MODEL_DEVICE)
        
        if active_provider == "sentence_transformer" and st_device.startswith("cuda"):
            if v > 1:
                logging.warning(
                    f"EMBEDDING_WORKERS set to {v} but using SentenceTransformer on CUDA. "
                    "Forcing workers to 1 to prevent VRAM contention and ensure stability."
                )
                return 1
        return v

    @field_validator('ST_BATCH_SIZE')
    @classmethod
    def set_st_batch_size_based_on_device(cls, v: int, info: ValidationInfo) -> int:
        # LLM_FLAG: CONDITIONAL_CUDA_CHECK_START
        active_provider = info.data.get('ACTIVE_EMBEDDING_PROVIDER', DEFAULT_ACTIVE_EMBEDDING_PROVIDER)
        if active_provider != "sentence_transformer":
            return v # No adjustment if ST is not active
        # LLM_FLAG: CONDITIONAL_CUDA_CHECK_END
        
        st_device = info.data.get('ST_MODEL_DEVICE', DEFAULT_ST_MODEL_DEVICE)
        if st_device.startswith("cuda") and v == DEFAULT_ST_BATCH_SIZE_CPU:
            logging.info(f"ST_MODEL_DEVICE is CUDA, adjusting ST_BATCH_SIZE to default CUDA value: {DEFAULT_ST_BATCH_SIZE_CUDA}")
            return DEFAULT_ST_BATCH_SIZE_CUDA
        return v

    @field_validator('EMBEDDING_DIMENSION', mode='after') 
    @classmethod
    def validate_embedding_dimension_vs_provider(cls, v: int, info: ValidationInfo) -> int:
        if v <= 0:
            raise ValueError("EMBEDDING_DIMENSION must be a positive integer.")

        active_provider = info.data.get('ACTIVE_EMBEDDING_PROVIDER')

        if active_provider == "openai":
            openai_model_name = info.data.get('OPENAI_EMBEDDING_MODEL_NAME', DEFAULT_OPENAI_EMBEDDING_MODEL_NAME)
            openai_dimensions_override = info.data.get('OPENAI_EMBEDDING_DIMENSIONS_OVERRIDE')
            
            expected_dimension_openai = None
            if openai_model_name == "text-embedding-3-small": expected_dimension_openai = DEFAULT_OPENAI_EMBEDDING_DIMENSIONS_SMALL
            elif openai_model_name == "text-embedding-3-large": expected_dimension_openai = DEFAULT_OPENAI_EMBEDDING_DIMENSIONS_LARGE
            elif openai_model_name == "text-embedding-ada-002": expected_dimension_openai = 1536

            final_expected_dim = expected_dimension_openai
            if openai_dimensions_override is not None:
                final_expected_dim = openai_dimensions_override
            
            if final_expected_dim is not None and v != final_expected_dim:
                 raise ValueError(
                    f"EMBEDDING_DIMENSION ({v}) must match the effective dimension ({final_expected_dim}) "
                    f"for OpenAI model '{openai_model_name}' (considering override: {openai_dimensions_override}). "
                    "Update EMBEDDING_DIMENSION in your environment/ConfigMap to match."
                )
            elif final_expected_dim is None: 
                logging.warning(f"OpenAI model '{openai_model_name}' has no default dimension in config. EMBEDDING_DIMENSION is {v}. Ensure this is correct.")

        elif active_provider == "sentence_transformer":
            st_model_name = info.data.get('ST_MODEL_NAME', DEFAULT_ST_MODEL_NAME)
            expected_dimension_st = None
            if st_model_name == "intfloat/e5-base-v2": expected_dimension_st = DEFAULT_ST_EMBEDDING_DIMENSION_E5_BASE_V2
            elif st_model_name == "intfloat/e5-large-v2": expected_dimension_st = DEFAULT_ST_EMBEDDING_DIMENSION_E5_LARGE_V2
            elif st_model_name == "intfloat/multilingual-e5-base": expected_dimension_st = DEFAULT_ST_EMBEDDING_DIMENSION_MULTILINGUAL_E5_BASE
            elif st_model_name == "sentence-transformers/all-MiniLM-L6-v2": expected_dimension_st = 384 

            if expected_dimension_st is not None:
                if v != expected_dimension_st:
                    raise ValueError(
                        f"EMBEDDING_DIMENSION ({v}) for 'sentence_transformer' provider does not match the expected dimension ({expected_dimension_st}) "
                        f"for ST model '{st_model_name}'. Correct EMBEDDING_DIMENSION or ST_MODEL_NAME."
                    )
            else: 
                logging.warning(f"ST model '{st_model_name}' has no default dimension in config. EMBEDDING_DIMENSION is {v}. Ensure this is correct.")
        
        return v
    
    @field_validator('OPENAI_API_KEY', mode='before')
    @classmethod
    def check_openai_api_key_if_provider(cls, v: Optional[str], info: ValidationInfo) -> Optional[SecretStr]:
        current_active_provider = info.data.get('ACTIVE_EMBEDDING_PROVIDER', DEFAULT_ACTIVE_EMBEDDING_PROVIDER)
        
        if current_active_provider == "openai":
            if v is None or (isinstance(v, str) and not v.strip()): 
                raise ValueError("OPENAI_API_KEY (EMBEDDING_OPENAI_API_KEY) is required when ACTIVE_EMBEDDING_PROVIDER is 'openai'.")
            return SecretStr(v)
        if v is not None and isinstance(v, str) and v.strip():
            return SecretStr(v)
        return None


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
        display_value = "********" if isinstance(value, SecretStr) else value
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