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
├── infrastructure
│   ├── __init__.py
│   └── embedding_models
│       ├── __init__.py
│       ├── fastembed_adapter.py
│       └── openai_adapter.py
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
from typing import List, Dict, Any

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
                    "model_name": "text-embedding-3-small", # Updated example
                    "dimension": 1536 # Updated example
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
from app.core.config import settings
from app.api.v1.endpoints import embedding_endpoint
from app.application.ports.embedding_model_port import EmbeddingModelPort
from app.application.use_cases.embed_texts_use_case import EmbedTextsUseCase
from app.infrastructure.embedding_models.openai_adapter import OpenAIAdapter
# from app.infrastructure.embedding_models.fastembed_adapter import FastEmbedAdapter # Kept for reference
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

    # --- Use OpenAIAdapter as the primary embedding provider ---
    if not settings.OPENAI_API_KEY or not settings.OPENAI_API_KEY.get_secret_value():
        log.critical("OpenAI_API_KEY is not set. Cannot initialize OpenAIAdapter.")
        SERVICE_MODEL_READY = False
        embedding_model_adapter = None
    else:
        model_adapter_instance = OpenAIAdapter()
        try:
            await model_adapter_instance.initialize_model()
            embedding_model_adapter = model_adapter_instance # Assign if successful
            SERVICE_MODEL_READY = True
            log.info("Embedding model client initialized successfully using OpenAIAdapter.")
        except Exception as e:
            SERVICE_MODEL_READY = False
            log.critical("CRITICAL: Failed to initialize OpenAI embedding model client during startup.", error=str(e), exc_info=True)
            # embedding_model_adapter will be the failed instance or None.
            # The health check will reflect this.

    if embedding_model_adapter and SERVICE_MODEL_READY:
        use_case_instance = EmbedTextsUseCase(embedding_model=embedding_model_adapter)
        embed_texts_use_case = use_case_instance # Assign to global
        set_embedding_service_dependencies(use_case_instance=use_case_instance, ready_flag=True)
        log.info("EmbedTextsUseCase instantiated and dependencies set with OpenAIAdapter.")
    else:
        # Ensure dependencies are set to reflect not-ready state
        set_embedding_service_dependencies(use_case_instance=None, ready_flag=False)
        log.error("Service not fully ready due to embedding model client initialization issues.")

    log.info(f"{settings.PROJECT_NAME} startup sequence finished. Model Client Ready: {SERVICE_MODEL_READY}")
    yield
    log.info(f"Shutting down {settings.PROJECT_NAME}...")
    if embedding_model_adapter and isinstance(embedding_model_adapter, OpenAIAdapter) and embedding_model_adapter._client:
        try:
            await embedding_model_adapter._client.close()
            log.info("OpenAI async client closed successfully.")
        except Exception as e:
            log.error("Error closing OpenAI async client.", error=str(e), exc_info=True)
    log.info("Shutdown complete.")


app = FastAPI(
    title=settings.PROJECT_NAME,
    version="1.1.1", 
    description="Atenex Embedding Service for generating text embeddings using OpenAI.",
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
    log.error("HTTP Exception caught", status_code=exc.status_code, detail=exc.detail)
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail, "request_id": structlog.contextvars.get_contextvars().get("request_id")},
        headers=getattr(exc, "headers", None)
    )

@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    log.warning("Request Validation Error", errors=exc.errors())
    return JSONResponse(
        status_code=fastapi_status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={
            "detail": "Validation Error",
            "errors": exc.errors(),
            "request_id": structlog.contextvars.get_contextvars().get("request_id")
        },
    )

@app.exception_handler(Exception)
async def generic_exception_handler(request: Request, exc: Exception):
    log.exception("Generic Unhandled Exception caught")
    return JSONResponse(
        status_code=fastapi_status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"detail": "An unexpected internal server error occurred.", "request_id": structlog.contextvars.get_contextvars().get("request_id")}
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
    model_name_str = None
    model_dim_int = None
    service_overall_status = "error" # Default to error

    if embedding_model_adapter: 
        is_healthy, status_msg = await embedding_model_adapter.health_check()
        model_info = embedding_model_adapter.get_model_info() # Get info regardless of health
        model_name_str = model_info.get("model_name")
        model_dim_int = model_info.get("dimension")

        if is_healthy and SERVICE_MODEL_READY: # Check both adapter health and global service readiness
            model_status_str = "client_ready"
            service_overall_status = "ok"
        elif is_healthy and not SERVICE_MODEL_READY:
             model_status_str = "client_initialization_pending_or_failed" # Adapter might be ok, but global state isn't
             health_log.error("Health check: Adapter client healthy but service global flag indicates not ready.")
        else: # Adapter not healthy
            model_status_str = "client_error"
            health_log.error("Health check: Embedding model client error.", model_status_message=status_msg)
    else: 
        health_log.warning("Health check: Embedding model adapter not initialized.")
        SERVICE_MODEL_READY = False # Ensure flag is accurate

    response_payload = schemas.HealthCheckResponse(
        status=service_overall_status,
        service=settings.PROJECT_NAME,
        model_status=model_status_str,
        model_name=model_name_str or settings.OPENAI_EMBEDDING_MODEL_NAME, 
        model_dimension=model_dim_int or settings.EMBEDDING_DIMENSION 
    ).model_dump(exclude_none=True)

    if not SERVICE_MODEL_READY or service_overall_status == "error":
        health_log.error("Service not ready (model client initialization failed, pending, or error).", current_status=model_status_str)
        return JSONResponse(
            status_code=fastapi_status.HTTP_503_SERVICE_UNAVAILABLE,
            content=response_payload
        )

    health_log.info("Health check successful.", model_status=model_status_str)
    return response_payload

# --- Root Endpoint (Simple Ack)  ---
@app.get("/", tags=["Root"], response_class=PlainTextResponse, include_in_schema=False)
async def root():
    return f"{settings.PROJECT_NAME} is running."


if __name__ == "__main__":
    port_to_run = settings.PORT
    log_level_main = settings.LOG_LEVEL.lower()
    print(f"----- Starting {settings.PROJECT_NAME} locally on port {port_to_run} -----")
    uvicorn.run("app.main:app", host="0.0.0.0", port=port_to_run, reload=True, log_level=log_level_main)
```

## File: `app\utils\__init__.py`
```py
# Utilities for embedding-service

```

## File: `pyproject.toml`
```toml
[tool.poetry]
name = "embedding-service"
version = "1.1.1"
description = "Atenex Embedding Service using FastAPI and OpenAI"
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

# --- Embedding Engine ---
openai = "^1.14.0" # OpenAI Python client library


[tool.poetry.group.dev.dependencies]
pytest = "^7.4.4"
pytest-asyncio = "^0.21.1"
httpx = "^0.27.0" # For testing the API client-side

[build-system]
requires = ["poetry-core>=1.0.0"]
build-backend = "poetry.core.masonry.api"
```
