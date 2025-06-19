# Estructura de la Codebase

```
app/
├── __init__.py
├── api
│   ├── __init__.py
│   └── v1
│       ├── __init__.py
│       ├── endpoints
│       │   ├── __init__.py
│       │   └── embedding_endpoint.py
│       └── schemas.py
├── application
│   ├── __init__.py
│   ├── ports
│   │   ├── __init__.py
│   │   └── embedding_model_port.py
│   └── use_cases
│       ├── __init__.py
│       └── embed_texts_use_case.py
├── core
│   ├── __init__.py
│   ├── config.py
│   └── logging_config.py
├── dependencies.py
├── domain
│   ├── __init__.py
│   └── models.py
├── gunicorn_conf.py
├── infrastructure
│   ├── __init__.py
│   └── embedding_models
│       ├── __init__.py
│       ├── fastembed_adapter.py
│       ├── instructor_adapter.py
│       ├── openai_adapter.py
│       └── sentence_transformer_adapter.py
├── main.py
└── utils
    └── __init__.py
```

# Codebase: `app`

## File: `app\__init__.py`
```py

```

## File: `app\api\__init__.py`
```py
# API package for embedding-service

```

## File: `app\api\v1\__init__.py`
```py
# v1 API package for embedding-service

```

## File: `app\api\v1\endpoints\__init__.py`
```py
# Endpoints package for v1 API

```

## File: `app\api\v1\endpoints\embedding_endpoint.py`
```py
# embedding-service/app/api/v1/endpoints/embedding_endpoint.py
import uuid
from typing import List
import structlog
from fastapi import APIRouter, Depends, HTTPException, status, Request

from app.api.v1 import schemas
from app.application.use_cases.embed_texts_use_case import EmbedTextsUseCase
from app.dependencies import get_embed_texts_use_case # Import a resolver from dependencies

router = APIRouter()
log = structlog.get_logger(__name__)

@router.post(
    "/embed",
    response_model=schemas.EmbedResponse,
    status_code=status.HTTP_200_OK,
    summary="Generate Embeddings for Texts",
    description="Receives a list of texts and returns their corresponding embeddings using the configured model.",
)
async def embed_texts_endpoint(
    request_body: schemas.EmbedRequest,
    use_case: EmbedTextsUseCase = Depends(get_embed_texts_use_case), # Use dependency resolver
    request: Request = None,
):
    request_id = request.headers.get("x-request-id", str(uuid.uuid4())) if request else str(uuid.uuid4())
    endpoint_log = log.bind(
        request_id=request_id,
        num_texts=len(request_body.texts)
    )
    endpoint_log.info("Received request to generate embeddings")

    if not request_body.texts:
        endpoint_log.warning("No texts provided for embedding.")
        # It's better to return an empty list than an error for no texts.
        # Or, validate in Pydantic schema to require at least one text.
        # For now, let the use case handle it or return empty.
        # raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No texts provided for embedding.")

    try:
        embeddings_list, model_info = await use_case.execute(request_body.texts)
        endpoint_log.info("Embeddings generated successfully", num_embeddings=len(embeddings_list))
        return schemas.EmbedResponse(embeddings=embeddings_list, model_info=model_info)
    except ValueError as ve: # Catch specific errors from use_case
        endpoint_log.error("Validation error during embedding", error=str(ve), exc_info=True)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(ve))
    except ConnectionError as ce: # If model loading fails critically
        endpoint_log.critical("Embedding model/service connection error", error=str(ce), exc_info=True)
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Embedding service is unavailable.")
    except Exception as e:
        endpoint_log.exception("Unexpected error generating embeddings")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Internal server error while generating embeddings.")
```

## File: `app\api\v1\schemas.py`
```py
# embedding-service/app/api/v1/schemas.py
from pydantic import BaseModel, Field, conlist
from typing import List, Dict, Any, Literal

TextType = Literal["query", "passage"]

class EmbedRequest(BaseModel):
    texts: conlist(str, min_length=1) = Field(
        ...,
        description="A list of texts to be embedded. Each text must not be empty.",
        examples=[["Hello world", "Another piece of text"]]
    )

class ModelInfo(BaseModel):
    model_name: str = Field(..., description="Name of the embedding model used.")
    dimension: int = Field(..., description="Dimension of the generated embeddings.")

class EmbedResponse(BaseModel):
    embeddings: List[List[float]] = Field(..., description="A list of embeddings, where each embedding is a list of floats.")
    model_info: ModelInfo = Field(..., description="Information about the model used for embedding.")

    class Config:
        json_schema_extra = {
            "example": {
                "embeddings": [
                    [0.001, -0.02, ..., 0.03],
                    [0.04, 0.005, ..., -0.006]
                ],
                "model_info": {
                    "model_name": "text-embedding-3-small",
                    "dimension": 1536
                }
            }
        }

class HealthCheckResponse(BaseModel):
    status: str = Field(default="ok", description="Overall status of the service: 'ok' or 'error'.")
    service: str = Field(..., description="Name of the service.")
    model_status: str = Field(
        ..., 
        description="Status of the embedding model client: 'client_ready', 'client_error', 'client_not_initialized', 'client_initialization_pending_or_failed'."
    )
    model_name: str | None = Field(None, description="Name of the configured/used embedding model, if available.")
    model_dimension: int | None = Field(None, description="Dimension of the configured/used embedding model, if available.")

    class Config:
        json_schema_extra = {
            "example_healthy": {
                "status": "ok",
                "service": "Atenex Embedding Service",
                "model_status": "client_ready",
                "model_name": "text-embedding-3-small",
                "model_dimension": 1536
            },
            "example_unhealthy_init_failed": {
                "status": "error",
                "service": "Atenex Embedding Service",
                "model_status": "client_initialization_pending_or_failed",
                "model_name": "text-embedding-3-small",
                "model_dimension": 1536
            },
             "example_unhealthy_client_error": {
                "status": "error",
                "service": "Atenex Embedding Service",
                "model_status": "client_error",
                "model_name": "text-embedding-3-small",
                "model_dimension": 1536
            }
        }
```

## File: `app\application\__init__.py`
```py

```

## File: `app\application\ports\__init__.py`
```py
# embedding-service/app/application/ports/__init__.py
from .embedding_model_port import EmbeddingModelPort

__all__ = ["EmbeddingModelPort"]
```

## File: `app\application\ports\embedding_model_port.py`
```py
# embedding-service/app/application/ports/embedding_model_port.py
import abc
from typing import List, Tuple, Dict, Any

class EmbeddingModelPort(abc.ABC):
    """
    Abstract port defining the interface for an embedding model.
    """

    @abc.abstractmethod
    async def embed_texts(self, texts: List[str]) -> List[List[float]]:
        """
        Generates embeddings for a list of texts.

        Args:
            texts: A list of strings to embed.

        Returns:
            A list of embeddings, where each embedding is a list of floats.

        Raises:
            Exception: If embedding generation fails.
        """
        raise NotImplementedError

    @abc.abstractmethod
    def get_model_info(self) -> Dict[str, Any]:
        """
        Returns information about the loaded embedding model.

        Returns:
            A dictionary containing model_name, dimension, etc.
        """
        raise NotImplementedError

    @abc.abstractmethod
    async def health_check(self) -> Tuple[bool, str]:
        """
        Checks the health of the embedding model.

        Returns:
            A tuple (is_healthy: bool, status_message: str).
        """
        raise NotImplementedError
```

## File: `app\application\use_cases\__init__.py`
```py

```

## File: `app\application\use_cases\embed_texts_use_case.py`
```py
# embedding-service/app/application/use_cases/embed_texts_use_case.py
import structlog
from typing import List, Tuple, Dict, Any

from app.application.ports.embedding_model_port import EmbeddingModelPort
from app.api.v1.schemas import ModelInfo # For response structure

log = structlog.get_logger(__name__)

class EmbedTextsUseCase:
    """
    Use case for generating embeddings for a list of texts.
    """
    def __init__(self, embedding_model: EmbeddingModelPort):
        self.embedding_model = embedding_model
        log.info("EmbedTextsUseCase initialized", model_adapter=type(embedding_model).__name__)

    async def execute(self, texts: List[str]) -> Tuple[List[List[float]], ModelInfo]:
        """
        Executes the embedding generation process.

        Args:
            texts: A list of strings to embed.

        Returns:
            A tuple containing:
                - A list of embeddings (list of lists of floats).
                - ModelInfo object containing details about the embedding model.

        Raises:
            ValueError: If no texts are provided.
            Exception: If embedding generation fails.
        """
        if not texts:
            log.warning("EmbedTextsUseCase executed with no texts.")
            # Return empty list and model info, consistent with schema
            model_info_dict = self.embedding_model.get_model_info()
            return [], ModelInfo(**model_info_dict)


        use_case_log = log.bind(num_texts=len(texts))
        use_case_log.info("Executing embedding generation for texts")

        try:
            embeddings = await self.embedding_model.embed_texts(texts)
            model_info_dict = self.embedding_model.get_model_info()
            model_info_obj = ModelInfo(**model_info_dict)

            use_case_log.info("Successfully generated embeddings", num_embeddings=len(embeddings))
            return embeddings, model_info_obj
        except Exception as e:
            use_case_log.exception("Failed to generate embeddings in use case")
            # Re-raise to be handled by the endpoint or a global exception handler
            raise
```

## File: `app\core\__init__.py`
```py

```

## File: `app\core\config.py`
```py
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
```

## File: `app\core\logging_config.py`
```py
# embedding-service/app/core/logging_config.py
import logging
import sys
import structlog
from app.core.config import settings # Ensures settings are loaded first

def setup_logging():
    """Configures structured logging with structlog for the Embedding Service."""

    shared_processors = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
    ]

    if settings.LOG_LEVEL == "DEBUG":
         shared_processors.append(structlog.processors.CallsiteParameterAdder(
             {
                 structlog.processors.CallsiteParameter.FILENAME,
                 structlog.processors.CallsiteParameter.LINENO,
             }
         ))

    structlog.configure(
        processors=shared_processors + [
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.JSONRenderer(),
        ],
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    # Avoid adding handler multiple times if already configured
    if not any(isinstance(h, logging.StreamHandler) and isinstance(h.formatter, structlog.stdlib.ProcessorFormatter) for h in root_logger.handlers):
        root_logger.addHandler(handler)

    root_logger.setLevel(settings.LOG_LEVEL.upper())

    # Silence verbose libraries that might be used by FastEmbed or its dependencies
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("huggingface_hub").setLevel(logging.WARNING)
    logging.getLogger("PIL").setLevel(logging.INFO)
    # Add others as needed

    log = structlog.get_logger("embedding_service")
    log.info("Logging configured for Embedding Service", log_level=settings.LOG_LEVEL)
```

## File: `app\dependencies.py`
```py
# embedding-service/app/dependencies.py
"""
Centralized dependency injection resolver for the Embedding Service.
"""
import structlog
from fastapi import HTTPException, status
from app.application.use_cases.embed_texts_use_case import EmbedTextsUseCase
from app.application.ports.embedding_model_port import EmbeddingModelPort

# These will be set by main.py at startup
_embed_texts_use_case_instance: EmbedTextsUseCase | None = None
_service_ready: bool = False

log = structlog.get_logger(__name__)

def set_embedding_service_dependencies(
    use_case_instance: EmbedTextsUseCase,
    ready_flag: bool
):
    """Called from main.py lifespan to set up shared instances."""
    global _embed_texts_use_case_instance, _service_ready
    _embed_texts_use_case_instance = use_case_instance
    _service_ready = ready_flag
    log.info("Embedding service dependencies set", use_case_ready=bool(use_case_instance), service_ready=ready_flag)

def get_embed_texts_use_case() -> EmbedTextsUseCase:
    """Dependency provider for EmbedTextsUseCase."""
    if not _service_ready or not _embed_texts_use_case_instance:
        log.error("EmbedTextsUseCase requested but service is not ready or use case not initialized.")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Embedding service is not ready. Please try again later."
        )
    return _embed_texts_use_case_instance
```

## File: `app\domain\__init__.py`
```py
# Domain package for embedding-service

```

## File: `app\domain\models.py`
```py
# embedding-service/app/domain/models.py
from pydantic import BaseModel, Field
from typing import List, Dict, Any, Optional

# Currently, requests and responses are simple enough to be handled by API schemas.
# This file is a placeholder if more complex domain logic/entities arise.

# Example of a potential domain model if needed:
# class EmbeddingResult(BaseModel):
#     text_id: Optional[str] = None # If texts need to be identified
#     vector: List[float]
#     source_text_preview: str # For context
```

## File: `app\gunicorn_conf.py`
```py
# embedding-service/app/gunicorn_conf.py
import os
import multiprocessing
from app.core.config import settings # Load settings to access configured WORKERS

# Gunicorn config variables
# `settings.WORKERS` is already validated in config.py based on device
workers = settings.WORKERS
worker_class = "uvicorn.workers.UvicornWorker"

# Bind to 0.0.0.0 to be accessible from outside the container
host = os.getenv("EMBEDDING_HOST", "0.0.0.0")
port = os.getenv("EMBEDDING_PORT", str(settings.PORT)) # Use settings.PORT as default
bind = f"{host}:{port}"

# Logging
# Gunicorn logging will be handled by structlog through FastAPI's setup
# accesslog = "-" # Log to stdout
# errorlog = "-"  # Log to stderr
# loglevel = settings.LOG_LEVEL.lower() # Already handled by app

# Worker timeout
timeout = int(os.getenv("EMBEDDING_GUNICORN_TIMEOUT", "120"))

# Other settings
# preload_app = True # Consider if model loading time is very long and you want to load before forking workers.
                     # However, for GPU models, it's often better to load in each worker if workers > 1,
                     # but we force workers=1 for GPU SentenceTransformer for now.

# Print effective Gunicorn configuration for clarity
print("--- Gunicorn Configuration ---")
print(f"Workers: {workers}")
print(f"Worker Class: {worker_class}")
print(f"Bind: {bind}")
print(f"Timeout: {timeout}")
# print(f"Preload App: {preload_app}")
print("----------------------------")
```

## File: `app\infrastructure\__init__.py`
```py
# Infrastructure package for embedding-service

```

## File: `app\infrastructure\embedding_models\__init__.py`
```py
# Models subpackage for infrastructure

```

## File: `app\infrastructure\embedding_models\fastembed_adapter.py`
```py
# embedding-service/app/infrastructure/embedding_models/fastembed_adapter.py
import structlog
from typing import List, Tuple, Dict, Any, Optional
import asyncio
import time

from fastembed import TextEmbedding 

from app.application.ports.embedding_model_port import EmbeddingModelPort
from app.core.config import settings

log = structlog.get_logger(__name__)

class FastEmbedAdapter(EmbeddingModelPort):
    """
    Adapter for FastEmbed library.
    Note: This adapter is currently not the primary one used by default.
    """
    _model: Optional[TextEmbedding] = None
    _model_name: str
    _model_dimension: int # This will be set from global settings.EMBEDDING_DIMENSION
    _model_loaded: bool = False
    _model_load_error: Optional[str] = None

    def __init__(self):
        self._model_name = settings.FASTEMBED_MODEL_NAME
        # IMPORTANT: This adapter will use the global EMBEDDING_DIMENSION from settings.
        # If settings.EMBEDDING_DIMENSION is configured for OpenAI (e.g., 1536),
        # and this FastEmbed model (e.g., all-MiniLM-L6-v2 -> 384) has a different dimension,
        # the health check/validation within initialize_model WILL LIKELY FAIL.
        # This is expected if the service is globally configured for a different provider like OpenAI.
        self._model_dimension = settings.EMBEDDING_DIMENSION
        log.info("FastEmbedAdapter initialized", configured_model_name=self._model_name, expected_dimension_from_global_settings=self._model_dimension)


    async def initialize_model(self):
        """
        Initializes and loads the FastEmbed model.
        This should be called during service startup (e.g., lifespan).
        """
        if self._model_loaded:
            log.debug("FastEmbed model already initialized.", model_name=self._model_name)
            return

        init_log = log.bind(adapter="FastEmbedAdapter", action="initialize_model", model_name=self._model_name)
        init_log.info("Initializing FastEmbed model...")
        start_time = time.perf_counter()
        try:
            # TextEmbedding initialization is synchronous
            self._model = TextEmbedding(
                model_name=self._model_name,
                cache_dir=settings.FASTEMBED_CACHE_DIR,
                threads=settings.FASTEMBED_THREADS,
                max_length=settings.FASTEMBED_MAX_LENGTH,
            )
            
            # Perform a test embedding to confirm dimension and successful loading
            # The .embed() method is CPU-bound, so run in a thread pool if called from async context
            test_embeddings_generator = await asyncio.to_thread(self._model.embed, ["test vector"])
            test_embeddings_list = list(test_embeddings_generator) 

            if not test_embeddings_list or not test_embeddings_list[0].any():
                raise ValueError("Test embedding with FastEmbed failed or returned empty result.")

            actual_dim = len(test_embeddings_list[0])
            
            # Validate against the dimension this adapter was initialized with (from global settings)
            if actual_dim != self._model_dimension:
                self._model_load_error = (
                    f"FastEmbed Model dimension mismatch. Global EMBEDDING_DIMENSION is {self._model_dimension}, "
                    f"but FastEmbed model '{self._model_name}' produced {actual_dim} dimensions."
                )
                init_log.error(self._model_load_error)
                self._model = None 
                self._model_loaded = False # Ensure model is not marked as loaded
                # This exception will be caught and handled by the main startup logic
                raise ValueError(self._model_load_error) 

            self._model_loaded = True
            self._model_load_error = None
            duration_ms = (time.perf_counter() - start_time) * 1000
            init_log.info("FastEmbed model initialized and validated successfully.", duration_ms=duration_ms, actual_dimension=actual_dim)

        except Exception as e:
            self._model_load_error = f"Failed to load FastEmbed model '{self._model_name}': {str(e)}"
            init_log.critical(self._model_load_error, exc_info=True)
            self._model = None
            self._model_loaded = False
            # Propagate as ConnectionError to indicate a critical startup failure for this adapter
            raise ConnectionError(self._model_load_error) from e


    async def embed_texts(self, texts: List[str]) -> List[List[float]]:
        if not self._model_loaded or not self._model:
            log.error("FastEmbed model not loaded. Cannot generate embeddings.", model_error=self._model_load_error)
            raise ConnectionError(f"FastEmbed model is not available. Load error: {self._model_load_error}")

        if not texts:
            return []

        embed_log = log.bind(adapter="FastEmbedAdapter", action="embed_texts", num_texts=len(texts))
        embed_log.debug("Generating embeddings with FastEmbed...")
        try:
            # FastEmbed's embed method is CPU-bound; run in thread pool.
            embeddings_generator = await asyncio.to_thread(self._model.embed, texts, batch_size=128)
            embeddings_list = [emb.tolist() for emb in embeddings_generator]

            embed_log.debug("FastEmbed embeddings generated successfully.")
            return embeddings_list
        except Exception as e:
            embed_log.exception("Error during FastEmbed embedding process")
            raise RuntimeError(f"FastEmbed embedding generation failed: {e}") from e

    def get_model_info(self) -> Dict[str, Any]:
        # Returns the dimension this adapter *expects* based on global config,
        # and the model name it's configured to use.
        # Actual dimension is validated during init.
        return {
            "model_name": self._model_name,
            "dimension": self._model_dimension, 
        }

    async def health_check(self) -> Tuple[bool, str]:
        if self._model_loaded and self._model:
            try:
                # Test with a short text
                test_embeddings_generator = await asyncio.to_thread(self._model.embed, ["health check"], batch_size=1)
                _ = list(test_embeddings_generator) 
                return True, f"FastEmbed model '{self._model_name}' loaded and responsive."
            except Exception as e:
                log.error("FastEmbed model health check failed during test embedding", error=str(e), exc_info=True)
                return False, f"FastEmbed model '{self._model_name}' loaded but unresponsive: {str(e)}"
        elif self._model_load_error:
            return False, f"FastEmbed model '{self._model_name}' failed to load: {self._model_load_error}"
        else:
            return False, f"FastEmbed model '{self._model_name}' not loaded."
```

## File: `app\infrastructure\embedding_models\instructor_adapter.py`
```py
# embedding-service/app/infrastructure/embedding_models/instructor_adapter.py
import structlog
import asyncio
import time
import os
from typing import List, Tuple, Dict, Any, Optional
import numpy as np

# LLM_FLAG: INSTRUCTOR_IMPORTS_START
# Defer import until initialization or if INSTRUCTOR is active provider
_InstructorEmbedding_imported = False
_torch_imported_for_instructor = False
try:
    if os.getenv("EMBEDDING_ACTIVE_EMBEDDING_PROVIDER", "").lower() == "instructor":
        from InstructorEmbedding import INSTRUCTOR as InstructorModelClass # Renamed for clarity
        _InstructorEmbedding_imported = True
        import torch
        _torch_imported_for_instructor = True
except ImportError as e:
    # This warning will also be logged by config loader if torch itself fails globally
    structlog.get_logger(__name__).warning(
        "InstructorEmbedding or torch not installed. InstructorAdapter will fail if used.",
        error=str(e)
    )
# LLM_FLAG: INSTRUCTOR_IMPORTS_END


from app.application.ports.embedding_model_port import EmbeddingModelPort
from app.core.config import settings

log = structlog.get_logger(__name__)

class InstructorAdapter(EmbeddingModelPort):
    _model: Optional[Any] = None # InstructorModelClass
    _model_name: str
    _model_dimension: int # This comes from global settings.EMBEDDING_DIMENSION
    _device: str
    _cache_dir: Optional[str]
    _batch_size: int
    _normalize_embeddings: bool

    _model_loaded: bool = False
    _model_load_error: Optional[str] = None

    def __init__(self):
        global _InstructorEmbedding_imported, _torch_imported_for_instructor
        if not _InstructorEmbedding_imported:
            log.critical("InstructorEmbedding library not found. Please install it to use InstructorAdapter: 'poetry install --with st'")
            # This adapter might be instantiated even if not active, so don't raise error here,
            # but initialization will fail if it's the active provider.
        if not _torch_imported_for_instructor:
            log.critical("PyTorch library not found. Please install it to use InstructorAdapter.")


        self._model_name = settings.INSTRUCTOR_MODEL_NAME
        self._model_dimension = settings.EMBEDDING_DIMENSION # Validated in config.py
        self._device = settings.INSTRUCTOR_MODEL_DEVICE # Validated in config.py for CUDA availability
        self._cache_dir = settings.INSTRUCTOR_HF_CACHE_DIR
        self._batch_size = settings.INSTRUCTOR_BATCH_SIZE
        self._normalize_embeddings = settings.INSTRUCTOR_NORMALIZE_EMBEDDINGS
        
        log.info(
            "InstructorAdapter instance created (model not loaded yet)",
            configured_model_name=self._model_name,
            target_dimension=self._model_dimension,
            initial_configured_device=self._device,
            batch_size=self._batch_size,
            normalize=self._normalize_embeddings,
            cache_dir=self._cache_dir
        )

    async def initialize_model(self):
        if self._model_loaded:
            log.debug("INSTRUCTOR model already initialized.", model_name=self._model_name)
            return

        if not _InstructorEmbedding_imported or not _torch_imported_for_instructor:
            self._model_load_error = "InstructorEmbedding library or PyTorch not available."
            log.critical(self._model_load_error)
            raise ConnectionError(self._model_load_error)

        init_log = log.bind(adapter="InstructorAdapter", action="initialize_model", model_name=self._model_name, device=self._device)
        init_log.info("Initializing INSTRUCTOR model...")
        
        start_time = time.perf_counter()
        
        try:
            if self._cache_dir:
                os.makedirs(self._cache_dir, exist_ok=True)
                init_log.info(f"Using HuggingFace cache directory for INSTRUCTOR: {self._cache_dir}")

            # INSTRUCTOR model loading is synchronous, run in thread pool
            self._model = await asyncio.to_thread(
                InstructorModelClass, # Use the imported and renamed class
                model_name_or_path=self._model_name,
                device=self._device,
                cache_folder=self._cache_dir
            )

            # Test embedding to confirm dimension (must match settings.EMBEDDING_DIMENSION)
            # Example instruction pair based on INSTRUCTOR usage
            test_instruction_pair = [["Represent the test query for model validation:", "hello world"]]
            test_vector_array = await asyncio.to_thread(
                self._model.encode,
                test_instruction_pair,
                batch_size=1,
                normalize_embeddings=self._normalize_embeddings,
                # convert_to_numpy=True # Default is numpy array
            )
            
            if not isinstance(test_vector_array, np.ndarray) or test_vector_array.ndim != 2:
                 raise ValueError(f"Test embedding with INSTRUCTOR failed or returned unexpected type: {type(test_vector_array)}")

            actual_dim = test_vector_array.shape[1]

            if actual_dim != self._model_dimension:
                self._model_load_error = (
                    f"INSTRUCTOR Model dimension mismatch. Global EMBEDDING_DIMENSION is {self._model_dimension}, "
                    f"but model '{self._model_name}' on device '{self._device}' produced {actual_dim} dimensions."
                )
                init_log.error(self._model_load_error)
                self._model = None
                self._model_loaded = False
                raise ValueError(self._model_load_error)

            self._model_loaded = True
            self._model_load_error = None
            duration_ms = (time.perf_counter() - start_time) * 1000
            init_log.info("INSTRUCTOR model initialized and validated successfully.", duration_ms=duration_ms, actual_dimension=actual_dim)

        except Exception as e:
            self._model_load_error = f"Failed to load INSTRUCTOR model '{self._model_name}': {str(e)}"
            init_log.critical(self._model_load_error, exc_info=True)
            self._model = None
            self._model_loaded = False
            raise ConnectionError(self._model_load_error) from e

    async def embed_texts(self, texts: List[str]) -> List[List[float]]:
        # text_type and task_instruction are not part of the port's method signature.
        # This adapter will use a default instruction strategy.
        # If customization is needed per request, the port and use_case would need changes.
        if not self._model_loaded or not self._model:
            log.error("INSTRUCTOR model not loaded. Cannot generate embeddings.", model_error=self._model_load_error)
            raise ConnectionError(f"INSTRUCTOR model is not available. Load error: {self._model_load_error}")

        if not texts:
            return []

        # Default instruction strategy: Use "Represent the passage:" for all texts.
        # This is a common generic instruction. For specific tasks (query, document),
        # this could be made configurable or more intelligent if requirements evolve.
        default_instruction_prefix = "Represent the general text for embedding: "
        
        # Create instruction-text pairs
        # Format: [[instruction, text], [instruction, text], ...]
        instruction_text_pairs = [[default_instruction_prefix, text] for text in texts]

        embed_log = log.bind(adapter="InstructorAdapter", action="embed_texts", num_texts=len(texts), num_pairs=len(instruction_text_pairs))
        embed_log.debug("Generating embeddings with INSTRUCTOR...")

        try:
            # .encode is CPU/GPU-bound; run in thread pool
            embeddings_array: np.ndarray = await asyncio.to_thread(
                self._model.encode,
                instruction_text_pairs,
                batch_size=self._batch_size,
                normalize_embeddings=self._normalize_embeddings,
                show_progress_bar=False, # Typically false for server-side
                # convert_to_numpy=True # is default
            )
            
            embeddings_list = embeddings_array.tolist()
            embed_log.debug("INSTRUCTOR embeddings generated successfully.")
            return embeddings_list
        except Exception as e:
            embed_log.exception("Error during INSTRUCTOR embedding process")
            raise RuntimeError(f"INSTRUCTOR embedding generation failed: {e}") from e

    def get_model_info(self) -> Dict[str, Any]:
        return {
            "model_name": self._model_name,
            "dimension": self._model_dimension,
            "device": self._device, # Effective device after validation
            "provider": "instructor"
        }

    async def health_check(self) -> Tuple[bool, str]:
        if self._model_loaded and self._model:
            try:
                # Perform a quick test encode
                test_pair = [["Represent the health check query:", "ping"]]
                _ = await asyncio.to_thread(
                    self._model.encode,
                    test_pair,
                    batch_size=1,
                    normalize_embeddings=self._normalize_embeddings
                )
                return True, f"INSTRUCTOR model '{self._model_name}' on '{self._device}' loaded and responsive."
            except Exception as e:
                log.error("INSTRUCTOR model health check failed during test embedding", error=str(e), exc_info=True)
                return False, f"INSTRUCTOR model '{self._model_name}' loaded but unresponsive: {str(e)}"
        elif self._model_load_error:
            return False, f"INSTRUCTOR model '{self._model_name}' failed to load: {self._model_load_error}"
        else:
            return False, f"INSTRUCTOR model '{self._model_name}' not loaded (or library missing)."
```

## File: `app\infrastructure\embedding_models\openai_adapter.py`
```py
# embedding-service/app/infrastructure/embedding_models/openai_adapter.py
import structlog
from typing import List, Tuple, Dict, Any, Optional
import asyncio
import time
from openai import AsyncOpenAI, APIConnectionError, RateLimitError, AuthenticationError, OpenAIError
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from app.application.ports.embedding_model_port import EmbeddingModelPort
from app.core.config import settings

log = structlog.get_logger(__name__)

class OpenAIAdapter(EmbeddingModelPort):
    """
    Adapter for OpenAI's Embedding API.
    """
    _client: Optional[AsyncOpenAI] = None
    _model_name: str
    _embedding_dimension: int
    _dimensions_override: Optional[int]
    _model_initialized: bool = False
    _initialization_error: Optional[str] = None

    def __init__(self):
        self._model_name = settings.OPENAI_EMBEDDING_MODEL_NAME
        self._embedding_dimension = settings.EMBEDDING_DIMENSION
        self._dimensions_override = settings.OPENAI_EMBEDDING_DIMENSIONS_OVERRIDE
        # Client initialization is deferred to an async method.
        log.info("OpenAIAdapter initialized", model_name=self._model_name, target_dimension=self._embedding_dimension)

    async def initialize_model(self):
        """
        Initializes the OpenAI client.
        This should be called during service startup (e.g., lifespan).
        """
        if self._model_initialized:
            log.debug("OpenAI client already initialized.", model_name=self._model_name)
            return

        init_log = log.bind(adapter="OpenAIAdapter", action="initialize_model", model_name=self._model_name)
        init_log.info("Initializing OpenAI client...")
        start_time = time.perf_counter()

        if not settings.OPENAI_API_KEY.get_secret_value():
            self._initialization_error = "OpenAI API Key is not configured."
            init_log.critical(self._initialization_error)
            self._model_initialized = False
            raise ConnectionError(self._initialization_error)

        try:
            self._client = AsyncOpenAI(
                api_key=settings.OPENAI_API_KEY.get_secret_value(),
                base_url=settings.OPENAI_API_BASE,
                timeout=settings.OPENAI_TIMEOUT_SECONDS,
                max_retries=0 # We use tenacity for retries in embed_texts
            )

            # Optional: Perform a lightweight test call to verify API key and connectivity
            # For example, listing models (can be slow, consider if truly needed for health)
            # await self._client.models.list(limit=1)

            self._model_initialized = True
            self._initialization_error = None
            duration_ms = (time.perf_counter() - start_time) * 1000
            init_log.info("OpenAI client initialized successfully.", duration_ms=duration_ms)

        except AuthenticationError as e:
            self._initialization_error = f"OpenAI API Authentication Failed: {e}. Check your API key."
            init_log.critical(self._initialization_error, exc_info=False) # exc_info=False for auth errors usually
            self._client = None
            self._model_initialized = False
            raise ConnectionError(self._initialization_error) from e
        except APIConnectionError as e:
            self._initialization_error = f"OpenAI API Connection Error: {e}. Check network or OpenAI status."
            init_log.critical(self._initialization_error, exc_info=True)
            self._client = None
            self._model_initialized = False
            raise ConnectionError(self._initialization_error) from e
        except Exception as e:
            self._initialization_error = f"Failed to initialize OpenAI client for model '{self._model_name}': {str(e)}"
            init_log.critical(self._initialization_error, exc_info=True)
            self._client = None
            self._model_initialized = False
            raise ConnectionError(self._initialization_error) from e

    @retry(
        stop=stop_after_attempt(settings.OPENAI_MAX_RETRIES + 1), # settings.OPENAI_MAX_RETRIES are retries, so +1 for initial attempt
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type((APIConnectionError, RateLimitError, OpenAIError)), # Add other retryable OpenAI errors if needed
        before_sleep=lambda retry_state: log.warning(
            "Retrying OpenAI embedding call",
            model_name=settings.OPENAI_EMBEDDING_MODEL_NAME,
            attempt_number=retry_state.attempt_number,
            wait_time=retry_state.next_action.sleep,
            error=str(retry_state.outcome.exception()) if retry_state.outcome else "Unknown error"
        )
    )
    async def embed_texts(self, texts: List[str]) -> List[List[float]]:
        if not self._model_initialized or not self._client:
            log.error("OpenAI client not initialized. Cannot generate embeddings.", init_error=self._initialization_error)
            raise ConnectionError("OpenAI embedding model is not available.")

        if not texts:
            return []

        embed_log = log.bind(adapter="OpenAIAdapter", action="embed_texts", num_texts=len(texts), model=self._model_name)
        embed_log.debug("Generating embeddings via OpenAI API...")

        try:
            api_params = {
                "model": self._model_name,
                "input": texts,
                "encoding_format": "float"
            }
            if self._dimensions_override is not None:
                api_params["dimensions"] = self._dimensions_override

            response = await self._client.embeddings.create(**api_params) # type: ignore

            if not response.data or not all(item.embedding for item in response.data):
                embed_log.error("OpenAI API returned no embedding data or empty embeddings.", api_response=response.model_dump_json(indent=2))
                raise ValueError("OpenAI API returned no valid embedding data.")

            # Verify dimensions of the first embedding as a sanity check
            if response.data and response.data[0].embedding:
                actual_dim = len(response.data[0].embedding)
                if actual_dim != self._embedding_dimension:
                    embed_log.warning(
                        "Dimension mismatch in OpenAI response.",
                        expected_dim=self._embedding_dimension,
                        actual_dim=actual_dim,
                        model_used=response.model
                    )
                    # This indicates a potential configuration issue or unexpected API change.
                    # Depending on strictness, could raise an error or just log.
                    # For now, we'll trust the configured EMBEDDING_DIMENSION.

            embeddings_list = [item.embedding for item in response.data]
            embed_log.debug("Embeddings generated successfully via OpenAI.", num_embeddings=len(embeddings_list), usage_tokens=response.usage.total_tokens if response.usage else "N/A")
            return embeddings_list
        except AuthenticationError as e:
            embed_log.error("OpenAI API Authentication Error during embedding", error=str(e))
            raise ConnectionError(f"OpenAI authentication failed: {e}") from e # Propagate as ConnectionError to be caught by endpoint
        except RateLimitError as e:
            embed_log.error("OpenAI API Rate Limit Exceeded during embedding", error=str(e))
            raise OpenAIError(f"OpenAI rate limit exceeded: {e}") from e # Let tenacity handle retry
        except APIConnectionError as e:
            embed_log.error("OpenAI API Connection Error during embedding", error=str(e))
            raise OpenAIError(f"OpenAI connection error: {e}") from e # Let tenacity handle retry
        except OpenAIError as e: # Catch other OpenAI specific errors
            embed_log.error(f"OpenAI API Error during embedding: {type(e).__name__}", error=str(e))
            raise RuntimeError(f"OpenAI API error: {e}") from e
        except Exception as e:
            embed_log.exception("Unexpected error during OpenAI embedding process")
            raise RuntimeError(f"Embedding generation failed with unexpected error: {e}") from e

    def get_model_info(self) -> Dict[str, Any]:
        return {
            "model_name": self._model_name,
            "dimension": self._embedding_dimension, # This is the validated, final dimension
        }

    async def health_check(self) -> Tuple[bool, str]:
        if self._model_initialized and self._client:
            # A more robust health check could involve a lightweight API call,
            # but be mindful of cost and rate limits for frequent health checks.
            # For now, if client is initialized, we assume basic health.
            # A true test is done during initialize_model or first embedding call.
            return True, f"OpenAI client initialized for model {self._model_name}."
        elif self._initialization_error:
            return False, f"OpenAI client initialization failed: {self._initialization_error}"
        else:
            return False, "OpenAI client not initialized."
```

## File: `app\infrastructure\embedding_models\sentence_transformer_adapter.py`
```py
# embedding-service/app/infrastructure/embedding_models/sentence_transformer_adapter.py
import structlog
from typing import List, Tuple, Dict, Any, Optional
import asyncio
import time
import os
import numpy as np



from app.application.ports.embedding_model_port import EmbeddingModelPort
from app.core.config import settings 
from app.api.v1.schemas import TextType 

log = structlog.get_logger(__name__)

class SentenceTransformerAdapter(EmbeddingModelPort):
    _model: Optional[Any] = None  # type: ignore  # SentenceTransformer is imported locally
    _model_name: str
    _model_dimension: int
    _device: str
    _cache_dir: Optional[str]
    _batch_size: int
    _normalize_embeddings: bool
    _use_fp16: bool 

    _model_loaded: bool = False
    _model_load_error: Optional[str] = None

    def __init__(self):
        # Import sentence_transformers and torch only when this adapter is instantiated
        global SentenceTransformer, torch
        try:
            from sentence_transformers import SentenceTransformer
            import torch
        except ImportError as e:
            log.critical("sentence_transformers or torch not installed. This adapter requires them.", error=str(e))
            raise
        self._model_name = settings.ST_MODEL_NAME
        self._model_dimension = settings.EMBEDDING_DIMENSION 
        self._device = settings.ST_MODEL_DEVICE 
        self._cache_dir = settings.ST_HF_CACHE_DIR
        self._batch_size = settings.ST_BATCH_SIZE
        self._normalize_embeddings = settings.ST_NORMALIZE_EMBEDDINGS
        
        log.info(
            "SentenceTransformerAdapter instance created (model not loaded yet)",
            configured_model_name=self._model_name,
            target_dimension=self._model_dimension,
            initial_configured_device=self._device, 
            batch_size=self._batch_size,
            normalize=self._normalize_embeddings,
        )

    async def initialize_model(self):
        if self._model_loaded:
            log.debug("SentenceTransformer model already initialized.", model_name=self._model_name)
            return

        init_log = log.bind(adapter="SentenceTransformerAdapter", action="initialize_model", model_name=self._model_name)
        
        
        final_device = self._device
        self._use_fp16 = False 
        if self._device.startswith("cuda"):
            if torch.cuda.is_available():
                init_log.info(f"CUDA is available. Checking capability for device: {self._device}")
                
                try:
                    current_cuda_device_for_check = torch.device(final_device)
                    major, _ = torch.cuda.get_device_capability(current_cuda_device_for_check)
                    init_log.info(f"CUDA device '{final_device}' capability: {major}.x")
                    if major >= 7: 
                        self._use_fp16 = settings.ST_USE_FP16
                        init_log.info(f"FP16 support confirmed. ST_USE_FP16: {self._use_fp16}.")
                    else:
                        init_log.warning(f"CUDA device '{final_device}' may have limited FP16 support. ST_USE_FP16 set to False.")
                        self._use_fp16 = False
                except Exception as e_cap:
                    init_log.error("Error getting CUDA device capability. Defaulting to CPU and no FP16.", error=str(e_cap), configured_device=self._device, exc_info=True)
                    final_device = "cpu"
                    self._use_fp16 = False
            else: 
                init_log.warning(f"ST_MODEL_DEVICE configured as '{self._device}' but CUDA is not available. Falling back to CPU.")
                final_device = "cpu"
                self._use_fp16 = False
        
        
        self._device = final_device
        init_log = init_log.bind(effective_device=self._device, use_fp16=self._use_fp16) 
        init_log.info("Initializing SentenceTransformer model with effective device settings...")
        
        start_time = time.perf_counter()
        
        try:
            if self._cache_dir:
                os.makedirs(self._cache_dir, exist_ok=True)
                init_log.info(f"Using HuggingFace cache directory: {self._cache_dir}")

            self._model = await asyncio.to_thread(
                SentenceTransformer,
                model_name_or_path=self._model_name,
                device=self._device, 
                cache_folder=self._cache_dir
            )

            if self._use_fp16 and self._device.startswith("cuda"): 
                init_log.info("Converting model to FP16 for CUDA device.")
                self._model = self._model.half() 

            test_vector = self._model.encode(["test vector"], normalize_embeddings=self._normalize_embeddings)
            actual_dim = test_vector.shape[1]

            if actual_dim != self._model_dimension:
                self._model_load_error = (
                    f"SentenceTransformer Model dimension mismatch. Global EMBEDDING_DIMENSION is {self._model_dimension}, "
                    f"but model '{self._model_name}' produced {actual_dim} dimensions."
                )
                init_log.error(self._model_load_error)
                self._model = None
                self._model_loaded = False
                raise ValueError(self._model_load_error)

            self._model_loaded = True
            self._model_load_error = None
            duration_ms = (time.perf_counter() - start_time) * 1000
            init_log.info("SentenceTransformer model initialized and validated successfully.", duration_ms=duration_ms, actual_dimension=actual_dim)

        except Exception as e:
            self._model_load_error = f"Failed to load SentenceTransformer model '{self._model_name}': {str(e)}"
            init_log.critical(self._model_load_error, exc_info=True)
            self._model = None
            self._model_loaded = False
            raise ConnectionError(self._model_load_error) from e

    async def embed_texts(self, texts: List[str], text_type: TextType = "passage") -> List[List[float]]:
        if not self._model_loaded or not self._model:
            log.error("SentenceTransformer model not loaded. Cannot generate embeddings.", model_error=self._model_load_error)
            raise ConnectionError(f"SentenceTransformer model is not available. Load error: {self._model_load_error}")

        if not texts:
            return []

        embed_log = log.bind(adapter="SentenceTransformerAdapter", action="embed_texts", num_texts=len(texts), text_type=text_type)
        
        if text_type == "query":
            prefix = "query: "
        elif text_type == "passage":
            prefix = "passage: "
        else: 
            log.warning("Unknown text_type received, defaulting to 'passage: ' prefix.", received_type=text_type)
            prefix = "passage: "
            
        prefixed_texts = [f"{prefix}{text}" for text in texts]
        embed_log.debug(f"Generating embeddings with SentenceTransformer using prefix '{prefix}'.")


        try:
            embeddings_array: np.ndarray = await asyncio.to_thread(
                self._model.encode, 
                sentences=prefixed_texts,
                batch_size=self._batch_size,
                convert_to_tensor=False, 
                convert_to_numpy=True,
                show_progress_bar=False, 
                normalize_embeddings=self._normalize_embeddings,
            )
            
            embeddings_list = embeddings_array.tolist()
            embed_log.debug("SentenceTransformer embeddings generated successfully.")
            return embeddings_list
        except Exception as e:
            embed_log.exception("Error during SentenceTransformer embedding process")
            raise RuntimeError(f"SentenceTransformer embedding generation failed: {e}") from e

    def get_model_info(self) -> Dict[str, Any]:
        return {
            "model_name": self._model_name,
            "dimension": self._model_dimension, 
            "device": self._device, 
            "provider": "sentence_transformer"
        }

    async def health_check(self) -> Tuple[bool, str]:
        if self._model_loaded and self._model:
            try:
                _ = await asyncio.to_thread(
                    self._model.encode, 
                    ["health check"], 
                    batch_size=1, 
                    normalize_embeddings=self._normalize_embeddings
                )
                return True, f"SentenceTransformer model '{self._model_name}' on '{self._device}' loaded and responsive."
            except Exception as e:
                log.error("SentenceTransformer model health check failed during test embedding", error=str(e), exc_info=True)
                return False, f"SentenceTransformer model '{self._model_name}' loaded but unresponsive: {str(e)}"
        elif self._model_load_error:
            return False, f"SentenceTransformer model '{self._model_name}' failed to load: {self._model_load_error}"
        else:
            return False, f"SentenceTransformer model '{self._model_name}' not loaded."
```

## File: `app\main.py`
```py
# embedding-service/app/main.py
import asyncio
import uuid
from contextlib import asynccontextmanager

import structlog
import uvicorn
from app.api.v1 import schemas
from fastapi import FastAPI, HTTPException, Request, status as fastapi_status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, PlainTextResponse

# Configurar logging primero
from app.core.logging_config import setup_logging
setup_logging() # Initialize logging early

# Import other components after logging is set up
from app.core.config import settings # This now loads the validated settings
from app.api.v1.endpoints import embedding_endpoint
from app.application.ports.embedding_model_port import EmbeddingModelPort
from app.application.use_cases.embed_texts_use_case import EmbedTextsUseCase
from app.infrastructure.embedding_models.openai_adapter import OpenAIAdapter
from app.infrastructure.embedding_models.sentence_transformer_adapter import SentenceTransformerAdapter
from app.infrastructure.embedding_models.instructor_adapter import InstructorAdapter # IMPORT_NEW_ADAPTER
from app.dependencies import set_embedding_service_dependencies

log = structlog.get_logger("embedding_service.main")

# Global instances for dependencies
embedding_model_adapter: EmbeddingModelPort | None = None
embed_texts_use_case: EmbedTextsUseCase | None = None
SERVICE_MODEL_READY = False


@asynccontextmanager
async def lifespan(app: FastAPI):
    global embedding_model_adapter, embed_texts_use_case, SERVICE_MODEL_READY
    log.info(f"Starting up {settings.PROJECT_NAME}...")
    log.info(f"Active embedding provider: {settings.ACTIVE_EMBEDDING_PROVIDER}")

    model_adapter_instance: EmbeddingModelPort | None = None

    if settings.ACTIVE_EMBEDDING_PROVIDER == "openai":
        if not settings.OPENAI_API_KEY or not settings.OPENAI_API_KEY.get_secret_value():
            log.critical("OpenAI_API_KEY is not set. Cannot initialize OpenAIAdapter.")
            SERVICE_MODEL_READY = False
        else:
            model_adapter_instance = OpenAIAdapter()
    elif settings.ACTIVE_EMBEDDING_PROVIDER == "sentence_transformer":
        model_adapter_instance = SentenceTransformerAdapter()
    elif settings.ACTIVE_EMBEDDING_PROVIDER == "instructor": # ADD_NEW_PROVIDER_CONDITION
        model_adapter_instance = InstructorAdapter()
    else:
        log.critical(f"Unsupported ACTIVE_EMBEDDING_PROVIDER: {settings.ACTIVE_EMBEDDING_PROVIDER}")
        SERVICE_MODEL_READY = False

    if model_adapter_instance:
        try:
            await model_adapter_instance.initialize_model()
            embedding_model_adapter = model_adapter_instance # Assign if successful
            SERVICE_MODEL_READY = True
            log.info(f"Embedding model client initialized successfully using {type(model_adapter_instance).__name__}.")
        except Exception as e:
            SERVICE_MODEL_READY = False
            log.critical(
                f"CRITICAL: Failed to initialize {type(model_adapter_instance).__name__} "
                "embedding model client during startup.",
                error=str(e), exc_info=True
            )
    else: 
        SERVICE_MODEL_READY = False
        if settings.ACTIVE_EMBEDDING_PROVIDER != "openai" or (settings.OPENAI_API_KEY and settings.OPENAI_API_KEY.get_secret_value()):
             # Log this only if it's not the known case of missing OpenAI key
            log.error("No valid embedding model adapter could be instantiated based on configuration.")


    if embedding_model_adapter and SERVICE_MODEL_READY:
        use_case_instance = EmbedTextsUseCase(embedding_model=embedding_model_adapter)
        embed_texts_use_case = use_case_instance 
        set_embedding_service_dependencies(use_case_instance=use_case_instance, ready_flag=True)
        log.info(f"EmbedTextsUseCase instantiated and dependencies set with {type(embedding_model_adapter).__name__}.")
    else:
        set_embedding_service_dependencies(use_case_instance=None, ready_flag=False)
        log.error("Service not fully ready due to embedding model client initialization issues.")

    log.info(f"{settings.PROJECT_NAME} startup sequence finished. Model Client Ready: {SERVICE_MODEL_READY}")
    yield
    log.info(f"Shutting down {settings.PROJECT_NAME}...")
    if isinstance(embedding_model_adapter, OpenAIAdapter) and embedding_model_adapter._client:
        try:
            await embedding_model_adapter._client.close()
            log.info("OpenAI async client closed successfully.")
        except Exception as e:
            log.error("Error closing OpenAI async client.", error=str(e), exc_info=True)
    # Cleanup for other adapters (e.g., SentenceTransformer, INSTRUCTOR) if needed
    # (e.g. releasing GPU memory explicitly, though Python GC usually handles ST/Instructor models)
    log.info("Shutdown complete.")


app = FastAPI(
    title=settings.PROJECT_NAME,
    version="1.2.0", 
    description=f"Atenex Embedding Service. Active provider: {settings.ACTIVE_EMBEDDING_PROVIDER}",
    openapi_url=f"{settings.API_V1_STR}/openapi.json",
    lifespan=lifespan
)

# --- Middleware for Request ID and Logging ---
@app.middleware("http")
async def request_context_middleware(request: Request, call_next):
    start_time = asyncio.get_event_loop().time()
    request_id = request.headers.get("x-request-id", str(uuid.uuid4()))

    structlog.contextvars.bind_contextvars(
        request_id=request_id,
        method=request.method,
        path=str(request.url.path),
        client_host=request.client.host if request.client else "unknown",
    )
    log.info("Request received")

    response = None
    try:
        response = await call_next(request)
        process_time_ms = (asyncio.get_event_loop().time() - start_time) * 1000
        structlog.contextvars.bind_contextvars(status_code=response.status_code, duration_ms=round(process_time_ms, 2))
        log_level = "warning" if 400 <= response.status_code < 500 else "error" if response.status_code >= 500 else "info"
        getattr(log, log_level)("Request finished") 
        response.headers["X-Request-ID"] = request_id 
        response.headers["X-Process-Time-Ms"] = f"{process_time_ms:.2f}"
    except Exception as e:
        process_time_ms = (asyncio.get_event_loop().time() - start_time) * 1000
        structlog.contextvars.bind_contextvars(status_code=500, duration_ms=round(process_time_ms, 2))
        log.exception("Unhandled exception during request processing") 
        response = JSONResponse(
            status_code=fastapi_status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"detail": "Internal Server Error", "request_id": request_id}
        )
        response.headers["X-Request-ID"] = request_id
        response.headers["X-Process-Time-Ms"] = f"{process_time_ms:.2f}"
    finally:
        structlog.contextvars.clear_contextvars()
    return response

# --- Exception Handlers ---
@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    request_id_ctx = structlog.contextvars.get_contextvars().get("request_id")
    log.error("HTTP Exception caught", status_code=exc.status_code, detail=exc.detail, request_id_from_ctx=request_id_ctx)
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail, "request_id": request_id_ctx},
        headers=getattr(exc, "headers", None)
    )

@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    request_id_ctx = structlog.contextvars.get_contextvars().get("request_id")
    log.warning("Request Validation Error", errors=exc.errors(), request_id_from_ctx=request_id_ctx)
    return JSONResponse(
        status_code=fastapi_status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={
            "detail": "Validation Error",
            "errors": exc.errors(),
            "request_id": request_id_ctx
        },
    )

@app.exception_handler(Exception)
async def generic_exception_handler(request: Request, exc: Exception):
    request_id_ctx = structlog.contextvars.get_contextvars().get("request_id")
    log.exception("Generic Unhandled Exception caught", request_id_from_ctx=request_id_ctx)
    return JSONResponse(
        status_code=fastapi_status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"detail": "An unexpected internal server error occurred.", "request_id": request_id_ctx}
    )


# --- API Router ---
app.include_router(embedding_endpoint.router, prefix=settings.API_V1_STR, tags=["Embeddings"])
log.info(f"Embedding API router included with prefix: {settings.API_V1_STR}")


# --- Health Check Endpoint ---
@app.get(
    "/health",
    response_model=schemas.HealthCheckResponse,
    tags=["Health Check"],
    summary="Service Health and Model Status"
)
async def health_check():
    global SERVICE_MODEL_READY, embedding_model_adapter
    health_log = log.bind(check="health_status")

    model_status_str = "client_not_initialized"
    model_name_str: Optional[str] = None
    model_dim_int: Optional[int] = None
    service_overall_status = "error" 

    if embedding_model_adapter: 
        is_healthy, status_msg = await embedding_model_adapter.health_check()
        model_info = embedding_model_adapter.get_model_info() 
        model_name_str = model_info.get("model_name")
        model_dim_int = model_info.get("dimension")

        if is_healthy and SERVICE_MODEL_READY:
            model_status_str = "client_ready"
            service_overall_status = "ok"
        elif is_healthy and not SERVICE_MODEL_READY:
             model_status_str = "client_initialization_pending_or_failed" 
             health_log.error("Health check: Adapter client healthy but service global flag indicates not ready.")
        else: 
            model_status_str = "client_error" 
            health_log.error("Health check: Embedding model client error.", model_status_message=status_msg, model_name=model_name_str)
    else: 
        health_log.warning("Health check: Embedding model adapter not initialized (no valid provider configured or initial error during startup).")
        SERVICE_MODEL_READY = False 
        
        # Try to get default model name/dim from config if adapter is None for better health info
        if settings.ACTIVE_EMBEDDING_PROVIDER == "openai":
            model_name_str = settings.OPENAI_EMBEDDING_MODEL_NAME
        elif settings.ACTIVE_EMBEDDING_PROVIDER == "sentence_transformer":
            model_name_str = settings.ST_MODEL_NAME
        elif settings.ACTIVE_EMBEDDING_PROVIDER == "instructor": # HEALTH_CHECK_PROVIDER_INFO
            model_name_str = settings.INSTRUCTOR_MODEL_NAME
        model_dim_int = settings.EMBEDDING_DIMENSION # Global dimension


    response_payload = schemas.HealthCheckResponse(
        status=service_overall_status,
        service=settings.PROJECT_NAME,
        model_status=model_status_str,
        model_name=model_name_str, 
        model_dimension=model_dim_int 
    ).model_dump(exclude_none=True)

    http_status_code = fastapi_status.HTTP_200_OK
    if not SERVICE_MODEL_READY or service_overall_status == "error":
        health_log.error("Service health check indicates an issue.", **response_payload)
        http_status_code = fastapi_status.HTTP_503_SERVICE_UNAVAILABLE
    else:
        health_log.info("Health check successful.", **response_payload)
        
    return JSONResponse(
        status_code=http_status_code,
        content=response_payload
    )

# --- Root Endpoint (Simple Ack)  ---
@app.get("/", tags=["Root"], response_class=PlainTextResponse, include_in_schema=False)
async def root():
    return f"{settings.PROJECT_NAME} (Provider: {settings.ACTIVE_EMBEDDING_PROVIDER}) is running."


if __name__ == "__main__":
    port_to_run = settings.PORT
    log_level_main = settings.LOG_LEVEL.lower()
    reload_flag = True # Typically true for local dev
    workers_uvicorn = 1 # Uvicorn manages its own loop for dev; Gunicorn handles workers in prod

    # Check if running in an environment where reload might be problematic (e.g. some debuggers)
    # or if a specific env var is set to disable it for Windows debugging.
    if os.getenv("APP_ENV_PROD_LIKE_LOCAL_DEV") == "true" or sys.platform == "win32" and os.getenv("POETRY_ACTIVE"):
        # Could disable reload on Windows if it causes issues with debugger or CUDA context
        # reload_flag = False 
        pass


    print(f"----- Starting {settings.PROJECT_NAME} locally on port {port_to_run} (Provider: {settings.ACTIVE_EMBEDDING_PROVIDER}) -----")
    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=port_to_run,
        reload=reload_flag, 
        log_level=log_level_main,
        workers=workers_uvicorn
    )
```

## File: `app\utils\__init__.py`
```py
# Utilities for embedding-service

```

## File: `pyproject.toml`
```toml
[tool.poetry]
name = "embedding-service"
version = "1.2.0" # Version incrementada
description = "Atenex Embedding Service using FastAPI and choice of embedding models (OpenAI, Sentence Transformers, INSTRUCTOR)"
authors = ["Atenex Team <dev@atenex.com>"]
readme = "README.md"

[tool.poetry.dependencies]
python = ">=3.10,<3.13"
fastapi = "^0.110.0"
uvicorn = {extras = ["standard"], version = "^0.28.0"}
gunicorn = "^21.2.0" # For production deployments
pydantic = {extras = ["email"], version = "^2.6.4"}
pydantic-settings = "^2.2.1"
structlog = "^24.1.0"
tenacity = "^8.2.3"
numpy = "^1.26.0"

# --- Embedding Engine ---
openai = "^1.14.0"


[tool.poetry.group.st]
optional = true

[tool.poetry.group.st.dependencies]
sentence-transformers = "^2.7.0"
InstructorEmbedding = "^1.0.1"
# torch se instalará como dependencia de sentence-transformers y/o InstructorEmbedding

[tool.poetry.group.dev.dependencies]
pytest = "^7.4.4"
pytest-asyncio = "^0.21.1"
httpx = "^0.27.0"

[build-system]
requires = ["poetry-core>=1.0.0"]
build-backend = "poetry.core.masonry.api"
```
