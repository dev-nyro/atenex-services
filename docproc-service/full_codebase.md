# Estructura de la Codebase

```
app/
├── __init__.py
├── api
│   ├── __init__.py
│   └── v1
│       ├── __init__.py
│       ├── endpoints
│       │   └── process_endpoint.py
│       └── schemas.py
├── application
│   ├── __init__.py
│   ├── ports
│   │   ├── __init__.py
│   │   ├── chunking_port.py
│   │   └── extraction_port.py
│   └── use_cases
│       ├── __init__.py
│       └── process_document_use_case.py
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
│   ├── chunkers
│   │   ├── __init__.py
│   │   └── default_chunker_adapter.py
│   └── extractors
│       ├── __init__.py
│       ├── base_extractor.py
│       ├── docx_adapter.py
│       ├── html_adapter.py
│       ├── md_adapter.py
│       ├── pdf_adapter.py
│       └── txt_adapter.py
└── main.py
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

## File: `app\api\v1\endpoints\process_endpoint.py`
```py
import structlog
from fastapi import (
    APIRouter, Depends, HTTPException, status,
    UploadFile, File, Form
)
from typing import Optional

from app.core.config import settings
from app.domain.models import ProcessResponse
from app.application.use_cases.process_document_use_case import ProcessDocumentUseCase
from app.application.ports.extraction_port import UnsupportedContentTypeError, ExtractionError
from app.application.ports.chunking_port import ChunkingError
from app.dependencies import get_process_document_use_case # Corrected import

router = APIRouter()
log = structlog.get_logger(__name__)

@router.post(
    "/process",
    response_model=ProcessResponse,
    summary="Process a document to extract text and generate chunks.",
    status_code=status.HTTP_200_OK,
    responses={
        status.HTTP_400_BAD_REQUEST: {"description": "Missing required form fields (file, original_filename, content_type)"},
        status.HTTP_415_UNSUPPORTED_MEDIA_TYPE: {"description": "Content type not supported for processing"},
        status.HTTP_422_UNPROCESSABLE_ENTITY: {"description": "File cannot be processed (e.g., corrupt, extraction error)"},
        status.HTTP_500_INTERNAL_SERVER_ERROR: {"description": "An unexpected error occurred"},
    }
)
async def process_document_endpoint(
    file: UploadFile = File(..., description="The document file to process."),
    original_filename: str = Form(..., description="Original filename of the uploaded document."),
    content_type: str = Form(..., description="MIME content type of the document."),
    document_id: Optional[str] = Form(None, description="Optional document ID for tracing purposes."),
    company_id: Optional[str] = Form(None, description="Optional company ID for tracing purposes."),
    # Chunking parameters can be made optional and taken from Form data
    # chunk_size: Optional[int] = Form(None, description=f"Optional chunk size (words/tokens). Defaults to service config: {settings.CHUNK_SIZE}."),
    # chunk_overlap: Optional[int] = Form(None, description=f"Optional chunk overlap. Defaults to service config: {settings.CHUNK_OVERLAP}."),
    use_case: ProcessDocumentUseCase = Depends(get_process_document_use_case) # Corrected dependency
):
    endpoint_log = log.bind(
        original_filename=original_filename,
        content_type=content_type,
        document_id_trace=document_id,
        company_id_trace=company_id
    )
    endpoint_log.info("Received document processing request")

    if not file or not original_filename or not content_type:
        # FastAPI usually handles this with 422 for missing File/Form fields
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Missing one or more required fields: file, original_filename, content_type."
        )
    
    if content_type not in settings.SUPPORTED_CONTENT_TYPES:
        endpoint_log.warning("Received unsupported content type")
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=f"Content type '{content_type}' is not supported. Supported types: {', '.join(settings.SUPPORTED_CONTENT_TYPES)}"
        )

    try:
        file_bytes = await file.read()
        if not file_bytes:
            endpoint_log.warning("Received an empty file.")
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Uploaded file is empty."
            )

        endpoint_log.debug("File read into bytes", file_size=len(file_bytes))

        # Pass optional chunk_size and chunk_overlap if they were part of the Form
        # For now, they are not, so use_case will use service defaults.
        response_data = await use_case.execute(
            file_bytes=file_bytes,
            original_filename=original_filename,
            content_type=content_type,
            # chunk_size=chunk_size, # Pass if defined in Form
            # chunk_overlap=chunk_overlap, # Pass if defined in Form
            document_id_trace=document_id,
            company_id_trace=company_id
        )
        
        endpoint_log.info("Document processed successfully by use case.")
        return ProcessResponse(data=response_data)

    except UnsupportedContentTypeError as e:
        endpoint_log.warning("Use case reported unsupported content type", error=str(e))
        raise HTTPException(status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, detail=str(e))
    except (ExtractionError, ChunkingError) as e: # Catch specific processing errors
        endpoint_log.error("Processing error (extraction/chunking)", error_type=type(e).__name__, error_detail=str(e), exc_info=True)
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=f"Failed to process document: {str(e)}")
    except HTTPException as e: # Re-raise HTTPExceptions directly
        raise e
    except Exception as e:
        endpoint_log.exception("Unexpected error during document processing")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"An unexpected error occurred: {type(e).__name__}"
        )
    finally:
        if file:
            await file.close()
            endpoint_log.debug("UploadFile closed.")
```

## File: `app\api\v1\schemas.py`
```py
# Re-exporting from domain models for clarity at API layer if needed,
# or define specific API DTOs if they differ from domain models.
# For this service, domain models are likely sufficient for API responses.

from app.domain.models import (
    ProcessResponse,
    ProcessResponseData,
    ProcessedDocumentMetadata,
    ProcessedChunk,
    ProcessedChunkSourceMetadata
)

__all__ = [
    "ProcessResponse",
    "ProcessResponseData",
    "ProcessedDocumentMetadata",
    "ProcessedChunk",
    "ProcessedChunkSourceMetadata"
]

# Example of an API-specific request schema if multipart form is not directly used by Pydantic model
# (FastAPI handles multipart form fields directly in endpoint signature)
# class ProcessRequest(BaseModel):
#     original_filename: str
#     content_type: str
#     document_id: Optional[str] = None # For tracing
#     company_id: Optional[str] = None  # For tracing
```

## File: `app\application\__init__.py`
```py

```

## File: `app\application\ports\__init__.py`
```py

```

## File: `app\application\ports\chunking_port.py`
```py
from abc import ABC, abstractmethod
from typing import List

class ChunkingError(Exception):
    """Base exception for chunking errors."""
    pass

class ChunkingPort(ABC):
    """
    Interface (Port) para la división de texto en fragmentos (chunks).
    """

    @abstractmethod
    def chunk_text(
        self,
        text_content: str,
        chunk_size: int,
        chunk_overlap: int
    ) -> List[str]:
        """
        Divide un bloque de texto en fragmentos más pequeños.

        Args:
            text_content: El texto a dividir.
            chunk_size: El tamaño deseado para cada chunk (en alguna unidad, ej. tokens o palabras).
            chunk_overlap: El tamaño del solapamiento entre chunks consecutivos.

        Returns:
            Una lista de strings, donde cada string es un chunk.

        Raises:
            ChunkingError: Si ocurre un error durante el chunking.
        """
        pass
```

## File: `app\application\ports\extraction_port.py`
```py
from abc import ABC, abstractmethod
from typing import List, Tuple, Union, Any, Dict # <--- AÑADIR Dict AQUÍ

class ExtractionError(Exception):
    """Base exception for extraction errors."""
    pass

class UnsupportedContentTypeError(ExtractionError):
    """Exception raised when a content type is not supported for extraction."""
    pass

class ExtractionPort(ABC):
    """
    Interface (Port) para la extracción de texto de documentos.
    """

    @abstractmethod
    def extract_text(
        self,
        file_bytes: bytes,
        filename: str,
        content_type: str
    ) -> Tuple[Union[str, List[Tuple[int, str]]], Dict[str, Any]]: # Ahora Dict es conocido
        """
        Extrae texto de los bytes de un archivo.

        Args:
            file_bytes: Contenido del archivo en bytes.
            filename: Nombre original del archivo (para logging y metadatos).
            content_type: Tipo MIME del archivo.

        Returns:
            Una tupla conteniendo:
            - El texto extraído. Puede ser un string único (para formatos sin páginas como TXT, MD, HTML)
              o una lista de tuplas (page_number, page_text) para formatos con páginas (como PDF).
            - Un diccionario con metadatos de la extracción (ej. {'total_pages_extracted': 10}).

        Raises:
            UnsupportedContentTypeError: Si el content_type no es soportado.
            ExtractionError: Para otros errores durante la extracción.
        """
        pass
```

## File: `app\application\use_cases\__init__.py`
```py

```

## File: `app\application\use_cases\process_document_use_case.py`
```py
import time
import structlog
from typing import List, Dict, Any, Optional, Tuple, Union

from app.core.config import settings
from app.domain.models import (
    ProcessedChunk,
    ProcessedDocumentMetadata,
    ProcessedChunkSourceMetadata,
    ProcessResponseData
)
from app.application.ports.extraction_port import ExtractionPort, UnsupportedContentTypeError, ExtractionError
from app.application.ports.chunking_port import ChunkingPort, ChunkingError

log = structlog.get_logger(__name__)

class ProcessDocumentUseCase:
    def __init__(
        self,
        extraction_port: ExtractionPort,
        chunking_port: ChunkingPort,
    ):
        self.extraction_port = extraction_port
        self.chunking_port = chunking_port
        self.log = log.bind(component="ProcessDocumentUseCase")

    async def execute(
        self,
        file_bytes: bytes,
        original_filename: str,
        content_type: str,
        # Parámetros de chunking pueden venir de la request o de la config del servicio
        chunk_size: Optional[int] = None, 
        chunk_overlap: Optional[int] = None,
        # IDs opcionales para tracing
        document_id_trace: Optional[str] = None, 
        company_id_trace: Optional[str] = None
    ) -> ProcessResponseData:
        
        start_time = time.perf_counter()
        
        use_case_log = self.log.bind(
            original_filename=original_filename, 
            content_type=content_type,
            document_id_trace=document_id_trace,
            company_id_trace=company_id_trace
        )
        use_case_log.info("Starting document processing")

        # Determinar parámetros de chunking
        effective_chunk_size = chunk_size if chunk_size is not None else settings.CHUNK_SIZE
        effective_chunk_overlap = chunk_overlap if chunk_overlap is not None else settings.CHUNK_OVERLAP

        # 1. Extracción de Texto
        try:
            extracted_data, extraction_meta = await self._run_extraction(file_bytes, original_filename, content_type)
        except UnsupportedContentTypeError as e:
            use_case_log.warning("Unsupported content type for extraction", error=str(e))
            raise  # Re-raise para que el endpoint lo maneje como 415
        except ExtractionError as e:
            use_case_log.error("Extraction failed", error=str(e))
            raise # Re-raise para que el endpoint lo maneje como 422 o 500

        # 2. Concatenación y Chunking
        all_chunks_data: List[ProcessedChunk] = []
        raw_text_length = 0

        if isinstance(extracted_data, str): # Para TXT, HTML, MD
            raw_text_length = len(extracted_data)
            if extracted_data.strip(): # Solo chunkear si hay contenido no vacío
                text_chunks = self._run_chunking(extracted_data, effective_chunk_size, effective_chunk_overlap)
                for text_chunk_content in text_chunks:
                    all_chunks_data.append(
                        ProcessedChunk(
                            text=text_chunk_content,
                            source_metadata=ProcessedChunkSourceMetadata() # Sin página para estos formatos
                        )
                    )
        elif isinstance(extracted_data, list): # Para PDF (lista de tuplas (page_num, page_text))
            current_page_text_for_chunking = ""
            for page_num, page_text in extracted_data:
                raw_text_length += len(page_text)
                if page_text.strip():
                    text_chunks_from_page = self._run_chunking(page_text, effective_chunk_size, effective_chunk_overlap)
                    for text_chunk_content in text_chunks_from_page:
                        all_chunks_data.append(
                            ProcessedChunk(
                                text=text_chunk_content,
                                source_metadata=ProcessedChunkSourceMetadata(page_number=page_num)
                            )
                        )
            # Alternativa: concatenar todo el texto del PDF y luego chunkear una vez.
            # Esto podría perder la granularidad de página para source_metadata de chunks que cruzan páginas.
            # La estrategia actual (chunkear página por página) es más simple para mantener metadatos de página.
        else:
            use_case_log.error("Unexpected data type from extraction port", type_received=type(extracted_data))
            raise ExtractionError("Internal error: Unexpected data type from extraction.")

        end_time = time.perf_counter()
        processing_time_ms = (end_time - start_time) * 1000

        doc_meta = ProcessedDocumentMetadata(
            original_filename=original_filename,
            content_type=content_type,
            total_pages_extracted=extraction_meta.get("total_pages_extracted"),
            raw_text_length_chars=raw_text_length,
            processing_time_ms=round(processing_time_ms, 2),
            num_chunks_generated=len(all_chunks_data)
        )

        use_case_log.info("Document processing finished successfully", 
                          num_chunks=len(all_chunks_data), 
                          processing_time_ms=doc_meta.processing_time_ms)
        
        return ProcessResponseData(document_metadata=doc_meta, chunks=all_chunks_data)

    async def _run_extraction(self, file_bytes: bytes, filename: str, content_type: str):
        # En un entorno real, podrías querer ejecutar esto en un ThreadPoolExecutor si es bloqueante
        # pero FastAPI maneja UploadFile de forma asíncrona y los extractores de PyMuPDF/docx
        # pueden ser CPU-bound pero no necesariamente bloquean el event loop si se manejan bien.
        # Por ahora, llamada directa.
        return self.extraction_port.extract_text(file_bytes, filename, content_type)

    def _run_chunking(self, text: str, chunk_size: int, chunk_overlap: int) -> List[str]:
        try:
            return self.chunking_port.chunk_text(text, chunk_size, chunk_overlap)
        except ChunkingError as e:
            self.log.error("Chunking failed during use case execution", error=str(e))
            # Decide si re-elevar o devolver lista vacía. Por ahora, re-elevar.
            raise


```

## File: `app\core\__init__.py`
```py

```

## File: `app\core\config.py`
```py
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
    "text/html"
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
                return parsed_list
            except json.JSONDecodeError:
                # Fallback to comma-separated if JSON parsing fails and it's a simple string
                if '[' not in v and ']' not in v: # Avoid trying to parse malformed JSON array as CSV
                    return [s.strip() for s in v.split(',') if s.strip()]
                raise ValueError("SUPPORTED_CONTENT_TYPES must be a valid JSON array of strings or a comma-separated string.")
        elif isinstance(v, list) and all(isinstance(item, str) for item in v):
            return v
        elif v is None: # Not set, use default
            return DEFAULT_SUPPORTED_CONTENT_TYPES
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
```

## File: `app\core\logging_config.py`
```py
import logging
import sys
import structlog
from app.core.config import settings # type: ignore
import os

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
    
    # Clear existing handlers only if we're not in a managed environment that might set them up (like Gunicorn)
    # A simple check is if any handlers are already StreamHandlers to stdout.
    # This avoids duplicate logs when Uvicorn/Gunicorn also configures logging.
    is_stdout_handler_present = any(
        isinstance(h, logging.StreamHandler) and h.stream == sys.stdout
        for h in root_logger.handlers
    )
    if not is_stdout_handler_present:
        # Clear all handlers if no specific stdout handler detected, to avoid conflicts
        # But this might be too aggressive if other handlers are desired (e.g. file logger from a lib)
        # For service logging, focusing on stdout is usually fine.
        # root_logger.handlers.clear() # Commented out to be less aggressive
        root_logger.addHandler(handler)
    elif not root_logger.handlers: # If no handlers at all, add ours.
        root_logger.addHandler(handler)


    root_logger.setLevel(settings.LOG_LEVEL)

    logging.getLogger("uvicorn").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("gunicorn.error").setLevel(logging.WARNING) # Gunicorn logs to stderr by default
    logging.getLogger("httpx").setLevel(logging.WARNING)
    
    # For PyMuPDF, set to WARNING to avoid too many debug messages unless needed
    logging.getLogger("fitz").setLevel(logging.WARNING)


    log = structlog.get_logger(settings.PROJECT_NAME)
    log.info("Logging configured", log_level=settings.LOG_LEVEL)
```

## File: `app\dependencies.py`
```py
from functools import lru_cache
from typing import Dict, Type

from app.application.ports.extraction_port import ExtractionPort
from app.application.ports.chunking_port import ChunkingPort
from app.application.use_cases.process_document_use_case import ProcessDocumentUseCase

from app.infrastructure.extractors.pdf_adapter import PdfAdapter
from app.infrastructure.extractors.docx_adapter import DocxAdapter
from app.infrastructure.extractors.txt_adapter import TxtAdapter
from app.infrastructure.extractors.html_adapter import HtmlAdapter
from app.infrastructure.extractors.md_adapter import MdAdapter
from app.infrastructure.chunkers.default_chunker_adapter import DefaultChunkerAdapter

from app.core.config import settings
import structlog

log = structlog.get_logger(__name__)

class CompositeExtractionAdapter(ExtractionPort):
    """
    Un adaptador de extracción compuesto que delega al adaptador apropiado
    basado en el content_type.
    """
    def __init__(self, adapters: Dict[str, ExtractionPort]):
        self.adapters = adapters
        self.log = log.bind(component="CompositeExtractionAdapter")

    def extract_text(self, file_bytes: bytes, filename: str, content_type: str):
        self.log.debug("Attempting extraction", filename=filename, content_type=content_type)
        
        # Find adapter that supports this content type
        selected_adapter: Optional[ExtractionPort] = None
        for adapter_instance in self.adapters.values():
            # Adapters should ideally expose their supported types or be selected more directly
            # For now, we assume adapters raise UnsupportedContentTypeError if they can't handle it
            # A better approach would be to register adapters with their supported MIME types.
            # Here, we try them or have specific logic.
            # For this example, we'll rely on a pre-defined mapping in the factory.
            # This Composite adapter is more of a dispatcher.
            # A simpler way in the factory is to just pick the right one.
            # Let's re-think: the factory should provide the *specific* adapter,
            # or this composite adapter needs a way to know which one to call.

            # Simplified: Assume the ProcessDocumentUseCase will choose one.
            # This composite is not strictly needed if the use case itself can select.
            # However, to keep the use case clean from knowing *all* adapters,
            # a factory function for ExtractionPort in dependencies.py is better.

            # Let's refine: This Composite is not used. Instead, a factory creates the right one.
            # Keeping this class for conceptual reference, but get_extraction_port is the way.
            pass # This class will not be directly used as a port implementation for DI to use case

        # This method will not be called if the factory directly provides the correct adapter.
        # This is more for a scenario where the UseCase gets *this* composite.
        # For now, it's unused.
        raise NotImplementedError("CompositeExtractionAdapter.extract_text should not be called directly if factory provides specific adapter.")


@lru_cache()
def get_pdf_adapter() -> PdfAdapter:
    return PdfAdapter()

@lru_cache()
def get_docx_adapter() -> DocxAdapter:
    return DocxAdapter()

@lru_cache()
def get_txt_adapter() -> TxtAdapter:
    return TxtAdapter()

@lru_cache()
def get_html_adapter() -> HtmlAdapter:
    return HtmlAdapter()

@lru_cache()
def get_md_adapter() -> MdAdapter:
    return MdAdapter()


# Factory function to provide the correct ExtractionPort based on content_type
# This is not directly injectable into FastAPI's Depends if content_type is runtime data.
# So, the ProcessDocumentUseCase will need access to all relevant adapters
# or a way to request one. Let's provide all to the use case for now, or make the use case resolve it.

# For simplicity, the UseCase will have a mapping or similar logic.
# Or, the endpoint can determine the adapter and pass it.
# Let's make the UseCase smarter with a dictionary of available extractors.

@lru_cache()
def get_all_extraction_adapters() -> Dict[str, ExtractionPort]:
    """Returns a dictionary of all configured extraction adapters, keyed by a representative content type."""
    # This is a simplified mapping. A more robust solution might involve
    # querying adapters for their supported types.
    adapters = {
        "application/pdf": get_pdf_adapter(),
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document": get_docx_adapter(),
        "application/msword": get_docx_adapter(), # DOCX adapter might handle some .doc
        "text/plain": get_txt_adapter(),
        "text/html": get_html_adapter(),
        "text/markdown": get_md_adapter(),
    }
    # Filter by configured supported types
    return {
        ct: adapter for ct, adapter in adapters.items() if ct in settings.SUPPORTED_CONTENT_TYPES
    }


class FlexibleExtractionPort(ExtractionPort):
    """
    An ExtractionPort implementation that dynamically selects the correct adapter.
    This is what will be injected into the UseCase.
    """
    def __init__(self):
        self.adapters_map = get_all_extraction_adapters()
        self.log = log.bind(component="FlexibleExtractionPort")
        self.log.info("Initialized FlexibleExtractionPort with adapters", adapters=list(self.adapters_map.keys()))


    def extract_text(self, file_bytes: bytes, filename: str, content_type: str):
        self.log.debug("FlexibleExtractionPort: Attempting extraction", filename=filename, content_type=content_type)
        
        adapter_to_use = None
        # Prioritize exact match
        if content_type in self.adapters_map:
            adapter_to_use = self.adapters_map[content_type]
        else:
            # Fallback for similar types, e.g. if only specific docx mimetype is registered
            # but a more generic one is passed. This logic can be expanded.
            if content_type.startswith("text/"): # Generic text might be plain
                 adapter_to_use = self.adapters_map.get("text/plain")

        if adapter_to_use:
            self.log.info(f"Using adapter {type(adapter_to_use).__name__} for {content_type}")
            return adapter_to_use.extract_text(file_bytes, filename, content_type)
        else:
            self.log.warning("No suitable adapter found for content type", content_type=content_type)
            from app.application.ports.extraction_port import UnsupportedContentTypeError
            raise UnsupportedContentTypeError(f"No configured adapter for content type: {content_type}")


@lru_cache()
def get_flexible_extraction_port() -> ExtractionPort:
    return FlexibleExtractionPort()


@lru_cache()
def get_default_chunker_adapter() -> ChunkingPort:
    return DefaultChunkerAdapter()

@lru_cache()
def get_process_document_use_case() -> ProcessDocumentUseCase:
    # This is where specific adapters are chosen or a composite/flexible one is passed
    extraction_port = get_flexible_extraction_port()
    chunking_port = get_default_chunker_adapter()
    
    log.info("Creating ProcessDocumentUseCase instance", 
             extraction_port_type=type(extraction_port).__name__,
             chunking_port_type=type(chunking_port).__name__)
    
    return ProcessDocumentUseCase(
        extraction_port=extraction_port,
        chunking_port=chunking_port
    )
```

## File: `app\domain\__init__.py`
```py

```

## File: `app\domain\models.py`
```py
from typing import Optional, Dict, Any, List
from pydantic import BaseModel, Field

class ProcessedChunkSourceMetadata(BaseModel):
    """Metadatos originados durante el procesamiento en docproc-service para un chunk."""
    page_number: Optional[int] = Field(None, description="Número de página original del chunk (si aplica, ej. PDF).")
    # Se pueden añadir otros metadatos específicos del extractor o chunker aquí
    # por ejemplo, tipo de contenido original del bloque (ej. "table", "paragraph")
    # original_block_type: Optional[str] = None

class ProcessedChunk(BaseModel):
    """Representa un chunk de texto procesado."""
    text: str = Field(..., description="El contenido textual del chunk.")
    source_metadata: ProcessedChunkSourceMetadata = Field(default_factory=ProcessedChunkSourceMetadata, description="Metadatos asociados al origen del chunk.")

class ProcessedDocumentMetadata(BaseModel):
    """Metadatos generales sobre el documento procesado."""
    original_filename: str = Field(..., description="Nombre original del archivo procesado.")
    content_type: str = Field(..., description="Tipo MIME del archivo procesado.")
    total_pages_extracted: Optional[int] = Field(None, description="Número total de páginas de las que se extrajo texto (ej. para PDF).")
    raw_text_length_chars: int = Field(..., description="Longitud del texto crudo extraído en caracteres.")
    processing_time_ms: float = Field(..., description="Tiempo total de procesamiento en milisegundos.")
    num_chunks_generated: int = Field(..., description="Número total de chunks generados.")

class ProcessResponseData(BaseModel):
    """Datos contenidos en una respuesta exitosa del endpoint de procesamiento."""
    document_metadata: ProcessedDocumentMetadata
    chunks: List[ProcessedChunk]

class ProcessResponse(BaseModel):
    """Schema de respuesta para el endpoint POST /api/v1/process."""
    data: ProcessResponseData

    class Config:
        json_schema_extra = {
            "example": {
                "data": {
                    "document_metadata": {
                        "original_filename": "example_document.pdf",
                        "content_type": "application/pdf",
                        "total_pages_extracted": 10,
                        "raw_text_length_chars": 15000,
                        "processing_time_ms": 543.21,
                        "num_chunks_generated": 15
                    },
                    "chunks": [
                        {
                            "text": "Este es el contenido del primer chunk procesado...",
                            "source_metadata": {"page_number": 1}
                        },
                        {
                            "text": "Este es el contenido del segundo chunk procesado...",
                            "source_metadata": {"page_number": 1}
                        }
                    ]
                }
            }
        }
```

## File: `app\infrastructure\__init__.py`
```py

```

## File: `app\infrastructure\chunkers\__init__.py`
```py

```

## File: `app\infrastructure\chunkers\default_chunker_adapter.py`
```py
import structlog
from typing import List

from app.application.ports.chunking_port import ChunkingPort, ChunkingError
from app.core.config import settings as service_settings # Use specific settings for docproc

log = structlog.get_logger(__name__)

class DefaultChunkerAdapter(ChunkingPort):
    """
    Adaptador de chunking por defecto, basado en división por palabras y solapamiento.
    Reutiliza la lógica de text_splitter.py del ingest-service.
    """

    def chunk_text(
        self,
        text_content: str,
        chunk_size: int, # Parameter passed from use case, originating from request or service default
        chunk_overlap: int
    ) -> List[str]:
        if not text_content or text_content.isspace():
            log.debug("DefaultChunkerAdapter: Empty or whitespace-only text provided, returning no chunks.")
            return []
        
        if chunk_size <= 0:
            raise ChunkingError(f"Chunk size must be positive. Received: {chunk_size}")
        if chunk_overlap < 0:
            raise ChunkingError(f"Chunk overlap must be non-negative. Received: {chunk_overlap}")
        if chunk_overlap >= chunk_size:
            raise ChunkingError(f"Chunk overlap ({chunk_overlap}) must be less than chunk size ({chunk_size}).")

        log.debug("DefaultChunkerAdapter: Splitting text into chunks", 
                  text_length=len(text_content), 
                  chunk_size=chunk_size, 
                  chunk_overlap=chunk_overlap)
        
        # Lógica de text_splitter.py (adaptada)
        # Asumimos que chunk_size y chunk_overlap se refieren a número de palabras.
        # Si se refiere a caracteres, la lógica debería ajustarse.
        # Por simplicidad y para coincidir con el text_splitter original, usamos palabras.
        
        words = text_content.split() # Split by whitespace
        if not words:
            log.debug("DefaultChunkerAdapter: Text content resulted in no words after split.")
            return []

        chunks: List[str] = []
        current_pos = 0
        
        while current_pos < len(words):
            end_pos = min(current_pos + chunk_size, len(words))
            chunk_words = words[current_pos:end_pos]
            chunks.append(" ".join(chunk_words))
            
            if end_pos == len(words): # Reached the end
                break
            
            current_pos += (chunk_size - chunk_overlap)
            if current_pos >= len(words): # Prevent infinite loop if step is too small or overlap too large making step 0 or negative.
                # This should not happen if overlap < size.
                log.warning("DefaultChunkerAdapter: Chunking step led to no progress, breaking loop.", current_pos=current_pos, num_words=len(words))
                break
        
        log.info("DefaultChunkerAdapter: Text split into chunks", num_chunks=len(chunks))
        return chunks
```

## File: `app\infrastructure\extractors\__init__.py`
```py

```

## File: `app\infrastructure\extractors\base_extractor.py`
```py
import structlog
from app.application.ports.extraction_port import ExtractionPort, ExtractionError

log = structlog.get_logger(__name__)

class BaseExtractorAdapter(ExtractionPort):
    """
    Clase base para adaptadores de extracción con logging común.
    """
    def _handle_extraction_error(self, e: Exception, filename: str, adapter_name: str) -> ExtractionError:
        log.error(f"{adapter_name} extraction failed", filename=filename, error=str(e), exc_info=True)
        raise ExtractionError(f"Error extracting with {adapter_name} for {filename}: {e}") from e
```

## File: `app\infrastructure\extractors\docx_adapter.py`
```py
import io
import docx  # python-docx
import structlog
from typing import Tuple, Dict, Any, Union, List

from app.application.ports.extraction_port import ExtractionPort, ExtractionError, UnsupportedContentTypeError
from app.infrastructure.extractors.base_extractor import BaseExtractorAdapter

log = structlog.get_logger(__name__)

class DocxAdapter(BaseExtractorAdapter):
    """Adaptador para extraer texto de archivos DOCX."""

    SUPPORTED_CONTENT_TYPES = [
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/msword" # python-docx can sometimes handle .doc, but it's not guaranteed
    ]

    def extract_text(
        self,
        file_bytes: bytes,
        filename: str,
        content_type: str
    ) -> Tuple[str, Dict[str, Any]]:
        if content_type not in self.SUPPORTED_CONTENT_TYPES:
            raise UnsupportedContentTypeError(f"DocxAdapter does not support content type: {content_type}")

        log.debug("DocxAdapter: Extracting text from DOCX bytes", filename=filename)
        extraction_metadata: Dict[str, Any] = {}
        try:
            doc = docx.Document(io.BytesIO(file_bytes))
            text = "\n".join([p.text for p in doc.paragraphs if p.text and not p.text.isspace()])
            
            extraction_metadata["num_paragraphs_extracted"] = len([p for p in doc.paragraphs if p.text and not p.text.isspace()])
            log.info("DocxAdapter: DOCX extraction successful", filename=filename, num_paragraphs=extraction_metadata["num_paragraphs_extracted"])
            return text, extraction_metadata
        except Exception as e:
            # If it's a .doc file, it might fail here. Log a specific warning.
            if content_type == "application/msword":
                log.warning("DocxAdapter: Failed to process .doc file. This format has limited support.", filename=filename, error=str(e))
            raise self._handle_extraction_error(e, filename, "DocxAdapter")
```

## File: `app\infrastructure\extractors\html_adapter.py`
```py
from bs4 import BeautifulSoup
import structlog
from typing import Tuple, Dict, Any, Union, List

from app.application.ports.extraction_port import ExtractionPort, ExtractionError, UnsupportedContentTypeError
from app.infrastructure.extractors.base_extractor import BaseExtractorAdapter

log = structlog.get_logger(__name__)

class HtmlAdapter(BaseExtractorAdapter):
    """Adaptador para extraer texto de archivos HTML."""

    SUPPORTED_CONTENT_TYPES = ["text/html"]

    def extract_text(
        self,
        file_bytes: bytes,
        filename: str,
        content_type: str,
        encoding: str = "utf-8" # Default encoding for HTML
    ) -> Tuple[str, Dict[str, Any]]:
        if content_type not in self.SUPPORTED_CONTENT_TYPES:
            raise UnsupportedContentTypeError(f"HtmlAdapter does not support content type: {content_type}")

        log.debug("HtmlAdapter: Extracting text from HTML bytes", filename=filename)
        extraction_metadata: Dict[str, Any] = {}
        try:
            # BeautifulSoup typically handles encoding detection well, but providing a hint can help.
            html_content = file_bytes.decode(encoding, errors='replace') # Replace errors to avoid decode failure
            soup = BeautifulSoup(html_content, "html.parser")
            
            # Remove script and style elements
            for script_or_style in soup(["script", "style"]):
                script_or_style.decompose()
            
            text = soup.get_text(separator="\n", strip=True)
            
            extraction_metadata["title_extracted"] = soup.title.string if soup.title else None
            log.info("HtmlAdapter: HTML extraction successful", filename=filename, length=len(text))
            return text, extraction_metadata
        except Exception as e:
            raise self._handle_extraction_error(e, filename, "HtmlAdapter")
```

## File: `app\infrastructure\extractors\md_adapter.py`
```py
import markdown
import html2text # To convert HTML generated from Markdown to clean text
import structlog
from typing import Tuple, Dict, Any, Union, List

from app.application.ports.extraction_port import ExtractionPort, ExtractionError, UnsupportedContentTypeError
from app.infrastructure.extractors.base_extractor import BaseExtractorAdapter

log = structlog.get_logger(__name__)

class MdAdapter(BaseExtractorAdapter):
    """Adaptador para extraer texto de archivos Markdown."""

    SUPPORTED_CONTENT_TYPES = ["text/markdown"]

    def extract_text(
        self,
        file_bytes: bytes,
        filename: str,
        content_type: str,
        encoding: str = "utf-8" # Default encoding
    ) -> Tuple[str, Dict[str, Any]]:
        if content_type not in self.SUPPORTED_CONTENT_TYPES:
            raise UnsupportedContentTypeError(f"MdAdapter does not support content type: {content_type}")

        log.debug("MdAdapter: Extracting text from MD bytes", filename=filename)
        extraction_metadata: Dict[str, Any] = {}
        try:
            md_text_content = file_bytes.decode(encoding)
            html = markdown.markdown(md_text_content)
            
            # Use html2text to get cleaner text from the rendered HTML
            text_maker = html2text.HTML2Text()
            text_maker.ignore_links = True # Example: ignore links, can be configured
            text_maker.ignore_images = True
            text = text_maker.handle(html)
            
            log.info("MdAdapter: MD extraction successful", filename=filename, length=len(text))
            return text, extraction_metadata
        except Exception as e:
            raise self._handle_extraction_error(e, filename, "MdAdapter")
```

## File: `app\infrastructure\extractors\pdf_adapter.py`
```py
import fitz  # PyMuPDF
import structlog
from typing import List, Tuple, Dict, Any, Union

from app.application.ports.extraction_port import ExtractionPort, ExtractionError, UnsupportedContentTypeError
from app.infrastructure.extractors.base_extractor import BaseExtractorAdapter


log = structlog.get_logger(__name__)

class PdfAdapter(BaseExtractorAdapter):
    """Adaptador para extraer texto de archivos PDF usando PyMuPDF."""

    SUPPORTED_CONTENT_TYPES = ["application/pdf"]

    def extract_text(
        self,
        file_bytes: bytes,
        filename: str,
        content_type: str
    ) -> Tuple[List[Tuple[int, str]], Dict[str, Any]]:
        if content_type not in self.SUPPORTED_CONTENT_TYPES:
            raise UnsupportedContentTypeError(f"PdfAdapter does not support content type: {content_type}")

        log.debug("PdfAdapter: Extracting text and pages from PDF bytes", filename=filename)
        pages_content: List[Tuple[int, str]] = []
        extraction_metadata: Dict[str, Any] = {"total_pages_extracted": 0}
        total_pages_in_doc = 0

        try:
            with fitz.open(stream=file_bytes, filetype="pdf") as doc:
                total_pages_in_doc = len(doc)
                log.info("PdfAdapter: Processing PDF document", filename=filename, num_pages_in_doc=total_pages_in_doc)
                for page_num_zero_based, page in enumerate(doc):
                    page_num_one_based = page_num_zero_based + 1
                    try:
                        page_text = page.get_text("text")
                        if page_text and not page_text.isspace():
                            pages_content.append((page_num_one_based, page_text))
                            log.debug("PdfAdapter: Extracted text from page", page=page_num_one_based, length=len(page_text))
                        else:
                            log.debug("PdfAdapter: Skipping empty or whitespace-only page", page=page_num_one_based)
                    except Exception as page_err:
                        log.warning("PdfAdapter: Error extracting text from PDF page", filename=filename, page=page_num_one_based, error=str(page_err))
                
                extraction_metadata["total_pages_extracted"] = len(pages_content)
                log.info("PdfAdapter: PDF extraction successful", filename=filename, pages_with_text=len(pages_content), total_doc_pages=total_pages_in_doc)
                return pages_content, extraction_metadata
        except Exception as e:
            raise self._handle_extraction_error(e, filename, "PdfAdapter")
```

## File: `app\infrastructure\extractors\txt_adapter.py`
```py
import structlog
from typing import Tuple, Dict, Any, Union, List

from app.application.ports.extraction_port import ExtractionPort, ExtractionError, UnsupportedContentTypeError
from app.infrastructure.extractors.base_extractor import BaseExtractorAdapter

log = structlog.get_logger(__name__)

class TxtAdapter(BaseExtractorAdapter):
    """Adaptador para extraer texto de archivos TXT."""

    SUPPORTED_CONTENT_TYPES = ["text/plain"]

    def extract_text(
        self,
        file_bytes: bytes,
        filename: str,
        content_type: str,
        encoding: str = "utf-8" # Default encoding
    ) -> Tuple[str, Dict[str, Any]]:
        if content_type not in self.SUPPORTED_CONTENT_TYPES:
            raise UnsupportedContentTypeError(f"TxtAdapter does not support content type: {content_type}")

        log.debug("TxtAdapter: Extracting text from TXT bytes", filename=filename, encoding=encoding)
        extraction_metadata: Dict[str, Any] = {}
        try:
            # Try common encodings if default utf-8 fails
            encodings_to_try = [encoding, 'latin-1', 'iso-8859-1', 'cp1252']
            text = None
            for enc in encodings_to_try:
                try:
                    text = file_bytes.decode(enc)
                    log.info(f"TxtAdapter: Successfully decoded with {enc}", filename=filename)
                    extraction_metadata["encoding_used"] = enc
                    break
                except UnicodeDecodeError:
                    log.debug(f"TxtAdapter: Failed to decode with {enc}, trying next.", filename=filename)
                    continue
            
            if text is None:
                log.error("TxtAdapter: Could not decode TXT file with tried encodings.", filename=filename)
                raise ExtractionError(f"Could not decode TXT file {filename} with tried encodings.")

            log.info("TxtAdapter: TXT extraction successful", filename=filename, length=len(text))
            return text, extraction_metadata
        except Exception as e:
            if not isinstance(e, ExtractionError): # Avoid re-wrapping known ExtractionError
                raise self._handle_extraction_error(e, filename, "TxtAdapter")
            raise e
```

## File: `app\main.py`
```py
import time
import uuid
import structlog
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, status as fastapi_status
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.exceptions import RequestValidationError, ResponseValidationError, HTTPException

# Setup logging first
from app.core.logging_config import setup_logging
setup_logging() # Initialize logging system

# Then import other modules that might use logging
from app.core.config import settings
from app.api.v1.endpoints import process_endpoint

log = structlog.get_logger(settings.PROJECT_NAME)

# --- Lifespan Management ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info(f"{settings.PROJECT_NAME} startup sequence initiated...")
    # Add any async resource initialization here (e.g., DB pools if needed)
    # For docproc-service, it's mostly stateless or initializes resources per request/use case.
    log.info(f"{settings.PROJECT_NAME} is ready and running.")
    yield
    # Add any async resource cleanup here
    log.info(f"{settings.PROJECT_NAME} shutdown sequence initiated...")
    log.info(f"{settings.PROJECT_NAME} shutdown complete.")

# --- FastAPI App Instance ---
app = FastAPI(
    title=settings.PROJECT_NAME,
    openapi_url=f"{settings.API_V1_STR}/openapi.json",
    version="0.1.0",
    description="Atenex Document Processing Service for text extraction and chunking.",
    lifespan=lifespan
)

# --- Middlewares ---
@app.middleware("http")
async def add_request_context_timing_logging(request: Request, call_next):
    start_time = time.perf_counter()
    request_id = request.headers.get("x-request-id", str(uuid.uuid4()))
    
    structlog.contextvars.bind_contextvars(request_id=request_id)
    # For access in endpoint logs if not using contextvars directly there
    request.state.request_id = request_id 

    req_log = log.bind(method=request.method, path=request.url.path, client_host=request.client.host if request.client else "unknown")
    req_log.info("Request received")

    response = None
    try:
        response = await call_next(request)
        process_time_ms = (time.perf_counter() - start_time) * 1000
        
        resp_log = req_log.bind(status_code=response.status_code, duration_ms=round(process_time_ms, 2))
        log_level_method = "warning" if 400 <= response.status_code < 500 else "error" if response.status_code >= 500 else "info"
        getattr(resp_log, log_level_method)("Request finished") # Use getattr to call log method

        response.headers["X-Request-ID"] = request_id
        response.headers["X-Process-Time-Ms"] = f"{process_time_ms:.2f}"
    except Exception as e:
        process_time_ms = (time.perf_counter() - start_time) * 1000
        exc_log = req_log.bind(status_code=500, duration_ms=round(process_time_ms, 2)) # Default to 500 for unhandled
        exc_log.exception("Unhandled exception during request processing") # Logs with traceback
        
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
async def custom_http_exception_handler(request: Request, exc: HTTPException):
    request_id = getattr(request.state, 'request_id', 'N/A')
    log_method = log.warning if exc.status_code < 500 else log.error
    log_method(
        "HTTP Exception caught", 
        status_code=exc.status_code, 
        detail=exc.detail,
        request_id=request_id
    )
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail, "request_id": request_id},
        headers=getattr(exc, "headers", None),
    )

@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    request_id = getattr(request.state, 'request_id', 'N/A')
    log.warning("Request Validation Error", errors=exc.errors(), path=request.url.path, request_id=request_id)
    return JSONResponse(
        status_code=fastapi_status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={"detail": "Validation Error", "errors": exc.errors(), "request_id": request_id},
    )

@app.exception_handler(ResponseValidationError)
async def response_validation_error_handler(request: Request, exc: ResponseValidationError):
    request_id = getattr(request.state, 'request_id', 'N/A')
    log.error("Response Validation Error", errors=exc.errors(), path=request.url.path, request_id=request_id, exc_info=True)
    return JSONResponse(
        status_code=fastapi_status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"detail": "Internal Error: Response validation failed", "errors": exc.errors(), "request_id": request_id},
    )

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    request_id = getattr(request.state, 'request_id', 'N/A')
    log.exception("Unhandled global exception caught", path=request.url.path, request_id=request_id) # Ensures full traceback
    return JSONResponse(
        status_code=fastapi_status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"detail": "An unexpected internal server error occurred.", "request_id": request_id}
    )

# --- API Router Inclusion ---
app.include_router(process_endpoint.router, prefix=settings.API_V1_STR, tags=["Document Processing"])
log.info(f"Included processing router with prefix: {settings.API_V1_STR}")


# --- Health Check Endpoint ---
@app.get(
    "/health",
    tags=["Health Check"],
    summary="Performs a health check of the service.",
    response_description="Returns the health status of the service.",
    status_code=fastapi_status.HTTP_200_OK,
)
async def health_check():
    log.debug("Health check endpoint called")
    return {
        "status": "ok",
        "service": settings.PROJECT_NAME,
        "version": app.version # FastAPI app version
    }

# --- Root Endpoint ---
@app.get("/", include_in_schema=False)
async def root():
    return PlainTextResponse(f"{settings.PROJECT_NAME} is running.")

if __name__ == "__main__":
    import uvicorn
    log.info(f"Starting {settings.PROJECT_NAME} locally on port {settings.PORT}")
    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=settings.PORT,
        log_level=settings.LOG_LEVEL.lower(),
        reload=True # Enable reload for local development
    )

# === 0.1.0 ===
# - jfu 2
```

## File: `pyproject.toml`
```toml
[tool.poetry]
name = "docproc-service"
version = "1.1.0"
description = "Atenex Document Processing Service: Extracts text and chunks documents."
authors = ["Atenex Team <dev@atenex.com>"]
readme = "README.md"

[tool.poetry.dependencies]
python = ">=3.10,<3.13"
fastapi = "^0.110.0"
uvicorn = {extras = ["standard"], version = "^0.28.0"}
gunicorn = "^21.2.0"
pydantic = {extras = ["email"], version = "^2.6.4"}
pydantic-settings = "^2.2.1"
structlog = "^24.1.0"
python-multipart = "^0.0.9" # For FastAPI File Uploads
httpx = "^0.27.0" # For potential internal calls or future use

# Extraction Libraries
pymupdf = "^1.25.0"
python-docx = ">=1.1.0,<2.0.0"
markdown = ">=3.5.1,<4.0.0"
beautifulsoup4 = ">=4.12.3,<5.0.0"
html2text = ">=2024.1.0,<2025.0.0"

[tool.poetry.group.dev.dependencies]
pytest = "^7.4.4"
pytest-asyncio = "^0.21.1"

[build-system]
requires = ["poetry-core>=1.0.0"]
build-backend = "poetry.core.masonry.api"
```
