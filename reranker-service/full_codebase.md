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
│       │   ├── rerank_endpoint.py
│       │   └── reranker_endpoint.py
│       └── schemas.py
├── application
│   ├── __init__.py
│   ├── ports
│   │   ├── __init__.py
│   │   └── reranker_model_port.py
│   └── use_cases
│       ├── __init__.py
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
from fastapi import APIRouter, HTTPException, Depends, Body, status
import structlog

from app.api.v1.schemas import RerankRequest, RerankResponse
from app.application.use_cases.rerank_documents_use_case import RerankDocumentsUseCase
from app.dependencies import get_rerank_use_case # Import dependency getter

logger = structlog.get_logger(__name__)
router = APIRouter()

@router.post(
    "/rerank",
    response_model=RerankResponse,
    summary="Rerank a list of documents based on a query",
    status_code=status.HTTP_200_OK
)
async def rerank_documents_endpoint(
    request_body: RerankRequest = Body(...),
    use_case: RerankDocumentsUseCase = Depends(get_rerank_use_case)
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
            documents=request_body.documents,
            top_n=request_body.top_n
        )
        endpoint_log.info("Reranking successful.", num_documents_output=len(response_data.reranked_documents))
        return RerankResponse(data=response_data)
    except RuntimeError as e:
        # Catch errors from use case or adapter (e.g., model not ready, prediction failed)
        endpoint_log.error("Error during reranking process", error_message=str(e), exc_info=True)
        # Check if it's a "model not ready" type of error to return 503
        if "not available" in str(e).lower() or "not ready" in str(e).lower():
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=f"Reranker service is temporarily unavailable: {e}"
            )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Internal server error during reranking: {e}"
        )
    except ValueError as e: # Catch Pydantic validation errors if any slip through, or other value errors
        endpoint_log.warning("Validation error during reranking request", error_message=str(e), exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Invalid input for reranking: {e}"
        )
    except Exception as e:
        endpoint_log.error("Unexpected error during reranking", error_message=str(e), exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"An unexpected error occurred: {e}"
        )
```

## File: `app\api\v1\endpoints\reranker_endpoint.py`
```py
# Endpoint for reranker API

```

## File: `app\api\v1\schemas.py`
```py
# reranker-service/app/api/v1/schemas.py
from pydantic import BaseModel, Field, field_validator
from typing import List, Optional

from app.domain.models import DocumentToRerank, RerankResponseData

class RerankRequest(BaseModel):
    query: str = Field(..., min_length=1, description="The user's query to rerank documents against.")
    documents: List[DocumentToRerank] = Field(..., min_items=1, description="A list of documents to be reranked.")
    top_n: Optional[int] = Field(None, gt=0, description="Optional. If provided, returns only the top N reranked documents.")

    @field_validator('documents')
    @classmethod
    def check_documents_not_empty(cls, v: List[DocumentToRerank]):
        if not v:
            raise ValueError('Documents list cannot be empty.')
        return v

class RerankResponse(BaseModel):
    data: RerankResponseData

class HealthCheckResponse(BaseModel):
    status: str
    service: str
    model_status: str
    model_name: Optional[str] = None
```

## File: `app\application\__init__.py`
```py

```

## File: `app\application\ports\__init__.py`
```py

```

## File: `app\application\ports\reranker_model_port.py`
```py
# reranker-service/app/application/ports/reranker_model_port.py
from abc import ABC, abstractmethod
from typing import List
from app.domain.models import DocumentToRerank, RerankedDocument

class RerankerModelPort(ABC):
    @abstractmethod
    async def rerank(
        self, query: str, documents: List[DocumentToRerank]
    ) -> List[RerankedDocument]:
        """
        Reranks a list of documents based on a query.
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
        Checks if the model is loaded and ready.
        """
        pass
```

## File: `app\application\use_cases\__init__.py`
```py

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
```

## File: `app\core\logging_config.py`
```py
# reranker-service/app/core/logging_config.py
import logging
import sys
import structlog
from app.core.config import settings # Importar settings del servicio actual

def setup_logging():
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
    
    # Evitar duplicar handlers si uvicorn/gunicorn ya configuró uno básico
    # Esta comprobación es simple; podría ser más robusta
    if not any(isinstance(h.formatter, structlog.stdlib.ProcessorFormatter) for h in root_logger.handlers):
        if root_logger.hasHandlers():
            root_logger.handlers.clear() # Limpiar handlers existentes si vamos a añadir el nuestro
        root_logger.addHandler(handler)
    
    root_logger.setLevel(settings.LOG_LEVEL.upper())

    # Silenciar librerías verbosas
    logging.getLogger("uvicorn").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("gunicorn").setLevel(logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("sentence_transformers").setLevel(logging.INFO) # O WARNING
    logging.getLogger("torch").setLevel(logging.INFO) # O WARNING
    logging.getLogger("transformers").setLevel(logging.WARNING) # Muy verboso en DEBUG/INFO

    log = structlog.get_logger("reranker_service")
    log.info("Logging configured for Reranker Service", log_level=settings.LOG_LEVEL)
```

## File: `app\dependencies.py`
```py
# reranker-service/app/dependencies.py
from fastapi import HTTPException, status, Request
from app.application.use_cases.rerank_documents_use_case import RerankDocumentsUseCase
from app.infrastructure.rerankers.sentence_transformer_adapter import SentenceTransformerRerankerAdapter
from app.application.ports.reranker_model_port import RerankerModelPort

# This approach uses global-like instances set up during lifespan.
# For more complex apps, a proper DI container (e.g., dependency-injector) is better.

_reranker_model_adapter_instance: RerankerModelPort = None
_rerank_use_case_instance: RerankDocumentsUseCase = None

def set_dependencies(
    model_adapter: RerankerModelPort,
    use_case: RerankDocumentsUseCase
):
    global _reranker_model_adapter_instance, _rerank_use_case_instance
    _reranker_model_adapter_instance = model_adapter
    _rerank_use_case_instance = use_case

def get_rerank_use_case() -> RerankDocumentsUseCase:
    if _rerank_use_case_instance is None or \
       _reranker_model_adapter_instance is None or \
       not _reranker_model_adapter_instance.is_ready():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Reranker service is not ready or dependencies not initialized."
        )
    return _rerank_use_case_instance

# Alternative: Get adapter from app.state if set in lifespan
# async def get_reranker_adapter_from_state(request: Request) -> RerankerModelPort:
#     if not hasattr(request.app.state, "reranker_adapter") or \
#        not request.app.state.reranker_adapter.is_ready():
#         raise HTTPException(status_code=503, detail="Reranker model adapter not ready.")
#     return request.app.state.reranker_adapter

# async def get_use_case_from_adapter_in_state(
#     adapter: RerankerModelPort = Depends(get_reranker_adapter_from_state)
# ) -> RerankDocumentsUseCase:
#     return RerankDocumentsUseCase(adapter)
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
    text: str = Field(..., description="The text content of the document or chunk to be reranked.")
    metadata: Dict[str, Any] = Field(default_factory=dict, description="Original metadata associated with the document.")

class RerankedDocument(BaseModel):
    id: str = Field(..., description="Unique identifier for the document or chunk.")
    text: str = Field(..., description="The text content (can be omitted if client doesn't need it back).")
    score: float = Field(..., description="Relevance score assigned by the reranker model.")
    metadata: Dict[str, Any] = Field(default_factory=dict, description="Original metadata preserved.")

class ModelInfo(BaseModel):
    model_name: str = Field(..., description="Name of the reranker model used.")
    # Potentially add device, etc. if needed by client

class RerankResponseData(BaseModel):
    reranked_documents: List[RerankedDocument]
    model_info: ModelInfo
```

## File: `app\infrastructure\__init__.py`
```py

```

## File: `app\infrastructure\rerankers\__init__.py`
```py

```

## File: `app\infrastructure\rerankers\sentence_transformer_adapter.py`
```py
# reranker-service/app/infrastructure/rerankers/sentence_transformer_adapter.py
import asyncio
from typing import List, Tuple, Callable, Optional
from sentence_transformers import CrossEncoder
import structlog
import time

from app.application.ports.reranker_model_port import RerankerModelPort
from app.domain.models import DocumentToRerank, RerankedDocument
from app.core.config import settings

logger = structlog.get_logger(__name__)

class SentenceTransformerRerankerAdapter(RerankerModelPort):
    _model: Optional[CrossEncoder] = None
    _model_name_loaded: Optional[str] = None
    _model_status: str = "unloaded" # unloaded, loading, loaded, error

    def __init__(self):
        # Model loading is deferred to an explicit load method or lifespan
        pass

    def load_model(self):
        """Loads the CrossEncoder model."""
        if SentenceTransformerRerankerAdapter._model_status == "loaded" and \
           SentenceTransformerRerankerAdapter._model_name_loaded == settings.MODEL_NAME:
            logger.info("Reranker model already loaded.", model_name=settings.MODEL_NAME)
            return

        SentenceTransformerRerankerAdapter._model_status = "loading"
        init_log = logger.bind(
            adapter="SentenceTransformerRerankerAdapter",
            action="load_model",
            model_name=settings.MODEL_NAME,
            device=settings.MODEL_DEVICE,
            cache_dir=settings.HF_CACHE_DIR
        )
        init_log.info("Initializing CrossEncoder model...")
        start_time = time.time()
        try:
            SentenceTransformerRerankerAdapter._model = CrossEncoder(
                model_name=settings.MODEL_NAME,
                max_length=settings.MAX_SEQ_LENGTH,
                device=settings.MODEL_DEVICE,
                # model_kwargs={'cache_dir': settings.HF_CACHE_DIR} if settings.HF_CACHE_DIR else {} # Newer sentence-transformers might not need model_kwargs for cache_dir
            )
            # For cache_dir with newer sentence-transformers, it often respects TRANSFORMERS_CACHE or HF_HOME env vars
            # or you can pass cache_folder to some specific model classes if they support it.
            # CrossEncoder itself might use the default Hugging Face cache.
            
            load_time = time.time() - start_time
            SentenceTransformerRerankerAdapter._model_name_loaded = settings.MODEL_NAME
            SentenceTransformerRerankerAdapter._model_status = "loaded"
            init_log.info("CrossEncoder model loaded successfully.", duration_ms=load_time * 1000)
        except Exception as e:
            SentenceTransformerRerankerAdapter._model_status = "error"
            SentenceTransformerRerankerAdapter._model = None
            init_log.error("Failed to load CrossEncoder model.", error_message=str(e), exc_info=True)
            # Optionally raise an exception here if loading is critical for startup
            # raise RuntimeError(f"Failed to load CrossEncoder model: {e}") from e

    async def _predict_scores_async(self, query_doc_pairs: List[Tuple[str, str]]) -> List[float]:
        if SentenceTransformerRerankerAdapter._model is None or SentenceTransformerRerankerAdapter._model_status != "loaded":
            logger.error("Reranker model not loaded or not ready for prediction.")
            raise RuntimeError("Reranker model is not available for prediction.")

        loop = asyncio.get_event_loop()
        try:
            scores = await loop.run_in_executor(
                None,  # Uses the default ThreadPoolExecutor
                SentenceTransformerRerankerAdapter._model.predict,
                query_doc_pairs,
                settings.BATCH_SIZE, # batch_size
                True, # show_progress_bar (set to False in prod)
                None, # activation_fct
                False, # convert_to_numpy
                True # convert_to_tensor (might not be needed if model handles it)
            )
            return [float(score) for score in scores]
        except Exception as e:
            logger.error("Error during reranker model prediction", exc_info=True, query_doc_pairs_count=len(query_doc_pairs))
            raise RuntimeError(f"Reranker prediction failed: {e}") from e

    async def rerank(
        self, query: str, documents: List[DocumentToRerank]
    ) -> List[RerankedDocument]:
        if not documents:
            return []

        if SentenceTransformerRerankerAdapter._model is None or SentenceTransformerRerankerAdapter._model_status != "loaded":
            logger.error("Attempted to rerank with model not loaded/ready.")
            # Fallback: return documents unsorted or raise specific error
            # For now, let's raise to make it explicit
            raise RuntimeError("Reranker model is not available.")


        query_doc_pairs: List[Tuple[str, str]] = [(query, doc.text) for doc in documents]
        
        scores = await self._predict_scores_async(query_doc_pairs)

        reranked_docs_with_scores = []
        for doc, score in zip(documents, scores):
            reranked_docs_with_scores.append(
                RerankedDocument(
                    id=doc.id,
                    text=doc.text, 
                    score=score,
                    metadata=doc.metadata
                )
            )
        
        reranked_docs_with_scores.sort(key=lambda x: x.score, reverse=True)
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
import structlog
import uvicorn
import asyncio
import uuid
from contextlib import asynccontextmanager

from app.core.config import settings
from app.core.logging_config import setup_logging

# Initialize logging as the first step
setup_logging()
logger = structlog.get_logger("reranker_service.main")

# Import API router
from app.api.v1.endpoints import rerank_endpoint

# Import components for dependency setup
from app.infrastructure.rerankers.sentence_transformer_adapter import SentenceTransformerRerankerAdapter
from app.application.use_cases.rerank_documents_use_case import RerankDocumentsUseCase
from app.dependencies import set_dependencies # Import setter for dependencies

# Global state for service readiness
SERVICE_IS_READY = False

@asynccontextmanager
async def lifespan(app: FastAPI):
    global SERVICE_IS_READY
    logger.info("Reranker service starting up...", service_name="AtenexRerankerService")
    
    # Initialize and load the model adapter
    # This adapter has internal static state for the model, so we just instantiate it
    # and call its load_model method.
    model_adapter = SentenceTransformerRerankerAdapter()
    try:
        # The load_model method in the adapter handles its own logging and status
        # It's synchronous but should be relatively quick or managed internally if very long.
        # For very long loads, consider a background task that updates readiness.
        await asyncio.to_thread(model_adapter.load_model) # Run sync load_model in a thread
        
        if model_adapter.is_ready():
            logger.info("Reranker model adapter initialized and model loaded successfully.")
            # Instantiate use case with the ready adapter
            rerank_use_case = RerankDocumentsUseCase(reranker_model=model_adapter)
            
            # Set dependencies for endpoint injection
            set_dependencies(model_adapter=model_adapter, use_case=rerank_use_case)
            SERVICE_IS_READY = True
            logger.info("Reranker service is ready.")
        else:
            logger.error("Reranker model failed to load. Service will be unhealthy.")
            SERVICE_IS_READY = False
            set_dependencies(model_adapter=None, use_case=None) # Ensure dependencies are not set

    except Exception as e:
        logger.fatal("Critical error during reranker model adapter initialization.", error=str(e), exc_info=True)
        SERVICE_IS_READY = False
        set_dependencies(model_adapter=None, use_case=None)

    yield
    logger.info("Reranker service shutting down...")
    # Cleanup if needed (e.g., explicitly releasing GPU memory if PyTorch doesn't do it well)

app = FastAPI(
    title="Atenex Reranker Service",
    version="0.1.0",
    description="Microservice for reranking documents based on query relevance.",
    lifespan=lifespan,
    openapi_url="/api/v1/openapi.json", # Standardize openapi url
    docs_url="/api/v1/docs", # Standardize docs url
    redoc_url="/api/v1/redoc" # Standardize redoc url
)

# Middleware for request ID, timing, and logging
@app.middleware("http")
async def request_context_middleware(request: Request, call_next):
    structlog.contextvars.clear_contextvars()
    request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
    structlog.contextvars.bind_contextvars(request_id=request_id)

    start_time = asyncio.get_event_loop().time()
    
    response = None
    try:
        response = await call_next(request)
    except Exception as e:
        logger.exception("Unhandled exception during request processing.")
        response = JSONResponse(
            status_code=fastapi_status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"detail": "Internal Server Error"}
        )
    finally:
        process_time = (asyncio.get_event_loop().time() - start_time) * 1000
        status_code = response.status_code if response else 500
        
        log_method = logger.info
        if status_code >= 500:
            log_method = logger.error
        elif status_code >= 400:
            log_method = logger.warning

        log_method(
            "Request finished",
            method=request.method,
            path=str(request.url.path),
            status_code=status_code,
            duration_ms=round(process_time, 2),
            client_host=request.client.host if request.client else "unknown"
        )
        if response:
            response.headers["X-Request-ID"] = request_id
            response.headers["X-Process-Time-Ms"] = f"{process_time:.2f}"
        
        structlog.contextvars.clear_contextvars()
    return response

# Exception Handlers
@app.exception_handler(HTTPException)
async def http_exception_handler_custom(request: Request, exc: HTTPException):
    logger.error(
        "HTTP Exception",
        status_code=exc.status_code,
        detail=exc.detail,
        path=str(request.url.path),
        method=request.method
    )
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail}
    )

@app.exception_handler(RequestValidationError)
async def validation_exception_handler_custom(request: Request, exc: RequestValidationError):
    logger.warning(
        "Request Validation Error",
        errors=exc.errors(),
        path=str(request.url.path),
        method=request.method
    )
    return JSONResponse(
        status_code=fastapi_status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={"detail": exc.errors()}
    )

@app.exception_handler(Exception)
async def generic_exception_handler_custom(request: Request, exc: Exception):
    logger.error(
        "Unhandled Exception",
        error_type=type(exc).__name__,
        error_message=str(exc),
        path=str(request.url.path),
        method=request.method,
        exc_info=True
    )
    return JSONResponse(
        status_code=fastapi_status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"detail": "An unexpected internal server error occurred."}
    )


# Include API router
app.include_router(rerank_endpoint.router, prefix="/api/v1", tags=["Reranking"])

# Health Check Endpoint
@app.get("/health", response_model=None, status_code=fastapi_status.HTTP_200_OK, tags=["Health"])
async def health_check():
    model_status = SentenceTransformerRerankerAdapter.get_model_status() # Access static method
    model_name = settings.MODEL_NAME

    if SERVICE_IS_READY and model_status == "loaded":
        return JSONResponse(content={
            "status": "ok",
            "service": "Atenex Reranker Service",
            "model_status": model_status,
            "model_name": model_name
        })
    else:
        logger.warning("Health check failed or service not ready", service_ready=SERVICE_IS_READY, model_status=model_status)
        raise HTTPException(
            status_code=fastapi_status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "status": "error",
                "service": "Atenex Reranker Service",
                "model_status": model_status,
                "model_name": model_name,
                "message": "Service is not ready or model loading failed."
            }
        )

@app.get("/", include_in_schema=False)
async def root_redirect():
    return PlainTextResponse("Atenex Reranker Service is running. See /api/v1/docs for API documentation.")

if __name__ == "__main__":
    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=settings.PORT,
        log_level=settings.LOG_LEVEL.lower(),
        reload=True # Set to False in production
    )
```

## File: `app\utils\__init__.py`
```py

```

## File: `pyproject.toml`
```toml
[tool.poetry]
name = "reranker-service"
version = "0.1.0"
description = "Atenex Reranker Microservice"
authors = ["Atenex Engineering <dev@atenex.com>"]
readme = "README.md"

[tool.poetry.dependencies]
python = "^3.10" # Compatible con query-service
fastapi = "^0.110.0"
uvicorn = {extras = ["standard"], version = "^0.29.0"}
gunicorn = "^22.0.0" # Actualizado a una versión reciente
pydantic = {extras = ["email"], version = "^2.7.0"}
pydantic-settings = "^2.2.1"
structlog = "^24.1.0"
tenacity = "^8.2.3" # Aunque no se use directamente, es buena práctica tenerla por si se añade resiliencia a la carga del modelo.

sentence-transformers = "^2.7.0"
# PyTorch es una dependencia transitiva de sentence-transformers para CrossEncoder.
# Se puede añadir explícitamente si se quiere controlar la versión de PyTorch:
# torch = {version = "^2.2.0", source = "pytorch_cpu"} # Ejemplo para CPU
# torchvision = {version = "^0.17.0", source = "pytorch_cpu"}
# torchaudio = {version = "^2.2.0", source = "pytorch_cpu"}

# [[tool.poetry.source]] # Descomentar si se necesita especificar fuente para PyTorch
# name = "pytorch_cpu"
# url = "https://download.pytorch.org/whl/cpu"
# priority = "explicit"


[tool.poetry.group.dev.dependencies]
pytest = "^8.0.0"
pytest-asyncio = "^0.23.0"
httpx = "^0.27.0" # Para TestClient

[build-system]
requires = ["poetry-core>=1.0.0"]
build-backend = "poetry.core.masonry.api"
```
