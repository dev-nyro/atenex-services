# reranker-service/app/core/config.py
import logging
import sys
from typing import Optional
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field, field_validator, ValidationInfo, ValidationError
import json
import torch 

# Definir IS_CUDA_AVAILABLE a nivel de módulo para que esté disponible globalmente
# y para los defaults de la clase Settings.
IS_CUDA_AVAILABLE = torch.cuda.is_available()
_config_validator_logger = logging.getLogger("reranker_service.config.validator")


# --- Default Values ---
DEFAULT_MODEL_NAME = "BAAI/bge-reranker-base"
DEFAULT_MODEL_DEVICE = "cpu" 
DEFAULT_LOG_LEVEL = "INFO"
DEFAULT_PORT = 8004
DEFAULT_HF_CACHE_DIR = "/app/.cache/huggingface"
DEFAULT_MAX_SEQ_LENGTH = 512

DEFAULT_BATCH_SIZE = 64 if IS_CUDA_AVAILABLE else 128
DEFAULT_GUNICORN_WORKERS = 1 if IS_CUDA_AVAILABLE else 2 
DEFAULT_TOKENIZER_WORKERS = 0 if IS_CUDA_AVAILABLE else 2 # Forzado a 0 con CUDA para estabilidad


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file='.env',
        env_prefix='RERANKER_',
        env_file_encoding='utf-8',
        case_sensitive=False,
        extra='ignore'
    )

    PROJECT_NAME: str = "Atenex Reranker Service"
    API_V1_STR: str = "/api/v1"

    LOG_LEVEL: str = Field(default=DEFAULT_LOG_LEVEL)
    PORT: int = Field(default=DEFAULT_PORT)

    MODEL_NAME: str = Field(default=DEFAULT_MODEL_NAME)
    MODEL_DEVICE: str = Field(default=DEFAULT_MODEL_DEVICE)
    HF_CACHE_DIR: Optional[str] = Field(default=DEFAULT_HF_CACHE_DIR) 
    
    BATCH_SIZE: int = Field(default=DEFAULT_BATCH_SIZE, gt=0)
    MAX_SEQ_LENGTH: int = Field(default=DEFAULT_MAX_SEQ_LENGTH, gt=0)

    WORKERS: int = Field(default=DEFAULT_GUNICORN_WORKERS, gt=0)
    TOKENIZER_WORKERS: int = Field(default=DEFAULT_TOKENIZER_WORKERS, ge=0)

    @field_validator('LOG_LEVEL')
    @classmethod
    def check_log_level(cls, v: str) -> str:
        valid_levels = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
        normalized_v = v.upper()
        if normalized_v not in valid_levels:
            raise ValueError(f"Invalid LOG_LEVEL '{v}'. Must be one of {valid_levels}")
        return normalized_v

    @field_validator('MODEL_DEVICE')
    @classmethod
    def check_model_device(cls, v: str, info: ValidationInfo) -> str:
        normalized_v = v.lower()
        # IS_CUDA_AVAILABLE ya está definida globalmente
        if normalized_v == "cuda" and not IS_CUDA_AVAILABLE:
            _config_validator_logger.warning(
                "MODEL_DEVICE set to 'cuda' but CUDA is not available. Falling back to 'cpu'."
            )
            return "cpu"
        
        allowed_devices_prefixes = ["cpu", "cuda", "mps"]
        if not any(normalized_v.startswith(prefix) for prefix in allowed_devices_prefixes):
            _config_validator_logger.warning(
                f"MODEL_DEVICE '{v}' is unusual. Ensure it's a valid device string for PyTorch/sentence-transformers."
            )
        return normalized_v

    @field_validator('WORKERS')
    @classmethod
    def limit_gunicorn_workers_on_cuda(cls, v: int, info: ValidationInfo) -> int:
        model_device_val = info.data.get('MODEL_DEVICE', DEFAULT_MODEL_DEVICE)
        if model_device_val.startswith('cuda') and v > 1:
            _config_validator_logger.warning(
                f"RERANKER_WORKERS (Gunicorn workers) was {v}, but MODEL_DEVICE is '{model_device_val}'. "
                "Forcing WORKERS=1 with GPU to prevent resource contention and potential CUDA errors."
            )
            return 1
        return v

    @field_validator('TOKENIZER_WORKERS')
    @classmethod
    def force_tokenizer_workers_zero_on_cuda(cls, v: int, info: ValidationInfo) -> int:
        model_device_val = info.data.get('MODEL_DEVICE', DEFAULT_MODEL_DEVICE)
        if model_device_val.startswith('cuda') and v > 0:
            _config_validator_logger.warning(
                f"RERANKER_TOKENIZER_WORKERS was {v}, but MODEL_DEVICE is '{model_device_val}'. "
                "Forcing TOKENIZER_WORKERS=0 with GPU to ensure stability (avoids multiprocessing for tokenization)."
            )
            return 0
        return v

# --- Global Settings Instance ---
_temp_log = logging.getLogger("reranker_service.config.loader") 
if not _temp_log.handlers: 
    _handler = logging.StreamHandler(sys.stdout)
    _formatter = logging.Formatter('%(levelname)s: [%(name)s] %(message)s')
    _handler.setFormatter(_formatter)
    _temp_log.addHandler(_handler)
    _temp_log.setLevel(logging.INFO) # Usar INFO para la carga de config

try:
    _temp_log.info("Loading Reranker Service settings...")
    _temp_log.info(f"Initial Check: torch.cuda.is_available() = {IS_CUDA_AVAILABLE}")
    settings = Settings() 
    _temp_log.info("Reranker Service Settings Loaded and Validated Successfully:")
    log_data = settings.model_dump() 
    for key_name, value_setting in log_data.items(): # Corregir nombre de variable
        _temp_log.info(f"  {key_name.upper()}: {value_setting}")

except (ValidationError, ValueError) as e:
    error_details_str = ""
    if isinstance(e, ValidationError):
        try:
            error_details_str = f"\nValidation Errors:\n{json.dumps(e.errors(), indent=2)}"
        except Exception: 
            error_details_str = f"\nRaw Errors: {e}"
    else: 
        error_details_str = f"\nError: {e}"
    
    _temp_log.critical(f"!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
    _temp_log.critical(f"! FATAL: Reranker Service configuration validation failed!{error_details_str}")
    _temp_log.critical(f"! Check environment variables (prefixed with RERANKER_) or .env file.")
    _temp_log.critical(f"!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
    sys.exit(1) 
except Exception as e:
    _temp_log.critical(f"FATAL: Unexpected error loading Reranker Service settings: {e}", exc_info=True)
    sys.exit(1)