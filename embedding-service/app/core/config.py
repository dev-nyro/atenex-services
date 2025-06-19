# embedding-service/app/core/config.py

# --- Load .env variables at import time ---
import os
try:
    from dotenv import load_dotenv
    load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), '../../.env'))
except ImportError:
    pass
import logging
# DEBUG: Print all EMBEDDING_* env vars at import time
print("DEBUG ENV:", {k: v for k, v in os.environ.items() if k.startswith("EMBEDDING")})

from typing import Optional, List, Any, Dict
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field, field_validator, ValidationError, ValidationInfo, SecretStr
import sys
import json
# LLM_FLAG: CONDITIONAL_IMPORT_START
_torch_imported = False
try:
    # Check if any provider needing torch is active
    active_provider_env = os.getenv("EMBEDDING_ACTIVE_EMBEDDING_PROVIDER", "openai").lower()
    if active_provider_env in ["sentence_transformer", "instructor"]:
        import torch
        _torch_imported = True
except ImportError:
    logging.getLogger("embedding_service.config.loader").warning(
        f"Torch import failed. If using '{active_provider_env}', ensure torch is installed."
    )
    _torch_imported = False
# LLM_FLAG: CONDITIONAL_IMPORT_END


# --- Defaults ---
DEFAULT_PROJECT_NAME = "Atenex Embedding Service"
DEFAULT_LOG_LEVEL = "INFO"
DEFAULT_API_V1_STR = "/api/v1"
DEFAULT_ACTIVE_EMBEDDING_PROVIDER = "openai" # "openai", "sentence_transformer", or "instructor"

# OpenAI specific defaults
DEFAULT_OPENAI_EMBEDDING_MODEL_NAME = "text-embedding-3-small"
DEFAULT_OPENAI_EMBEDDING_DIMENSIONS_SMALL = 1536
DEFAULT_OPENAI_EMBEDDING_DIMENSIONS_LARGE = 3072
DEFAULT_OPENAI_TIMEOUT_SECONDS = 30
DEFAULT_OPENAI_MAX_RETRIES = 3

# SentenceTransformer (ST) specific defaults
DEFAULT_ST_MODEL_NAME = "intfloat/multilingual-e5-base"
DEFAULT_ST_EMBEDDING_DIMENSION_MULTILINGUAL_E5_BASE = 768
DEFAULT_ST_MODEL_DEVICE = "cpu"
DEFAULT_ST_BATCH_SIZE_CPU = 64
DEFAULT_ST_BATCH_SIZE_CUDA = 128
DEFAULT_ST_NORMALIZE_EMBEDDINGS = True
DEFAULT_ST_USE_FP16 = True

# INSTRUCTOR specific defaults
DEFAULT_INSTRUCTOR_MODEL_NAME = "hkunlp/instructor-large"
DEFAULT_INSTRUCTOR_EMBEDDING_DIMENSION_LARGE = 768
DEFAULT_INSTRUCTOR_MODEL_DEVICE = "cpu" # Matches ST_MODEL_DEVICE logic
DEFAULT_INSTRUCTOR_BATCH_SIZE_CPU = 32 # Common default for INSTRUCTOR
DEFAULT_INSTRUCTOR_BATCH_SIZE_CUDA = 64 # Can be higher on GPU for INSTRUCTOR
DEFAULT_INSTRUCTOR_NORMALIZE_EMBEDDINGS = False # INSTRUCTOR often not normalized by default

# FastEmbed specific defaults
DEFAULT_FASTEMBED_MODEL_NAME = "sentence-transformers/all-MiniLM-L6-v2"
DEFAULT_FASTEMBED_EMBEDDING_DIMENSION = 384
DEFAULT_FASTEMBED_MAX_LENGTH = 512


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file='.env',
        env_prefix='EMBEDDING_',
        env_file_encoding='utf-8',
        case_sensitive=False,
        extra='ignore'
    )

    # --- General ---
    PROJECT_NAME: str = Field(default=DEFAULT_PROJECT_NAME)
    API_V1_STR: str = Field(default=DEFAULT_API_V1_STR)
    LOG_LEVEL: str = Field(default=DEFAULT_LOG_LEVEL)
    PORT: int = Field(default=8003)
    WORKERS: int = Field(default=2)


    # --- Active Embedding Provider ---
    ACTIVE_EMBEDDING_PROVIDER: str = Field(default=DEFAULT_ACTIVE_EMBEDDING_PROVIDER, description="Active embedding provider: 'openai', 'sentence_transformer', or 'instructor'.")

    # --- OpenAI Embedding Model ---
    OPENAI_API_KEY: Optional[SecretStr] = Field(default=None)
    OPENAI_EMBEDDING_MODEL_NAME: str = Field(default=DEFAULT_OPENAI_EMBEDDING_MODEL_NAME)
    OPENAI_EMBEDDING_DIMENSIONS_OVERRIDE: Optional[int] = Field(default=None, gt=0)
    OPENAI_API_BASE: Optional[str] = Field(default=None)
    OPENAI_TIMEOUT_SECONDS: int = Field(default=DEFAULT_OPENAI_TIMEOUT_SECONDS, gt=0)
    OPENAI_MAX_RETRIES: int = Field(default=DEFAULT_OPENAI_MAX_RETRIES, ge=0)

    # --- SentenceTransformer (ST) Model ---
    ST_MODEL_NAME: str = Field(default=DEFAULT_ST_MODEL_NAME)
    ST_MODEL_DEVICE: str = Field(default=DEFAULT_ST_MODEL_DEVICE)
    ST_HF_CACHE_DIR: Optional[str] = Field(default=None)
    ST_BATCH_SIZE: int = Field(default=DEFAULT_ST_BATCH_SIZE_CPU, gt=0)
    ST_NORMALIZE_EMBEDDINGS: bool = Field(default=DEFAULT_ST_NORMALIZE_EMBEDDINGS)
    ST_USE_FP16: bool = Field(default=DEFAULT_ST_USE_FP16)

    # --- INSTRUCTOR Model ---
    INSTRUCTOR_MODEL_NAME: str = Field(default=DEFAULT_INSTRUCTOR_MODEL_NAME)
    INSTRUCTOR_MODEL_DEVICE: str = Field(default=DEFAULT_INSTRUCTOR_MODEL_DEVICE) # Shared logic with ST_MODEL_DEVICE for validation
    INSTRUCTOR_HF_CACHE_DIR: Optional[str] = Field(default=None, description="Cache directory for HuggingFace models for INSTRUCTOR (if different from ST). Defaults to ST_HF_CACHE_DIR if not set.")
    INSTRUCTOR_BATCH_SIZE: int = Field(default=DEFAULT_INSTRUCTOR_BATCH_SIZE_CPU, gt=0)
    INSTRUCTOR_NORMALIZE_EMBEDDINGS: bool = Field(default=DEFAULT_INSTRUCTOR_NORMALIZE_EMBEDDINGS)
    # INSTRUCTOR_USE_FP16 could be added if needed, similar to ST_USE_FP16. For now, assumed handled by model if on CUDA.

    # --- Embedding Dimension (Crucial, validated based on active provider) ---
    EMBEDDING_DIMENSION: int = Field(default_factory=lambda: int(os.getenv("EMBEDDING_DIMENSION", DEFAULT_OPENAI_EMBEDDING_DIMENSIONS_SMALL)))

    # --- FastEmbed Model ---
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
        valid_providers = ["openai", "sentence_transformer", "instructor"]
        if v.lower() not in valid_providers:
            raise ValueError(f"Invalid ACTIVE_EMBEDDING_PROVIDER '{v}'. Must be one of {valid_providers}")
        return v.lower()

    @field_validator('ST_MODEL_DEVICE', 'INSTRUCTOR_MODEL_DEVICE')
    @classmethod
    def check_torch_model_device(cls, v: str, info: ValidationInfo) -> str:
        # LLM_FLAG: CONDITIONAL_CUDA_CHECK_START
        active_provider = info.data.get('ACTIVE_EMBEDDING_PROVIDER', DEFAULT_ACTIVE_EMBEDDING_PROVIDER)
        field_name = info.field_name # 'st_model_device' or 'instructor_model_device'
        
        is_relevant_provider = False
        if field_name == 'st_model_device' and active_provider == "sentence_transformer":
            is_relevant_provider = True
        elif field_name == 'instructor_model_device' and active_provider == "instructor":
            is_relevant_provider = True

        if not is_relevant_provider:
            # If not using the provider this device setting is for, validate format but it might be ignored.
            if v.lower() not in ["cpu", "cuda"] and not v.lower().startswith("cuda:"):
                 logging.getLogger("embedding_service.config.validator").warning(f"{field_name.upper()} '{v}' provided but provider '{active_provider}' is not active for it. Value may be ignored.")
            return v

        if not _torch_imported:
            logging.getLogger("embedding_service.config.validator").warning(f"Torch not imported. Cannot perform CUDA checks for {field_name.upper()}. Assuming 'cpu'.")
            return "cpu"
        # LLM_FLAG: CONDITIONAL_CUDA_CHECK_END

        device_str = v.lower()
        if device_str.startswith("cuda"):
            if not torch.cuda.is_available():
                logging.warning(f"{field_name.upper()} set to '{v}' but CUDA is not available. Forcing to 'cpu'.")
                return "cpu"
            if ":" in device_str:
                try:
                    idx = int(device_str.split(":")[1])
                    if idx >= torch.cuda.device_count():
                        logging.warning(f"CUDA device index {idx} for {field_name.upper()} is invalid. Max available: {torch.cuda.device_count()-1}. Using default CUDA device 'cuda'.")
                        return "cuda" 
                except ValueError:
                    logging.warning(f"Invalid CUDA device format '{device_str}' for {field_name.upper()}. Using default CUDA device 'cuda'.")
                    return "cuda"
        elif device_str != "cpu" and device_str != "mps": # MPS for Apple Silicon
             logging.warning(f"Unsupported {field_name.upper()} '{v}'. Using 'cpu'. Supported: 'cpu', 'cuda', 'cuda:N', 'mps'.")
             return "cpu"
        return device_str

    @field_validator('WORKERS')
    @classmethod
    def check_workers_gpu(cls, v: int, info: ValidationInfo) -> int:
        active_provider = info.data.get('ACTIVE_EMBEDDING_PROVIDER', DEFAULT_ACTIVE_EMBEDDING_PROVIDER)
        device = "cpu" # Default
        if active_provider == "sentence_transformer":
            device = info.data.get('ST_MODEL_DEVICE', DEFAULT_ST_MODEL_DEVICE)
        elif active_provider == "instructor":
            device = info.data.get('INSTRUCTOR_MODEL_DEVICE', DEFAULT_INSTRUCTOR_MODEL_DEVICE)
        
        if device.startswith("cuda"):
            if v > 1:
                logging.warning(
                    f"EMBEDDING_WORKERS set to {v} but using provider '{active_provider}' on CUDA. "
                    "Forcing workers to 1 to prevent VRAM contention and ensure stability."
                )
                return 1
        return v

    @field_validator('ST_BATCH_SIZE')
    @classmethod
    def set_st_batch_size_based_on_device(cls, v: int, info: ValidationInfo) -> int:
        active_provider = info.data.get('ACTIVE_EMBEDDING_PROVIDER', DEFAULT_ACTIVE_EMBEDDING_PROVIDER)
        if active_provider != "sentence_transformer":
            return v 
        
        st_device = info.data.get('ST_MODEL_DEVICE', DEFAULT_ST_MODEL_DEVICE)
        if st_device.startswith("cuda") and v == DEFAULT_ST_BATCH_SIZE_CPU: # If user left it as CPU default
            logging.info(f"ST_MODEL_DEVICE is CUDA, adjusting ST_BATCH_SIZE to default CUDA value: {DEFAULT_ST_BATCH_SIZE_CUDA}")
            return DEFAULT_ST_BATCH_SIZE_CUDA
        return v

    @field_validator('INSTRUCTOR_BATCH_SIZE')
    @classmethod
    def set_instructor_batch_size_based_on_device(cls, v: int, info: ValidationInfo) -> int:
        active_provider = info.data.get('ACTIVE_EMBEDDING_PROVIDER', DEFAULT_ACTIVE_EMBEDDING_PROVIDER)
        if active_provider != "instructor":
            return v
        
        instructor_device = info.data.get('INSTRUCTOR_MODEL_DEVICE', DEFAULT_INSTRUCTOR_MODEL_DEVICE)
        if instructor_device.startswith("cuda") and v == DEFAULT_INSTRUCTOR_BATCH_SIZE_CPU: # If user left it as CPU default
            logging.info(f"INSTRUCTOR_MODEL_DEVICE is CUDA, adjusting INSTRUCTOR_BATCH_SIZE to default CUDA value: {DEFAULT_INSTRUCTOR_BATCH_SIZE_CUDA}")
            return DEFAULT_INSTRUCTOR_BATCH_SIZE_CUDA
        return v
    
    @field_validator('INSTRUCTOR_HF_CACHE_DIR', mode='before')
    @classmethod
    def set_instructor_hf_cache_dir_default(cls, v: Optional[str], info: ValidationInfo) -> Optional[str]:
        if v is None: # If not explicitly set for INSTRUCTOR
            return info.data.get('ST_HF_CACHE_DIR') # Default to ST cache dir
        return v


    @field_validator('EMBEDDING_DIMENSION', mode='after') 
    @classmethod
    def validate_embedding_dimension_vs_provider(cls, v: int, info: ValidationInfo) -> int:
        if v <= 0:
            raise ValueError("EMBEDDING_DIMENSION must be a positive integer.")

        active_provider = info.data.get('ACTIVE_EMBEDDING_PROVIDER')
        logger = logging.getLogger("embedding_service.config.validator")

        if active_provider == "openai":
            openai_model_name = info.data.get('OPENAI_EMBEDDING_MODEL_NAME', DEFAULT_OPENAI_EMBEDDING_MODEL_NAME)
            openai_dimensions_override = info.data.get('OPENAI_EMBEDDING_DIMENSIONS_OVERRIDE')
            
            expected_dimension_openai = None
            if openai_model_name == "text-embedding-3-small": expected_dimension_openai = DEFAULT_OPENAI_EMBEDDING_DIMENSIONS_SMALL
            elif openai_model_name == "text-embedding-3-large": expected_dimension_openai = DEFAULT_OPENAI_EMBEDDING_DIMENSIONS_LARGE
            elif openai_model_name == "text-embedding-ada-002": expected_dimension_openai = 1536
            # Add other OpenAI models here if needed

            final_expected_dim = expected_dimension_openai
            if openai_dimensions_override is not None:
                final_expected_dim = openai_dimensions_override
            
            if final_expected_dim is not None and v != final_expected_dim:
                 raise ValueError(
                    f"EMBEDDING_DIMENSION ({v}) must match the effective dimension ({final_expected_dim}) "
                    f"for OpenAI model '{openai_model_name}' (override: {openai_dimensions_override})."
                )
            elif final_expected_dim is None: 
                logger.warning(f"OpenAI model '{openai_model_name}' has no default dimension in config. EMBEDDING_DIMENSION is {v}. Ensure this is correct.")

        elif active_provider == "sentence_transformer":
            st_model_name = info.data.get('ST_MODEL_NAME', DEFAULT_ST_MODEL_NAME)
            expected_dimension_st = None
            # Common ST models
            if st_model_name == "intfloat/multilingual-e5-base": expected_dimension_st = DEFAULT_ST_EMBEDDING_DIMENSION_MULTILINGUAL_E5_BASE
            elif st_model_name == "intfloat/e5-large-v2": expected_dimension_st = 1024
            elif st_model_name == "sentence-transformers/all-MiniLM-L6-v2": expected_dimension_st = 384
            # Add other ST models here

            if expected_dimension_st is not None and v != expected_dimension_st:
                raise ValueError(
                    f"EMBEDDING_DIMENSION ({v}) for 'sentence_transformer' provider does not match expected dimension ({expected_dimension_st}) "
                    f"for ST model '{st_model_name}'. Correct EMBEDDING_DIMENSION or ST_MODEL_NAME."
                )
            elif expected_dimension_st is None:
                logger.warning(f"ST model '{st_model_name}' has no default dimension mapping in config. EMBEDDING_DIMENSION is {v}. Ensure this is correct by checking the model's documentation.")

        elif active_provider == "instructor":
            instructor_model_name = info.data.get('INSTRUCTOR_MODEL_NAME', DEFAULT_INSTRUCTOR_MODEL_NAME)
            expected_dimension_instructor = None
            # Common INSTRUCTOR models
            if instructor_model_name == "hkunlp/instructor-large": expected_dimension_instructor = DEFAULT_INSTRUCTOR_EMBEDDING_DIMENSION_LARGE
            elif instructor_model_name == "hkunlp/instructor-base": expected_dimension_instructor = 768
            elif instructor_model_name == "hkunlp/instructor-xl": expected_dimension_instructor = 1024
            # Add other INSTRUCTOR models here

            if expected_dimension_instructor is not None and v != expected_dimension_instructor:
                raise ValueError(
                    f"EMBEDDING_DIMENSION ({v}) for 'instructor' provider does not match expected dimension ({expected_dimension_instructor}) "
                    f"for INSTRUCTOR model '{instructor_model_name}'. Correct EMBEDDING_DIMENSION or INSTRUCTOR_MODEL_NAME."
                )
            elif expected_dimension_instructor is None:
                logger.warning(f"INSTRUCTOR model '{instructor_model_name}' has no default dimension mapping in config. EMBEDDING_DIMENSION is {v}. Ensure this is correct by checking the model's documentation.")

        return v
    
    @field_validator('OPENAI_API_KEY', mode='before')
    @classmethod
    def check_openai_api_key_if_provider(cls, v: Optional[str], info: ValidationInfo) -> Optional[SecretStr]:
        current_active_provider = info.data.get('ACTIVE_EMBEDDING_PROVIDER', os.getenv('EMBEDDING_ACTIVE_EMBEDDING_PROVIDER', DEFAULT_ACTIVE_EMBEDDING_PROVIDER))
        
        if current_active_provider == "openai":
            if v is None or (isinstance(v, str) and not v.strip()): 
                raise ValueError("OPENAI_API_KEY (EMBEDDING_OPENAI_API_KEY) is required when ACTIVE_EMBEDDING_PROVIDER is 'openai'.")
            return SecretStr(v)
        if v is not None and isinstance(v, str) and v.strip(): # If provided even when not active, still parse as SecretStr
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
    # Log effective cache dir for INSTRUCTOR (after potential default from ST)
    if settings.ACTIVE_EMBEDDING_PROVIDER == 'instructor':
        settings_dump = settings.model_dump()
        settings_dump['INSTRUCTOR_HF_CACHE_DIR'] = settings.INSTRUCTOR_HF_CACHE_DIR
    else:
        settings_dump = settings.model_dump()

    temp_log_config.info("--- Embedding Service Settings Loaded ---")
    for key, value in settings_dump.items():
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