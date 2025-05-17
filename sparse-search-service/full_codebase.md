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
│   │   ├── sparse_index_storage_port.py
│   │   └── sparse_search_port.py
│   └── use_cases
│       ├── __init__.py
│       └── load_and_search_index_use_case.py
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
│   ├── cache
│   │   ├── __init__.py
│   │   └── index_lru_cache.py
│   ├── persistence
│   │   ├── __init__.py
│   │   ├── postgres_connector.py
│   │   └── postgres_repositories.py
│   ├── sparse_retrieval
│   │   ├── __init__.py
│   │   └── bm25_adapter.py
│   └── storage
│       ├── __init__.py
│       └── gcs_index_storage_adapter.py
├── jobs
│   ├── __init__.py
│   └── index_builder_cronjob.py
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
# LLM: CORRECTION - Importar el caso de uso correcto y la clase correcta
from app.application.use_cases.load_and_search_index_use_case import LoadAndSearchIndexUseCase
from app.dependencies import get_sparse_search_use_case 
from app.core.config import settings

log = structlog.get_logger(__name__) 

router = APIRouter()

async def get_required_company_id_header(
    x_company_id: uuid.UUID = Header(..., description="Required X-Company-ID header.")
) -> uuid.UUID:
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
    # LLM: CORRECTION - El tipo de la dependencia debe ser el correcto
    use_case: LoadAndSearchIndexUseCase = Depends(get_sparse_search_use_case),
):
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
        
        response_data = schemas.SparseSearchResponse(
            query=request_data.query,
            company_id=request_data.company_id,
            results=search_results_domain 
        )
        
        endpoint_log.info(f"Sparse search successful. Returning {len(search_results_domain)} results.")
        return response_data

    except ConnectionError as ce: 
        endpoint_log.error("Service dependency (Database) unavailable.", error_details=str(ce), exc_info=False)
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=f"A critical service dependency is unavailable: {ce}")
    except ValueError as ve: 
        endpoint_log.warning("Invalid input or data processing error during sparse search.", error_details=str(ve), exc_info=True)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Data processing error: {ve}")
    except RuntimeError as re: 
        endpoint_log.error("Runtime error during sparse search execution.", error_details=str(re), exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"An internal error occurred: {re}")
    except Exception as e:
        endpoint_log.exception("Unexpected error during sparse search.") 
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
from typing import Dict, List, Optional, Any # LLM: CORRECTION - Añadir Any

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
    ) -> List[Dict[str, Any]]: # El uso de Any aquí es correcto ahora
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

## File: `app\application\ports\sparse_index_storage_port.py`
```py
# sparse-search-service/app/application/ports/sparse_index_storage_port.py
import abc
import uuid
from typing import Tuple, Optional

class SparseIndexStoragePort(abc.ABC):
    """
    Puerto abstracto para cargar y guardar archivos de índice BM25
    (el índice serializado y el mapa de IDs) desde/hacia un almacenamiento persistente.
    """

    @abc.abstractmethod
    async def load_index_files(self, company_id: uuid.UUID) -> Tuple[Optional[str], Optional[str]]:
        """
        Descarga los archivos de índice (dump BM25 y mapa de IDs JSON)
        desde el almacenamiento para una compañía específica.

        Args:
            company_id: El UUID de la compañía.

        Returns:
            Una tupla conteniendo las rutas a los archivos locales temporales:
            (local_bm2s_dump_path, local_id_map_path).
            Retorna (None, None) si los archivos no se encuentran, no se pueden descargar,
            o si ocurre cualquier error durante el proceso.
        """
        raise NotImplementedError

    @abc.abstractmethod
    async def save_index_files(self, company_id: uuid.UUID, local_bm2s_dump_path: str, local_id_map_path: str) -> None:
        """
        Guarda los archivos de índice locales (dump BM25 y mapa de IDs JSON)
        en el almacenamiento persistente para una compañía específica.

        Args:
            company_id: El UUID de la compañía.
            local_bm2s_dump_path: Ruta al archivo local del dump BM25.
            local_id_map_path: Ruta al archivo local del mapa de IDs JSON.

        Raises:
            Exception: Si ocurre un error durante la subida de los archivos.
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

## File: `app\application\use_cases\load_and_search_index_use_case.py`
```py
# sparse-search-service/app/application/use_cases/load_and_search_index_use_case.py
import uuid
import json
import structlog
from typing import List, Optional, Any
from pathlib import Path
import asyncio

from app.domain.models import SparseSearchResultItem
from app.application.ports.sparse_index_storage_port import SparseIndexStoragePort
from app.application.ports.sparse_search_port import SparseSearchPort 
from app.infrastructure.cache.index_lru_cache import IndexLRUCache, CachedIndexData
from app.infrastructure.sparse_retrieval.bm25_adapter import BM25Adapter 

# LLM: REMOVED - No es necesario importar bm2s aquí
# try:
#     import bm2s
# except ImportError:
#     bm2s = None

log = structlog.get_logger(__name__)

class LoadAndSearchIndexUseCase:
    def __init__(
        self,
        index_cache: IndexLRUCache,
        index_storage: SparseIndexStoragePort,
        sparse_search_engine: SparseSearchPort 
    ):
        self.index_cache = index_cache
        self.index_storage = index_storage
        self.sparse_search_engine = sparse_search_engine 
        log.info(
            "LoadAndSearchIndexUseCase initialized",
            cache_type=type(index_cache).__name__,
            storage_type=type(index_storage).__name__,
            search_engine_type=type(sparse_search_engine).__name__
        )

    async def execute(
        self,
        query: str,
        company_id: uuid.UUID,
        top_k: int
    ) -> List[SparseSearchResultItem]:
        use_case_log = log.bind(
            use_case="LoadAndSearchIndexUseCase",
            action="execute",
            company_id=str(company_id),
            query_preview=query[:50] + "...",
            requested_top_k=top_k
        )
        use_case_log.info("Executing load-and-search for sparse index.")

        cached_data: Optional[CachedIndexData] = self.index_cache.get(company_id)
        bm25_instance: Optional[Any] = None 
        id_map: Optional[List[str]] = None

        if cached_data:
            bm25_instance, id_map = cached_data
            use_case_log.info("BM25 index found in LRU cache.")
        else:
            use_case_log.info("BM25 index not in cache. Attempting to load from GCS.")
            
            local_bm2s_path_str, local_id_map_path_str = await self.index_storage.load_index_files(company_id)

            if local_bm2s_path_str and local_id_map_path_str:
                local_bm2s_path = Path(local_bm2s_path_str)
                local_id_map_path = Path(local_id_map_path_str)
                try:
                    use_case_log.debug("Loading BM25 instance from local file...", file_path=str(local_bm2s_path))
                    # LLM: La carga ahora es manejada por el adapter
                    bm25_instance = BM25Adapter.load_bm2s_from_file(str(local_bm2s_path))
                    
                    use_case_log.debug("Loading ID map from local JSON file...", file_path=str(local_id_map_path))
                    with open(local_id_map_path, 'r') as f:
                        id_map = json.load(f)
                    
                    if not isinstance(id_map, list):
                        use_case_log.error("ID map loaded from JSON is not a list.", id_map_type=type(id_map).__name__)
                        raise ValueError("ID map must be a list.")

                    use_case_log.info("BM25 index and ID map loaded successfully from GCS files.")
                    
                    self.index_cache.put(company_id, bm25_instance, id_map)
                    use_case_log.info("BM25 index and ID map stored in LRU cache.")

                except Exception as e_load:
                    use_case_log.error("Failed to load index/id_map from downloaded files.", error=str(e_load), exc_info=True)
                    bm25_instance = None
                    id_map = None
                finally:
                    try:
                        if local_bm2s_path.exists(): local_bm2s_path.unlink()
                        if local_id_map_path.exists(): local_id_map_path.unlink()
                        temp_dir = local_bm2s_path.parent
                        if temp_dir.is_dir() and not any(temp_dir.iterdir()): # Solo borrar si está vacío
                            temp_dir.rmdir()
                    except OSError as e_clean:
                        use_case_log.error("Error cleaning up temporary index files.", error=str(e_clean))
            else:
                use_case_log.warning("Index files not found in GCS or download failed. Cannot perform search.")
                return [] 

        if not bm25_instance or not id_map:
            use_case_log.error("BM25 instance or ID map is not available after cache/GCS lookup. Cannot search.")
            return []

        use_case_log.debug("Performing search with loaded BM25 instance and ID map...")
        try:
            # LLM: El adapter ahora espera la instancia bm25 y el id_map
            search_results: List[SparseSearchResultItem] = await self.sparse_search_engine.search(
                query=query,
                bm25_instance=bm25_instance,
                id_map=id_map,
                top_k=top_k,
                company_id=company_id 
            )
            use_case_log.info(f"Search executed. Found {len(search_results)} results.")
            return search_results
        except Exception as e_search:
            use_case_log.exception("An unexpected error occurred during search execution with loaded index.")
            raise RuntimeError(f"Search with loaded index failed: {e_search}") from e_search
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
DEFAULT_GCS_INDEX_BUCKET = "atenex-sparse-indices" 
DEFAULT_INDEX_CACHE_MAX_ITEMS = 50
DEFAULT_INDEX_CACHE_TTL_SECONDS = 3600 

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
    SERVICE_VERSION: str = "1.0.0" 

    # --- Database (PostgreSQL) ---
    POSTGRES_USER: str = Field(default=POSTGRES_K8S_USER_DEFAULT)
    POSTGRES_PASSWORD: SecretStr
    POSTGRES_SERVER: str = Field(default=POSTGRES_K8S_HOST_DEFAULT)
    POSTGRES_PORT: int = Field(default=POSTGRES_K8S_PORT_DEFAULT)
    POSTGRES_DB: str = Field(default=POSTGRES_K8S_DB_DEFAULT)
    DB_POOL_MIN_SIZE: int = Field(default=2)
    DB_POOL_MAX_SIZE: int = Field(default=10)
    DB_CONNECT_TIMEOUT: int = Field(default=30) 
    DB_COMMAND_TIMEOUT: int = Field(default=60) 

    # --- GCS Index Storage ---
    SPARSE_INDEX_GCS_BUCKET_NAME: str = Field(
        default=DEFAULT_GCS_INDEX_BUCKET,
        description="GCS bucket name for storing precomputed BM25 indexes."
    )

    # --- LRU/TTL Cache for BM25 Instances ---
    SPARSE_INDEX_CACHE_MAX_ITEMS: int = Field(
        default=DEFAULT_INDEX_CACHE_MAX_ITEMS,
        description="Maximum number of company BM25 indexes to keep in the LRU cache."
    )
    SPARSE_INDEX_CACHE_TTL_SECONDS: int = Field(
        default=DEFAULT_INDEX_CACHE_TTL_SECONDS,
        description="Time-to-live in seconds for items in the BM25 index cache."
    )
    
    # --- BM25 Parameters (Opcional, bm2s usa sus propios defaults) ---
    # SPARSE_BM2S_K1: float = Field(default=1.5)
    # SPARSE_BM2S_B: float = Field(default=0.75)


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
    
    @field_validator('SPARSE_INDEX_GCS_BUCKET_NAME', mode='before')
    @classmethod
    def check_gcs_bucket_name(cls, v: Any, info: ValidationInfo) -> Any:
        if v is None or v == "":
            raise ValueError(f"Required field '{info.field_name}' (SPARSE_INDEX_GCS_BUCKET_NAME) cannot be empty.")
        return v

# --- Global Settings Instance ---
temp_log = logging.getLogger("sparse_search_service.config.loader")
if not temp_log.handlers: 
    handler = logging.StreamHandler(sys.stdout)
    formatter = logging.Formatter('%(levelname)s: [%(asctime)s] [%(name)s] %(message)s')
    handler.setFormatter(formatter)
    temp_log.addHandler(handler)
    temp_log.setLevel(logging.INFO) 

try:
    temp_log.info("Loading Sparse Search Service settings...")
    settings = Settings()
    temp_log.info("--- Sparse Search Service Settings Loaded (v1.0.0) ---")

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
from fastapi import HTTPException, status
from typing import Optional
import structlog

from app.application.ports.repository_ports import ChunkContentRepositoryPort
from app.application.ports.sparse_search_port import SparseSearchPort
from app.application.ports.sparse_index_storage_port import SparseIndexStoragePort
from app.infrastructure.cache.index_lru_cache import IndexLRUCache
from app.application.use_cases.load_and_search_index_use_case import LoadAndSearchIndexUseCase


log = structlog.get_logger(__name__)

_chunk_content_repo_instance: Optional[ChunkContentRepositoryPort] = None
_sparse_search_engine_instance: Optional[SparseSearchPort] = None 
_gcs_index_storage_instance: Optional[SparseIndexStoragePort] = None
_index_cache_instance: Optional[IndexLRUCache] = None
_load_and_search_use_case_instance: Optional[LoadAndSearchIndexUseCase] = None
_service_ready_flag: bool = False

def set_global_dependencies(
    chunk_repo: Optional[ChunkContentRepositoryPort],
    search_engine: Optional[SparseSearchPort],
    gcs_storage: Optional[SparseIndexStoragePort],
    index_cache: Optional[IndexLRUCache],
    use_case: Optional[LoadAndSearchIndexUseCase], 
    service_ready: bool
):
    global _chunk_content_repo_instance, _sparse_search_engine_instance
    global _gcs_index_storage_instance, _index_cache_instance
    global _load_and_search_use_case_instance, _service_ready_flag

    _chunk_content_repo_instance = chunk_repo
    _sparse_search_engine_instance = search_engine
    _gcs_index_storage_instance = gcs_storage
    _index_cache_instance = index_cache
    _load_and_search_use_case_instance = use_case
    _service_ready_flag = service_ready
    log.debug("Global dependencies set in sparse-search-service.dependencies", service_ready=_service_ready_flag)


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

def get_gcs_index_storage() -> SparseIndexStoragePort:
    if not _service_ready_flag or not _gcs_index_storage_instance:
        log.critical("Attempted to get GCSIndexStorage before service is ready or instance is None.")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="GCS index storage is not available at the moment."
        )
    return _gcs_index_storage_instance

def get_index_cache() -> IndexLRUCache:
    if not _service_ready_flag or not _index_cache_instance:
        log.critical("Attempted to get IndexCache before service is ready or instance is None.")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Index cache is not available at the moment."
        )
    return _index_cache_instance
    
def get_sparse_search_use_case() -> LoadAndSearchIndexUseCase: 
    if not _service_ready_flag or not _load_and_search_use_case_instance:
        log.critical("Attempted to get LoadAndSearchIndexUseCase before service is ready or instance is None.")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Sparse search processing service is not ready."
        )
    return _load_and_search_use_case_instance

def get_service_status() -> bool:
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

## File: `app\infrastructure\cache\__init__.py`
```py

```

## File: `app\infrastructure\cache\index_lru_cache.py`
```py
# sparse-search-service/app/infrastructure/cache/index_lru_cache.py
import uuid
from typing import Tuple, Optional, List, Any
import sys
import structlog
from cachetools import TTLCache

try:
    import bm2s 
except ImportError:
    bm2s = None


log = structlog.get_logger(__name__)

CachedIndexData = Tuple[Any, List[str]] 

class IndexLRUCache:
    def __init__(self, max_items: int, ttl_seconds: int):
        self.cache: TTLCache[str, CachedIndexData] = TTLCache(maxsize=max_items, ttl=ttl_seconds)
        self.max_items = max_items
        self.ttl_seconds = ttl_seconds
        self.log = log.bind(cache_type="IndexLRUCache", max_items=max_items, ttl_seconds=ttl_seconds)
        self.log.info("IndexLRUCache initialized.")

    def get(self, company_id: uuid.UUID) -> Optional[CachedIndexData]:
        cache_log = self.log.bind(company_id=str(company_id), action="cache_get")
        key = str(company_id)
        cached_item = self.cache.get(key)
        if cached_item:
            cache_log.info("Cache hit.")
            return cached_item
        else:
            cache_log.info("Cache miss.")
            return None

    def put(self, company_id: uuid.UUID, bm25_instance: Any, id_map: List[str]) -> None:
        cache_log = self.log.bind(company_id=str(company_id), action="cache_put")
        key = str(company_id)
        
        try:
            self.cache[key] = (bm25_instance, id_map)
            cache_log.info("Item added/updated in cache.", current_cache_size=self.cache.currsize)
        except Exception as e:
            cache_log.error("Failed to put item into cache.", error=str(e), exc_info=True)

    def clear(self) -> None:
        self.log.info("Clearing all items from cache.")
        self.cache.clear()

    def __len__(self) -> int:
        return len(self.cache)
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
from pathlib import Path
import pickle 


try:
    from rank_bm25 import BM25Okapi, BM25Plus 
except ImportError:
    BM25Okapi = None
    BM25Plus = None

from app.application.ports.sparse_search_port import SparseSearchPort
from app.domain.models import SparseSearchResultItem
from app.core.config import settings 

log = structlog.get_logger(__name__)

BM25_IMPLEMENTATION = BM25Okapi 

class BM25Adapter(SparseSearchPort):
    def __init__(self):
        self._bm2s_available = False 
        if BM25_IMPLEMENTATION is None:
            log.error(
                
                "rank_bm25 library not installed or its classes (BM25Okapi/BM25Plus) not found. BM25 search functionality will be UNAVAILABLE. "
                "Install with: poetry add rank_bm25"
            )
        else:
            self._bm2s_available = True 
            log.info(f"BM25Adapter initialized. rank_bm25 library ({BM25_IMPLEMENTATION.__name__}) is available.")

    async def initialize_engine(self) -> None:
        if not self._bm2s_available: 
            log.warning(f"BM25 engine (rank_bm25 library - {BM25_IMPLEMENTATION.__name__ if BM25_IMPLEMENTATION else 'N/A'}) not available. Search will fail if attempted.")
        else:
            log.info(f"BM25 engine (rank_bm25 library - {BM25_IMPLEMENTATION.__name__}) available and ready.")

    def is_available(self) -> bool:
        return self._bm2s_available 

    @staticmethod
    def load_bm2s_from_file(file_path: str) -> Any: 
        load_log = log.bind(action="load_bm25_from_file", file_path=file_path)
        if not BM25_IMPLEMENTATION:
            load_log.error("rank_bm25 library not available, cannot load index.")
            raise RuntimeError("rank_bm25 library is not installed.")
        try:
            with open(file_path, 'rb') as f:
                loaded_retriever = pickle.load(f)
            
            if not isinstance(loaded_retriever, BM25_IMPLEMENTATION):
                load_log.error(f"Loaded object is not of type {BM25_IMPLEMENTATION.__name__}", loaded_type=type(loaded_retriever).__name__)
                raise TypeError(f"Expected {BM25_IMPLEMENTATION.__name__}, got {type(loaded_retriever).__name__}")

            load_log.info(f"BM25 index ({BM25_IMPLEMENTATION.__name__}) loaded successfully from file.")
            return loaded_retriever
        except FileNotFoundError:
            load_log.error("BM25 index file not found.")
            raise
        except Exception as e:
            load_log.exception("Failed to load BM25 index from file.")
            raise RuntimeError(f"Failed to load BM25 index from {file_path}: {e}") from e

    @staticmethod
    def dump_bm2s_to_file(instance: Any, file_path: str): 
        dump_log = log.bind(action="dump_bm25_to_file", file_path=file_path)
        if not BM25_IMPLEMENTATION:
            dump_log.error("rank_bm25 library not available, cannot dump index.")
            raise RuntimeError("rank_bm25 library is not installed.")
        if not isinstance(instance, BM25_IMPLEMENTATION): 
            dump_log.error(f"Invalid instance type provided for dumping. Expected {BM25_IMPLEMENTATION.__name__}", instance_type=type(instance).__name__)
            raise TypeError(f"Instance to dump must be a {BM25_IMPLEMENTATION.__name__} object.")
        try:
            with open(file_path, 'wb') as f:
                pickle.dump(instance, f)
            dump_log.info(f"BM25 index ({BM25_IMPLEMENTATION.__name__}) dumped successfully to file.")
        except Exception as e:
            dump_log.exception("Failed to dump BM25 index to file.")
            raise RuntimeError(f"Failed to dump BM25 index to {file_path}: {e}") from e

    async def search(
        self,
        query: str,
        bm25_instance: Any, 
        id_map: List[str],  
        top_k: int,
        company_id: Optional[uuid.UUID] = None 
    ) -> List[SparseSearchResultItem]:
        adapter_log = log.bind(
            adapter="BM25Adapter",
            action="search_with_instance",
            company_id=str(company_id) if company_id else "N/A",
            query_preview=query[:50] + "...",
            num_ids_in_map=len(id_map),
            top_k=top_k
        )

        if not self._bm2s_available:
            adapter_log.error("rank_bm25 library not available. Cannot perform BM25 search.")
            return []
        
        if not bm25_instance or not isinstance(bm25_instance, BM25_IMPLEMENTATION):
            adapter_log.error(f"Invalid or no BM25 instance provided. Expected {BM25_IMPLEMENTATION.__name__}, got {type(bm25_instance).__name__}")
            return []

        if not id_map:
            adapter_log.warning("ID map is empty. No way to map results to original chunk IDs.")
            return []
            
        if not query.strip():
            adapter_log.warning("Query is empty. Returning no results.")
            return []

        start_time = time.monotonic()
        adapter_log.debug("Starting BM25 search with pre-loaded instance...")

        try:
            tokenized_query = query.lower().split() 
            
            doc_scores = bm25_instance.get_scores(tokenized_query)
            
            scored_indices = []
            for i, score in enumerate(doc_scores):
                if i < len(id_map): 
                    scored_indices.append((score, i))
            
            scored_indices.sort(key=lambda x: x[0], reverse=True)
            
            top_n_results = scored_indices[:top_k]

            retrieval_time_ms = (time.monotonic() - start_time) * 1000
            adapter_log.debug(f"BM25 retrieval complete. Hits considered: {len(doc_scores)}, Top_k requested: {top_k}.",
                              duration_ms=round(retrieval_time_ms,2))

            final_results: List[SparseSearchResultItem] = []
            for score_val, original_index in top_n_results:
                original_chunk_id = id_map[original_index]
                final_results.append(
                    SparseSearchResultItem(chunk_id=original_chunk_id, score=float(score_val))
                )
            
            adapter_log.info(
                f"BM25 search finished. Returning {len(final_results)} results.",
                total_duration_ms=round(retrieval_time_ms, 2)
            )
            return final_results

        except Exception as e:
            adapter_log.exception("Error during BM25 search processing with pre-loaded instance.")
            return []
```

## File: `app\infrastructure\storage\__init__.py`
```py

```

## File: `app\infrastructure\storage\gcs_index_storage_adapter.py`
```py
# sparse-search-service/app/infrastructure/storage/gcs_index_storage_adapter.py
import asyncio
import json
import tempfile
import uuid
from pathlib import Path
from typing import Tuple, Optional
import functools # <--- CORRECCIÓN: Importación añadida

import structlog
from google.cloud import storage
from google.api_core.exceptions import NotFound, GoogleAPIError

from app.application.ports.sparse_index_storage_port import SparseIndexStoragePort
from app.core.config import settings

log = structlog.get_logger(__name__)

BM25_DUMP_FILENAME = "bm25_index.bm2s"
ID_MAP_FILENAME = "id_map.json"
GCS_INDEX_ROOT_PATH = "indices" 

class GCSIndexStorageError(Exception):
    pass

class GCSIndexStorageAdapter(SparseIndexStoragePort):
    def __init__(self, bucket_name: Optional[str] = None):
        self.bucket_name = bucket_name or settings.SPARSE_INDEX_GCS_BUCKET_NAME
        try:
            self._client = storage.Client()
            self._bucket = self._client.bucket(self.bucket_name)
        except Exception as e:
            log.critical("Failed to initialize GCS client or bucket handle.", error=str(e), exc_info=True)
            raise GCSIndexStorageError(f"Failed to initialize GCS client for bucket '{self.bucket_name}': {e}") from e
        self.log = log.bind(gcs_bucket=self.bucket_name, adapter="GCSIndexStorageAdapter")

    def _get_gcs_object_path(self, company_id: uuid.UUID, filename: str) -> str:
        return f"{GCS_INDEX_ROOT_PATH}/{str(company_id)}/{filename}"

    async def load_index_files(self, company_id: uuid.UUID) -> Tuple[Optional[str], Optional[str]]:
        adapter_log = self.log.bind(company_id=str(company_id), action="load_index_files")
        adapter_log.info("Attempting to load index files from GCS.")

        temp_dir = tempfile.mkdtemp(prefix=f"sparse_idx_{company_id}_")
        local_bm2s_path = Path(temp_dir) / BM25_DUMP_FILENAME
        local_id_map_path = Path(temp_dir) / ID_MAP_FILENAME

        gcs_bm2s_object_path = self._get_gcs_object_path(company_id, BM25_DUMP_FILENAME)
        gcs_id_map_object_path = self._get_gcs_object_path(company_id, ID_MAP_FILENAME)

        loop = asyncio.get_running_loop()

        async def _download_file(gcs_path: str, local_path: Path) -> bool:
            try:
                blob = self._bucket.blob(gcs_path)
                await loop.run_in_executor(None, blob.download_to_filename, str(local_path))
                adapter_log.debug(f"Successfully downloaded GCS object to local file.", gcs_object=gcs_path, local_file=str(local_path))
                return True
            except NotFound:
                adapter_log.warning(f"GCS object not found.", gcs_object=gcs_path)
                return False
            except GoogleAPIError as e:
                adapter_log.error(f"GCS API error downloading object.", gcs_object=gcs_path, error=str(e))
                return False
            except Exception as e:
                adapter_log.exception(f"Unexpected error downloading GCS object.", gcs_object=gcs_path)
                return False

        bm2s_downloaded = await _download_file(gcs_bm2s_object_path, local_bm2s_path)
        id_map_downloaded = await _download_file(gcs_id_map_object_path, local_id_map_path)

        if bm2s_downloaded and id_map_downloaded:
            adapter_log.info("Both index files downloaded successfully from GCS.")
            return str(local_bm2s_path), str(local_id_map_path)
        else:
            adapter_log.warning("Failed to download one or both index files from GCS. Cleaning up temporary files.")
            try:
                if local_bm2s_path.exists(): local_bm2s_path.unlink()
                if local_id_map_path.exists(): local_id_map_path.unlink()
                Path(temp_dir).rmdir()
            except OSError as e_clean:
                adapter_log.error("Error cleaning up temporary directory.", temp_dir=temp_dir, error=str(e_clean))
            return None, None

    async def save_index_files(self, company_id: uuid.UUID, local_bm2s_dump_path: str, local_id_map_path: str) -> None:
        adapter_log = self.log.bind(company_id=str(company_id), action="save_index_files")
        adapter_log.info("Attempting to save index files to GCS.")

        gcs_bm2s_object_path = self._get_gcs_object_path(company_id, BM25_DUMP_FILENAME)
        gcs_id_map_object_path = self._get_gcs_object_path(company_id, ID_MAP_FILENAME)

        loop = asyncio.get_running_loop()

        async def _upload_file(local_path: str, gcs_path: str, content_type: Optional[str] = None):
            try:
                blob = self._bucket.blob(gcs_path)
                # <--- CORRECCIÓN: Usar functools.partial para pasar content_type ---
                upload_func = functools.partial(blob.upload_from_filename, local_path, content_type=content_type)
                await loop.run_in_executor(None, upload_func)
                # --- Fin de la corrección ---
                adapter_log.debug(f"Successfully uploaded local file to GCS object.", local_file=local_path, gcs_object=gcs_path)
            except GoogleAPIError as e:
                adapter_log.error(f"GCS API error uploading file.", local_file=local_path, gcs_object=gcs_path, error=str(e))
                raise GCSIndexStorageError(f"GCS API error uploading {local_path} to {gcs_path}: {e}") from e
            except Exception as e:
                adapter_log.exception(f"Unexpected error uploading file to GCS.", local_file=local_path, gcs_object=gcs_path)
                raise GCSIndexStorageError(f"Unexpected error uploading {local_path} to {gcs_path}: {e}") from e

        try:
            await _upload_file(local_bm2s_dump_path, gcs_bm2s_object_path, content_type="application/octet-stream")
            await _upload_file(local_id_map_path, gcs_id_map_object_path, content_type="application/json")
            adapter_log.info("Both index files uploaded successfully to GCS.")
        except GCSIndexStorageError: 
            raise
        except Exception as e: 
            adapter_log.exception("Unexpected failure during save_index_files orchestration.")
            raise GCSIndexStorageError(f"Orchestration failure in save_index_files: {e}") from e
```

## File: `app\jobs\__init__.py`
```py

```

## File: `app\jobs\index_builder_cronjob.py`
```py
# sparse-search-service/app/jobs/index_builder_cronjob.py
import argparse
import asyncio
import json
import os
import tempfile
import uuid
from pathlib import Path
import sys
import pickle # Para serializar
from typing import List # <--- CORRECCIÓN: Importación añadida

try:
    # LLM: CORRECTION - Importar de rank_bm25
    from rank_bm25 import BM25Okapi, BM25Plus
except ImportError:
    BM25Okapi = None
    BM25Plus = None
    print("ERROR: rank_bm25 library not found. Please install it: poetry add rank_bm25", file=sys.stderr)
    sys.exit(1)

import structlog

if __name__ == '__main__':
    SCRIPT_DIR = Path(__file__).resolve().parent
    PROJECT_ROOT = SCRIPT_DIR.parent.parent 
    sys.path.insert(0, str(PROJECT_ROOT))


from app.core.config import settings as app_settings 
from app.core.logging_config import setup_logging as app_setup_logging
from app.infrastructure.persistence.postgres_repositories import PostgresChunkContentRepository
from app.infrastructure.persistence import postgres_connector
from app.infrastructure.storage.gcs_index_storage_adapter import GCSIndexStorageAdapter, GCSIndexStorageError
from app.infrastructure.sparse_retrieval.bm25_adapter import BM25Adapter # Sigue siendo útil para dump/load
from typing import Optional # Añadido para la firma de main_builder_logic

# LLM: CORRECTION - Usar la implementación definida en bm25_adapter o una aquí
BM25_IMPLEMENTATION_FOR_BUILDER = BM25Okapi 

app_setup_logging() 
log = structlog.get_logger("index_builder_cronjob")


async def build_and_upload_index_for_company(
    company_id_str: str,
    repo: PostgresChunkContentRepository,
    gcs_adapter: GCSIndexStorageAdapter
):
    builder_log = log.bind(company_id=company_id_str, job_action="build_and_upload_index")
    builder_log.info("Starting index build process for company.")
    
    try:
        company_uuid = uuid.UUID(company_id_str)
    except ValueError:
        builder_log.error("Invalid company_id format. Skipping.", company_id_input=company_id_str)
        return

    builder_log.debug("Fetching chunks from PostgreSQL...")
    try:
        chunks_data = await repo.get_chunks_with_metadata_by_company(company_uuid)
    except ConnectionError as e_db_conn:
        builder_log.error("Database connection error while fetching chunks. Skipping company.", error=str(e_db_conn))
        return
    except Exception as e_db_fetch:
        builder_log.exception("Failed to fetch chunks from PostgreSQL. Skipping company.")
        return
        
    if not chunks_data:
        builder_log.warning("No processable chunks found for company. Skipping index build.")
        return

    corpus_texts_full = [chunk['content'] for chunk in chunks_data if chunk.get('content','').strip()]
    # LLM: CORRECTION - Tokenizar el corpus para rank_bm25
    corpus_texts_tokenized = [text.lower().split() for text in corpus_texts_full]
    id_map = [chunk['id'] for chunk in chunks_data if chunk.get('content','').strip()] 

    if not corpus_texts_tokenized: # LLM: CORRECTION - Verificar corpus tokenizado
        builder_log.warning("Corpus is empty after filtering and tokenizing content. Skipping index build.")
        return

    builder_log.info(f"Building BM25 index for {len(corpus_texts_tokenized)} chunks...")
    try:
        # LLM: CORRECTION - Instanciar y usar rank_bm25
        if not BM25_IMPLEMENTATION_FOR_BUILDER:
            raise RuntimeError("rank_bm25 is not available for building index.")
        
        # BM25Okapi(corpus_tokenizado)
        retriever = BM25_IMPLEMENTATION_FOR_BUILDER(corpus_texts_tokenized)
        # El método .index() no existe en rank_bm25 de la misma forma. La indexación ocurre al instanciar.
        builder_log.info(f"BM25 index ({BM25_IMPLEMENTATION_FOR_BUILDER.__name__}) built successfully.")
    except Exception as e_bm25_index:
        builder_log.exception("Error during BM25 index building.")
        return


    with tempfile.TemporaryDirectory(prefix=f"bm25_build_{company_id_str}_") as tmpdir_str:
        tmpdir = Path(tmpdir_str)
        bm2s_file_path = tmpdir / "bm25_index.bm2s" # El nombre del archivo puede mantenerse por consistencia
        id_map_file_path = tmpdir / "id_map.json"

        builder_log.debug("Dumping BM25 index to temporary file.", file_path=str(bm2s_file_path))
        try:
            # LLM: CORRECTION - BM25Adapter ahora usa pickle, y su método estático es útil aquí
            BM25Adapter.dump_bm2s_to_file(retriever, str(bm2s_file_path))
        except Exception as e_dump:
            builder_log.exception("Failed to dump BM25 index.")
            return

        builder_log.debug("Saving ID map to temporary JSON file.", file_path=str(id_map_file_path))
        try:
            with open(id_map_file_path, 'w') as f:
                json.dump(id_map, f)
        except IOError as e_json_io:
            builder_log.exception("Failed to save ID map JSON.")
            return
        
        builder_log.info("Index and ID map saved to temporary local files. Uploading to GCS...")
        try:
            await gcs_adapter.save_index_files(company_uuid, str(bm2s_file_path), str(id_map_file_path))
            builder_log.info("Index and ID map uploaded to GCS successfully.")
        except GCSIndexStorageError as e_gcs_upload:
            builder_log.error("Failed to upload index files to GCS.", error=str(e_gcs_upload), exc_info=True)
        except Exception as e_gcs_generic:
            builder_log.exception("Unexpected error during GCS upload.")

async def get_all_active_company_ids(repo: PostgresChunkContentRepository) -> List[uuid.UUID]:
    fetch_log = log.bind(job_action="fetch_active_companies")
    fetch_log.info("Fetching active company IDs from database...")
    query = "SELECT DISTINCT company_id FROM documents WHERE status = 'processed';" 
    pool = await postgres_connector.get_db_pool()
    conn = None
    try:
        conn = await pool.acquire()
        rows = await conn.fetch(query)
        company_ids = [row['company_id'] for row in rows if row['company_id']]
        fetch_log.info(f"Found {len(company_ids)} active company IDs with processed documents.")
        return company_ids
    except Exception as e:
        fetch_log.exception("Failed to fetch active company IDs.")
        return []
    finally:
        if conn:
            await pool.release(conn)


async def main_builder_logic(target_company_id_str: Optional[str]):
    log.info("Index Builder CronJob starting...", target_company=target_company_id_str or "ALL")
    
    # LLM: CORRECTION - Verificar si la librería está disponible
    if not BM25Okapi and not BM25Plus: # Si ninguna implementación está disponible
        log.critical("rank_bm25 library or its classes (BM25Okapi, BM25Plus) are not available. Index builder cannot run.")
        return

    await postgres_connector.get_db_pool() 
    repo = PostgresChunkContentRepository()
    
    gcs_bucket_for_indices = app_settings.SPARSE_INDEX_GCS_BUCKET_NAME
    if not gcs_bucket_for_indices:
        log.critical("SPARSE_INDEX_GCS_BUCKET_NAME is not configured. Cannot proceed.")
        await postgres_connector.close_db_pool()
        return
        
    gcs_adapter = GCSIndexStorageAdapter(bucket_name=gcs_bucket_for_indices)

    companies_to_process: List[str] = []

    if target_company_id_str and target_company_id_str.upper() != "ALL":
        companies_to_process.append(target_company_id_str)
    else:
        log.info("Target is ALL companies. Fetching list of active company IDs...")
        active_company_uuids = await get_all_active_company_ids(repo)
        companies_to_process = [str(uid) for uid in active_company_uuids]
        if not companies_to_process:
            log.info("No active companies found to process.")

    log.info(f"Will process indices for {len(companies_to_process)} companies.", companies_list_preview=companies_to_process[:5])

    for comp_id_str in companies_to_process:
        await build_and_upload_index_for_company(comp_id_str, repo, gcs_adapter)

    await postgres_connector.close_db_pool()
    log.info("Index Builder CronJob finished.")

if __name__ == "__main__":
    # LLM: CORRECTION - Verificar disponibilidad antes de parsear args
    if not BM25Okapi and not BM25Plus:
        print("FATAL: rank_bm25 library (BM25Okapi or BM25Plus) is required but not found.", file=sys.stderr)
        sys.exit(1)

    parser = argparse.ArgumentParser(description="BM25 Index Builder for Sparse Search Service.")
    parser.add_argument(
        "--company-id",
        type=str,
        default="ALL", 
        help="UUID of the company to build index for, or 'ALL' for all active companies."
    )
    args = parser.parse_args()

    asyncio.run(main_builder_logic(args.company_id))
```

## File: `app\main.py`
```py
# sparse-search-service/app/main.py
from fastapi import FastAPI, HTTPException, status as fastapi_status, Request
from fastapi.exceptions import RequestValidationError, ResponseValidationError
from fastapi.responses import JSONResponse, PlainTextResponse
import structlog
import uvicorn 
import logging 
import sys
import asyncio
import json
import uuid 
from contextlib import asynccontextmanager
from typing import Optional, Dict 

from app.core.config import settings 
from app.core.logging_config import setup_logging

setup_logging() 
main_log = structlog.get_logger("sparse_search_service.main") 


from app.api.v1.endpoints import search_endpoint

from app.api.v1 import schemas

from app.application.ports.repository_ports import ChunkContentRepositoryPort
from app.application.ports.sparse_search_port import SparseSearchPort
from app.application.ports.sparse_index_storage_port import SparseIndexStoragePort
from app.infrastructure.persistence.postgres_repositories import PostgresChunkContentRepository
from app.infrastructure.sparse_retrieval.bm25_adapter import BM25Adapter
from app.infrastructure.storage.gcs_index_storage_adapter import GCSIndexStorageAdapter, GCSIndexStorageError
from app.infrastructure.cache.index_lru_cache import IndexLRUCache
from app.application.use_cases.load_and_search_index_use_case import LoadAndSearchIndexUseCase


from app.infrastructure.persistence import postgres_connector
from app.dependencies import set_global_dependencies, get_service_status


SERVICE_NAME = settings.PROJECT_NAME
SERVICE_VERSION = settings.SERVICE_VERSION


@asynccontextmanager
async def lifespan(app: FastAPI):
    main_log.info(f"Starting up {SERVICE_NAME} v{SERVICE_VERSION}...")
    service_ready_final = False
    critical_startup_error_message = ""

    db_pool_ok: bool = False
    chunk_repo: Optional[ChunkContentRepositoryPort] = None
    bm25_engine: Optional[SparseSearchPort] = None
    gcs_adapter: Optional[SparseIndexStoragePort] = None
    index_cache: Optional[IndexLRUCache] = None
    load_search_uc: Optional[LoadAndSearchIndexUseCase] = None

    try:
        await postgres_connector.get_db_pool() 
        db_pool_ok = await postgres_connector.check_db_connection()
        if db_pool_ok:
            main_log.info("PostgreSQL connection pool initialized and verified.")
            chunk_repo = PostgresChunkContentRepository() 
        else:
            critical_startup_error_message = "Failed PostgreSQL connection verification during startup."
            main_log.critical(critical_startup_error_message)
    except ConnectionError as e:
        critical_startup_error_message = f"CRITICAL: Failed to connect to PostgreSQL: {e}"
        main_log.critical(critical_startup_error_message, error_details=str(e))
    except Exception as e:
        critical_startup_error_message = f"CRITICAL: Unexpected error initializing PostgreSQL pool: {e}"
        main_log.critical(critical_startup_error_message, error_details=str(e), exc_info=True)

    if not db_pool_ok: 
        main_log.error("Aborting further service initialization due to PostgreSQL connection failure.")
    else:
        try:
            gcs_adapter = GCSIndexStorageAdapter(bucket_name=settings.SPARSE_INDEX_GCS_BUCKET_NAME)
            main_log.info("GCSIndexStorageAdapter initialized.", bucket_name=settings.SPARSE_INDEX_GCS_BUCKET_NAME)
        except GCSIndexStorageError as e_gcs: 
            critical_startup_error_message = f"CRITICAL: Failed GCSIndexStorageAdapter initialization: {e_gcs}"
            main_log.critical(critical_startup_error_message, error_details=str(e_gcs))
            gcs_adapter = None
        except Exception as e_gcs_other:
            critical_startup_error_message = f"CRITICAL: Unexpected error initializing GCSIndexStorageAdapter: {e_gcs_other}"
            main_log.critical(critical_startup_error_message, error_details=str(e_gcs_other), exc_info=True)
            gcs_adapter = None

        try:
            bm25_engine = BM25Adapter()
            await bm25_engine.initialize_engine() 
            if not bm25_engine.is_available():
                 main_log.warning("BM25 engine (bm2s library) not available. Search functionality will be impaired/unavailable.")
            main_log.info("BM25Adapter (SparseSearchPort) initialized.")
        except Exception as e_bm25:
            main_log.error(f"Failed to initialize BM25Adapter: {e_bm25}", error_details=str(e_bm25), exc_info=True)
            bm25_engine = None 
        
        try:
            index_cache = IndexLRUCache(
                max_items=settings.SPARSE_INDEX_CACHE_MAX_ITEMS,
                ttl_seconds=settings.SPARSE_INDEX_CACHE_TTL_SECONDS
            )
            main_log.info("IndexLRUCache initialized.")
        except Exception as e_cache:
            main_log.error(f"Failed to initialize IndexLRUCache: {e_cache}", error_details=str(e_cache), exc_info=True)
            index_cache = None 

        if db_pool_ok and gcs_adapter is not None and bm25_engine is not None and index_cache is not None:
            try:
                load_search_uc = LoadAndSearchIndexUseCase(
                    index_cache=index_cache,
                    index_storage=gcs_adapter,
                    sparse_search_engine=bm25_engine 
                )
                main_log.info("LoadAndSearchIndexUseCase instantiated.")
                service_ready_final = True 
            except Exception as e_uc:
                critical_startup_error_message = f"Failed to instantiate LoadAndSearchIndexUseCase: {e_uc}"
                main_log.critical(critical_startup_error_message, error_details=str(e_uc), exc_info=True)
                service_ready_final = False
        else:
            main_log.warning("Not all components ready for LoadAndSearchIndexUseCase instantiation.",
                             db_ok=db_pool_ok, gcs_ok=bool(gcs_adapter), 
                             bm25_ok=bool(bm25_engine), cache_ok=(index_cache is not None))
            service_ready_final = False 

    set_global_dependencies(
        chunk_repo=chunk_repo, 
        search_engine=bm25_engine, 
        gcs_storage=gcs_adapter,
        index_cache=index_cache,
        use_case=load_search_uc, 
        service_ready=service_ready_final
    )

    if service_ready_final:
        main_log.info(f"{SERVICE_NAME} service components initialized. SERVICE IS READY.")
    else:
        final_startup_error_msg = critical_startup_error_message or "One or more components failed to initialize."
        main_log.critical(f"{SERVICE_NAME} startup finished. {final_startup_error_msg} SERVICE IS NOT READY.")

    yield 

    main_log.info(f"Shutting down {SERVICE_NAME}...")
    await postgres_connector.close_db_pool()
    if index_cache: index_cache.clear() 
    main_log.info(f"{SERVICE_NAME} shutdown complete.")


app = FastAPI(
    title=SERVICE_NAME,
    openapi_url=f"{settings.API_V1_STR}/openapi.json",
    version=SERVICE_VERSION, 
    description="Atenex microservice for performing sparse (keyword-based) search using BM25 with GCS-backed indexes.",
    lifespan=lifespan
)

@app.middleware("http")
async def request_context_middleware(request: Request, call_next):
    start_time = asyncio.get_event_loop().time()
    request_id = request.headers.get("x-request-id", str(uuid.uuid4()))

    structlog.contextvars.bind_contextvars(
        request_id=request_id,
        method=request.method,
        path=str(request.url.path),
        client_host=request.client.host if request.client else "unknown_client",
    )
    middleware_log = structlog.get_logger("sparse_search_service.request")
    middleware_log.info("Request received")
    
    response = None
    try:
        response = await call_next(request)
    except Exception as e_call_next: 
        process_time_ms = (asyncio.get_event_loop().time() - start_time) * 1000
        structlog.contextvars.bind_contextvars(duration_ms=round(process_time_ms, 2), status_code=500)
        middleware_log.exception("Unhandled exception during request processing pipeline.")
        response = JSONResponse(
            status_code=fastapi_status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"request_id": request_id, "detail": "Internal Server Error during request handling."}
        )
    finally:
        if response: 
            process_time_ms = (asyncio.get_event_loop().time() - start_time) * 1000
            structlog.contextvars.bind_contextvars(duration_ms=round(process_time_ms, 2), status_code=response.status_code)
            
            log_method = middleware_log.info
            if 400 <= response.status_code < 500: log_method = middleware_log.warning
            elif response.status_code >= 500: log_method = middleware_log.error
            log_method("Request finished")
            
            response.headers["X-Request-ID"] = request_id
            response.headers["X-Process-Time-Ms"] = f"{process_time_ms:.2f}"
        
        structlog.contextvars.clear_contextvars() 

    return response


@app.exception_handler(HTTPException)
async def http_exception_handler_custom(request: Request, exc: HTTPException):
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
    except Exception: 
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

@app.exception_handler(ResponseValidationError) 
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

@app.exception_handler(Exception) 
async def generic_exception_handler_custom(request: Request, exc: Exception):
    exc_log = structlog.get_logger("sparse_search_service.exception_handler")
    exc_log.exception("Unhandled generic Exception caught") 
    return JSONResponse(
        status_code=fastapi_status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={
            "request_id": structlog.contextvars.get_contextvars().get("request_id"),
            "detail": f"An unexpected internal server error occurred: {type(exc).__name__}"
        }
    )

app.include_router(search_endpoint.router, prefix=settings.API_V1_STR, tags=["Sparse Search"])
main_log.info(f"API router included under prefix '{settings.API_V1_STR}/search'")

@app.get(
    "/health",
    response_model=schemas.HealthCheckResponse,
    tags=["Health Check"],
    summary="Service Health and Dependencies Check"
)
async def health_check():
    health_log = structlog.get_logger("sparse_search_service.health_check")
    is_globally_ready = get_service_status() 
    
    dependencies_status_dict: Dict[str, str] = {}
    
    db_ok = await postgres_connector.check_db_connection()
    dependencies_status_dict["PostgreSQL"] = "ok" if db_ok else "error"
    
    bm2s_engine_available = False
    bm2s_adapter_details = "unavailable"
    try:
        from app.dependencies import get_sparse_search_engine
        engine_adapter = get_sparse_search_engine() 
        if engine_adapter and hasattr(engine_adapter, 'is_available'):
            bm2s_engine_available = engine_adapter.is_available()
            bm2s_adapter_details = "ok (bm2s library loaded and adapter initialized)" if bm2s_engine_available else "unavailable (bm2s library potentially missing)"
        else:
             bm2s_adapter_details = "adapter not fully initialized or is_available method missing"
    except HTTPException as http_exc_dep: 
        bm2s_adapter_details = f"adapter not ready ({http_exc_dep.status_code})"
        health_log.warning("Could not get search engine adapter for health check.", detail=str(http_exc_dep.detail))
    except Exception as e_bm2s_check:
        bm2s_adapter_details = f"error checking adapter: {type(e_bm2s_check).__name__}"
        health_log.error("Error checking BM2S engine adapter status.", error=str(e_bm2s_check))

    dependencies_status_dict["BM2S_Engine"] = bm2s_adapter_details

    gcs_adapter_ready = False
    gcs_adapter_details = "unavailable"
    try:
        from app.dependencies import get_gcs_index_storage
        gcs_storage_adapter = get_gcs_index_storage() 
        if gcs_storage_adapter: 
            gcs_adapter_ready = True 
            gcs_adapter_details = "ok (adapter initialized)"
        else:
            gcs_adapter_details = "adapter not initialized"
    except HTTPException as http_exc_gcs:
        gcs_adapter_details = f"adapter not ready ({http_exc_gcs.status_code})"
        health_log.warning("Could not get GCS storage adapter for health check.", detail=str(http_exc_gcs.detail))
    except Exception as e_gcs_check:
        gcs_adapter_details = f"error checking adapter: {type(e_gcs_check).__name__}"
        health_log.error("Error checking GCS adapter status.", error=str(e_gcs_check))
        
    dependencies_status_dict["GCS_Index_Storage"] = gcs_adapter_details
    
    final_http_status: int
    response_content: schemas.HealthCheckResponse

    if is_globally_ready and db_ok and gcs_adapter_ready : 
        final_http_status = fastapi_status.HTTP_200_OK
        response_content = schemas.HealthCheckResponse(
            status="ok",
            service=SERVICE_NAME,
            ready=True, 
            dependencies=dependencies_status_dict
        )
        health_log.debug("Health check successful.", **response_content.model_dump())
    else:
        final_http_status = fastapi_status.HTTP_503_SERVICE_UNAVAILABLE
        response_content = schemas.HealthCheckResponse(
            status="error",
            service=SERVICE_NAME,
            ready=False, 
            dependencies=dependencies_status_dict
        )
        health_log.error("Health check failed.", **response_content.model_dump())

    return JSONResponse(
        status_code=final_http_status,
        content=response_content.model_dump()
    )


@app.get("/", include_in_schema=False)
async def root():
    return PlainTextResponse(f"{SERVICE_NAME} v{SERVICE_VERSION} is running. Visit /docs for API documentation or /health for status.")


if __name__ == "__main__":
    port_to_use = settings.PORT
    log_level_str = settings.LOG_LEVEL.lower() 
    print(f"----- Starting {SERVICE_NAME} v{SERVICE_VERSION} locally on port {port_to_use} with log level {log_level_str} -----")
    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=port_to_use,
        reload=True, 
        log_level=log_level_str
    )

# JFU
```

## File: `pyproject.toml`
```toml
[tool.poetry]
name = "sparse-search-service"
version = "1.0.0" 
description = "Atenex Sparse Search Service using BM25 with precomputed GCS indexes."
authors = ["Atenex Backend Team <dev@atenex.com>"]
readme = "README.md"

[tool.poetry.dependencies]
python = "^3.10"
fastapi = "^0.110.0"
uvicorn = {extras = ["standard"], version = "^0.28.0"}
gunicorn = "^21.2.0"
pydantic = {extras = ["email"], version = "^2.6.4"}
pydantic-settings = "^2.2.1"
structlog = "^24.1.0"
asyncpg = "^0.29.0"
rank_bm25 = "^0.2.2"
tenacity = "^8.2.3"
numpy = "1.26.4"
google-cloud-storage = "^2.16.0" 
cachetools = "^5.3.3"        


[tool.poetry.group.dev.dependencies]
pytest = "^7.4.4"
pytest-asyncio = "^0.21.1"
httpx = "^0.27.0" 

[build-system]
requires = ["poetry-core>=1.0.0"]
build-backend = "poetry.core.masonry.api"
```
