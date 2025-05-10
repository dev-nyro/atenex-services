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
│       └── embed_text_use_case.py
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
│   └── models
│       ├── __init__.py
│       └── fastembed_adapter.py
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
    # prefix: Optional[str] = Field(None, description="Prefix used for query embeddings, if any.")

class EmbedResponse(BaseModel):
    embeddings: List[List[float]] = Field(..., description="A list of embeddings, where each embedding is a list of floats.")
    model_info: ModelInfo = Field(..., description="Information about the model used for embedding.")

    class Config:
        json_schema_extra = {
            "example": {
                "embeddings": [
                    [0.1, 0.2, 0.3, -0.1, -0.2, -0.3],
                    [0.4, 0.5, 0.6, -0.4, -0.5, -0.6]
                ],
                "model_info": {
                    "model_name": "sentence-transformers/all-MiniLM-L6-v2",
                    "dimension": 384
                }
            }
        }

class HealthCheckResponse(BaseModel):
    status: str = Field(default="ok", description="Overall status of the service.")
    service: str = Field(..., description="Name of the service.")
    model_status: str = Field(..., description="Status of the embedding model ('loaded', 'error', 'not_loaded').")
    model_name: str | None = Field(None, description="Name of the loaded embedding model, if any.")
    model_dimension: int | None = Field(None, description="Dimension of the loaded embedding model, if any.")
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

## File: `app\application\use_cases\embed_text_use_case.py`
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

## File: `app\infrastructure\models\__init__.py`
```py
# Models subpackage for infrastructure

```

## File: `app\infrastructure\models\fastembed_adapter.py`
```py
# embedding-service/app/infrastructure/embedding_models/fastembed_adapter.py
import structlog
from typing import List, Tuple, Dict, Any, Optional
import asyncio
import time

from fastembed import TextEmbedding, DefaultEmbedding, EmbeddingModel # Qdrant/FastEmbed
# from sentence_transformers import SentenceTransformer # Alternative if not using FastEmbed directly

from app.application.ports.embedding_model_port import EmbeddingModelPort
from app.core.config import settings

log = structlog.get_logger(__name__)

class FastEmbedAdapter(EmbeddingModelPort):
    """
    Adapter for FastEmbed library.
    """
    _model: Optional[TextEmbedding] = None # FastEmbed's TextEmbedding instance
    _model_name: str
    _model_dimension: int
    _model_loaded: bool = False
    _model_load_error: Optional[str] = None

    def __init__(self):
        self._model_name = settings.FASTEMBED_MODEL_NAME
        self._model_dimension = settings.EMBEDDING_DIMENSION
        # Model loading is deferred to an async method, typically called during startup.

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
            # FastEmbed's TextEmbedding can take kwargs for specific models
            # For sentence-transformers models, it usually handles them well.
            # cache_dir can be specified to save models locally.
            # threads for tokenization, max_length for sequence truncation.
            self._model = await asyncio.to_thread(
                TextEmbedding,
                model_name=self._model_name,
                cache_dir=settings.FASTEMBED_CACHE_DIR,
                threads=settings.FASTEMBED_THREADS,
                max_length=settings.FASTEMBED_MAX_LENGTH,
                # Add other relevant parameters if needed, e.g., onnx_providers
            )
            # Perform a test embedding to confirm dimension and successful loading
            test_embeddings = list(self._model.embed(["test vector"]))
            if not test_embeddings or not test_embeddings[0].any():
                raise ValueError("Test embedding failed or returned empty result.")

            actual_dim = len(test_embeddings[0])
            if actual_dim != self._model_dimension:
                self._model_load_error = (
                    f"Model dimension mismatch. Expected {self._model_dimension}, "
                    f"got {actual_dim} for model {self._model_name}."
                )
                init_log.error(self._model_load_error)
                self._model = None # Ensure model is not used
                raise ValueError(self._model_load_error)

            self._model_loaded = True
            self._model_load_error = None
            duration_ms = (time.perf_counter() - start_time) * 1000
            init_log.info("FastEmbed model initialized and validated successfully.", duration_ms=duration_ms, dimension=actual_dim)

        except Exception as e:
            self._model_load_error = f"Failed to load FastEmbed model '{self._model_name}': {str(e)}"
            init_log.critical(self._model_load_error, exc_info=True)
            self._model = None
            self._model_loaded = False
            # Re-raise as a ConnectionError or specific custom error if startup should fail hard
            raise ConnectionError(self._model_load_error) from e


    async def embed_texts(self, texts: List[str]) -> List[List[float]]:
        if not self._model_loaded or not self._model:
            log.error("FastEmbed model not loaded. Cannot generate embeddings.", model_error=self._model_load_error)
            # Depending on policy, could raise an exception or return empty/error state
            raise ConnectionError("Embedding model is not available.")

        embed_log = log.bind(adapter="FastEmbedAdapter", action="embed_texts", num_texts=len(texts))
        embed_log.debug("Generating embeddings...")
        try:
            # FastEmbed's embed method is synchronous, so run in a thread
            # It returns a generator of numpy arrays.
            # Convert to list of lists of floats.
            # Ensure batch_size is appropriate if embedding many texts at once.
            embeddings_generator = await asyncio.to_thread(self._model.embed, texts, batch_size=128) # Example batch_size
            embeddings_list = [emb.tolist() for emb in embeddings_generator]

            embed_log.debug("Embeddings generated successfully.")
            return embeddings_list
        except Exception as e:
            embed_log.exception("Error during FastEmbed embedding process")
            raise RuntimeError(f"Embedding generation failed: {e}") from e

    def get_model_info(self) -> Dict[str, Any]:
        return {
            "model_name": self._model_name,
            "dimension": self._model_dimension,
            # "prefix": settings.FASTEMBED_QUERY_PREFIX # If applicable
        }

    async def health_check(self) -> Tuple[bool, str]:
        if self._model_loaded and self._model:
            # Quick check to see if model responds (optional, could be too much for simple health)
            try:
                _ = list(self._model.embed(["health check"], batch_size=1))
                return True, "Model loaded and responsive."
            except Exception as e:
                log.error("Model health check failed during test embedding", error=str(e))
                return False, f"Model loaded but unresponsive: {str(e)}"
        elif self._model_load_error:
            return False, f"Model failed to load: {self._model_load_error}"
        else:
            return False, "Model not loaded."
```

## File: `app\main.py`
```py
# embedding-service/app/main.py
import asyncio
import uuid
from contextlib import asynccontextmanager

import structlog
import uvicorn
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
from app.infrastructure.embedding_models.fastembed_adapter import FastEmbedAdapter
from app.dependencies import set_embedding_service_dependencies # Import the setter

log = structlog.get_logger("embedding_service.main")

# Global instances for dependencies
embedding_model_adapter: EmbeddingModelPort | None = None
embed_texts_use_case: EmbedTextsUseCase | None = None
SERVICE_MODEL_READY = False


@asynccontextmanager
async def lifespan(app: FastAPI):
    global embedding_model_adapter, embed_texts_use_case, SERVICE_MODEL_READY
    log.info(f"Starting up {settings.PROJECT_NAME}...")

    model_adapter_instance = FastEmbedAdapter()
    try:
        await model_adapter_instance.initialize_model()
        embedding_model_adapter = model_adapter_instance # Assign if successful
        SERVICE_MODEL_READY = True
        log.info("Embedding model initialized successfully via FastEmbedAdapter.")
    except Exception as e:
        SERVICE_MODEL_READY = False
        log.critical("CRITICAL: Failed to initialize embedding model during startup.", error=str(e), exc_info=True)
        # embedding_model_adapter will remain None or be the failed instance.
        # The health check will reflect this.

    if embedding_model_adapter and SERVICE_MODEL_READY:
        use_case_instance = EmbedTextsUseCase(embedding_model=embedding_model_adapter)
        embed_texts_use_case = use_case_instance # Assign to global
        set_embedding_service_dependencies(use_case_instance=use_case_instance, ready_flag=True)
        log.info("EmbedTextsUseCase instantiated and dependencies set.")
    else:
        # Ensure dependencies are set to reflect not-ready state
        set_embedding_service_dependencies(use_case_instance=None, ready_flag=False)
        log.error("Service not fully ready due to embedding model initialization issues.")

    log.info(f"{settings.PROJECT_NAME} startup sequence finished. Model Ready: {SERVICE_MODEL_READY}")
    yield
    log.info(f"Shutting down {settings.PROJECT_NAME}...")
    # Cleanup if necessary (e.g., close connections, though FastEmbed might not need explicit cleanup)
    log.info("Shutdown complete.")


app = FastAPI(
    title=settings.PROJECT_NAME,
    version="0.1.0",
    description="Atenex Embedding Service for generating text embeddings using FastEmbed.",
    openapi_url=f"{settings.API_V1_STR}/openapi.json",
    lifespan=lifespan
)

# --- Middleware for Request ID and Logging ---
@app.middleware("http")
async def request_context_middleware(request: Request, call_next):
    start_time = asyncio.get_event_loop().time()
    request_id = request.headers.get("x-request-id", str(uuid.uuid4()))

    # Bind essential request info to contextvars for all loggers
    structlog.contextvars.bind_contextvars(
        request_id=request_id,
        method=request.method,
        path=str(request.url.path),
        client_host=request.client.host if request.client else "unknown",
    )
    # Initial log for request received
    log.info("Request received")

    response = None
    try:
        response = await call_next(request)
        process_time_ms = (asyncio.get_event_loop().time() - start_time) * 1000
        # Bind response status for final log
        structlog.contextvars.bind_contextvars(status_code=response.status_code, duration_ms=round(process_time_ms, 2))
        log_level = "warning" if 400 <= response.status_code < 500 else "error" if response.status_code >= 500 else "info"
        getattr(log, log_level)("Request finished") # Use bound logger
        response.headers["X-Request-ID"] = request_id # Echo request ID
        response.headers["X-Process-Time-Ms"] = f"{process_time_ms:.2f}"
    except Exception as e:
        process_time_ms = (asyncio.get_event_loop().time() - start_time) * 1000
        structlog.contextvars.bind_contextvars(status_code=500, duration_ms=round(process_time_ms, 2))
        log.exception("Unhandled exception during request processing") # Use bound logger
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
    # Request context is already bound by middleware
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

    model_status_str = "not_loaded"
    model_name_str = None
    model_dim_int = None

    if embedding_model_adapter: # Check if adapter instance exists
        is_healthy, status_msg = await embedding_model_adapter.health_check()
        if is_healthy:
            model_status_str = "loaded"
            model_info = embedding_model_adapter.get_model_info()
            model_name_str = model_info.get("model_name")
            model_dim_int = model_info.get("dimension")
        else:
            model_status_str = "error"
            health_log.error("Health check: Embedding model error.", model_status_message=status_msg)
    else: # Adapter not even initialized
        health_log.warning("Health check: Embedding model adapter not initialized.")
        SERVICE_MODEL_READY = False # Ensure flag is accurate

    if not SERVICE_MODEL_READY: # This global flag is set by lifespan based on successful init
        health_log.error("Service not ready (model initialization failed or pending).")
        raise HTTPException(
            status_code=fastapi_status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=schemas.HealthCheckResponse(
                status="error",
                service=settings.PROJECT_NAME,
                model_status=model_status_str,
                model_name=model_name_str,
                model_dimension=model_dim_int
            ).model_dump(exclude_none=True)
        )

    health_log.info("Health check successful.", model_status=model_status_str)
    return schemas.HealthCheckResponse(
        status="ok",
        service=settings.PROJECT_NAME,
        model_status=model_status_str,
        model_name=model_name_str,
        model_dimension=model_dim_int
    )

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
version = "0.1.0"
description = "Atenex Embedding Service using FastAPI and FastEmbed"
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
tenacity = "^8.2.3" # For potential retries in clients if this service calls others (not typical for embedding service)

# --- Embedding Engine ---
fastembed = ">=0.3.0,<0.4.0" # Qdrant's FastEmbed
# sentence-transformers is a dependency of FastEmbed for many models,
# but FastEmbed manages its specific version.
# onnxruntime is also often a dependency for some FastEmbed models,
# ensure it's compatible or install explicitly if needed.
onnxruntime = "^1.18.0" # Recommended to align with ingest/query service if they also use it.

[tool.poetry.group.dev.dependencies]
pytest = "^7.4.4"
pytest-asyncio = "^0.21.1"
httpx = "^0.27.0" # For testing the API client-side

[build-system]
requires = ["poetry-core>=1.0.0"]
build-backend = "poetry.core.masonry.api"
```
