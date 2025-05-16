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
│       │   └── rerank_endpoint.py
│       └── schemas.py
├── application
│   ├── __init__.py
│   ├── ports
│   │   ├── __init__.py
│   │   └── reranker_model_port.py
│   └── use_cases
│       ├── __init__.py
│       ├── rerank_documents_use_case.py
│       └── rerank_texts_use_case.py
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
│   └── rerankers
│       ├── __init__.py
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

```

## File: `app\api\v1\__init__.py`
```py

```

## File: `app\api\v1\endpoints\__init__.py`
```py

```

## File: `app\api\v1\endpoints\rerank_endpoint.py`
```py
# reranker-service/app/api/v1/endpoints/rerank_endpoint.py
from fastapi import APIRouter, HTTPException, Depends, Body, status as fastapi_status
import structlog
from typing import Annotated # For FastAPI Depends with type hints

from app.api.v1.schemas import RerankRequest, RerankResponse
from app.application.use_cases.rerank_documents_use_case import RerankDocumentsUseCase
from app.dependencies import get_rerank_use_case # Import dependency getter

logger = structlog.get_logger(__name__)
router = APIRouter()

@router.post(
    "/rerank",
    response_model=RerankResponse,
    summary="Rerank a list of documents based on a query",
    status_code=fastapi_status.HTTP_200_OK,
    responses={
        fastapi_status.HTTP_503_SERVICE_UNAVAILABLE: {"description": "Reranker service is not ready or model unavailable."},
        fastapi_status.HTTP_500_INTERNAL_SERVER_ERROR: {"description": "Internal server error during reranking."},
        fastapi_status.HTTP_422_UNPROCESSABLE_ENTITY: {"description": "Invalid input data."}
    }
)
async def rerank_documents_endpoint(
    request_body: RerankRequest = Body(...),
    # Use Annotated for clearer dependency injection with type hints
    use_case: Annotated[RerankDocumentsUseCase, Depends(get_rerank_use_case)] = None
):
    endpoint_log = logger.bind(
        action="rerank_documents_endpoint", 
        query_length=len(request_body.query), 
        num_documents_input=len(request_body.documents),
        top_n_requested=request_body.top_n
    )
    endpoint_log.info("Received rerank request.")

    try:
        response_data = await use_case.execute(
            query=request_body.query,
            documents=request_body.documents, # Pydantic should have validated these against DocumentToRerank
            top_n=request_body.top_n
        )
        endpoint_log.info(
            "Reranking successful.", 
            num_documents_output=len(response_data.reranked_documents),
            model_used=response_data.model_info.model_name
            )
        return RerankResponse(data=response_data)
    except RuntimeError as e:
        endpoint_log.error("Error during reranking process (RuntimeError).", error_message=str(e), exc_info=True)
        # Check if it's a "model not ready" type of error to return 503
        if "not available" in str(e).lower() or "not ready" in str(e).lower() or "model is not available" in str(e).lower() :
            raise HTTPException(
                status_code=fastapi_status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=f"Reranker service is temporarily unavailable: Model issue."
            )
        raise HTTPException(
            status_code=fastapi_status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Internal server error during reranking: {e}"
        )
    except ValueError as e: 
        endpoint_log.warning("Validation or value error during reranking request.", error_message=str(e), exc_info=True)
        raise HTTPException(
            status_code=fastapi_status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Invalid input for reranking: {e}"
        )
    except Exception as e:
        endpoint_log.error("Unexpected error during reranking.", error_message=str(e), exc_info=True)
        raise HTTPException(
            status_code=fastapi_status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"An unexpected error occurred: {type(e).__name__}"
        )
```

## File: `app\api\v1\schemas.py`
```py
# reranker-service/app/api/v1/schemas.py
from pydantic import BaseModel, Field, field_validator, conlist
from typing import List, Optional

# Import domain models to be wrapped or used directly in API responses/requests
from app.domain.models import DocumentToRerank, RerankResponseData

class RerankRequest(BaseModel):
    query: str = Field(..., min_length=1, description="The user's query to rerank documents against.")
    # Use conlist to ensure at least one document is provided
    documents: conlist(DocumentToRerank, min_length=1) = Field( # type: ignore
        ..., 
        description="A list of documents to be reranked. Must contain at least one document."
    )
    top_n: Optional[int] = Field(
        None, 
        gt=0, 
        description="Optional. If provided, returns only the top N reranked documents."
    )

class RerankResponse(BaseModel):
    """
    Standard API response structure wrapping the actual data.
    """
    data: RerankResponseData

class HealthCheckResponse(BaseModel):
    """
    Response model for the health check endpoint.
    """
    status: str = Field(..., description="Overall status of the service (e.g., 'ok', 'error').")
    service: str = Field(..., description="Name of the service.")
    model_status: str = Field(..., description="Status of the reranker model (e.g., 'loaded', 'loading', 'error', 'unloaded').")
    model_name: Optional[str] = Field(None, description="Name of the reranker model if loaded or configured.")
    message: Optional[str] = Field(None, description="Additional details, especially in case of error.")
```

## File: `app\application\__init__.py`
```py

```

## File: `app\application\ports\__init__.py`
```py
# reranker-service/app/application/ports/__init__.py
from .reranker_model_port import RerankerModelPort

__all__ = ["RerankerModelPort"]
```

## File: `app\application\ports\reranker_model_port.py`
```py
# reranker-service/app/application/ports/reranker_model_port.py
from abc import ABC, abstractmethod
from typing import List
from app.domain.models import DocumentToRerank, RerankedDocument # Import from current service's domain

class RerankerModelPort(ABC):
    """
    Abstract port defining the contract for a reranker model adapter.
    """
    @abstractmethod
    async def rerank(
        self, query: str, documents: List[DocumentToRerank]
    ) -> List[RerankedDocument]:
        """
        Reranks a list of documents based on a query.

        Args:
            query: The query string.
            documents: A list of DocumentToRerank objects.

        Returns:
            A list of RerankedDocument objects, sorted by relevance.
        
        Raises:
            RuntimeError: If the model is not ready or prediction fails.
        """
        pass

    @abstractmethod
    def get_model_name(self) -> str:
        """
        Returns the name of the underlying reranker model.
        """
        pass

    @abstractmethod
    def is_ready(self) -> bool:
        """
        Checks if the model is loaded and ready to perform reranking.
        """
        pass
```

## File: `app\application\use_cases\__init__.py`
```py
# reranker-service/app/application/use_cases/__init__.py
from .rerank_documents_use_case import RerankDocumentsUseCase

__all__ = ["RerankDocumentsUseCase"]
```

## File: `app\application\use_cases\rerank_documents_use_case.py`
```py
# reranker-service/app/application/use_cases/rerank_documents_use_case.py
from typing import List, Optional
import structlog

from app.application.ports.reranker_model_port import RerankerModelPort
from app.domain.models import DocumentToRerank, RerankedDocument, RerankResponseData, ModelInfo

logger = structlog.get_logger(__name__)

class RerankDocumentsUseCase:
    """
    Use case for reranking documents. It orchestrates the interaction
    with the reranker model port.
    """
    def __init__(self, reranker_model: RerankerModelPort):
        self.reranker_model = reranker_model
        logger.debug("RerankDocumentsUseCase initialized", reranker_model_type=type(reranker_model).__name__)

    async def execute(
        self, query: str, documents: List[DocumentToRerank], top_n: Optional[int] = None
    ) -> RerankResponseData:
        
        use_case_log = logger.bind(
            action="execute_rerank_documents_use_case", 
            query_preview=query[:50] + "..." if len(query) > 50 else query,
            num_documents_input=len(documents), 
            requested_top_n=top_n
        )
        use_case_log.info("Executing rerank documents use case.")

        if not self.reranker_model.is_ready():
            use_case_log.error("Reranker model is not ready. Cannot execute reranking.")
            raise RuntimeError("Reranker model service is not ready or model failed to load.")

        try:
            reranked_results = await self.reranker_model.rerank(query, documents)
            use_case_log.debug("Reranking completed by model port.", num_results_from_port=len(reranked_results))

            if top_n is not None and top_n > 0:
                use_case_log.debug(f"Applying top_n={top_n} to reranked results.")
                reranked_results = reranked_results[:top_n]
            
            model_info = ModelInfo(model_name=self.reranker_model.get_model_name())
            response_data = RerankResponseData(reranked_documents=reranked_results, model_info=model_info)
            
            use_case_log.info(
                "Reranking use case execution successful.", 
                num_reranked_documents_output=len(reranked_results),
                model_name=model_info.model_name
            )
            return response_data
        except RuntimeError as e: # Catch errors from the adapter/port
            use_case_log.error("Runtime error during reranking execution.", error_message=str(e), exc_info=True)
            raise # Re-raise to be caught by the endpoint handler
        except Exception as e:
            use_case_log.error("Unexpected error during reranking execution.", error_message=str(e), exc_info=True)
            raise RuntimeError(f"An unexpected error occurred while reranking documents: {e}") from e
```

## File: `app\application\use_cases\rerank_texts_use_case.py`
```py
# reranker-service/app/application/use_cases/rerank_documents_use_case.py
from typing import List, Optional
import structlog

from app.application.ports.reranker_model_port import RerankerModelPort
from app.domain.models import DocumentToRerank, RerankedDocument, RerankResponseData, ModelInfo

logger = structlog.get_logger(__name__)

class RerankDocumentsUseCase:
    def __init__(self, reranker_model: RerankerModelPort):
        self.reranker_model = reranker_model

    async def execute(
        self, query: str, documents: List[DocumentToRerank], top_n: Optional[int] = None
    ) -> RerankResponseData:
        
        if not self.reranker_model.is_ready():
            logger.error("Reranker model is not ready, cannot execute use case.")
            raise RuntimeError("Reranker model service is not ready.")

        use_case_log = logger.bind(
            action="rerank_documents_use_case", 
            num_documents=len(documents), 
            top_n=top_n
        )
        use_case_log.info("Executing rerank documents use case.")

        try:
            reranked_results = await self.reranker_model.rerank(query, documents)

            if top_n is not None and top_n > 0:
                use_case_log.debug(f"Applying top_n={top_n} to reranked results.")
                reranked_results = reranked_results[:top_n]
            
            model_info = ModelInfo(model_name=self.reranker_model.get_model_name())
            response_data = RerankResponseData(reranked_documents=reranked_results, model_info=model_info)
            
            use_case_log.info("Reranking successful.", num_reranked=len(reranked_results))
            return response_data
        except Exception as e:
            use_case_log.error("Error during reranking execution.", error=str(e), exc_info=True)
            # Re-raise or handle as specific application error
            raise RuntimeError(f"Failed to rerank documents: {e}") from e
```

## File: `app\core\__init__.py`
```py

```

## File: `app\core\config.py`
```py
# reranker-service/app/core/config.py
import logging
import sys
from typing import Optional
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field, field_validator, ValidationInfo, ValidationError, AnyHttpUrl
import json

# --- Default Values ---
DEFAULT_MODEL_NAME = "BAAI/bge-reranker-base"
DEFAULT_MODEL_DEVICE = "cpu"
DEFAULT_LOG_LEVEL = "INFO"
DEFAULT_PORT = 8004
DEFAULT_HF_CACHE_DIR = "/app/.cache/huggingface"
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

    PROJECT_NAME: str = "Atenex Reranker Service"
    API_V1_STR: str = "/api/v1"

    LOG_LEVEL: str = Field(default=DEFAULT_LOG_LEVEL)
    PORT: int = Field(default=DEFAULT_PORT)

    MODEL_NAME: str = Field(default=DEFAULT_MODEL_NAME)
    MODEL_DEVICE: str = Field(default=DEFAULT_MODEL_DEVICE)
    # Optional because HuggingFace libs have their own defaults if not set
    HF_CACHE_DIR: Optional[str] = Field(default=DEFAULT_HF_CACHE_DIR) 
    
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
        # Basic validation for common device strings
        allowed_devices_prefixes = ["cpu", "cuda", "mps"]
        normalized_v = v.lower()
        if not any(normalized_v.startswith(prefix) for prefix in allowed_devices_prefixes):
            logging.warning(f"MODEL_DEVICE '{v}' is unusual. Ensure it's a valid device string for PyTorch/sentence-transformers.")
        return normalized_v


# --- Global Settings Instance ---
_temp_log = logging.getLogger("reranker_service.config.loader") # Use a distinct name
if not _temp_log.handlers: # Avoid adding handlers multiple times
    _handler = logging.StreamHandler(sys.stdout)
    _formatter = logging.Formatter('%(levelname)s: [%(name)s] %(message)s')
    _handler.setFormatter(_formatter)
    _temp_log.addHandler(_handler)
    _temp_log.setLevel(logging.INFO)

try:
    _temp_log.info("Loading Reranker Service settings...")
    settings = Settings()
    _temp_log.info("Reranker Service Settings Loaded Successfully:")
    # Use model_dump for Pydantic v2
    log_data = settings.model_dump() 
    for key, value in log_data.items():
        _temp_log.info(f"  {key.upper()}: {value}")

except (ValidationError, ValueError) as e:
    error_details_str = ""
    if isinstance(e, ValidationError):
        try:
            # Attempt to get structured error details if Pydantic ValidationError
            error_details_str = f"\nValidation Errors:\n{json.dumps(e.errors(), indent=2)}"
        except Exception: # Fallback for other error types or if e.errors() fails
            error_details_str = f"\nRaw Errors: {e}"
    else: # For generic ValueError
        error_details_str = f"\nError: {e}"
    
    _temp_log.critical(f"!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
    _temp_log.critical(f"! FATAL: Reranker Service configuration validation failed!{error_details_str}")
    _temp_log.critical(f"! Check environment variables (prefixed with RERANKER_) or .env file.")
    _temp_log.critical(f"!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
    sys.exit(1) # Exit if configuration fails
except Exception as e:
    _temp_log.critical(f"FATAL: Unexpected error loading Reranker Service settings: {e}", exc_info=True)
    sys.exit(1)
```

## File: `app\core\logging_config.py`
```py
# reranker-service/app/core/logging_config.py
import logging
import sys
import structlog
import os # Import os to check for Gunicorn environment

# Import settings from the current service's config module
from app.core.config import settings

def setup_logging():
    """Configures structured logging using structlog for the Reranker Service."""

    log_level_str = settings.LOG_LEVEL.upper()
    log_level_int = getattr(logging, log_level_str, logging.INFO)

    # Common processors for structlog
    shared_processors = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.processors.StackInfoRenderer(), # For tracebacks
        structlog.dev.set_exc_info, # Add exception info if present
        structlog.processors.TimeStamper(fmt="iso", utc=True), # ISO format timestamps in UTC
    ]

    # Add callsite parameters only if log level is DEBUG for performance
    if log_level_int <= logging.DEBUG:
        shared_processors.append(
            structlog.processors.CallsiteParameterAdder(
                {
                    structlog.processors.CallsiteParameter.FILENAME,
                    structlog.processors.CallsiteParameter.LINENO,
                    structlog.processors.CallsiteParameter.FUNC_NAME,
                }
            )
        )

    # Configure structlog
    structlog.configure(
        processors=shared_processors + [
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger, # Standard bound logger
        cache_logger_on_first_use=True,
    )

    # Configure the stdlib formatter for output
    # This formatter will process the already structured log records from structlog
    formatter = structlog.stdlib.ProcessorFormatter(
        # Processor for formatting the records from structlog before rendering.
        foreign_pre_chain=shared_processors, # Apply shared processors to non-structlog records too
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta, # Remove structlog's internal keys
            structlog.processors.JSONRenderer(), # Render the final log record as JSON
            # For development, you might prefer:
            # structlog.dev.ConsoleRenderer(colors=True),
        ],
    )

    # Get the root logger
    root_logger = logging.getLogger()
    # Clear any existing handlers to avoid duplicate logs, especially in Gunicorn/Uvicorn
    if root_logger.hasHandlers():
        root_logger.handlers.clear()

    # Add a new StreamHandler with our configured formatter
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)
    root_logger.addHandler(handler)
    root_logger.setLevel(log_level_int)

    # Configure levels for noisy libraries
    logging.getLogger("uvicorn").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING) # Access logs can be very verbose
    logging.getLogger("gunicorn.error").setLevel(logging.INFO) # Gunicorn's own error logs
    logging.getLogger("httpx").setLevel(logging.WARNING) # HTTP client library
    logging.getLogger("sentence_transformers").setLevel(logging.INFO) # Can be verbose
    logging.getLogger("torch").setLevel(logging.INFO) # PyTorch
    logging.getLogger("transformers.modeling_utils").setLevel(logging.WARNING) # Suppress download messages unless error

    # Get a logger specific to this service after configuration
    log = structlog.get_logger(settings.PROJECT_NAME.lower().replace(" ", "-"))
    log.info(
        "Logging configured for Reranker Service",
        log_level=log_level_str,
        json_logs_enabled=True # Assuming JSONRenderer is used
    )
```

## File: `app\dependencies.py`
```py
# reranker-service/app/dependencies.py
from fastapi import HTTPException, status as fastapi_status, Request
from typing import Optional, Annotated

from app.application.use_cases.rerank_documents_use_case import RerankDocumentsUseCase
from app.application.ports.reranker_model_port import RerankerModelPort
# The actual adapter instance will be set during app lifespan.

# Globals to hold instances, set by lifespan. This is a simple DI approach.
_reranker_model_adapter_instance: Optional[RerankerModelPort] = None
_rerank_use_case_instance: Optional[RerankDocumentsUseCase] = None

def set_dependencies(
    model_adapter: RerankerModelPort,
    use_case: RerankDocumentsUseCase
):
    """
    Called during application startup (lifespan) to set the shared instances.
    """
    global _reranker_model_adapter_instance, _rerank_use_case_instance
    _reranker_model_adapter_instance = model_adapter
    _rerank_use_case_instance = use_case
    # Add logging here if needed to confirm dependencies are set.

def get_rerank_use_case() -> RerankDocumentsUseCase:
    """
    FastAPI dependency getter for RerankDocumentsUseCase.
    Ensures the use case and its underlying model adapter are ready.
    """
    if _rerank_use_case_instance is None or \
       _reranker_model_adapter_instance is None or \
       not _reranker_model_adapter_instance.is_ready():
        # This detailed check helps pinpoint if the adapter or use case itself wasn't set,
        # or if the adapter is set but not ready (model load failed).
        raise HTTPException(
            status_code=fastapi_status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Reranker service is not ready. Dependencies (model or use case) not initialized or model failed to load."
        )
    return _rerank_use_case_instance
```

## File: `app\domain\__init__.py`
```py

```

## File: `app\domain\models.py`
```py
# reranker-service/app/domain/models.py
from pydantic import BaseModel, Field
from typing import List, Dict, Any, Optional

class DocumentToRerank(BaseModel):
    id: str = Field(..., description="Unique identifier for the document or chunk.")
    text: str = Field(..., min_length=1, description="The text content of the document or chunk to be reranked.")
    metadata: Dict[str, Any] = Field(default_factory=dict, description="Original metadata associated with the document.")

class RerankedDocument(BaseModel):
    id: str = Field(..., description="Unique identifier for the document or chunk.")
    text: str = Field(..., description="The text content (can be omitted if client doesn't need it back, but useful for debugging).")
    score: float = Field(..., description="Relevance score assigned by the reranker model.")
    metadata: Dict[str, Any] = Field(default_factory=dict, description="Original metadata preserved.")

class ModelInfo(BaseModel):
    model_name: str = Field(..., description="Name of the reranker model used.")
    # Potentially add device info if it's useful for the client to know
    # model_device: Optional[str] = None 

class RerankResponseData(BaseModel):
    reranked_documents: List[RerankedDocument]
    model_info: ModelInfo
```

## File: `app\infrastructure\__init__.py`
```py

```

## File: `app\infrastructure\rerankers\__init__.py`
```py
# reranker-service/app/infrastructure/rerankers/__init__.py
from .sentence_transformer_adapter import SentenceTransformerRerankerAdapter

__all__ = ["SentenceTransformerRerankerAdapter"]
```

## File: `app\infrastructure\rerankers\sentence_transformer_adapter.py`
```py
# reranker-service/app/infrastructure/rerankers/sentence_transformer_adapter.py
import asyncio
from typing import List, Tuple, Optional
from sentence_transformers import CrossEncoder # type: ignore
import structlog
import time
import os # For Hugging Face cache directory environment variable

from app.application.ports.reranker_model_port import RerankerModelPort
from app.domain.models import DocumentToRerank, RerankedDocument
from app.core.config import settings

logger = structlog.get_logger(__name__)

class SentenceTransformerRerankerAdapter(RerankerModelPort):
    """
    Adapter for sentence-transformers CrossEncoder models.
    Manages model loading and prediction.
    The model instance and status are class-level to act as a singleton
    managed by the lifespan.
    """
    _model: Optional[CrossEncoder] = None
    _model_name_loaded: Optional[str] = None
    _model_status: str = "unloaded" # States: unloaded, loading, loaded, error

    def __init__(self):
        logger.debug("SentenceTransformerRerankerAdapter instance created.")

    @classmethod
    def load_model(cls):
        """
        Loads the CrossEncoder model based on settings.
        This method is intended to be called once, e.g., during application startup.
        """
        if cls._model_status == "loaded" and cls._model_name_loaded == settings.MODEL_NAME:
            logger.info("Reranker model already loaded and configured.", model_name=settings.MODEL_NAME)
            return

        cls._model_status = "loading"
        cls._model = None 
        init_log = logger.bind(
            adapter_action="load_model",
            model_name=settings.MODEL_NAME,
            device=settings.MODEL_DEVICE,
            configured_hf_cache_dir=settings.HF_CACHE_DIR
        )
        init_log.info("Attempting to load CrossEncoder model...")
        
        if settings.HF_CACHE_DIR:
            os.environ['HF_HOME'] = settings.HF_CACHE_DIR
            os.environ['TRANSFORMERS_CACHE'] = settings.HF_CACHE_DIR
            init_log.info(f"Set HF_HOME/TRANSFORMERS_CACHE to: {settings.HF_CACHE_DIR}")

        start_time = time.time()
        try:
            cls._model = CrossEncoder(
                model_name=settings.MODEL_NAME,
                max_length=settings.MAX_SEQ_LENGTH,
                device=settings.MODEL_DEVICE,
            )
            load_time = time.time() - start_time
            cls._model_name_loaded = settings.MODEL_NAME
            cls._model_status = "loaded"
            init_log.info("CrossEncoder model loaded successfully.", duration_seconds=round(load_time, 3))
        except Exception as e:
            cls._model_status = "error"
            cls._model = None 
            init_log.error("Failed to load CrossEncoder model.", error_message=str(e), exc_info=True)

    async def _predict_scores_async(self, query_doc_pairs: List[Tuple[str, str]]) -> List[float]:
        """
        Performs model prediction asynchronously in a thread pool.
        """
        if not self.is_ready() or SentenceTransformerRerankerAdapter._model is None:
            logger.error("Reranker model not loaded or not ready for prediction.")
            raise RuntimeError("Reranker model is not available for prediction.")

        predict_log = logger.bind(adapter_action="_predict_scores_async", num_pairs=len(query_doc_pairs))
        predict_log.debug("Starting asynchronous prediction.")
        
        loop = asyncio.get_event_loop()
        try:
            scores_numpy_array = await loop.run_in_executor(
                None, 
                SentenceTransformerRerankerAdapter._model.predict,
                query_doc_pairs,
                batch_size=settings.BATCH_SIZE,
                show_progress_bar=False,
                num_workers=0,  # Explicitly set num_workers
                activation_fct=None,
                apply_softmax=False,
                convert_to_numpy=True, # Ensure output is numpy array
                convert_to_tensor=False
            )
            scores = scores_numpy_array.tolist() # Convert numpy array to Python list of floats
            predict_log.debug("Prediction successful.")
            return scores
        except Exception as e:
            predict_log.error("Error during reranker model prediction.", error_message=str(e), exc_info=True)
            raise RuntimeError(f"Reranker prediction failed: {e}") from e

    async def rerank(
        self, query: str, documents: List[DocumentToRerank]
    ) -> List[RerankedDocument]:
        """
        Reranks documents based on the query using the loaded CrossEncoder model.
        """
        rerank_log = logger.bind(
            adapter_action="rerank", 
            query_preview=query[:50]+"..." if len(query) > 50 else query,
            num_documents_input=len(documents)
        )
        rerank_log.info("Starting rerank operation.")

        if not documents:
            rerank_log.debug("No documents provided for reranking.")
            return []

        if not self.is_ready():
            rerank_log.error("Attempted to rerank when model is not ready.")
            raise RuntimeError("Reranker model is not available or failed to load.")

        query_doc_pairs: List[Tuple[str, str]] = []
        valid_documents_for_reranking: List[DocumentToRerank] = []

        for doc in documents:
            if doc.text and isinstance(doc.text, str) and doc.text.strip():
                query_doc_pairs.append((query, doc.text))
                valid_documents_for_reranking.append(doc)
            else:
                rerank_log.warning("Skipping document due to empty or invalid text.", document_id=doc.id)
        
        if not valid_documents_for_reranking:
            rerank_log.warning("No valid documents with text found for reranking.")
            return []

        rerank_log.debug(f"Processing {len(valid_documents_for_reranking)} documents for reranking.")
        scores = await self._predict_scores_async(query_doc_pairs)

        reranked_docs_with_scores: List[RerankedDocument] = []
        for doc, score in zip(valid_documents_for_reranking, scores):
            reranked_docs_with_scores.append(
                RerankedDocument(
                    id=doc.id,
                    text=doc.text, 
                    score=score, 
                    metadata=doc.metadata 
                )
            )
        
        reranked_docs_with_scores.sort(key=lambda x: x.score, reverse=True)
        
        rerank_log.info("Rerank operation completed.", num_documents_output=len(reranked_docs_with_scores))
        return reranked_docs_with_scores

    def get_model_name(self) -> str:
        return settings.MODEL_NAME 

    def is_ready(self) -> bool:
        return SentenceTransformerRerankerAdapter._model is not None and \
               SentenceTransformerRerankerAdapter._model_status == "loaded"

    @classmethod
    def get_model_status(cls) -> str:
        return cls._model_status
```

## File: `app\main.py`
```py
# reranker-service/app/main.py
from fastapi import FastAPI, HTTPException, Request, status as fastapi_status
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.exceptions import RequestValidationError
from contextlib import asynccontextmanager
import structlog
import uvicorn # For local execution if __name__ == "__main__"
import asyncio
import uuid # For request IDs

# Import core components first
from app.core.config import settings
from app.core.logging_config import setup_logging

# Initialize logging as the very first step
setup_logging()
logger = structlog.get_logger(settings.PROJECT_NAME.lower().replace(" ", "-") + ".main")

# Import API router
from app.api.v1.endpoints import rerank_endpoint

# Import components for dependency setup during lifespan
from app.infrastructure.rerankers.sentence_transformer_adapter import SentenceTransformerRerankerAdapter
from app.application.use_cases.rerank_documents_use_case import RerankDocumentsUseCase
from app.dependencies import set_dependencies # Import setter for dependencies
from app.api.v1.schemas import HealthCheckResponse # For health check response model

# Global state for service readiness, managed by lifespan
SERVICE_IS_READY = False

@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Handles application startup and shutdown events.
    - Loads the reranker model.
    - Sets up shared dependencies.
    - Manages service readiness state.
    """
    global SERVICE_IS_READY
    logger.info(
        f"{settings.PROJECT_NAME} service starting up...", 
        version="0.1.0", # Consider moving version to config if it changes often
        port=settings.PORT,
        log_level=settings.LOG_LEVEL
    )
    
    # Instantiate the concrete adapter. Model loading is a class method.
    model_adapter = SentenceTransformerRerankerAdapter()
    
    try:
        # Trigger model loading. This is a class method that updates static/class variables.
        # Running synchronous model loading in a thread to avoid blocking lifespan.
        await asyncio.to_thread(SentenceTransformerRerankerAdapter.load_model) 
        
        if model_adapter.is_ready(): # is_ready() checks the class-level status
            logger.info(
                "Reranker model adapter initialized and model loaded successfully.",
                model_name=model_adapter.get_model_name()
            )
            # Instantiate use case with the (now ready) adapter
            rerank_use_case = RerankDocumentsUseCase(reranker_model=model_adapter)
            
            # Set shared instances for dependency injection
            set_dependencies(model_adapter=model_adapter, use_case=rerank_use_case)
            SERVICE_IS_READY = True
            logger.info(f"{settings.PROJECT_NAME} is ready to serve requests.")
        else:
            logger.error(
                "Reranker model failed to load during startup. Service will be unhealthy.",
                model_name=settings.MODEL_NAME # Log configured name even if load failed
            )
            SERVICE_IS_READY = False
            # Ensure dependencies reflect unready state if use_case requires a ready model
            set_dependencies(model_adapter=model_adapter, use_case=None) 

    except Exception as e:
        logger.fatal(
            "Critical error during reranker model adapter initialization or loading in lifespan.", 
            error_message=str(e), 
            exc_info=True
        )
        SERVICE_IS_READY = False
        set_dependencies(model_adapter=model_adapter if 'model_adapter' in locals() else None, use_case=None)

    yield # Application runs here

    # --- Shutdown Logic ---
    logger.info(f"{settings.PROJECT_NAME} service shutting down...")
    # Add any cleanup logic here if necessary (e.g., releasing GPU resources explicitly, though PyTorch usually handles this)
    # For this service, model cleanup is not explicitly managed by instance, but by Python's GC when process ends.
    logger.info(f"{settings.PROJECT_NAME} has been shut down.")

# Create FastAPI application instance
app = FastAPI(
    title=settings.PROJECT_NAME,
    version="0.1.0", # Should match pyproject.toml
    description="Microservice for reranking documents based on query relevance using CrossEncoder models.",
    lifespan=lifespan,
    openapi_url=f"{settings.API_V1_STR}/openapi.json",
    docs_url=f"{settings.API_V1_STR}/docs",
    redoc_url=f"{settings.API_V1_STR}/redoc"
)

# Middleware for request ID, timing, and logging
@app.middleware("http")
async def request_context_middleware(request: Request, call_next):
    # Clear contextvars at the beginning of each request
    structlog.contextvars.clear_contextvars()
    
    # Bind request-specific information for all logs within this request's scope
    request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
    structlog.contextvars.bind_contextvars(request_id=request_id)

    start_time = asyncio.get_event_loop().time()
    
    response = None
    try:
        response = await call_next(request)
    except Exception as e:
        # This will catch unhandled exceptions from routes/dependencies
        logger.exception("Unhandled exception during request processing by middleware.") # Log with full traceback
        response = JSONResponse(
            status_code=fastapi_status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"detail": "An unexpected internal server error occurred."}
        )
    finally:
        process_time_ms = (asyncio.get_event_loop().time() - start_time) * 1000
        status_code_for_log = response.status_code if response else 500 # Default to 500 if no response
        
        log_method = logger.info
        if status_code_for_log >= 500:
            log_method = logger.error
        elif status_code_for_log >= 400:
            log_method = logger.warning

        log_method(
            "Request finished",
            http_method=request.method,
            http_path=str(request.url.path),
            http_status_code=status_code_for_log,
            http_duration_ms=round(process_time_ms, 2),
            client_host=request.client.host if request.client else "unknown_client"
        )
        if response: # Ensure headers are added only if a response object exists
            response.headers["X-Request-ID"] = request_id
            response.headers["X-Process-Time-Ms"] = f"{process_time_ms:.2f}"
        
        # Clear contextvars after the request is fully processed
        structlog.contextvars.clear_contextvars()
    return response

# Custom Exception Handlers
@app.exception_handler(HTTPException)
async def http_exception_handler_custom(request: Request, exc: HTTPException):
    # Logged by middleware already if it bubbles up
    # logger.error("HTTP Exception handled", status_code=exc.status_code, detail=exc.detail)
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail}
    )

@app.exception_handler(RequestValidationError)
async def validation_exception_handler_custom(request: Request, exc: RequestValidationError):
    # Logged by middleware already
    # logger.warning("Request Validation Error handled", errors=exc.errors())
    return JSONResponse(
        status_code=fastapi_status.HTTP_422_UNPROCESSABLE_ENTITY,
        # Provide structured error details from Pydantic
        content={"detail": exc.errors()} 
    )

@app.exception_handler(Exception) # Catch-all for any other unhandled exceptions
async def generic_exception_handler_custom(request: Request, exc: Exception):
    # Logged by middleware already
    # logger.error("Generic Unhandled Exception handled", error_type=type(exc).__name__, error_message=str(exc), exc_info=True)
    return JSONResponse(
        status_code=fastapi_status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"detail": "An unexpected internal server error occurred."}
    )

# Include API router
app.include_router(rerank_endpoint.router, prefix=settings.API_V1_STR, tags=["Reranking Operations"])
logger.info("API routers included.", prefix=settings.API_V1_STR)

# Health Check Endpoint
@app.get(
    "/health", 
    response_model=HealthCheckResponse, 
    tags=["Health"],
    summary="Service Health and Model Status Check"
)
async def health_check():
    # Access model status via the class method of the adapter
    model_status = SentenceTransformerRerankerAdapter.get_model_status()
    current_model_name = settings.MODEL_NAME # Get configured model name

    health_log = logger.bind(service_ready_flag=SERVICE_IS_READY, model_actual_status=model_status)

    if SERVICE_IS_READY and model_status == "loaded":
        health_log.debug("Health check: OK")
        return HealthCheckResponse(
            status="ok",
            service=settings.PROJECT_NAME,
            model_status=model_status,
            model_name=current_model_name
        )
    else:
        unhealthy_reason = "Service dependencies not fully initialized."
        if model_status != "loaded":
            unhealthy_reason = f"Model status is '{model_status}'."
        
        health_log.warning("Health check: FAILED", reason=unhealthy_reason)
        # Return 503 with JSON body as per schema
        return JSONResponse(
            status_code=fastapi_status.HTTP_503_SERVICE_UNAVAILABLE,
            content={
                "status": "error",
                "service": settings.PROJECT_NAME,
                "model_status": model_status,
                "model_name": current_model_name,
                "message": f"Service is not ready. {unhealthy_reason}"
            }
        )

# Root endpoint for basic "is it alive" check or simple info
@app.get("/", include_in_schema=False)
async def root_redirect():
    return PlainTextResponse(
        f"{settings.PROJECT_NAME} is running. See {settings.API_V1_STR}/docs for API documentation."
    )

# For local development: uvicorn app.main:app --reload --port 8004
if __name__ == "__main__":
    logger.info(f"Starting {settings.PROJECT_NAME} locally with Uvicorn...")
    uvicorn.run(
        "app.main:app",
        host="0.0.0.0", # Listen on all available IPs
        port=settings.PORT,
        log_level=settings.LOG_LEVEL.lower(), # Uvicorn's own log level
        reload=True # Enable auto-reload for development
    )

# jfu
```

## File: `app\utils\__init__.py`
```py

```

## File: `pyproject.toml`
```toml
[tool.poetry]
name = "reranker-service"
version = "0.1.0"
description = "Atenex Reranker Microservice for document relevance scoring."
authors = ["Atenex Engineering <dev@atenex.com>"]
readme = "README.md"
license = "Proprietary"
# homepage = "https://atenex.ai"
# repository = "https://github.com/atenex/reranker-service"

[tool.poetry.dependencies]
python = "^3.10"
fastapi = "^0.111.0"
uvicorn = {extras = ["standard"], version = "^0.29.0"}
gunicorn = "^22.0.0"
pydantic = {extras = ["email"], version = "^2.7.1"} # Matched with query-service
pydantic-settings = "^2.2.1"
structlog = "^24.1.0"
tenacity = "^8.2.3" # For potential retries if needed in future

# Core ML dependency for reranking
sentence-transformers = "^2.7.0"
# PyTorch is a transitive dependency of sentence-transformers.
# Forcing CPU version if specific hardware is not guaranteed or for lighter images.
# torch = {version = "~2.2.0", source = "pytorch_cpu"} # Example for CPU constraint
# torchvision = {version = "~0.17.0", source = "pytorch_cpu"}
# torchaudio = {version = "~0.17.0", source = "pytorch_cpu"}
# numpy = "~1.26.4" # Often a dependency, good to pin. sentence-transformers will pull a compatible one.

# [[tool.poetry.source]]
# name = "pytorch_cpu"
# url = "https://download.pytorch.org/whl/cpu"
# priority = "explicit"


[tool.poetry.group.dev.dependencies]
pytest = "^8.1.1"
pytest-asyncio = "^0.23.6"
httpx = "^0.27.0" # For TestClient

[build-system]
requires = ["poetry-core>=1.0.0"]
build-backend = "poetry.core.masonry.api"

[tool.pytest.ini_options]
asyncio_mode = "auto"
```
