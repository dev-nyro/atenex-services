# Estructura de la Codebase

```
app/
├── api
│   └── v1
│       ├── __init__.py
│       ├── endpoints
│       │   ├── __init__.py
│       │   └── search_endpoint.py
│       └── schemas.py
├── application
│   ├── __init__.py
│   ├── ports
│   │   ├── __init__.py
│   │   ├── repository_ports.py
│   │   └── sparse_search_port.py
│   └── use_cases
│       ├── __init__.py
│       └── sparse_search_use_case.py
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
│   ├── persistence
│   │   ├── __init__.py
│   │   ├── postgres_connector.py
│   │   └── postgres_repositories.py
│   └── sparse_retrieval
│       ├── __init__.py
│       └── bm25_adapter.py
└── main.py
```

# Codebase: `app`

## File: `app\api\v1\__init__.py`
```py

```

## File: `app\api\v1\endpoints\__init__.py`
```py

```

## File: `app\api\v1\endpoints\search_endpoint.py`
```py
# sparse-search-service/app/api/v1/endpoints/search_endpoint.py
import uuid
import structlog
from fastapi import APIRouter, Depends, HTTPException, status, Body, Header, Request

from app.api.v1 import schemas
from app.application.use_cases.sparse_search_use_case import SparseSearchUseCase
from app.dependencies import get_sparse_search_use_case # Asumiendo que se define en dependencies.py
from app.core.config import settings

log = structlog.get_logger(__name__) # Logger para el endpoint

router = APIRouter()

# --- Headers Dependencies (Reutilizado y adaptado de query-service) ---
# En un monorepo o librería compartida, esto podría ser común.
async def get_required_company_id_header(
    x_company_id: uuid.UUID = Header(..., description="Required X-Company-ID header.")
) -> uuid.UUID:
    # La validación de UUID ya la hace FastAPI al convertir el tipo.
    # Si no se provee, FastAPI devuelve 422.
    # Si el formato es incorrecto, FastAPI devuelve 422.
    return x_company_id

@router.post(
    "/search",
    response_model=schemas.SparseSearchResponse,
    status_code=status.HTTP_200_OK,
    summary="Perform Sparse Search (BM25)",
    description="Receives a query and company ID, performs a BM25 search over the company's documents, "
                "and returns a ranked list of relevant chunk IDs and their scores.",
)
async def perform_sparse_search(
    request_data: schemas.SparseSearchRequest = Body(...),
    # X-Company-ID del body es prioritaria, pero el header se puede usar como fallback o verificación
    # header_company_id: uuid.UUID = Depends(get_required_company_id_header), # Ejemplo si se requiriera header
    use_case: SparseSearchUseCase = Depends(get_sparse_search_use_case),
    # request: Request # Para X-Request-ID, se puede añadir con middleware
):
    # Si se usa X-Company-ID del header y se quiere validar contra el body:
    # if request_data.company_id != header_company_id:
    #     log.warning("Mismatch between X-Company-ID header and request body company_id.",
    #                 header_cid=str(header_company_id), body_cid=str(request_data.company_id))
    #     raise HTTPException(
    #         status_code=status.HTTP_400_BAD_REQUEST,
    #         detail="X-Company-ID header does not match company_id in request body."
    #     )

    endpoint_log = log.bind(
        action="perform_sparse_search_endpoint",
        company_id=str(request_data.company_id),
        query_preview=request_data.query[:50] + "...",
        requested_top_k=request_data.top_k
    )
    endpoint_log.info("Sparse search request received.")

    try:
        search_results_domain = await use_case.execute(
            query=request_data.query,
            company_id=request_data.company_id,
            top_k=request_data.top_k
        )
        
        # Mapear resultados del dominio (ya son SparseSearchResultItem) a la respuesta de la API
        # (el schema SparseSearchResponse espera una lista de SparseSearchResultItem)
        
        response_data = schemas.SparseSearchResponse(
            query=request_data.query,
            company_id=request_data.company_id,
            results=search_results_domain # Ya está en el formato correcto
        )
        
        endpoint_log.info(f"Sparse search successful. Returning {len(search_results_domain)} results.")
        return response_data

    except ConnectionError as ce: # Errores de conexión a DB desde el use case
        endpoint_log.error("Service dependency (Database) unavailable.", error_details=str(ce), exc_info=False)
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=f"A critical service dependency is unavailable: {ce}")
    except ValueError as ve: # Errores de validación o datos inválidos
        endpoint_log.warning("Invalid input or data processing error during sparse search.", error_details=str(ve), exc_info=True)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Data processing error: {ve}")
    except RuntimeError as re: # Errores genéricos del use case/adapter que no son ConnectionError
        endpoint_log.error("Runtime error during sparse search execution.", error_details=str(re), exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"An internal error occurred: {re}")
    except Exception as e:
        endpoint_log.exception("Unexpected error during sparse search.") # Log con traceback completo
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="An unexpected internal server error occurred.")
```

## File: `app\api\v1\schemas.py`
```py
# sparse-search-service/app/api/v1/schemas.py
import uuid
from pydantic import BaseModel, Field, conint
from typing import List, Optional, Dict, Any

from app.domain.models import SparseSearchResultItem # Reutilizar el modelo de dominio

# --- Request Schemas ---

class SparseSearchRequest(BaseModel):
    query: str = Field(..., min_length=1, description="La consulta del usuario en lenguaje natural.")
    company_id: uuid.UUID = Field(..., description="El ID de la compañía para la cual realizar la búsqueda.")
    top_k: conint(gt=0, le=200) = Field(default=10, description="El número máximo de resultados a devolver.")
    # metadata_filter: Optional[Dict[str, Any]] = Field(None, description="Filtros de metadatos adicionales (futuro).")

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "query": "cómo configuro las notificaciones?",
                    "company_id": "a1b2c3d4-e5f6-7890-1234-567890abcdef",
                    "top_k": 5
                }
            ]
        }
    }

# --- Response Schemas ---

class SparseSearchResponse(BaseModel):
    query: str = Field(..., description="La consulta original enviada.")
    company_id: uuid.UUID = Field(..., description="El ID de la compañía para la cual se realizó la búsqueda.")
    results: List[SparseSearchResultItem] = Field(default_factory=list, description="Lista de chunks relevantes encontrados, ordenados por score descendente.")
    # performance_ms: Optional[float] = Field(None, description="Tiempo tomado para la búsqueda en milisegundos.")
    # index_info: Optional[Dict[str, Any]] = Field(None, description="Información sobre el índice BM25 utilizado (e.g., tamaño, fecha de creación).")

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "query": "cómo configuro las notificaciones?",
                    "company_id": "a1b2c3d4-e5f6-7890-1234-567890abcdef",
                    "results": [
                        {"chunk_id": "doc_abc_chunk_3", "score": 15.76},
                        {"chunk_id": "doc_xyz_chunk_12", "score": 12.33}
                    ]
                }
            ]
        }
    }

class HealthCheckResponse(BaseModel):
    status: str = Field(default="ok", description="Overall status of the service ('ok' or 'error').")
    service: str = Field(..., description="Name of the service.")
    ready: bool = Field(..., description="Indicates if the service is ready to serve requests (dependencies are OK).")
    dependencies: Dict[str, str] = Field(..., description="Status of critical dependencies (e.g., 'PostgreSQL': 'ok'/'error').")
    # bm2s_available: bool = Field(..., description="Indicates if the bm2s library was successfully imported.")
```

## File: `app\application\__init__.py`
```py

```

## File: `app\application\ports\__init__.py`
```py

```

## File: `app\application\ports\repository_ports.py`
```py
# sparse-search-service/app/application/ports/repository_ports.py
import abc
import uuid
from typing import Dict, List, Optional

class ChunkContentRepositoryPort(abc.ABC):
    """
    Puerto abstracto para obtener contenido textual de chunks desde la persistencia.
    Este servicio necesita esto para construir los índices BM25.
    """

    @abc.abstractmethod
    async def get_chunk_contents_by_company(self, company_id: uuid.UUID) -> Dict[str, str]:
        """
        Obtiene un diccionario de {chunk_id: content} para una compañía específica.
        El `chunk_id` aquí se espera que sea el `embedding_id` o `pk_id` que se utiliza
        como identificador único del chunk en el sistema de búsqueda vectorial y logging.

        Args:
            company_id: El UUID de la compañía.

        Returns:
            Un diccionario donde las claves son los IDs de los chunks (str) y los valores
            son el contenido textual de dichos chunks (str).

        Raises:
            ConnectionError: Si hay problemas de comunicación con la base de datos.
            Exception: Para otros errores inesperados.
        """
        raise NotImplementedError

    @abc.abstractmethod
    async def get_chunks_with_metadata_by_company(
        self, company_id: uuid.UUID
    ) -> List[Dict[str, Any]]:
        """
        Obtiene una lista de chunks para una compañía, cada uno como un diccionario
        que incluye 'id' (el embedding_id/pk_id), 'content', y opcionalmente
        otros metadatos relevantes para BM25 si se quisieran usar para filtrar
        pre-indexación o post-búsqueda (aunque BM25 puro es sobre contenido).

        Args:
            company_id: El UUID de la compañía.

        Returns:
            Una lista de diccionarios, cada uno representando un chunk con al menos
            {'id': str, 'content': str}.
        """
        raise NotImplementedError
```

## File: `app\application\ports\sparse_search_port.py`
```py
# sparse-search-service/app/application/ports/sparse_search_port.py
import abc
import uuid
from typing import List, Tuple, Dict, Any

from app.domain.models import SparseSearchResultItem # Reutilizar el modelo de dominio

class SparseSearchPort(abc.ABC):
    """
    Puerto abstracto para realizar búsquedas dispersas (como BM25).
    """

    @abc.abstractmethod
    async def search(
        self,
        query: str,
        company_id: uuid.UUID,
        corpus_chunks: List[Dict[str, Any]], # Lista de chunks [{'id': str, 'content': str}, ...]
        top_k: int
    ) -> List[SparseSearchResultItem]:
        """
        Realiza una búsqueda dispersa en el corpus de chunks proporcionado.

        Args:
            query: La consulta del usuario.
            company_id: El ID de la compañía (para logging o contexto, aunque el corpus ya está filtrado).
            corpus_chunks: Una lista de diccionarios, donde cada diccionario representa
                           un chunk y debe contener al menos las claves 'id' (str, único)
                           y 'content' (str).
            top_k: El número máximo de resultados a devolver.

        Returns:
            Una lista de objetos SparseSearchResultItem, ordenados por relevancia descendente.

        Raises:
            ValueError: Si los datos de entrada son inválidos.
            Exception: Para errores inesperados durante la búsqueda.
        """
        raise NotImplementedError

    @abc.abstractmethod
    async def initialize_engine(self) -> None:
        """
        Método para inicializar cualquier componente pesado del motor de búsqueda,
        como cargar modelos o verificar dependencias. Se llama durante el startup.
        """
        raise NotImplementedError
```

## File: `app\application\use_cases\__init__.py`
```py

```

## File: `app\application\use_cases\sparse_search_use_case.py`
```py
# sparse-search-service/app/application/use_cases/sparse_search_use_case.py
import uuid
import structlog
from typing import List, Dict, Any, Optional

from app.domain.models import SparseSearchResultItem
from app.application.ports.repository_ports import ChunkContentRepositoryPort
from app.application.ports.sparse_search_port import SparseSearchPort
from app.core.config import settings # Si se necesita algún ajuste del use case

log = structlog.get_logger(__name__)

class SparseSearchUseCase:
    def __init__(
        self,
        chunk_content_repo: ChunkContentRepositoryPort,
        sparse_search_engine: SparseSearchPort # e.g., BM25Adapter
    ):
        self.chunk_content_repo = chunk_content_repo
        self.sparse_search_engine = sparse_search_engine
        log.info(
            "SparseSearchUseCase initialized",
            chunk_repo_type=type(chunk_content_repo).__name__,
            search_engine_type=type(sparse_search_engine).__name__
        )

    async def execute(
        self,
        query: str,
        company_id: uuid.UUID,
        top_k: int
    ) -> List[SparseSearchResultItem]:
        use_case_log = log.bind(
            use_case="SparseSearchUseCase",
            action="execute",
            company_id=str(company_id),
            query_preview=query[:50] + "...",
            requested_top_k=top_k
        )
        use_case_log.info("Executing sparse search.")

        try:
            # 1. Obtener el corpus de chunks para la compañía.
            # El repositorio debe devolver una lista de diccionarios, cada uno con 'id' y 'content'.
            use_case_log.debug("Fetching corpus chunks for company from repository...")
            corpus_chunks: List[Dict[str, Any]] = await self.chunk_content_repo.get_chunks_with_metadata_by_company(company_id)

            if not corpus_chunks:
                use_case_log.warning("No corpus chunks found for the company. Returning empty results.")
                return []
            
            use_case_log.info(f"Retrieved {len(corpus_chunks)} chunks for company to search.")

            # 2. Realizar la búsqueda usando el motor de búsqueda dispersa (BM25Adapter).
            use_case_log.debug("Performing search with sparse search engine...")
            search_results: List[SparseSearchResultItem] = await self.sparse_search_engine.search(
                query=query,
                company_id=company_id, # Pasa company_id para logging en el adapter
                corpus_chunks=corpus_chunks,
                top_k=top_k
            )

            use_case_log.info(f"Sparse search executed. Found {len(search_results)} results.")
            return search_results

        except ConnectionError as e: # Específicamente para errores de DB al obtener el corpus
            use_case_log.error(
                "Database connection error while fetching corpus for sparse search.",
                error_details=str(e),
                exc_info=False # No incluir traceback completo para ConnectionError
            )
            # Esto debería resultar en una respuesta 503 Service Unavailable en el endpoint.
            raise # Re-lanzar para que el endpoint lo maneje.
        except ValueError as ve: # Errores de validación (e.g., del motor de búsqueda si el input es malo)
            use_case_log.warning("Value error during sparse search execution.", error_details=str(ve), exc_info=True)
            # Esto podría ser un 400 Bad Request si el error es por la query, o 500 si es interno.
            raise # Re-lanzar
        except Exception as e:
            use_case_log.exception("An unexpected error occurred during sparse search execution.")
            # Esto debería ser un 500 Internal Server Error.
            raise # Re-lanzar
```

## File: `app\core\__init__.py`
```py

```

## File: `app\core\config.py`
```py
# sparse-search-service/app/core/config.py
import logging
import os
import sys
import json
from typing import Optional, Dict, Any
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field, field_validator, SecretStr, ValidationInfo, ValidationError

# --- Default Values ---
POSTGRES_K8S_HOST_DEFAULT = "postgresql-service.nyro-develop.svc.cluster.local"
POSTGRES_K8S_PORT_DEFAULT = 5432
POSTGRES_K8S_DB_DEFAULT = "atenex"
POSTGRES_K8S_USER_DEFAULT = "postgres"

DEFAULT_SERVICE_PORT = 8004

# BM25 Default Parameters (bm2s usa sus propios defaults si no se especifican)
# k1 ≈ 1.2-2.0, b ≈ 0.75
# No los configuraremos aquí explícitamente a menos que sea necesario anular los de bm2s.

class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file='.env',
        env_prefix='SPARSE_',
        env_file_encoding='utf-8',
        case_sensitive=False,
        extra='ignore'
    )

    # --- General ---
    PROJECT_NAME: str = "Atenex Sparse Search Service"
    API_V1_STR: str = "/api/v1"
    LOG_LEVEL: str = Field(default="INFO")
    PORT: int = Field(default=DEFAULT_SERVICE_PORT)

    # --- Database (PostgreSQL) ---
    # Requerido para obtener el contenido de los chunks para indexar con BM25
    POSTGRES_USER: str = Field(default=POSTGRES_K8S_USER_DEFAULT)
    POSTGRES_PASSWORD: SecretStr
    POSTGRES_SERVER: str = Field(default=POSTGRES_K8S_HOST_DEFAULT)
    POSTGRES_PORT: int = Field(default=POSTGRES_K8S_PORT_DEFAULT)
    POSTGRES_DB: str = Field(default=POSTGRES_K8S_DB_DEFAULT)
    DB_POOL_MIN_SIZE: int = Field(default=2)
    DB_POOL_MAX_SIZE: int = Field(default=10)
    DB_CONNECT_TIMEOUT: int = Field(default=30) # segundos
    DB_COMMAND_TIMEOUT: int = Field(default=60) # segundos


    # --- Cache for BM25 Indexes (Opcional, podría implementarse más adelante) ---
    # INDEX_CACHE_ENABLED: bool = Field(default=False)
    # INDEX_CACHE_TTL_SECONDS: int = Field(default=3600) # 1 hora
    # INDEX_CACHE_MAX_SIZE: int = Field(default=100)    # Max 100 company indexes

    @field_validator('LOG_LEVEL', mode='before')
    @classmethod
    def normalize_log_level(cls, v: str) -> str:
        return v.upper()

    @field_validator('LOG_LEVEL')
    @classmethod
    def check_log_level(cls, v: str) -> str:
        valid_levels = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
        if v not in valid_levels:
            raise ValueError(f"Invalid LOG_LEVEL '{v}'. Must be one of {valid_levels}")
        return v

    @field_validator('POSTGRES_PASSWORD', mode='before')
    @classmethod
    def check_postgres_password(cls, v: Any, info: ValidationInfo) -> Any:
        if isinstance(v, SecretStr):
            secret_value = v.get_secret_value()
            if secret_value is None or secret_value == "":
                raise ValueError(f"Required secret field 'SPARSE_POSTGRES_PASSWORD' cannot be empty.")
        elif v is None or v == "":
            raise ValueError(f"Required secret field 'SPARSE_POSTGRES_PASSWORD' cannot be empty.")
        return v

# --- Global Settings Instance ---
temp_log = logging.getLogger("sparse_search_service.config.loader")
if not temp_log.handlers: # Evitar duplicar handlers si se recarga
    handler = logging.StreamHandler(sys.stdout)
    formatter = logging.Formatter('%(levelname)s: [%(asctime)s] [%(name)s] %(message)s')
    handler.setFormatter(formatter)
    temp_log.addHandler(handler)
    temp_log.setLevel(logging.INFO) # Default to INFO for config loading phase

try:
    temp_log.info("Loading Sparse Search Service settings...")
    settings = Settings()
    temp_log.info("--- Sparse Search Service Settings Loaded ---")

    excluded_fields = {'POSTGRES_PASSWORD'}
    log_data = settings.model_dump(exclude=excluded_fields)

    for key, value in log_data.items():
        temp_log.info(f"  {key.upper()}: {value}")

    pg_pass_status = '*** SET ***' if settings.POSTGRES_PASSWORD and settings.POSTGRES_PASSWORD.get_secret_value() else '!!! NOT SET !!!'
    temp_log.info(f"  POSTGRES_PASSWORD: {pg_pass_status}")
    temp_log.info(f"------------------------------------")

except (ValidationError, ValueError) as e:
    error_details = ""
    if isinstance(e, ValidationError):
        try: error_details = f"\nValidation Errors:\n{json.dumps(e.errors(), indent=2)}"
        except Exception: error_details = f"\nRaw Errors: {e}"
    else: error_details = f"\nError: {e}"

    temp_log.critical(f"!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
    temp_log.critical(f"! FATAL: Sparse Search Service configuration validation failed!{error_details}")
    temp_log.critical(f"! Check environment variables (prefixed with SPARSE_) or .env file.")
    temp_log.critical(f"!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
    sys.exit(1)
except Exception as e:
    temp_log.exception(f"FATAL: Unexpected error loading Sparse Search Service settings: {e}")
    sys.exit(1)
```

## File: `app\core\logging_config.py`
```py
# sparse-search-service/app/core/logging_config.py
import logging
import sys
import structlog
from app.core.config import settings # Asegúrate que 'settings' se cargue correctamente

def setup_logging():
    """Configura el logging estructurado con structlog."""
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

    # Evitar añadir handler múltiples veces
    if not any(isinstance(h, logging.StreamHandler) and isinstance(h.formatter, structlog.stdlib.ProcessorFormatter) for h in root_logger.handlers):
        # root_logger.handlers.clear() # Descomentar con precaución
        root_logger.addHandler(handler)

    # Establecer el nivel de log ANTES de que structlog intente usarlo
    try:
        effective_log_level = settings.LOG_LEVEL.upper()
        if effective_log_level not in ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]:
            effective_log_level = "INFO" # Fallback seguro
            logging.getLogger("sparse_search_service_early_log").warning(f"Invalid LOG_LEVEL '{settings.LOG_LEVEL}', defaulting to 'INFO'.")
    except AttributeError: # Si settings aún no está completamente cargado
        effective_log_level = "INFO"
        logging.getLogger("sparse_search_service_early_log").warning("Settings not fully loaded during logging setup, defaulting log level to 'INFO'.")

    root_logger.setLevel(effective_log_level)


    # Silenciar bibliotecas verbosas
    logging.getLogger("uvicorn").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("gunicorn").setLevel(logging.INFO)
    logging.getLogger("asyncpg").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING) # Si se usa httpx

    # Logger específico para este servicio
    log = structlog.get_logger("sparse_search_service")
    # Este log puede que no aparezca si el nivel global es más restrictivo en el momento de esta llamada
    log.info("Logging configured for Sparse Search Service", log_level=effective_log_level)
```

## File: `app\dependencies.py`
```py
# sparse-search-service/app/dependencies.py
"""
Centralized dependency injection for the Sparse Search Service.
Instances are initialized during application startup (lifespan).
"""
from fastapi import HTTPException, status
from typing import Optional
import structlog

# Ports
from app.application.ports.repository_ports import ChunkContentRepositoryPort
from app.application.ports.sparse_search_port import SparseSearchPort

# Use Cases
from app.application.use_cases.sparse_search_use_case import SparseSearchUseCase

log = structlog.get_logger(__name__)

# Global instances to be populated at startup
_chunk_content_repo_instance: Optional[ChunkContentRepositoryPort] = None
_sparse_search_engine_instance: Optional[SparseSearchPort] = None
_sparse_search_use_case_instance: Optional[SparseSearchUseCase] = None
_service_ready_flag: bool = False

def set_global_dependencies(
    chunk_repo: ChunkContentRepositoryPort,
    search_engine: SparseSearchPort,
    use_case: SparseSearchUseCase,
    service_ready: bool
):
    global _chunk_content_repo_instance, _sparse_search_engine_instance
    global _sparse_search_use_case_instance, _service_ready_flag

    _chunk_content_repo_instance = chunk_repo
    _sparse_search_engine_instance = search_engine
    _sparse_search_use_case_instance = use_case
    _service_ready_flag = service_ready
    log.debug("Global dependencies set in sparse-search-service.dependencies", service_ready=_service_ready_flag)

# --- Getter functions for FastAPI Depends ---

def get_chunk_content_repository() -> ChunkContentRepositoryPort:
    if not _service_ready_flag or not _chunk_content_repo_instance:
        log.critical("Attempted to get ChunkContentRepository before service is ready or instance is None.")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Chunk content repository is not available at the moment."
        )
    return _chunk_content_repo_instance

def get_sparse_search_engine() -> SparseSearchPort:
    if not _service_ready_flag or not _sparse_search_engine_instance:
        log.critical("Attempted to get SparseSearchEngine before service is ready or instance is None.")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Sparse search engine is not available at the moment."
        )
    return _sparse_search_engine_instance

def get_sparse_search_use_case() -> SparseSearchUseCase:
    if not _service_ready_flag or not _sparse_search_use_case_instance:
        log.critical("Attempted to get SparseSearchUseCase before service is ready or instance is None.")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Sparse search processing service is not ready."
        )
    return _sparse_search_use_case_instance

def get_service_status() -> bool:
    """Returns the current readiness status of the service."""
    return _service_ready_flag
```

## File: `app\domain\__init__.py`
```py

```

## File: `app\domain\models.py`
```py
# sparse-search-service/app/domain/models.py
import uuid
from pydantic import BaseModel, Field
from typing import Optional, List, Dict, Any

class SparseSearchResultItem(BaseModel):
    """
    Representa un único item de resultado de la búsqueda dispersa (BM25).
    Contiene el ID del chunk y su score de relevancia.
    """
    chunk_id: str = Field(..., description="El ID único del chunk (generalmente el embedding_id o pk_id de Milvus/PostgreSQL).")
    score: float = Field(..., description="La puntuación de relevancia asignada por el algoritmo BM25.")
    # No se incluye el contenido aquí para mantener el servicio enfocado.
    # El servicio que consume este resultado (e.g., Query Service)
    # será responsable de obtener el contenido si es necesario.

class CompanyCorpusStats(BaseModel):
    """
    Estadísticas sobre el corpus de una compañía utilizado para la indexación BM25.
    """
    company_id: uuid.UUID
    total_chunks_in_db: int
    chunks_indexed_in_bm25: int
    last_indexed_at: Optional[Any] # datetime, pero Any por si se usa timestamp numérico
    index_size_bytes: Optional[int] # Estimación del tamaño del índice en memoria
```

## File: `app\gunicorn_conf.py`
```py
# sparse-search-service/app/gunicorn_conf.py
import os
import multiprocessing

# --- Server Mechanics ---
# bind = f"0.0.0.0:{os.environ.get('PORT', '8004')}" # FastAPI/Uvicorn main.py reads PORT
# Gunicorn will use the PORT env var by default if not specified with -b

# --- Worker Processes ---
# Autotune based on CPU cores if GUNICORN_PROCESSES is not set
default_workers = (multiprocessing.cpu_count() * 2) + 1
workers = int(os.environ.get('GUNICORN_PROCESSES', str(default_workers)))
if workers <= 0: workers = default_workers

# Threads per worker (UvicornWorker is async, so threads are less critical but can help with blocking I/O)
threads = int(os.environ.get('GUNICORN_THREADS', '1')) # Default to 1 for async workers, can be increased.

worker_class = 'uvicorn.workers.UvicornWorker'
worker_tmp_dir = "/dev/shm" # Use shared memory for worker temp files

# --- Logging ---
# Gunicorn's log level for its own messages. App logs are handled by structlog.
loglevel = os.environ.get('GUNICORN_LOG_LEVEL', 'info').lower()
accesslog = '-' # Log to stdout
errorlog = '-'  # Log to stderr

# --- Process Naming ---
# proc_name = 'sparse-search-service' # Set a process name

# --- Timeouts ---
timeout = int(os.environ.get('GUNICORN_TIMEOUT', '120')) # Default worker timeout
graceful_timeout = int(os.environ.get('GUNICORN_GRACEFUL_TIMEOUT', '30')) # Timeout for graceful shutdown
keepalive = int(os.environ.get('GUNICORN_KEEPALIVE', '5')) # HTTP Keep-Alive header timeout

# --- Security ---
# forward_allow_ips = '*' # Trust X-Forwarded-* headers from all proxies (common in K8s)

# --- Raw Environment Variables for Workers ---
# Pass application-specific log level to Uvicorn workers
# This ensures Uvicorn itself respects the log level set for the application.
# The app's structlog setup will use SPARSE_LOG_LEVEL from the environment.
# This raw_env is for Gunicorn to pass to Uvicorn workers if Uvicorn uses it.
raw_env = [
    f"SPARSE_LOG_LEVEL={os.environ.get('SPARSE_LOG_LEVEL', 'INFO')}",
    # Add other env vars if needed by workers specifically at this stage
]

# Example of print statements to verify Gunicorn config during startup (remove for production)
print(f"[Gunicorn Config] Workers: {workers}")
print(f"[Gunicorn Config] Threads: {threads}")
print(f"[Gunicorn Config] Log Level (Gunicorn): {loglevel}")
print(f"[Gunicorn Config] App Log Level (SPARSE_LOG_LEVEL for Uvicorn worker): {os.environ.get('SPARSE_LOG_LEVEL', 'INFO')}")
```

## File: `app\infrastructure\__init__.py`
```py

```

## File: `app\infrastructure\persistence\__init__.py`
```py

```

## File: `app\infrastructure\persistence\postgres_connector.py`
```py
# sparse-search-service/app/infrastructure/persistence/postgres_connector.py
import asyncpg
import structlog
import json
from typing import Optional

from app.core.config import settings

log = structlog.get_logger(__name__) # logger específico para el conector

_pool: Optional[asyncpg.Pool] = None

async def get_db_pool() -> asyncpg.Pool:
    """Gets the existing asyncpg pool or creates a new one for Sparse Search Service."""
    global _pool
    if _pool is None or _pool._closed:
        connector_log = log.bind(
            service_context="SparseSearchPostgresConnector",
            host=settings.POSTGRES_SERVER,
            port=settings.POSTGRES_PORT,
            user=settings.POSTGRES_USER,
            db=settings.POSTGRES_DB
        )
        connector_log.info("Creating PostgreSQL connection pool...")
        try:
            # Función para configurar codecs JSON (opcional pero recomendado)
            def _json_encoder(value): return json.dumps(value)
            def _json_decoder(value): return json.loads(value)
            async def init_connection(conn):
                await conn.set_type_codec('jsonb', encoder=_json_encoder, decoder=_json_decoder, schema='pg_catalog', format='text')
                await conn.set_type_codec('json', encoder=_json_encoder, decoder=_json_decoder, schema='pg_catalog', format='text')
                connector_log.debug("JSON(B) type codecs configured for new connection.")

            _pool = await asyncpg.create_pool(
                user=settings.POSTGRES_USER,
                password=settings.POSTGRES_PASSWORD.get_secret_value(),
                database=settings.POSTGRES_DB,
                host=settings.POSTGRES_SERVER,
                port=settings.POSTGRES_PORT,
                min_size=settings.DB_POOL_MIN_SIZE,
                max_size=settings.DB_POOL_MAX_SIZE,
                timeout=settings.DB_CONNECT_TIMEOUT, # Timeout para establecer una conexión
                command_timeout=settings.DB_COMMAND_TIMEOUT, # Timeout para ejecutar un comando
                init=init_connection, # Función para ejecutar en nuevas conexiones
                statement_cache_size=0 # Deshabilitar cache de statements si hay problemas o se prefiere simplicidad
            )
            connector_log.info("PostgreSQL connection pool created successfully.")
        except (asyncpg.exceptions.InvalidPasswordError, OSError, ConnectionRefusedError) as conn_err:
            connector_log.critical("CRITICAL: Failed to connect to PostgreSQL.", error_details=str(conn_err), exc_info=False) # No exc_info para errores comunes
            _pool = None # Asegurar que el pool es None si falla
            raise ConnectionError(f"Failed to connect to PostgreSQL for Sparse Search Service: {conn_err}") from conn_err
        except Exception as e:
            connector_log.critical("CRITICAL: Unexpected error creating PostgreSQL connection pool.", error_details=str(e), exc_info=True)
            _pool = None
            raise RuntimeError(f"Failed to create PostgreSQL pool for Sparse Search Service: {e}") from e
    return _pool

async def close_db_pool():
    """Closes the asyncpg connection pool for Sparse Search Service."""
    global _pool
    connector_log = log.bind(service_context="SparseSearchPostgresConnector")
    if _pool and not _pool._closed:
        connector_log.info("Closing PostgreSQL connection pool...")
        try:
            await _pool.close()
            connector_log.info("PostgreSQL connection pool closed successfully.")
        except Exception as e:
            connector_log.error("Error while closing PostgreSQL connection pool.", error_details=str(e), exc_info=True)
        finally:
            _pool = None
    elif _pool and _pool._closed:
        connector_log.warning("Attempted to close an already closed PostgreSQL pool.")
        _pool = None # Asegurar que esté limpio
    else:
        connector_log.info("No active PostgreSQL connection pool to close.")

async def check_db_connection() -> bool:
    """Checks if a connection to the database can be established."""
    pool = None
    conn = None
    connector_log = log.bind(service_context="SparseSearchPostgresConnector", action="check_db_connection")
    try:
        pool = await get_db_pool() # Esto intentará crear el pool si no existe
        conn = await pool.acquire() # Tomar una conexión del pool
        result = await conn.fetchval("SELECT 1")
        connector_log.debug("Database connection check successful (SELECT 1).", result=result)
        return result == 1
    except Exception as e:
        connector_log.error("Database connection check failed.", error_details=str(e), exc_info=False) # No exc_info aquí para no ser muy verboso
        return False
    finally:
        if conn and pool: # Asegurarse que pool no sea None si conn existe
             await pool.release(conn) # Devolver la conexión al pool
```

## File: `app\infrastructure\persistence\postgres_repositories.py`
```py
# sparse-search-service/app/infrastructure/persistence/postgres_repositories.py
import uuid
from typing import Any, Optional, Dict, List
import asyncpg
import structlog

from app.core.config import settings
from app.application.ports.repository_ports import ChunkContentRepositoryPort
from .postgres_connector import get_db_pool

log = structlog.get_logger(__name__) # Logger para este módulo

class PostgresChunkContentRepository(ChunkContentRepositoryPort):
    """
    Implementación concreta para obtener contenido de chunks desde PostgreSQL
    para el Sparse Search Service.
    """

    async def get_chunk_contents_by_company(self, company_id: uuid.UUID) -> Dict[str, str]:
        """
        Obtiene todos los chunks y sus contenidos para una compañía.
        El ID del chunk devuelto es `embedding_id` que se asume es el PK de Milvus.
        """
        repo_log = log.bind(
            repo="PostgresChunkContentRepository",
            action="get_chunk_contents_by_company",
            company_id=str(company_id)
        )
        repo_log.info("Fetching all chunk contents (keyed by embedding_id) for company.")

        # Query para obtener `embedding_id` (clave primaria de Milvus, usada como chunk_id aquí) y `content`
        # Asume que `documents.status = 'processed'` es un buen filtro para chunks válidos.
        query = """
        SELECT dc.embedding_id, dc.content
        FROM document_chunks dc
        JOIN documents d ON dc.document_id = d.id
        WHERE d.company_id = $1
          AND d.status = 'processed'  -- Solo de documentos procesados
          AND dc.embedding_id IS NOT NULL
          AND dc.content IS NOT NULL AND dc.content <> ''; -- Asegurar que hay contenido
        """
        pool = await get_db_pool()
        conn = None
        try:
            conn = await pool.acquire()
            rows = await conn.fetch(query, company_id)
            
            # Crear el diccionario {embedding_id: content}
            # embedding_id es el ID que usa el query-service para referirse a los chunks de Milvus
            # y es el que se espera para la fusión de resultados.
            contents = {row['embedding_id']: row['content'] for row in rows}
            
            repo_log.info(f"Retrieved content for {len(contents)} chunks (keyed by embedding_id).")
            if not contents:
                repo_log.warning("No chunk content found for the company or no documents are processed.", company_id=str(company_id))
            return contents
        except asyncpg.exceptions.PostgresConnectionError as db_conn_err:
            repo_log.error("Database connection error.", error_details=str(db_conn_err), exc_info=False)
            raise ConnectionError(f"Database connection error: {db_conn_err}") from db_conn_err
        except Exception as e:
            repo_log.exception("Failed to get chunk contents by company (keyed by embedding_id).")
            # No relanzar ConnectionError genéricamente, solo para errores de conexión explícitos.
            raise RuntimeError(f"Failed to retrieve chunk contents: {e}") from e
        finally:
            if conn:
                await pool.release(conn)

    async def get_chunks_with_metadata_by_company(
        self, company_id: uuid.UUID
    ) -> List[Dict[str, Any]]:
        """
        Obtiene una lista de chunks para una compañía, cada uno como un diccionario
        que incluye 'id' (el embedding_id/pk_id) y 'content'.
        """
        repo_log = log.bind(
            repo="PostgresChunkContentRepository",
            action="get_chunks_with_metadata_by_company",
            company_id=str(company_id)
        )
        repo_log.info("Fetching chunks with content (ID is embedding_id) for company.")

        query = """
        SELECT
            dc.embedding_id AS id,  -- Renombrar embedding_id a 'id' para consistencia con corpus_chunks
            dc.content
            -- Puedes añadir más metadatos de dc o d aquí si fueran necesarios para BM25
            -- Por ejemplo: dc.document_id, d.file_name
        FROM document_chunks dc
        JOIN documents d ON dc.document_id = d.id
        WHERE d.company_id = $1
          AND d.status = 'processed'
          AND dc.embedding_id IS NOT NULL
          AND dc.content IS NOT NULL AND dc.content <> '';
        """
        pool = await get_db_pool()
        conn = None
        try:
            conn = await pool.acquire()
            rows = await conn.fetch(query, company_id)
            
            # Convertir cada fila a un diccionario
            # El contrato es List[Dict[str, Any]] donde cada Dict tiene 'id' y 'content'
            chunk_list = [{'id': row['id'], 'content': row['content']} for row in rows]
            
            repo_log.info(f"Retrieved {len(chunk_list)} chunks with their content.")
            if not chunk_list:
                repo_log.warning("No chunks with content found for the company or no documents processed.", company_id=str(company_id))
            return chunk_list
        except asyncpg.exceptions.PostgresConnectionError as db_conn_err:
            repo_log.error("Database connection error.", error_details=str(db_conn_err), exc_info=False)
            raise ConnectionError(f"Database connection error: {db_conn_err}") from db_conn_err
        except Exception as e:
            repo_log.exception("Failed to get chunks with metadata by company.")
            raise RuntimeError(f"Failed to retrieve chunks with metadata: {e}") from e
        finally:
            if conn:
                await pool.release(conn)
```

## File: `app\infrastructure\sparse_retrieval\__init__.py`
```py

```

## File: `app\infrastructure\sparse_retrieval\bm25_adapter.py`
```py
# sparse-search-service/app/infrastructure/sparse_retrieval/bm25_adapter.py
import structlog
import asyncio
import time
import uuid
from typing import List, Tuple, Dict, Any, Optional

try:
    import bm2s
except ImportError:
    bm2s = None # Manejar dependencia opcional

from app.application.ports.sparse_search_port import SparseSearchPort
from app.domain.models import SparseSearchResultItem
from app.core.config import settings # Si se necesita alguna config específica de BM25

log = structlog.get_logger(__name__)

class BM25Adapter(SparseSearchPort):
    """
    Implementación de SparseSearchPort usando la librería bm2s.
    Este adaptador construye un índice BM25 en memoria basado en el `corpus_chunks`
    proporcionado en cada llamada a `search`. Es sin estado entre llamadas
    con respecto a los índices.
    """

    def __init__(self):
        self._bm2s_available = False
        if bm2s is None:
            log.error(
                "bm2s library not installed. BM25 search functionality will be UNAVAILABLE. "
                "Install with: poetry add bm2s"
            )
        else:
            self._bm2s_available = True
            log.info("BM25Adapter initialized. bm2s library is available.")

    async def initialize_engine(self) -> None:
        """
        Verifica la disponibilidad de la librería bm2s.
        No hay una inicialización pesada del motor BM25 en este adaptador
        ya que los índices se construyen por solicitud.
        """
        if not self._bm2s_available:
            log.warning("BM25 engine (bm2s library) not available. Search will fail.")
        else:
            log.info("BM25 engine (bm2s library) available and ready.")

    def is_available(self) -> bool:
        """Retorna True si la librería bm2s está disponible."""
        return self._bm2s_available

    async def search(
        self,
        query: str,
        company_id: uuid.UUID, # Se usa para logging
        corpus_chunks: List[Dict[str, Any]], # Formato: [{'id': str, 'content': str}, ...]
        top_k: int
    ) -> List[SparseSearchResultItem]:
        """
        Busca chunks usando BM25s. El corpus se proporciona directamente.
        """
        adapter_log = log.bind(
            adapter="BM25Adapter",
            action="search",
            company_id=str(company_id),
            query_preview=query[:50] + "...",
            num_corpus_chunks=len(corpus_chunks),
            top_k=top_k
        )

        if not self._bm2s_available:
            adapter_log.error("bm2s library not available. Cannot perform BM25 search.")
            # Devuelve una lista vacía para indicar fallo pero no detener el flujo completo
            # si el servicio consumidor puede manejarlo (e.g., solo usar búsqueda densa).
            # Sin embargo, para un servicio dedicado a búsqueda dispersa, esto podría ser un error 503.
            # Por ahora, se alinea con cómo lo manejaría el query_service (omitiría resultados BM25).
            return []

        if not corpus_chunks:
            adapter_log.warning("Corpus_chunks is empty. No data to search.")
            return []

        if not query.strip():
            adapter_log.warning("Query is empty. Returning no results.")
            return []

        start_time = time.monotonic()
        adapter_log.debug("Starting BM25 search...")

        try:
            # 1. Preparar corpus y mapeo de IDs
            # Los IDs son los `embedding_id` que vienen de la DB
            chunk_ids_list: List[str] = []
            corpus_texts: List[str] = []

            for chunk_data in corpus_chunks:
                chunk_id = chunk_data.get("id")
                content = chunk_data.get("content")
                if chunk_id and content and isinstance(content, str) and content.strip():
                    chunk_ids_list.append(str(chunk_id)) # Asegurar que el ID es string
                    corpus_texts.append(content)
                else:
                    adapter_log.warning(
                        "Skipping chunk due to missing ID, content, or invalid content type.",
                        chunk_data_preview={k: (str(v)[:30] + "..." if isinstance(v, str) and len(v)>30 else v) for k,v in chunk_data.items()}
                    )

            if not corpus_texts:
                adapter_log.warning("Corpus is empty after filtering invalid chunks. No search performed.")
                return []

            # 2. Tokenizar corpus y consulta usando bm2s
            # bm2s.tokenize maneja una lista de strings para el corpus
            # y un solo string para la consulta.
            # bm2s internamente usa tokenización por espacios y pasa a minúsculas por defecto.
            # Se pueden pasar parámetros de tokenización a `bm2s.BM25(...)` si es necesario.
            adapter_log.debug("Tokenizing query and corpus with bm2s defaults...")
            # No necesitamos tokenizar explícitamente antes si usamos los métodos de bm2s que aceptan strings.
            # query_tokens = bm2s.tokenize(query) # bm2s.tokenize puede tokenizar una sola cadena
            # corpus_tokens = bm2s.tokenize(corpus_texts) # o una lista de cadenas

            # 3. Crear el índice BM25s e indexar el corpus
            # Los parámetros k1 y b pueden ajustarse, usando los defaults de bm2s por ahora.
            # bm2s.BM25(corpus=corpus_texts) también es una opción para indexar directamente.
            retriever = bm2s.BM25() # O bm2s.BM25(k1=..., b=...)
            retriever.index(corpus_texts) # Indexa los textos directamente (bm2s tokeniza internamente)
            
            index_build_time = time.monotonic()
            adapter_log.debug(f"BM25s index built for {len(corpus_texts)} texts.",
                              duration_ms=(index_build_time - start_time) * 1000)

            # 4. Realizar la búsqueda
            # `retriever.retrieve` toma la consulta tokenizada (o string y la tokeniza) y el corpus tokenizado.
            # Alternativamente, si se indexaron strings, `retrieve` también puede tomar un string de consulta.
            # El método `retrieve` espera una lista de consultas tokenizadas (o una lista con una sola consulta).
            # Devolverá una tupla (indices, scores) donde cada elemento es una lista (una por consulta).
            
            # Pasar la consulta como string, bm2s la tokenizará.
            # El corpus ya está indexado.
            results_indices_per_query, results_scores_per_query = retriever.retrieve(
                query, # Pasar la consulta como string
                k=top_k
            )

            # Como solo hay una consulta, tomamos el primer (y único) elemento de cada lista
            doc_indices: List[int] = results_indices_per_query[0]
            scores: List[float] = results_scores_per_query[0]
            
            retrieval_time = time.monotonic()
            adapter_log.debug(f"BM25s retrieval complete. Hits found: {len(doc_indices)}.",
                              duration_ms=(retrieval_time - index_build_time) * 1000)

            # 5. Mapear resultados a SparseSearchResultItem
            final_results: List[SparseSearchResultItem] = []
            for i, score_val in zip(doc_indices, scores):
                if 0 <= i < len(chunk_ids_list):
                    original_chunk_id = chunk_ids_list[i]
                    final_results.append(
                        SparseSearchResultItem(chunk_id=original_chunk_id, score=float(score_val))
                    )
                else:
                    adapter_log.error(
                        "BM25s returned index out of bounds.",
                        returned_index=i,
                        corpus_size=len(chunk_ids_list)
                    )
            
            total_duration_ms = (time.monotonic() - start_time) * 1000
            adapter_log.info(
                f"BM25 search finished. Returning {len(final_results)} results.",
                total_duration_ms=round(total_duration_ms, 2)
            )
            return final_results

        except Exception as e:
            adapter_log.exception("Error during BM25 search processing.")
            # En un servicio dedicado, un error aquí podría justificar un 500.
            # Por ahora, devolvemos lista vacía para consistencia con la falta de bm2s.
            # raise RuntimeError(f"BM25 search failed unexpectedly: {e}") from e
            return []
```

## File: `app\main.py`
```py
# sparse-search-service/app/main.py
from fastapi import FastAPI, HTTPException, status as fastapi_status, Request
from fastapi.exceptions import RequestValidationError, ResponseValidationError
from fastapi.responses import JSONResponse, PlainTextResponse
import structlog
import uvicorn # Para ejecución local
import logging # Para configuración inicial
import sys
import asyncio
import json
import uuid # Para X-Request-ID
from contextlib import asynccontextmanager
from typing import Optional, Annotated

from app.core.config import settings # Importar settings primero
from app.core.logging_config import setup_logging

# --- Setup Logging ---
# Es crucial que setup_logging se llame ANTES de que otros módulos intenten usar structlog
# y ANTES de que se cargue 'settings' completamente si logging_config depende de ello.
# En este caso, config.py carga settings, y logging_config.py usa settings.
# El orden de importación en Python generalmente maneja esto, pero ser explícito es bueno.
# setup_logging() se llamará después de que 'settings' esté disponible.
setup_logging() # Configura el logging basado en 'settings' ya cargado
main_log = structlog.get_logger("sparse_search_service.main") # Logger específico para main


# --- Import Routers ---
from app.api.v1.endpoints import search_endpoint

# --- Import Ports, Adapters, and Use Cases for Lifespan ---
from app.application.ports.repository_ports import ChunkContentRepositoryPort
from app.application.ports.sparse_search_port import SparseSearchPort
from app.infrastructure.persistence.postgres_repositories import PostgresChunkContentRepository
from app.infrastructure.sparse_retrieval.bm25_adapter import BM25Adapter
from app.application.use_cases.sparse_search_use_case import SparseSearchUseCase

# --- Database Connector ---
from app.infrastructure.persistence import postgres_connector

# --- Dependency Management ---
from app.dependencies import set_global_dependencies, get_service_status

# --- Global State (Application lifespan will manage these) ---
# Se usan las funciones de app.dependencies para setear/obtener estas instancias.

SERVICE_NAME = settings.PROJECT_NAME

# --- Lifespan Manager (Startup and Shutdown) ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    main_log.info(f"Starting up {SERVICE_NAME}...")
    service_ready = False
    critical_failure_message = ""

    # Instancias a inicializar
    chunk_repo: Optional[ChunkContentRepositoryPort] = None
    search_engine: Optional[SparseSearchPort] = None
    search_use_case: Optional[SparseSearchUseCase] = None

    # 1. Initialize PostgreSQL Connection Pool
    try:
        await postgres_connector.get_db_pool() # Crea el pool si no existe
        db_ready = await postgres_connector.check_db_connection()
        if db_ready:
            main_log.info("PostgreSQL connection pool initialized and verified.")
            chunk_repo = PostgresChunkContentRepository()
        else:
            critical_failure_message = "Failed PostgreSQL connection verification during startup."
            main_log.critical(critical_failure_message)
            # No salir todavía, permitir que el health check falle
    except ConnectionError as e_pg_conn: # Específico para fallos de conexión al crear el pool
        critical_failure_message = f"CRITICAL: Failed to connect to PostgreSQL during startup: {e_pg_conn}"
        main_log.critical(critical_failure_message, error_details=str(e_pg_conn))
    except Exception as e_pg_init:
        critical_failure_message = f"CRITICAL: Failed PostgreSQL pool initialization: {e_pg_init}"
        main_log.critical(critical_failure_message, error_details=str(e_pg_init), exc_info=True)

    if critical_failure_message: # Si la DB falló, no continuar con componentes dependientes
        main_log.error("Aborting further initialization due to database failure.")
        set_global_dependencies(None, None, None, False) # Marcar servicio como no listo
        # yield # Permitir que FastAPI inicie para que los health checks fallen correctamente
        # Se comenta el yield aquí para que no intente correr la app si la DB es crítica.
        # Sin embargo, para K8s, es mejor que el pod inicie y falle el health check.
        # Así que, permitimos que el lifespan continúe y el health check se encargue.
    else: # Si la DB está OK, continuar
        # 2. Initialize Sparse Search Engine (BM25 Adapter)
        try:
            search_engine = BM25Adapter()
            await search_engine.initialize_engine() # Verifica bm2s
            if not search_engine.is_available(): # BM25Adapter tiene este método
                 # Esto no es un fallo crítico del servicio si bm2s no está,
                 # pero el health check debe reportarlo.
                 main_log.warning("BM25 engine (bm2s) not available. Search functionality will be impaired.")
                 # No se considera error crítico para el arranque del servicio, el health check lo indicará.
            main_log.info("Sparse Search Engine (BM25Adapter) initialized.")
        except Exception as e_search_engine:
            # Si BM25 es crítico, esto podría ser un error fatal.
            # Por ahora, lo logueamos pero permitimos que el servicio intente iniciar.
            # El use case fallará si el search_engine no es usable.
            critical_failure_message = f"Failed to initialize Sparse Search Engine: {e_search_engine}"
            main_log.error(critical_failure_message, error_details=str(e_search_engine), exc_info=True)
            search_engine = None # Asegurar que es None

    # 3. Instantiate Use Case (solo si las dependencias críticas están listas)
    if chunk_repo and search_engine: # Ambas deben estar disponibles
        try:
            search_use_case = SparseSearchUseCase(
                chunk_content_repo=chunk_repo,
                sparse_search_engine=search_engine
            )
            main_log.info("SparseSearchUseCase instantiated successfully.")
            service_ready = True # El servicio principal está listo
        except Exception as e_usecase:
            critical_failure_message = f"Failed to instantiate SparseSearchUseCase: {e_usecase}"
            main_log.critical(critical_failure_message, error_details=str(e_usecase), exc_info=True)
            service_ready = False # Marcar como no listo si el use case falla
    else:
        if not chunk_repo:
            critical_failure_message = critical_failure_message or "Chunk repository not available for use case."
        if not search_engine: # No es error crítico si bm2s no está, pero se loguea.
             main_log.warning("Search engine not fully available for use case (likely bm2s missing).")
        # service_ready permanece False o lo que ya era.

    # 4. Set global dependencies for injection
    set_global_dependencies(
        chunk_repo=chunk_repo,
        search_engine=search_engine,
        use_case=search_use_case,
        service_ready=service_ready # La bandera final de preparación
    )

    if service_ready:
        main_log.info(f"{SERVICE_NAME} service components initialized. SERVICE IS READY.")
    else:
        final_msg = critical_failure_message or "One or more critical components failed to initialize."
        main_log.critical(f"{SERVICE_NAME} startup finished. {final_msg} SERVICE IS NOT READY.")


    yield # Application runs here


    # --- Shutdown Logic ---
    main_log.info(f"Shutting down {SERVICE_NAME}...")
    await postgres_connector.close_db_pool()
    # No hay otros clientes externos que cerrar en este servicio por ahora.
    main_log.info(f"{SERVICE_NAME} shutdown complete.")


# --- FastAPI Application Instance ---
app = FastAPI(
    title=SERVICE_NAME,
    openapi_url=f"{settings.API_V1_STR}/openapi.json",
    version="0.1.0", # Actualizar según sea necesario
    description="Atenex microservice for performing sparse (keyword-based) search using BM25.",
    lifespan=lifespan
)

# --- Middleware for Request ID, Timing, and Logging ---
@app.middleware("http")
async def request_context_middleware(request: Request, call_next):
    start_time = asyncio.get_event_loop().time()
    request_id = request.headers.get("x-request-id", str(uuid.uuid4()))

    # Bind variables to structlog context for this request
    structlog.contextvars.bind_contextvars(
        request_id=request_id,
        method=request.method,
        path=str(request.url.path),
        client_host=request.client.host if request.client else "unknown_client",
        # company_id = request.headers.get("x-company-id", "N/A") # Si se pasa por header
    )
    middleware_log = structlog.get_logger("sparse_search_service.request")
    middleware_log.info("Request received")
    
    response = None
    try:
        response = await call_next(request)
    except Exception as e_call_next: # Captura excepciones no manejadas por los handlers de FastAPI
        process_time_ms = (asyncio.get_event_loop().time() - start_time) * 1000
        structlog.contextvars.bind_contextvars(duration_ms=round(process_time_ms, 2), status_code=500)
        middleware_log.exception("Unhandled exception during request processing pipeline.")
        response = JSONResponse(
            status_code=fastapi_status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"request_id": request_id, "detail": "Internal Server Error during request handling."}
        )
    finally:
        if response: # Asegurar que el response existe antes de calcular tiempo y loguear
            process_time_ms = (asyncio.get_event_loop().time() - start_time) * 1000
            structlog.contextvars.bind_contextvars(duration_ms=round(process_time_ms, 2), status_code=response.status_code)
            
            log_method = middleware_log.info
            if 400 <= response.status_code < 500: log_method = middleware_log.warning
            elif response.status_code >= 500: log_method = middleware_log.error
            log_method("Request finished")
            
            response.headers["X-Request-ID"] = request_id
            response.headers["X-Process-Time-Ms"] = f"{process_time_ms:.2f}"
        
        structlog.contextvars.clear_contextvars() # Limpiar contexto para la siguiente solicitud

    return response


# --- Exception Handlers (Reutilizados de query-service, adaptados) ---
@app.exception_handler(HTTPException)
async def http_exception_handler_custom(request: Request, exc: HTTPException):
    # El request_context_middleware ya habrá logueado la info básica y el status_code.
    # Aquí podemos añadir detalles específicos de la excepción si es necesario.
    exc_log = structlog.get_logger("sparse_search_service.exception_handler")
    exc_log.error("HTTPException caught", detail=exc.detail, status_code=exc.status_code)
    return JSONResponse(
        status_code=exc.status_code,
        content={"request_id": structlog.contextvars.get_contextvars().get("request_id"), "detail": exc.detail}
    )

@app.exception_handler(RequestValidationError)
async def validation_exception_handler_custom(request: Request, exc: RequestValidationError):
    exc_log = structlog.get_logger("sparse_search_service.exception_handler")
    try:
        errors = exc.errors()
    except Exception: # Por si exc.errors() mismo falla
        errors = [{"loc": ["unknown"], "msg": "Error parsing validation details.", "type": "internal_error"}]
    exc_log.warning("RequestValidationError caught", validation_errors=errors)
    return JSONResponse(
        status_code=fastapi_status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={
            "request_id": structlog.contextvars.get_contextvars().get("request_id"),
            "detail": "Request validation failed.",
            "errors": errors
        },
    )

@app.exception_handler(ResponseValidationError) # Si la respuesta del endpoint no cumple el schema
async def response_validation_exception_handler_custom(request: Request, exc: ResponseValidationError):
    exc_log = structlog.get_logger("sparse_search_service.exception_handler")
    exc_log.error("ResponseValidationError caught", validation_errors=exc.errors(), exc_info=True)
    return JSONResponse(
        status_code=fastapi_status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={
            "request_id": structlog.contextvars.get_contextvars().get("request_id"),
            "detail": "Internal Server Error: Response data failed validation."
        }
    )

@app.exception_handler(Exception) # Handler genérico para cualquier otra excepción
async def generic_exception_handler_custom(request: Request, exc: Exception):
    exc_log = structlog.get_logger("sparse_search_service.exception_handler")
    exc_log.exception("Unhandled generic Exception caught") # Log con traceback completo
    return JSONResponse(
        status_code=fastapi_status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={
            "request_id": structlog.contextvars.get_contextvars().get("request_id"),
            "detail": f"An unexpected internal server error occurred: {type(exc).__name__}"
        }
    )

# --- Include API Routers ---
app.include_router(search_endpoint.router, prefix=settings.API_V1_STR, tags=["Sparse Search"])
main_log.info(f"API router included under prefix '{settings.API_V1_STR}/search'")

# --- Health Check Endpoint ---
@app.get(
    "/health",
    response_model=schemas.HealthCheckResponse,
    tags=["Health Check"],
    summary="Service Health and Dependencies Check"
)
async def health_check():
    health_log = structlog.get_logger("sparse_search_service.health_check")
    is_ready = get_service_status() # Obtener el estado global de preparación
    
    dependencies_status = {}
    db_status = "ok" if await postgres_connector.check_db_connection() else "error"
    dependencies_status["PostgreSQL"] = db_status
    
    bm2s_adapter_available = False
    # Intentar obtener el search engine para verificar bm2s (esto puede fallar si el servicio no está listo)
    try:
        from app.dependencies import get_sparse_search_engine
        engine = get_sparse_search_engine()
        if engine and hasattr(engine, 'is_available'):
            bm2s_adapter_available = engine.is_available()
    except HTTPException: # Si get_sparse_search_engine lanza 503 porque no está listo
        bm2s_adapter_available = False # Asumir no disponible
    except AttributeError: # Si el engine no tiene 'is_available'
        bm2s_adapter_available = False
        health_log.warning("Sparse search engine adapter does not have 'is_available' method.")

    dependencies_status["BM2S_Engine"] = "ok" if bm2s_adapter_available else "unavailable (bm2s library potentially missing or adapter init failed)"

    if not is_ready or db_status == "error": # Si el servicio global no está listo o la DB falla
        overall_status = "error"
        is_ready_final = False # Asegurar que 'ready' es False si hay fallos críticos
        http_status_code = fastapi_status.HTTP_503_SERVICE_UNAVAILABLE
        health_log.error(
            "Health check failed.",
            service_ready_flag=is_ready,
            dependencies=dependencies_status
        )
    else:
        overall_status = "ok"
        is_ready_final = True
        http_status_code = fastapi_status.HTTP_200_OK
        health_log.debug("Health check successful.", dependencies=dependencies_status)

    return JSONResponse(
        status_code=http_status_code,
        content=schemas.HealthCheckResponse(
            status=overall_status,
            service=SERVICE_NAME,
            ready=is_ready_final,
            dependencies=dependencies_status
        ).model_dump()
    )


# --- Root Endpoint ---
@app.get("/", include_in_schema=False)
async def root():
    return PlainTextResponse(f"{SERVICE_NAME} is running. Visit /docs for API documentation or /health for status.")

# --- Uvicorn Runner (for local development) ---
if __name__ == "__main__":
    port_to_use = settings.PORT
    log_level_str = settings.LOG_LEVEL.lower() # Uvicorn espera minúsculas
    print(f"----- Starting {SERVICE_NAME} locally on port {port_to_use} with log level {log_level_str} -----")
    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=port_to_use,
        reload=True, # Habilitar reload para desarrollo
        log_level=log_level_str
    )
```

## File: `pyproject.toml`
```toml
[tool.poetry]
name = "sparse-search-service"
version = "0.1.0"
description = "Atenex Sparse Search Service using BM25 for keyword-based document retrieval."
authors = ["Your Name <you@example.com>"] # Reemplaza con tu información
readme = "README.md"

[tool.poetry.dependencies]
python = "^3.10"
fastapi = "^0.110.0" # Mantener versiones consistentes con otros servicios si es posible
uvicorn = {extras = ["standard"], version = "^0.28.0"}
gunicorn = "^21.2.0"
pydantic = {extras = ["email"], version = "^2.6.4"}
pydantic-settings = "^2.2.1"
structlog = "^24.1.0"
asyncpg = "^0.29.0" # Para conectar a PostgreSQL
bm2s = "^0.3.2"      # Librería para BM25
tenacity = "^8.2.3"  # Para reintentos en DB si es necesario

# numpy es una dependencia de bm2s, pero se puede añadir explícitamente
numpy = "1.26.4"


[tool.poetry.group.dev.dependencies]
pytest = "^7.4.4"
pytest-asyncio = "^0.21.1"
httpx = "^0.27.0" # Para tests de API

[build-system]
requires = ["poetry-core>=1.0.0"]
build-backend = "poetry.core.masonry.api"
```
