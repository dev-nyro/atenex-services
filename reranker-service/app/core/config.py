# reranker-service/app/core/config.py
import logging
import sys
from typing import Optional
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field, field_validator, ValidationInfo, ValidationError
import json

# --- Default Values ---
DEFAULT_MODEL_NAME = "BAAI/bge-reranker-base"
DEFAULT_MODEL_DEVICE = "cpu"
DEFAULT_LOG_LEVEL = "INFO"
DEFAULT_PORT = 8004
DEFAULT_HF_CACHE_DIR = "/app/.cache/huggingface" # Dentro del contenedor
DEFAULT_BATCH_SIZE = 32
DEFAULT_MAX_SEQ_LENGTH = 512
DEFAULT_GUNICORN_WORKERS = 2


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file='.env',
        env_prefix='RERANKER_',
        env_file_encoding='utf-8',
        case_sensitive=False,
        extra='ignore'
    )

    LOG_LEVEL: str = Field(default=DEFAULT_LOG_LEVEL)
    PORT: int = Field(default=DEFAULT_PORT)

    MODEL_NAME: str = Field(default=DEFAULT_MODEL_NAME)
    MODEL_DEVICE: str = Field(default=DEFAULT_MODEL_DEVICE)
    HF_CACHE_DIR: Optional[str] = Field(default=DEFAULT_HF_CACHE_DIR) # Permite None para usar default de HF
    
    BATCH_SIZE: int = Field(default=DEFAULT_BATCH_SIZE, gt=0)
    MAX_SEQ_LENGTH: int = Field(default=DEFAULT_MAX_SEQ_LENGTH, gt=0)

    WORKERS: int = Field(default=DEFAULT_GUNICORN_WORKERS, gt=0)

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
    def check_model_device(cls, v: str) -> str:
        # Podría añadir validación más específica si fuera necesario (ej. regex para 'cuda:X')
        return v.lower()


# --- Global Settings Instance ---
# Similar al query-service, para logging durante la carga
_temp_log = logging.getLogger("reranker_service.config.loader")
if not _temp_log.handlers:
    _handler = logging.StreamHandler(sys.stdout)
    _formatter = logging.Formatter('%(levelname)s: [%(name)s] %(message)s')
    _handler.setFormatter(_formatter)
    _temp_log.addHandler(_handler)
    _temp_log.setLevel(logging.INFO) # Usar INFO para la carga

try:
    _temp_log.info("Loading Reranker Service settings...")
    settings = Settings()
    _temp_log.info("Reranker Service Settings Loaded Successfully:")
    log_data = settings.model_dump()
    for key, value in log_data.items():
        _temp_log.info(f"  {key}: {value}")

except (ValidationError, ValueError) as e:
    error_details = ""
    if isinstance(e, ValidationError):
        try: error_details = f"\nValidation Errors:\n{json.dumps(e.errors(), indent=2)}"
        except Exception: error_details = f"\nRaw Errors: {e}"
    else: error_details = f"\nError: {e}"
    _temp_log.critical(f"CRITICAL: Reranker Service configuration validation failed!{error_details}")
    _temp_log.critical(f"Check environment variables (prefixed with RERANKER_) or .env file.")
    sys.exit(1)
except Exception as e:
    _temp_log.exception(f"CRITICAL: Unexpected error loading Reranker Service settings")
    sys.exit(1)