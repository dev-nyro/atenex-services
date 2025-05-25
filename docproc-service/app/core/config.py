import logging
import sys
import json
from typing import List, Optional

from pydantic import Field, field_validator, AnyHttpUrl, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

# --- Defaults ---
DEFAULT_PORT = 8005
DEFAULT_CHUNK_SIZE = 1000
DEFAULT_CHUNK_OVERLAP = 200
DEFAULT_SUPPORTED_CONTENT_TYPES = [
    "application/pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",  # DOCX
    "application/msword",  # DOC (will also be handled by docx_extractor typically)
    "text/plain",
    "text/markdown",
    "text/html",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", # XLSX
    "application/vnd.ms-excel" # XLS
]

class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file='.env', env_prefix='DOCPROC_', env_file_encoding='utf-8',
        case_sensitive=False, extra='ignore'
    )

    PROJECT_NAME: str = "Atenex Document Processing Service"
    API_V1_STR: str = "/api/v1"
    LOG_LEVEL: str = "INFO"
    PORT: int = DEFAULT_PORT

    CHUNK_SIZE: int = DEFAULT_CHUNK_SIZE
    CHUNK_OVERLAP: int = DEFAULT_CHUNK_OVERLAP
    SUPPORTED_CONTENT_TYPES: List[str] = Field(default_factory=lambda: DEFAULT_SUPPORTED_CONTENT_TYPES)

    # Optional: If this service needs to call other internal services
    # HTTP_CLIENT_TIMEOUT: int = 60
    # HTTP_CLIENT_MAX_RETRIES: int = 3
    # HTTP_CLIENT_BACKOFF_FACTOR: float = 0.5

    @field_validator("LOG_LEVEL")
    @classmethod
    def check_log_level(cls, v: str) -> str:
        valid_levels = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
        normalized_v = v.upper()
        if normalized_v not in valid_levels:
            raise ValueError(f"Invalid LOG_LEVEL '{v}'. Must be one of {valid_levels}")
        return normalized_v

    @field_validator("SUPPORTED_CONTENT_TYPES", mode='before')
    @classmethod
    def assemble_supported_content_types(cls, v: Optional[str | List[str]]) -> List[str]:
        if isinstance(v, str):
            try:
                parsed_list = json.loads(v)
                if not isinstance(parsed_list, list) or not all(isinstance(item, str) for item in parsed_list):
                    raise ValueError("If string, must be a JSON array of strings.")
                # Convert to lowercase for consistent comparison
                return [s.strip().lower() for s in parsed_list if s.strip()]
            except json.JSONDecodeError:
                if '[' not in v and ']' not in v:
                     # Convert to lowercase for consistent comparison
                    return [s.strip().lower() for s in v.split(',') if s.strip()]
                raise ValueError("SUPPORTED_CONTENT_TYPES must be a valid JSON array of strings or a comma-separated string.")
        elif isinstance(v, list) and all(isinstance(item, str) for item in v):
            # Convert to lowercase for consistent comparison
            return [s.strip().lower() for s in v if s.strip()]
        elif v is None: 
            # Convert to lowercase for consistent comparison
            return [s.lower() for s in DEFAULT_SUPPORTED_CONTENT_TYPES]
        raise ValueError("SUPPORTED_CONTENT_TYPES must be a list of strings or a JSON string array.")

    @field_validator('CHUNK_SIZE', 'CHUNK_OVERLAP')
    @classmethod
    def check_positive_integer(cls, v: int, info) -> int:
        if v < 0:
            raise ValueError(f"{info.field_name} must be non-negative.")
        return v

    @field_validator('CHUNK_OVERLAP')
    @classmethod
    def check_overlap_less_than_size(cls, v: int, info) -> int:
        chunk_size = info.data.get('CHUNK_SIZE', DEFAULT_CHUNK_SIZE)
        if v >= chunk_size:
            raise ValueError(f"CHUNK_OVERLAP ({v}) must be less than CHUNK_SIZE ({chunk_size}).")
        return v

temp_log_config = logging.getLogger("docproc_service.config.loader")
if not temp_log_config.handlers:
    handler = logging.StreamHandler(sys.stdout)
    formatter = logging.Formatter('%(levelname)-8s [%(asctime)s] [%(name)s] %(message)s', datefmt='%Y-%m-%d %H:%M:%S')
    handler.setFormatter(formatter)
    temp_log_config.addHandler(handler)
    temp_log_config.setLevel(logging.INFO)

try:
    temp_log_config.info("Loading Document Processing Service settings...")
    settings = Settings()
    temp_log_config.info("--- Document Processing Service Settings Loaded ---")
    temp_log_config.info(f"  PROJECT_NAME:            {settings.PROJECT_NAME}")
    temp_log_config.info(f"  LOG_LEVEL:               {settings.LOG_LEVEL}")
    temp_log_config.info(f"  PORT:                    {settings.PORT}")
    temp_log_config.info(f"  API_V1_STR:              {settings.API_V1_STR}")
    temp_log_config.info(f"  CHUNK_SIZE:              {settings.CHUNK_SIZE}")
    temp_log_config.info(f"  CHUNK_OVERLAP:           {settings.CHUNK_OVERLAP}")
    temp_log_config.info(f"  SUPPORTED_CONTENT_TYPES: {settings.SUPPORTED_CONTENT_TYPES}")
    temp_log_config.info(f"---------------------------------------------")

except (ValidationError, ValueError) as e:
    error_details_config = ""
    if isinstance(e, ValidationError):
        try: error_details_config = f"\nValidation Errors:\n{e.json(indent=2)}"
        except Exception: error_details_config = f"\nRaw Errors: {e.errors()}"
    temp_log_config.critical("!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
    temp_log_config.critical(f"! FATAL: DocProc Service configuration validation failed:{error_details_config}")
    temp_log_config.critical(f"! Check environment variables (prefixed with DOCPROC_) or .env file.")
    temp_log_config.critical(f"! Original Error: {e}")
    temp_log_config.critical("!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
    sys.exit(1)
except Exception as e_config:
    temp_log_config.exception(f"FATAL: Unexpected error loading DocProc Service settings: {e_config}")
    sys.exit(1)