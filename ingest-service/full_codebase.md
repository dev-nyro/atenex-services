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
│       │   └── ingest_endpoint.py
│       └── schemas.py
├── application
│   ├── __init__.py
│   ├── ports
│   │   ├── __init__.py
│   │   ├── chunk_repository_port.py
│   │   ├── docproc_service_port.py
│   │   ├── document_repository_port.py
│   │   ├── embedding_service_port.py
│   │   ├── file_storage_port.py
│   │   ├── task_queue_port.py
│   │   └── vector_store_port.py
│   └── use_cases
│       ├── __init__.py
│       ├── delete_document_use_case.py
│       ├── get_document_stats_use_case.py
│       ├── get_document_status_use_case.py
│       ├── list_documents_use_case.py
│       ├── process_document_use_case.py
│       ├── retry_document_use_case.py
│       └── upload_document_use_case.py
├── core
│   ├── __init__.py
│   ├── config.py
│   └── logging_config.py
├── db
│   └── postgres_client.py
├── domain
│   ├── __init__.py
│   ├── entities.py
│   ├── enums.py
│   └── exceptions.py
├── infrastructure
│   ├── __init__.py
│   ├── clients
│   │   ├── __init__.py
│   │   ├── remote_docproc_adapter.py
│   │   └── remote_embedding_adapter.py
│   ├── dependency_injection.py
│   ├── persistence
│   │   ├── __init__.py
│   │   ├── postgres_chunk_repository.py
│   │   ├── postgres_connector.py
│   │   └── postgres_document_repository.py
│   ├── storage
│   │   ├── __init__.py
│   │   └── gcs_adapter.py
│   ├── tasks
│   │   ├── __init__.py
│   │   ├── celery_app_config.py
│   │   ├── celery_task_adapter.py
│   │   └── celery_worker.py
│   └── vectorstore
│       ├── __init__.py
│       └── milvus_adapter.py
├── main.py
├── models
│   └── domain.py
├── services
│   ├── __init__.py
│   ├── base_client.py
│   ├── clients
│   │   ├── docproc_service_client.py
│   │   └── embedding_service_client.py
│   ├── gcs_client.py
│   └── ingest_pipeline.py
└── tasks
    └── process_document.py
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

## File: `app\api\v1\endpoints\ingest_endpoint.py`
```py
from fastapi import APIRouter, Depends, HTTPException, status, UploadFile, File, Form, Query, Path, Request, Body
from typing import List, Optional, Dict, Any
import uuid
import json
from datetime import date

from app.api.v1.schemas import IngestResponse, StatusResponse, DocumentStatsResponse, ErrorDetail # PaginatedStatusResponse removed as List[StatusResponse] is used directly
from app.domain.entities import Document # For type hint
from app.domain.enums import DocumentStatus

from app.infrastructure.dependency_injection import (
    get_upload_document_use_case,
    get_get_document_status_use_case,
    get_list_documents_use_case,
    get_retry_document_use_case,
    get_delete_document_use_case,
    get_get_document_stats_use_case
)
from app.application.use_cases.upload_document_use_case import UploadDocumentUseCase
from app.application.use_cases.get_document_status_use_case import GetDocumentStatusUseCase
from app.application.use_cases.list_documents_use_case import ListDocumentsUseCase
from app.application.use_cases.retry_document_use_case import RetryDocumentUseCase
from app.application.use_cases.delete_document_use_case import DeleteDocumentUseCase
from app.application.use_cases.get_document_stats_use_case import GetDocumentStatsUseCase

from app.domain.exceptions import (
    DocumentNotFoundException, 
    DuplicateDocumentException, 
    OperationConflictException, 
    InvalidInputException,
    DomainException # Generic domain exception
)
import structlog

log = structlog.get_logger(__name__)
router = APIRouter()


@router.post(
    "/upload", 
    response_model=IngestResponse, 
    status_code=status.HTTP_202_ACCEPTED,
    summary="Upload a document for asynchronous ingestion",
    responses={
        status.HTTP_400_BAD_REQUEST: {"model": ErrorDetail, "description": "Invalid input or metadata"},
        status.HTTP_409_CONFLICT: {"model": ErrorDetail, "description": "Duplicate document"},
        status.HTTP_422_UNPROCESSABLE_ENTITY: {"model": ErrorDetail, "description": "Missing or invalid headers"},
        status.HTTP_503_SERVICE_UNAVAILABLE: {"model": ErrorDetail, "description": "Dependent service error (DB, Storage, Task Queue)"}
    }
)
async def upload_document_endpoint(
    request: Request,
    file: UploadFile = File(...),
    metadata_json: Optional[str] = Form(None),
    use_case: UploadDocumentUseCase = Depends(get_upload_document_use_case)
):
    endpoint_log = log.bind(request_id=getattr(request.state, "request_id", None))
    company_id_str = request.headers.get("X-Company-ID")
    user_id_str = request.headers.get("X-User-ID")

    if not company_id_str or not user_id_str:
        endpoint_log.warning("Missing required X-Company-ID or X-User-ID headers for upload.")
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Missing required headers: X-Company-ID and X-User-ID")
    
    try:
        company_uuid = uuid.UUID(company_id_str)
        user_uuid = uuid.UUID(user_id_str) # user_uuid currently not used by use_case but good to validate
    except ValueError:
        endpoint_log.warning("Invalid UUID format in headers for upload.", company_id=company_id_str, user_id=user_id_str)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid UUID format in headers")

    try:
        file_content = await file.read()
        metadata_dict = {}
        if metadata_json:
            try:
                metadata_dict = json.loads(metadata_json)
                if not isinstance(metadata_dict, dict):
                    raise ValueError("Metadata must be a JSON object.")
            except (json.JSONDecodeError, ValueError) as e:
                endpoint_log.warning("Invalid metadata JSON for upload.", error=str(e))
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Invalid metadata JSON: {e}")

        result_dict = await use_case.execute(
            company_id=company_uuid,
            user_id=user_uuid,
            file_name=file.filename or "untitled",
            file_type=file.content_type or "application/octet-stream",
            file_content=file_content,
            metadata=metadata_dict
        )
        return IngestResponse(**result_dict)
    
    except InvalidInputException as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except DuplicateDocumentException as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e))
    except DomainException as e: # Catch other specific domain errors
        raise HTTPException(status_code=e.status_code, detail=str(e))
    except Exception as e: # Catch-all for unexpected errors
        endpoint_log.exception("Unexpected error during document upload endpoint.", error=str(e))
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="An unexpected error occurred during upload.")
    finally:
        if file:
            await file.close()


@router.get(
    "/status/{document_id}", 
    response_model=StatusResponse,
    summary="Get the status of a specific document with live consistency checks",
    responses={
        status.HTTP_404_NOT_FOUND: {"model": ErrorDetail, "description": "Document not found"},
        status.HTTP_400_BAD_REQUEST: {"model": ErrorDetail, "description": "Invalid UUID format"},
        status.HTTP_422_UNPROCESSABLE_ENTITY: {"model": ErrorDetail, "description": "Missing X-Company-ID header"},
        status.HTTP_503_SERVICE_UNAVAILABLE: {"model": ErrorDetail, "description": "Error checking dependent services (DB, GCS, Milvus)"}
    }
)
async def get_document_status_endpoint(
    request: Request,
    document_id: uuid.UUID = Path(..., description="The UUID of the document"),
    use_case: GetDocumentStatusUseCase = Depends(get_get_document_status_use_case)
):
    endpoint_log = log.bind(request_id=getattr(request.state, "request_id", None), document_id=str(document_id))
    company_id_str = request.headers.get("X-Company-ID")
    if not company_id_str:
        endpoint_log.warning("Missing X-Company-ID header for get status.")
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Missing required header: X-Company-ID")
    try:
        company_uuid = uuid.UUID(company_id_str)
    except ValueError:
        endpoint_log.warning("Invalid Company ID format for get status.", company_id=company_id_str)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid Company ID format.")

    try:
        doc_entity: Optional[Document] = await use_case.execute(document_id=document_id, company_id=company_uuid)
        if not doc_entity: # Should be caught by DocumentNotFoundException from use_case
            raise DocumentNotFoundException(document_id=str(document_id))
        
        # Convert Document entity to StatusResponse DTO
        return StatusResponse(
            id=doc_entity.id, # Alias for document_id
            company_id=doc_entity.company_id,
            file_name=doc_entity.file_name,
            file_type=doc_entity.file_type,
            file_path=doc_entity.file_path,
            metadata=doc_entity.metadata,
            status=doc_entity.status.value,
            chunk_count=doc_entity.chunk_count,
            error_message=doc_entity.error_message,
            uploaded_at=doc_entity.uploaded_at,
            updated_at=doc_entity.updated_at,
            gcs_exists=doc_entity.gcs_exists,
            milvus_chunk_count=doc_entity.milvus_chunk_count_live # Name changed in entity
        )
    except DocumentNotFoundException as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except DomainException as e:
        raise HTTPException(status_code=e.status_code, detail=str(e))
    except Exception as e:
        endpoint_log.exception("Unexpected error getting document status.", error=str(e))
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="An unexpected error occurred.")


@router.get(
    "/status", 
    response_model=List[StatusResponse], # Directly return List[StatusResponse]
    summary="List document statuses for a company with pagination and live checks",
    responses={
        status.HTTP_400_BAD_REQUEST: {"model": ErrorDetail, "description": "Invalid Company ID format"},
        status.HTTP_422_UNPROCESSABLE_ENTITY: {"model": ErrorDetail, "description": "Missing X-Company-ID header"},
        status.HTTP_503_SERVICE_UNAVAILABLE: {"model": ErrorDetail, "description": "Error checking dependent services"}
    }
)
async def list_document_statuses_endpoint(
    request: Request,
    limit: int = Query(30, ge=1, le=100),
    offset: int = Query(0, ge=0),
    use_case: ListDocumentsUseCase = Depends(get_list_documents_use_case)
):
    endpoint_log = log.bind(request_id=getattr(request.state, "request_id", None))
    company_id_str = request.headers.get("X-Company-ID")
    if not company_id_str:
        endpoint_log.warning("Missing X-Company-ID header for list statuses.")
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Missing required header: X-Company-ID")
    try:
        company_uuid = uuid.UUID(company_id_str)
    except ValueError:
        endpoint_log.warning("Invalid Company ID format for list statuses.", company_id=company_id_str)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid Company ID format.")

    try:
        doc_entities: List[Document] = await use_case.execute(company_id=company_uuid, limit=limit, offset=offset)
        return [
            StatusResponse(
                id=doc.id, company_id=doc.company_id, file_name=doc.file_name, file_type=doc.file_type,
                file_path=doc.file_path, metadata=doc.metadata, status=doc.status.value,
                chunk_count=doc.chunk_count, error_message=doc.error_message,
                uploaded_at=doc.uploaded_at, updated_at=doc.updated_at,
                gcs_exists=doc.gcs_exists, milvus_chunk_count=doc.milvus_chunk_count_live
            ) for doc in doc_entities
        ]
    except DomainException as e: # Catch specific domain errors from list use case or its dependencies
        raise HTTPException(status_code=e.status_code, detail=str(e))
    except Exception as e:
        endpoint_log.exception("Unexpected error listing document statuses.", error=str(e))
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="An unexpected error occurred.")


@router.post(
    "/retry/{document_id}", 
    response_model=IngestResponse, 
    status_code=status.HTTP_202_ACCEPTED,
    summary="Retry ingestion for a document currently in 'error' state",
    responses={
        status.HTTP_404_NOT_FOUND: {"model": ErrorDetail, "description": "Document not found"},
        status.HTTP_409_CONFLICT: {"model": ErrorDetail, "description": "Document not in 'error' state or other conflict"},
        status.HTTP_400_BAD_REQUEST: {"model": ErrorDetail, "description": "Invalid UUID format"},
        status.HTTP_422_UNPROCESSABLE_ENTITY: {"model": ErrorDetail, "description": "Missing headers"}
    }
)
async def retry_document_endpoint(
    request: Request,
    document_id: uuid.UUID = Path(..., description="The UUID of the document to retry"),
    use_case: RetryDocumentUseCase = Depends(get_retry_document_use_case)
):
    endpoint_log = log.bind(request_id=getattr(request.state, "request_id", None), document_id=str(document_id))
    company_id_str = request.headers.get("X-Company-ID")
    user_id_str = request.headers.get("X-User-ID") # Though user_id not strictly needed for retry logic by use_case
    
    if not company_id_str or not user_id_str: # Ensure X-User-ID is also present as per original logic
        endpoint_log.warning("Missing required headers for retry.")
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Missing required headers: X-Company-ID and X-User-ID")
    try:
        company_uuid = uuid.UUID(company_id_str)
    except ValueError:
        endpoint_log.warning("Invalid Company ID format for retry.", company_id=company_id_str)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid Company ID format.")

    try:
        result_dict = await use_case.execute(document_id=document_id, company_id=company_uuid)
        return IngestResponse(**result_dict)
    except DocumentNotFoundException as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except OperationConflictException as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e))
    except DomainException as e:
        raise HTTPException(status_code=e.status_code, detail=str(e))
    except Exception as e:
        endpoint_log.exception("Unexpected error during document retry.", error=str(e))
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="An unexpected error occurred during retry.")


@router.delete(
    "/{document_id}", 
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a document and its associated data",
    responses={
        status.HTTP_404_NOT_FOUND: {"model": ErrorDetail, "description": "Document not found"},
        status.HTTP_400_BAD_REQUEST: {"model": ErrorDetail, "description": "Invalid UUID format"},
        status.HTTP_422_UNPROCESSABLE_ENTITY: {"model": ErrorDetail, "description": "Missing X-Company-ID header"}
    }
)
async def delete_document_endpoint(
    request: Request,
    document_id: uuid.UUID = Path(..., description="The UUID of the document to delete"),
    use_case: DeleteDocumentUseCase = Depends(get_delete_document_use_case)
):
    endpoint_log = log.bind(request_id=getattr(request.state, "request_id", None), document_id=str(document_id))
    company_id_str = request.headers.get("X-Company-ID")
    if not company_id_str:
        endpoint_log.warning("Missing X-Company-ID header for delete.")
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Missing required header: X-Company-ID")
    try:
        company_uuid = uuid.UUID(company_id_str)
    except ValueError:
        endpoint_log.warning("Invalid Company ID format for delete.", company_id=company_id_str)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid Company ID format.")

    try:
        result = await use_case.execute(document_id=document_id, company_id=company_uuid)
        if result.get("status") == "not_found": # Or check for DocumentNotFoundException
             raise DocumentNotFoundException(document_id=str(document_id))
        # For 204, no content is returned
    except DocumentNotFoundException as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except DomainException as e:
        raise HTTPException(status_code=e.status_code, detail=str(e))
    except Exception as e:
        endpoint_log.exception("Unexpected error deleting document.", error=str(e))
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="An unexpected error occurred during deletion.")


@router.get(
    "/stats", 
    response_model=DocumentStatsResponse,
    summary="Get aggregated document statistics for a company",
     responses={
        status.HTTP_400_BAD_REQUEST: {"model": ErrorDetail, "description": "Invalid Company ID or date format"},
        status.HTTP_422_UNPROCESSABLE_ENTITY: {"model": ErrorDetail, "description": "Missing X-Company-ID header"}
    }
)
async def get_document_stats_endpoint(
    request: Request,
    from_date_str: Optional[str] = Query(None, alias="from_date", description="Filter from date (YYYY-MM-DD)"),
    to_date_str: Optional[str] = Query(None, alias="to_date", description="Filter up to date (YYYY-MM-DD)"),
    status_filter_str: Optional[str] = Query(None, alias="status", description="Filter by document status"),
    use_case: GetDocumentStatsUseCase = Depends(get_get_document_stats_use_case)
):
    endpoint_log = log.bind(request_id=getattr(request.state, "request_id", None))
    company_id_str = request.headers.get("X-Company-ID")
    if not company_id_str:
        endpoint_log.warning("Missing X-Company-ID header for stats.")
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Missing required header: X-Company-ID")
    
    try:
        company_uuid = uuid.UUID(company_id_str)
    except ValueError:
        endpoint_log.warning("Invalid Company ID format for stats.", company_id=company_id_str)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid Company ID format.")

    parsed_from_date: Optional[date] = None
    if from_date_str:
        try: parsed_from_date = date.fromisoformat(from_date_str)
        except ValueError: raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid from_date format. Use YYYY-MM-DD.")
    
    parsed_to_date: Optional[date] = None
    if to_date_str:
        try: parsed_to_date = date.fromisoformat(to_date_str)
        except ValueError: raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid to_date format. Use YYYY-MM-DD.")

    parsed_status_filter: Optional[DocumentStatus] = None
    if status_filter_str:
        try: parsed_status_filter = DocumentStatus(status_filter_str)
        except ValueError: raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Invalid status_filter value. Allowed: {', '.join([s.value for s in DocumentStatus])}")

    try:
        stats_data = await use_case.execute(
            company_id=company_uuid, 
            from_date=parsed_from_date, 
            to_date=parsed_to_date, 
            status_filter=parsed_status_filter
        )
        # The use case now returns a dict which should align with DocumentStatsResponse
        # The raw rows are returned, and transformation to schema happens here or in Pydantic model
        return DocumentStatsResponse(**stats_data) # If use_case returns a dict matching the schema
    except DomainException as e:
        raise HTTPException(status_code=e.status_code, detail=str(e))
    except Exception as e:
        endpoint_log.exception("Unexpected error getting document stats.", error=str(e))
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="An unexpected error occurred.")
```

## File: `app\api\v1\schemas.py`
```py
# ingest-service/app/api/v1/schemas.py
import uuid
from pydantic import BaseModel, Field, field_validator
from typing import Optional, Dict, Any, List
from app.models.domain import DocumentStatus
from datetime import datetime, date # Asegurar que date está importado
import json
import logging

log = logging.getLogger(__name__)

class ErrorDetail(BaseModel):
    """Schema for error details in responses."""
    detail: str

class IngestResponse(BaseModel):
    document_id: uuid.UUID
    task_id: str
    status: str
    message: str = "Document upload received and queued for processing."

    class Config:
        json_schema_extra = {
            "example": {
                "document_id": "123e4567-e89b-12d3-a456-426614174000",
                "task_id": "c79ba436-fe88-4b82-9afc-44b1091564e4",
                "status": DocumentStatus.UPLOADED.value,
                "message": "Document upload accepted, processing started."
            }
        }

class StatusResponse(BaseModel):
    document_id: uuid.UUID = Field(..., alias="id")
    company_id: Optional[uuid.UUID] = None
    file_name: str
    file_type: str
    file_path: Optional[str] = Field(None, description="Path to the original file in GCS.")
    metadata: Optional[Dict[str, Any]] = None
    status: str
    chunk_count: Optional[int] = 0
    error_message: Optional[str] = None
    uploaded_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    gcs_exists: Optional[bool] = Field(None, description="Indicates if the original file currently exists in GCS.")
    milvus_chunk_count: Optional[int] = Field(None, description="Live count of chunks found in Milvus for this document (-1 if check failed).")
    message: Optional[str] = None

    @field_validator('metadata', mode='before')
    @classmethod
    def metadata_to_dict(cls, v):
        if isinstance(v, str):
            try:
                return json.loads(v)
            except json.JSONDecodeError:
                log.warning("Invalid metadata JSON found in DB record", raw_metadata=v)
                return {"error": "invalid metadata JSON in DB"}
        return v if v is None or isinstance(v, dict) else {}

    class Config:
        validate_assignment = True
        populate_by_name = True
        json_schema_extra = {
             "example": {
                "id": "52ad2ba8-cab9-4108-a504-b9822fe99bdc",
                "company_id": "51a66c8f-f6b1-43bd-8038-8768471a8b09",
                "file_name": "Anexo-00-Modificaciones-de-la-Guia-5.1.0.pdf",
                "file_type": "application/pdf",
                "file_path": "51a66c8f-f6b1-43bd-8038-8768471a8b09/52ad2ba8-cab9-4108-a504-b9822fe99bdc/Anexo-00-Modificaciones-de-la-Guia-5.1.0.pdf",
                "metadata": {"source": "manual upload", "version": "1.1"},
                "status": DocumentStatus.ERROR.value,
                "chunk_count": 0,
                "error_message": "Processing timed out after 600 seconds.",
                "uploaded_at": "2025-04-19T19:42:38.671016Z",
                "updated_at": "2025-04-19T19:42:42.337854Z",
                "gcs_exists": True,
                "milvus_chunk_count": 0,
                "message": "El procesamiento falló: Timeout."
            }
        }

class PaginatedStatusResponse(BaseModel):
    """Schema for paginated list of document statuses."""
    items: List[StatusResponse] = Field(..., description="List of document status objects on the current page.")
    total: int = Field(..., description="Total number of documents matching the query.")
    limit: int = Field(..., description="Number of items requested per page.")
    offset: int = Field(..., description="Number of items skipped for pagination.")

    class Config:
        json_schema_extra = {
            "example": {
                "items": [
                     {
                        "id": "52ad2ba8-cab9-4108-a504-b9822fe99bdc",
                        "company_id": "51a66c8f-f6b1-43bd-8038-8768471a8b09",
                        "file_name": "Anexo-00-Modificaciones-de-la-Guia-5.1.0.pdf",
                        "file_type": "application/pdf",
                        "file_path": "51a66c8f-f6b1-43bd-8038-8768471a8b09/52ad2ba8-cab9-4108-a504-b9822fe99bdc/Anexo-00-Modificaciones-de-la-Guia-5.1.0.pdf",
                        "metadata": {"source": "manual upload", "version": "1.1"},
                        "status": DocumentStatus.ERROR.value,
                        "chunk_count": 0,
                        "error_message": "Processing timed out after 600 seconds.",
                        "uploaded_at": "2025-04-19T19:42:38.671016Z",
                        "updated_at": "2025-04-19T19:42:42.337854Z",
                        "gcs_exists": True,
                        "milvus_chunk_count": 0,
                        "message": "El procesamiento falló: Timeout."
                    }
                ],
                "total": 1,
                "limit": 30,
                "offset": 0
            }
        }

# --- NUEVOS SCHEMAS PARA ESTADÍSTICAS DE DOCUMENTOS ---
class DocumentStatsByStatus(BaseModel):
    processed: int = 0
    processing: int = 0
    uploaded: int = 0
    error: int = 0
    pending: int = 0 # Añadir pending ya que es un DocumentStatus

class DocumentStatsByType(BaseModel):
    pdf: int = Field(0, alias="application/pdf")
    docx: int = Field(0, alias="application/vnd.openxmlformats-officedocument.wordprocessingml.document")
    doc: int = Field(0, alias="application/msword")
    txt: int = Field(0, alias="text/plain")
    md: int = Field(0, alias="text/markdown")
    html: int = Field(0, alias="text/html")
    other: int = 0

    class Config:
        populate_by_name = True


class DocumentStatsByUser(BaseModel):
    # Esta parte es más compleja de implementar en ingest-service si no tiene acceso a la tabla de users
    # Por ahora, la definición del schema está aquí, pero su implementación podría ser básica o diferida.
    user_id: str # Podría ser UUID del user que subió el doc, pero no está en la tabla 'documents'
    name: Optional[str] = None
    count: int

class DocumentRecentActivity(BaseModel):
    # Similar, agrupar por fecha y estado es una query más compleja.
    # Definición aquí, implementación podría ser básica.
    date: date
    uploaded: int = 0
    processing: int = 0
    processed: int = 0
    error: int = 0
    pending: int = 0

class DocumentStatsResponse(BaseModel):
    total_documents: int
    total_chunks_processed: int # Suma de chunk_count para documentos en estado 'processed'
    by_status: DocumentStatsByStatus
    by_type: DocumentStatsByType
    # by_user: List[DocumentStatsByUser] # Omitir por ahora para simplificar
    # recent_activity: List[DocumentRecentActivity] # Omitir por ahora para simplificar
    oldest_document_date: Optional[datetime] = None
    newest_document_date: Optional[datetime] = None

    model_config = { # Anteriormente Config
        "from_attributes": True # Anteriormente orm_mode
    }
```

## File: `app\application\__init__.py`
```py

```

## File: `app\application\ports\__init__.py`
```py

```

## File: `app\application\ports\chunk_repository_port.py`
```py
from abc import ABC, abstractmethod
from typing import List
from app.domain.entities import Chunk

class ChunkRepositoryPort(ABC):
    @abstractmethod
    def save_bulk(self, chunks: List[Chunk]) -> int:
        pass

```

## File: `app\application\ports\docproc_service_port.py`
```py
from abc import ABC, abstractmethod
from typing import Optional, Dict, Any

class DocProcServicePort(ABC):
    @abstractmethod
    def process_document(self, file_bytes: bytes, original_filename: str, content_type: str, document_id: Optional[str], company_id: Optional[str]) -> Dict[str, Any]:
        pass
    
    @abstractmethod
    def process_document_sync(self, file_bytes: bytes, original_filename: str, content_type: str, document_id: Optional[str], company_id: Optional[str]) -> Dict[str, Any]:
        pass
```

## File: `app\application\ports\document_repository_port.py`
```py
from abc import ABC, abstractmethod
from typing import Optional, Tuple, List, Dict, Any
import uuid
from datetime import date
from app.domain.entities import Document
from app.domain.enums import DocumentStatus

class DocumentRepositoryPort(ABC):
    @abstractmethod
    async def save(self, document: Document) -> None:
        pass

    @abstractmethod
    async def find_by_id(self, doc_id: uuid.UUID, company_id: uuid.UUID) -> Optional[Document]:
        pass

    @abstractmethod
    async def find_by_name_and_company(self, filename: str, company_id: uuid.UUID) -> Optional[Document]:
        pass

    @abstractmethod
    async def update_status(self, doc_id: uuid.UUID, status: DocumentStatus, chunk_count: Optional[int] = None, error_message: Optional[str] = None) -> bool:
        pass

    @abstractmethod
    async def list_paginated(self, company_id: uuid.UUID, limit: int, offset: int) -> Tuple[List[Document], int]:
        pass

    @abstractmethod
    async def delete(self, doc_id: uuid.UUID, company_id: uuid.UUID) -> bool:
        pass

    @abstractmethod
    async def get_stats(self, company_id: uuid.UUID, from_date: Optional[date], to_date: Optional[date], status_filter: Optional[DocumentStatus]) -> Dict[str, Any]:
        pass

```

## File: `app\application\ports\embedding_service_port.py`
```py
from abc import ABC, abstractmethod
from typing import List, Dict, Tuple, Any

class EmbeddingServicePort(ABC):
    @abstractmethod
    def get_embeddings(self, texts: List[str], text_type: str = "passage") -> Tuple[List[List[float]], Dict[str, Any]]:
        pass

    @abstractmethod
    def get_embeddings_sync(self, texts: List[str], text_type: str = "passage") -> Tuple[List[List[float]], Dict[str, Any]]:
        pass
```

## File: `app\application\ports\file_storage_port.py`
```py
from abc import ABC, abstractmethod
from typing import Optional

class FileStoragePort(ABC):
    @abstractmethod
    async def upload(self, object_name: str, data: bytes, content_type: str) -> str:
        pass

    @abstractmethod
    def download(self, object_name: str, target_path: str) -> None: # Kept sync for ProcessDocumentUseCase
        pass

    @abstractmethod
    async def exists(self, object_name: str) -> bool: # Async for API use
        pass

    @abstractmethod
    def exists_sync(self, object_name: str) -> bool: # Sync for worker use
        pass

    @abstractmethod
    async def delete(self, object_name: str) -> None: # Async for API use
        pass
    
    @abstractmethod
    def delete_sync(self, object_name: str) -> None: # Sync for worker use
        pass
```

## File: `app\application\ports\task_queue_port.py`
```py
from abc import ABC, abstractmethod

class TaskQueuePort(ABC):
    @abstractmethod
    async def enqueue_process_document_task(self, document_id: str, company_id: str, filename: str, content_type: str) -> str:
        pass

```

## File: `app\application\ports\vector_store_port.py`
```py
from abc import ABC, abstractmethod
from typing import List, Dict, Tuple, Any

class VectorStorePort(ABC):
    @abstractmethod
    def index_chunks(
        self,
        chunks_with_embeddings: List[Dict[str, Any]], # Combined chunk data and its embedding
        filename: str, # For metadata in Milvus
        company_id: str, 
        document_id: str, 
        delete_existing: bool
    ) -> Tuple[int, List[str], List[Dict[str, Any]]]: # inserted_count, milvus_pks, chunks_for_pg (with embedding_id)
        pass

    @abstractmethod
    def delete_document_chunks(self, company_id: str, document_id: str) -> int:
        pass

    @abstractmethod
    def count_document_chunks(self, company_id: str, document_id: str) -> int: # Sync for API and worker
        pass
    
    @abstractmethod
    async def count_document_chunks_async(self, company_id: str, document_id: str) -> int: # Async version for API
        pass


    @abstractmethod
    def ensure_collection_and_indexes(self) -> None: # Typically sync, called at init or worker startup
        pass
```

## File: `app\application\use_cases\__init__.py`
```py

```

## File: `app\application\use_cases\delete_document_use_case.py`
```py
import uuid
from typing import Optional, Dict, Any
from app.application.ports.document_repository_port import DocumentRepositoryPort
from app.application.ports.file_storage_port import FileStoragePort
from app.application.ports.vector_store_port import VectorStorePort

class DeleteDocumentUseCase:
    def __init__(self, 
                 document_repository: DocumentRepositoryPort, 
                 file_storage: FileStoragePort, 
                 vector_store: VectorStorePort):
        self.document_repository = document_repository
        self.file_storage = file_storage
        self.vector_store = vector_store

    async def execute(self, *,
                     document_id: uuid.UUID,
                     company_id: uuid.UUID) -> Dict[str, Any]:
        doc = await self.document_repository.find_by_id(document_id, company_id)
        if not doc:
            return {"error": "Document not found", "status": "not_found"}
        # Eliminar chunks en vector store
        self.vector_store.delete_document_chunks(str(company_id), str(document_id))
        # Eliminar archivo en storage
        if doc.file_path:
            await self.file_storage.delete(doc.file_path)
        # Eliminar registro en DB
        await self.document_repository.delete(document_id, company_id)
        return {"status": "deleted", "document_id": str(document_id)}

```

## File: `app\application\use_cases\get_document_stats_use_case.py`
```py
import uuid
from typing import Optional, Dict, Any
from datetime import date
from app.application.ports.document_repository_port import DocumentRepositoryPort

class GetDocumentStatsUseCase:
    def __init__(self, document_repository: DocumentRepositoryPort):
        self.document_repository = document_repository

    async def execute(self, *,
                     company_id: uuid.UUID,
                     from_date: Optional[date] = None,
                     to_date: Optional[date] = None,
                     status_filter: Optional[str] = None) -> Dict[str, Any]:
        return await self.document_repository.get_stats(company_id, from_date, to_date, status_filter)

```

## File: `app\application\use_cases\get_document_status_use_case.py`
```py
import uuid
from typing import Optional, Dict, Any
from datetime import datetime, timezone, timedelta

from app.application.ports.document_repository_port import DocumentRepositoryPort
from app.application.ports.file_storage_port import FileStoragePort
from app.application.ports.vector_store_port import VectorStorePort
from app.domain.entities import Document
from app.domain.enums import DocumentStatus
from app.domain.exceptions import DocumentNotFoundException
import structlog

log = structlog.get_logger(__name__)

GRACE_PERIOD_SECONDS = 120 # Grace period for inconsistencies for recently processed docs

class GetDocumentStatusUseCase:
    def __init__(self, 
                 document_repository: DocumentRepositoryPort, 
                 file_storage: FileStoragePort, 
                 vector_store: VectorStorePort):
        self.document_repository = document_repository
        self.file_storage = file_storage # Needs to offer exists_sync or be async
        self.vector_store = vector_store # Needs to offer count_document_chunks_async

    async def execute(self, *,
                     document_id: uuid.UUID,
                     company_id: uuid.UUID) -> Optional[Document]:
        
        case_log = log.bind(document_id=str(document_id), company_id=str(company_id))
        case_log.info("GetDocumentStatusUseCase execution started.")

        doc = await self.document_repository.find_by_id(document_id, company_id)
        if not doc:
            case_log.warning("Document not found in repository.")
            raise DocumentNotFoundException(document_id=str(document_id))

        case_log.info("Document retrieved from repository.", status=doc.status.value)
        
        # --- Live Checks & Potential Auto-Correction ---
        needs_db_update = False
        original_status = doc.status
        original_chunk_count = doc.chunk_count
        original_error_message = doc.error_message

        # Check GCS
        live_gcs_exists = False
        if doc.file_path:
            try:
                live_gcs_exists = await self.file_storage.exists(doc.file_path)
                case_log.debug("GCS existence check complete.", gcs_path=doc.file_path, exists=live_gcs_exists)
                if not live_gcs_exists and doc.status not in [DocumentStatus.ERROR, DocumentStatus.PENDING]:
                    case_log.warning("File missing in GCS but DB status suggests otherwise.", current_db_status=doc.status.value)
                    if not self._is_in_grace_period(doc.updated_at, doc.status):
                        needs_db_update = True
                        doc.status = DocumentStatus.ERROR
                        doc.error_message = self._append_error(doc.error_message, "File missing from storage.")
                        doc.chunk_count = 0 
            except Exception as e_gcs:
                case_log.error("Error checking GCS file existence.", error=str(e_gcs), exc_info=True)
                # Assume file doesn't exist if check fails, and potentially mark for error
                if doc.status not in [DocumentStatus.ERROR, DocumentStatus.PENDING] and not self._is_in_grace_period(doc.updated_at, doc.status):
                    needs_db_update = True
                    doc.status = DocumentStatus.ERROR
                    doc.error_message = self._append_error(doc.error_message, f"GCS check error: {type(e_gcs).__name__}.")
                    doc.chunk_count = 0
        else:
            case_log.warning("Document file_path is null, cannot check GCS.", doc_id=str(doc.id))
            if doc.status not in [DocumentStatus.ERROR, DocumentStatus.PENDING] and not self._is_in_grace_period(doc.updated_at, doc.status):
                needs_db_update = True
                doc.status = DocumentStatus.ERROR
                doc.error_message = self._append_error(doc.error_message, "File path missing in DB.")
                doc.chunk_count = 0
        
        doc.gcs_exists = live_gcs_exists


        # Check Milvus
        live_milvus_chunk_count = -1
        try:
            live_milvus_chunk_count = await self.vector_store.count_document_chunks_async(str(company_id), str(document_id))
            case_log.debug("Milvus chunk count check complete.", count=live_milvus_chunk_count)

            if live_milvus_chunk_count == -1 and doc.status == DocumentStatus.PROCESSED: # Error from count
                 if not self._is_in_grace_period(doc.updated_at, doc.status):
                    needs_db_update = True
                    doc.status = DocumentStatus.ERROR
                    doc.error_message = self._append_error(doc.error_message, "Milvus count check failed for processed doc.")
            elif live_milvus_chunk_count > 0:
                if doc.status in [DocumentStatus.ERROR, DocumentStatus.UPLOADED, DocumentStatus.PROCESSING] and live_gcs_exists:
                    case_log.warning("Inconsistency: Chunks in Milvus & GCS, but DB status is not PROCESSED. Correcting.", current_db_status=doc.status.value)
                    needs_db_update = True
                    doc.status = DocumentStatus.PROCESSED
                    doc.error_message = None # Clear error if correcting to processed
                if doc.status == DocumentStatus.PROCESSED and doc.chunk_count != live_milvus_chunk_count:
                    case_log.warning("Inconsistency: DB chunk_count differs from Milvus. Updating DB.", db_count=doc.chunk_count, milvus_count=live_milvus_chunk_count)
                    needs_db_update = True
            elif live_milvus_chunk_count == 0:
                if doc.status == DocumentStatus.PROCESSED:
                    if not self._is_in_grace_period(doc.updated_at, doc.status):
                        case_log.warning("Inconsistency: DB status PROCESSED but no chunks in Milvus. Correcting.")
                        needs_db_update = True
                        doc.status = DocumentStatus.ERROR
                        doc.error_message = self._append_error(doc.error_message, "Processed data missing from Milvus.")
                if doc.status != DocumentStatus.PROCESSED and doc.chunk_count != 0: # e.g. ERROR but had chunks
                    needs_db_update = True
            
            if needs_db_update and doc.status == DocumentStatus.PROCESSED: # If corrected to processed
                doc.chunk_count = live_milvus_chunk_count
            elif doc.status == DocumentStatus.ERROR: # If set to error
                doc.chunk_count = 0


        except Exception as e_milvus:
            case_log.error("Error checking Milvus chunk count.", error=str(e_milvus), exc_info=True)
            live_milvus_chunk_count = -1 # Indicate error
            if doc.status == DocumentStatus.PROCESSED and not self._is_in_grace_period(doc.updated_at, doc.status):
                needs_db_update = True
                doc.status = DocumentStatus.ERROR
                doc.error_message = self._append_error(doc.error_message, f"Milvus check error: {type(e_milvus).__name__}.")
                doc.chunk_count = 0
        
        doc.milvus_chunk_count_live = live_milvus_chunk_count


        # Persist changes if any inconsistencies were corrected
        if needs_db_update:
            # Check if status actually changed to avoid unnecessary DB write if only chunk_count/error_message changed
            if doc.status != original_status or doc.chunk_count != original_chunk_count or doc.error_message != original_error_message:
                case_log.warning("Inconsistency detected, updating document in DB.", new_status=doc.status.value, new_chunk_count=doc.chunk_count, new_error=doc.error_message)
                try:
                    await self.document_repository.update_status(
                        doc_id=doc.id, 
                        status=doc.status, 
                        chunk_count=doc.chunk_count, 
                        error_message=doc.error_message,
                        updated_at=datetime.now(timezone.utc) # Ensure updated_at is refreshed
                    )
                    case_log.info("Document successfully updated in DB after consistency checks.")
                except Exception as e_db_update:
                    case_log.error("Failed to update document in DB after consistency checks.", error=str(e_db_update), exc_info=True)
                    # Potentially revert doc object to original state or handle error
                    # For now, we'll return the corrected doc object, but log the DB update failure.
                    doc.status = original_status # Revert if DB update failed to reflect actual state
                    doc.chunk_count = original_chunk_count
                    doc.error_message = self._append_error(original_error_message, "DB update failed during self-correction.")
            else:
                 case_log.info("DB update deemed necessary but no actual change in status, chunk_count or error_message. Skipping DB write.")


        return doc

    def _is_in_grace_period(self, updated_at: Optional[datetime], status: DocumentStatus) -> bool:
        if status != DocumentStatus.PROCESSED: # Grace period only for recently processed docs
            return False
        if not updated_at:
            return False # No timestamp, assume not in grace period
        
        now_utc = datetime.now(timezone.utc)
        # Ensure updated_at is offset-aware for comparison
        if updated_at.tzinfo is None:
            updated_at_aware = updated_at.replace(tzinfo=timezone.utc) # Assume UTC if naive
        else:
            updated_at_aware = updated_at.astimezone(timezone.utc)

        if (now_utc - updated_at_aware).total_seconds() < GRACE_PERIOD_SECONDS:
            log.debug("Document is within grace period for inconsistency checks.")
            return True
        return False

    def _append_error(self, existing_msg: Optional[str], new_err: str) -> str:
        if not existing_msg:
            return new_err
        if new_err in existing_msg: # Avoid duplicate messages
            return existing_msg
        return f"{existing_msg.strip()} {new_err.strip()}"
```

## File: `app\application\use_cases\list_documents_use_case.py`
```py
import uuid
import asyncio
from typing import List, Dict, Any, Optional
from app.application.ports.document_repository_port import DocumentRepositoryPort
from app.application.ports.file_storage_port import FileStoragePort
from app.application.ports.vector_store_port import VectorStorePort
from app.domain.entities import Document # Import Document for type hinting
from app.application.use_cases.get_document_status_use_case import GetDocumentStatusUseCase # For individual check logic
import structlog

log = structlog.get_logger(__name__)

class ListDocumentsUseCase:
    def __init__(self, 
                 document_repository: DocumentRepositoryPort, 
                 file_storage: FileStoragePort, # Used by GetDocumentStatusUseCase
                 vector_store: VectorStorePort): # Used by GetDocumentStatusUseCase
        self.document_repository = document_repository
        # Instantiate GetDocumentStatusUseCase to reuse its checking logic
        self.get_status_use_case = GetDocumentStatusUseCase(document_repository, file_storage, vector_store)


    async def execute(self, *,
                     company_id: uuid.UUID,
                     limit: int = 30,
                     offset: int = 0) -> List[Document]: # Return list of Document entities
        
        case_log = log.bind(company_id=str(company_id), limit=limit, offset=offset)
        case_log.info("ListDocumentsUseCase execution started.")

        docs_from_repo, total_count = await self.document_repository.list_paginated(company_id, limit, offset)
        case_log.info(f"Retrieved {len(docs_from_repo)} documents from repository, total: {total_count}.")

        if not docs_from_repo:
            return []

        # Concurrently check/enrich each document status
        enriched_docs_tasks = [
            self.get_status_use_case.execute(document_id=doc.id, company_id=company_id)
            for doc in docs_from_repo
        ]
        
        results = await asyncio.gather(*enriched_docs_tasks, return_exceptions=True)
        
        final_doc_list: List[Document] = []
        for i, res_or_exc in enumerate(results):
            original_doc = docs_from_repo[i]
            if isinstance(res_or_exc, Exception):
                case_log.error("Error enriching document status in list.", doc_id=str(original_doc.id), error=str(res_or_exc))
                # Append original doc, or a version marked with error during enrichment
                original_doc.error_message = self.get_status_use_case._append_error(original_doc.error_message, f"Status enrichment failed: {type(res_or_exc).__name__}")
                final_doc_list.append(original_doc)
            elif res_or_exc is None: # Should not happen if find_by_id was successful before
                case_log.warning("Enriched document status returned None unexpectedly.", doc_id=str(original_doc.id))
                final_doc_list.append(original_doc)
            else: # Successfully enriched
                final_doc_list.append(res_or_exc) # res_or_exc is the enriched Document object

        case_log.info("ListDocumentsUseCase execution finished.", num_returned=len(final_doc_list))
        return final_doc_list
```

## File: `app\application\use_cases\process_document_use_case.py`
```py
import uuid
import tempfile
import pathlib
import asyncio # Para ejecutar tareas async desde sync si es necesario
from typing import Dict, Any, List

from app.domain.entities import Document, Chunk, ChunkMetadata
from app.domain.enums import DocumentStatus, ChunkVectorStatus
from app.application.ports.document_repository_port import DocumentRepositoryPort
from app.application.ports.chunk_repository_port import ChunkRepositoryPort
from app.application.ports.file_storage_port import FileStoragePort
from app.application.ports.docproc_service_port import DocProcServicePort
from app.application.ports.embedding_service_port import EmbeddingServicePort
from app.application.ports.vector_store_port import VectorStorePort
import structlog

log = structlog.get_logger(__name__)

class ProcessDocumentUseCase:
    def __init__(self,
                 document_repository: DocumentRepositoryPort,
                 chunk_repository: ChunkRepositoryPort,
                 file_storage: FileStoragePort,
                 docproc_service: DocProcServicePort,
                 embedding_service: EmbeddingServicePort,
                 vector_store: VectorStorePort):
        self.document_repository = document_repository
        self.chunk_repository = chunk_repository
        self.file_storage = file_storage
        self.docproc_service = docproc_service
        self.embedding_service = embedding_service
        self.vector_store = vector_store

    def execute(self, *,
                document_id_str: str, # Renamed for clarity
                company_id_str: str,  # Renamed for clarity
                filename: str, # Renamed from file_path as it's just the name here
                content_type: str # Renamed from file_type for consistency
                ) -> Dict[str, Any]:
        
        document_id = uuid.UUID(document_id_str)
        company_id = uuid.UUID(company_id_str)

        case_log = log.bind(document_id=document_id_str, company_id=company_id_str, filename=filename)
        case_log.info("ProcessDocumentUseCase execution started.")

        gcs_file_path = f"{company_id_str}/{document_id_str}/{filename}" # Construct GCS path

        try:
            # 1. Actualizar estado a PROCESSING
            case_log.info("Updating document status to PROCESSING.")
            # Llamamos al método síncrono del repositorio
            if not self.document_repository.update_status_sync(document_id, DocumentStatus.PROCESSING):
                case_log.warning("Failed to update document status to PROCESSING, document might not exist or already processed/deleted.")
                # Consider if this should be a hard failure or specific return
                return {"error": "Failed to set document to processing state", "status_code": 409}


            # 2. Descargar archivo de almacenamiento
            with tempfile.TemporaryDirectory() as temp_dir:
                local_path = pathlib.Path(temp_dir) / filename
                case_log.info("Downloading file from storage.", gcs_path=gcs_file_path, local_path=str(local_path))
                self.file_storage.download(gcs_file_path, str(local_path)) # Assumes sync download
                case_log.info("File downloaded successfully.")

                # 3. Procesar documento (DocProc)
                case_log.info("Processing document with DocProcService.")
                with open(local_path, "rb") as f:
                    file_bytes = f.read()
                
                docproc_result = self.docproc_service.process_document(
                    file_bytes=file_bytes,
                    original_filename=filename,
                    content_type=content_type,
                    document_id=document_id_str,
                    company_id=company_id_str
                )
            case_log.info("Document processed by DocProcService.", num_chunks_from_docproc=len(docproc_result.get("data", {}).get("chunks", [])))
            
            # Extract chunks from docproc response, expecting a list of dicts
            chunks_from_docproc = docproc_result.get("data", {}).get("chunks", [])
            if not chunks_from_docproc:
                case_log.warning("DocProcService returned no chunks. Finalizing as processed with 0 chunks.")
                self.document_repository.update_status_sync(document_id, DocumentStatus.PROCESSED, chunk_count=0)
                return {"document_id": document_id_str, "status": DocumentStatus.PROCESSED.value, "message": "Document processed, no textual content found.", "chunk_count": 0}

            # 4. Obtener embeddings
            case_log.info("Getting embeddings for processed chunks.")
            chunk_texts_for_embedding = [chunk['text'] for chunk in chunks_from_docproc if chunk.get('text','').strip()]
            if not chunk_texts_for_embedding:
                case_log.warning("No non-empty text found in chunks from DocProc. Finalizing with 0 chunks.")
                self.document_repository.update_status_sync(document_id, DocumentStatus.PROCESSED, chunk_count=0)
                return {"document_id": document_id_str, "status": DocumentStatus.PROCESSED.value, "message": "Document processed, no valid textual content in chunks.", "chunk_count": 0}

            embeddings, model_info = self.embedding_service.get_embeddings(chunk_texts_for_embedding) # Assumes sync
            case_log.info("Embeddings received.", num_embeddings=len(embeddings), model_info=model_info)

            if len(embeddings) != len(chunk_texts_for_embedding):
                 error_msg = "Mismatch between number of texts sent for embedding and embeddings received."
                 case_log.error(error_msg, num_texts=len(chunk_texts_for_embedding), num_embeddings=len(embeddings))
                 self.document_repository.update_status_sync(document_id, DocumentStatus.ERROR, error_message=error_msg)
                 raise RuntimeError(error_msg)

            # Prepare chunks_with_embeddings for MilvusAdapter
            # Ensure that only chunks that had text and got an embedding are passed
            chunks_with_embeddings_for_milvus: List[Dict[str, Any]] = []
            embedding_idx = 0
            for i, original_chunk_data in enumerate(chunks_from_docproc):
                if original_chunk_data.get('text','').strip(): # If this chunk had text
                    if embedding_idx < len(embeddings):
                        chunks_with_embeddings_for_milvus.append({
                            "text": original_chunk_data['text'],
                            "embedding": embeddings[embedding_idx],
                            "source_metadata": original_chunk_data.get('source_metadata', {}),
                            # Include original_chunk_index if needed by MilvusAdapter for PK generation strategy that relies on it
                            "original_chunk_index": i 
                        })
                        embedding_idx += 1
                    else:
                        case_log.warning("More text chunks than embeddings, skipping a chunk.", original_chunk_index=i)


            # 5. Indexar en vector store
            case_log.info("Indexing chunks in VectorStore (Milvus).")
            inserted_milvus_count, _, chunks_for_pg_raw = self.vector_store.index_chunks(
                chunks_with_embeddings=chunks_with_embeddings_for_milvus,
                filename=filename,
                company_id=company_id_str,
                document_id=document_id_str,
                delete_existing=True # Always delete existing for a full reprocess
            )
            case_log.info("VectorStore indexing complete.", inserted_milvus_count=inserted_milvus_count)

            # 6. Guardar chunks en DB
            # Convert raw dicts from MilvusAdapter (which are prepared for PG) to Chunk domain entities
            chunk_domain_objects_for_pg: List[Chunk] = []
            for pg_data in chunks_for_pg_raw:
                # The 'metadata' in pg_data should already be a dict from MilvusAdapter preparation
                # If 'id' is already a UUID from MilvusAdapter, use it, else it will be generated by DB default.
                # Chunk entity expects metadata as ChunkMetadata object.
                pg_data_meta = pg_data.get('metadata', {})
                chunk_meta_obj = ChunkMetadata(**pg_data_meta) if isinstance(pg_data_meta, dict) else ChunkMetadata()
                
                chunk_domain_objects_for_pg.append(Chunk(
                    id=pg_data.get('id'), # Allow adapter to provide ID, or DB to generate
                    document_id=uuid.UUID(pg_data['document_id']),
                    company_id=uuid.UUID(pg_data['company_id']),
                    chunk_index=pg_data['chunk_index'],
                    content=pg_data['content'],
                    metadata=chunk_meta_obj,
                    embedding_id=pg_data.get('embedding_id'), # This is the Milvus PK
                    vector_status=ChunkVectorStatus(pg_data.get('vector_status', 'pending'))
                ))
            
            if chunk_domain_objects_for_pg:
                case_log.info("Saving chunk details to PostgreSQL.", num_chunks_to_save=len(chunk_domain_objects_for_pg))
                inserted_pg_count = self.chunk_repository.save_bulk(chunk_domain_objects_for_pg) # Assumes sync
                case_log.info("Chunks saved to PostgreSQL.", inserted_pg_count=inserted_pg_count)
                final_chunk_count = inserted_pg_count
            else:
                case_log.warning("No chunks prepared for PostgreSQL insertion.")
                final_chunk_count = 0
            
            # 7. Actualizar estado a PROCESSED
            case_log.info("Updating document status to PROCESSED.", final_chunk_count=final_chunk_count)
            self.document_repository.update_status_sync(document_id, DocumentStatus.PROCESSED, chunk_count=final_chunk_count)
            
            case_log.info("ProcessDocumentUseCase execution finished successfully.")
            return {
                "document_id": document_id_str,
                "status": DocumentStatus.PROCESSED.value,
                "message": "Document processed successfully.",
                "chunk_count": final_chunk_count
            }

        except Exception as e:
            error_msg_for_db = f"Processing failed: {type(e).__name__} - {str(e)[:250]}"
            case_log.error("Error during ProcessDocumentUseCase execution", error=str(e), exc_info=True)
            try:
                self.document_repository.update_status_sync(document_id, DocumentStatus.ERROR, error_message=error_msg_for_db)
                case_log.info("Document status updated to ERROR due to exception.")
            except Exception as db_update_err:
                case_log.error("Failed to update document status to ERROR after main exception", nested_error=str(db_update_err))
            # Re-raise the original exception to be handled by Celery
            raise
```

## File: `app\application\use_cases\retry_document_use_case.py`
```py
import uuid
from app.application.ports.document_repository_port import DocumentRepositoryPort
from app.application.ports.task_queue_port import TaskQueuePort
from app.domain.enums import DocumentStatus
from app.domain.exceptions import DocumentNotFoundException, OperationConflictException, TaskQueueException, DatabaseException
import structlog

log = structlog.get_logger(__name__)

class RetryDocumentUseCase:
    def __init__(self, 
                 document_repository: DocumentRepositoryPort, 
                 task_queue: TaskQueuePort):
        self.document_repository = document_repository
        self.task_queue = task_queue

    async def execute(self, *,
                     document_id: uuid.UUID,
                     company_id: uuid.UUID) -> dict: # Removed file_name and file_type from params

        use_case_log = log.bind(document_id=str(document_id), company_id=str(company_id))
        use_case_log.info("RetryDocumentUseCase execution started.")

        doc = await self.document_repository.find_by_id(document_id, company_id)
        if not doc:
            use_case_log.warning("Document not found for retry.")
            raise DocumentNotFoundException(document_id=str(document_id))
        
        use_case_log.info("Document found.", current_status=doc.status.value, file_name=doc.file_name, file_type=doc.file_type)

        if doc.status != DocumentStatus.ERROR:
            use_case_log.warning("Document is not in ERROR state, cannot retry.")
            raise OperationConflictException(f"Document is not in 'error' state (current state: {doc.status.value}). Cannot retry.")
        
        # Update status to PENDING (or UPLOADED if file is verified to be in GCS)
        # For simplicity here, setting to PENDING to re-trigger the whole pipeline
        # Error message and chunk_count should be cleared/reset
        try:
            await self.document_repository.update_status(doc_id=document_id, status=DocumentStatus.PENDING, chunk_count=0, error_message=None)
            use_case_log.info("Document status updated to PENDING for retry.")
        except Exception as e:
            use_case_log.error("Failed to update document status for retry.", error=str(e), exc_info=True)
            raise DatabaseException(f"Failed to update document status for retry: {e}")

        try:
            task_id = await self.task_queue.enqueue_process_document_task(
                document_id=str(document_id),
                company_id=str(company_id),
                filename=doc.file_name, # Use filename from DB
                content_type=doc.file_type  # Use filetype from DB
            )
            use_case_log.info("Document reprocessing task enqueued successfully.", task_id=task_id)
        except Exception as e:
            use_case_log.error("Failed to re-enqueue document processing task.", error=str(e), exc_info=True)
            # Attempt to revert status to ERROR if task queueing fails
            try:
                 await self.document_repository.update_status(document_id, DocumentStatus.ERROR, error_message=f"Retry failed: Could not enqueue task - {type(e).__name__}")
            except Exception as db_err:
                 use_case_log.error("Failed to revert document status to ERROR after task queue failure.", nested_error=str(db_err))
            raise TaskQueueException(f"Failed to re-enqueue document processing task: {e}")

        return {
            "document_id": str(document_id),
            "task_id": task_id,
            "status": DocumentStatus.PENDING.value, # Or UPLOADED, consistent with what the task expects
            "message": "Document retry accepted, processing started."
        }
```

## File: `app\application\use_cases\upload_document_use_case.py`
```py
import uuid
from typing import Optional, Dict, Any
from datetime import datetime, timezone
from app.domain.entities import Document
from app.domain.enums import DocumentStatus
from app.application.ports.document_repository_port import DocumentRepositoryPort
from app.application.ports.file_storage_port import FileStoragePort
from app.application.ports.task_queue_port import TaskQueuePort
from app.domain.exceptions import DuplicateDocumentException, InvalidInputException, StorageException, TaskQueueException
import structlog

log = structlog.get_logger(__name__)

class UploadDocumentUseCase:
    def __init__(self, 
                 document_repository: DocumentRepositoryPort, 
                 file_storage: FileStoragePort, 
                 task_queue: TaskQueuePort):
        self.document_repository = document_repository
        self.file_storage = file_storage
        self.task_queue = task_queue

    async def execute(self, *,
                     company_id: uuid.UUID,
                     user_id: uuid.UUID, # user_id is passed but not used in this use case logic directly
                     file_name: str,
                     file_type: str,
                     file_content: bytes,
                     metadata: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        
        use_case_log = log.bind(company_id=str(company_id), file_name=file_name, file_type=file_type)
        use_case_log.info("UploadDocumentUseCase execution started.")

        if not file_name or not file_type or not file_content:
            use_case_log.warning("Invalid input for document upload.")
            raise InvalidInputException("File name, type, and content are required.")

        # 1. Validar duplicados
        use_case_log.debug("Checking for duplicate document.")
        existing = await self.document_repository.find_by_name_and_company(file_name, company_id)
        if existing and existing.status != DocumentStatus.ERROR: # Allow re-upload if previous was error
            use_case_log.warning("Duplicate document found and not in error state.", existing_doc_id=str(existing.id), existing_status=existing.status.value)
            raise DuplicateDocumentException(filename=file_name, message=f"Document '{file_name}' already exists with status '{existing.status.value}'.")
        
        document_id = uuid.uuid4()
        current_time = datetime.now(timezone.utc)
        file_path = f"{company_id}/{document_id}/{file_name}" # Corrected file_path construction

        # 2. Crear registro en DB (PENDING)
        document = Document(
            id=document_id,
            company_id=company_id,
            file_name=file_name,
            file_type=file_type,
            status=DocumentStatus.PENDING,
            metadata=metadata or {},
            file_path=file_path, # Set file_path here
            chunk_count=0,
            uploaded_at=current_time,
            updated_at=current_time
        )
        use_case_log.debug("Saving document record to DB with PENDING status.", document_id=str(document_id))
        try:
            await self.document_repository.save(document)
        except Exception as e:
            use_case_log.error("Failed to save document record to DB.", error=str(e), exc_info=True)
            raise DatabaseException(f"Failed to create initial document record: {e}")


        # 3. Subir a almacenamiento
        use_case_log.debug("Uploading file to storage.", gcs_path=file_path)
        try:
            await self.file_storage.upload(file_path, file_content, file_type)
        except Exception as e:
            use_case_log.error("Failed to upload file to storage.", error=str(e), exc_info=True)
            # Attempt to mark document as error in DB
            try:
                await self.document_repository.update_status(document_id, DocumentStatus.ERROR, error_message=f"Storage upload failed: {type(e).__name__}")
            except Exception as db_err:
                use_case_log.error("Failed to update document to ERROR after storage failure.", nested_error=str(db_err))
            raise StorageException(f"Failed to upload file to storage: {e}")

        # 4. Actualizar estado a UPLOADED
        use_case_log.debug("Updating document status to UPLOADED in DB.")
        try:
            await self.document_repository.update_status(document_id, DocumentStatus.UPLOADED)
        except Exception as e:
            use_case_log.error("Failed to update document status to UPLOADED.", error=str(e), exc_info=True)
            # If this fails, the document is in GCS but DB state is PENDING. Consider cleanup or retry logic.
            # For now, re-raise as DB exception
            raise DatabaseException(f"Failed to update document status after upload: {e}")


        # 5. Encolar tarea de procesamiento
        use_case_log.debug("Enqueuing document processing task.")
        try:
            task_id = await self.task_queue.enqueue_process_document_task(
                document_id=str(document_id),
                company_id=str(company_id),
                filename=file_name, # Pass original filename for task
                content_type=file_type
            )
            use_case_log.info("Document processing task enqueued.", task_id=task_id)
        except Exception as e:
            use_case_log.error("Failed to enqueue document processing task.", error=str(e), exc_info=True)
            # Mark document as error because task could not be queued
            try:
                await self.document_repository.update_status(document_id, DocumentStatus.ERROR, error_message=f"Failed to queue processing task: {type(e).__name__}")
            except Exception as db_err:
                use_case_log.error("Failed to update document to ERROR after task queue failure.", nested_error=str(db_err))
            raise TaskQueueException(f"Failed to enqueue document processing task: {e}")
        
        use_case_log.info("UploadDocumentUseCase execution finished successfully.")
        return {
            "document_id": str(document_id),
            "task_id": task_id,
            "status": DocumentStatus.UPLOADED.value,
            "message": "Document upload accepted, processing started."
        }
```

## File: `app\core\__init__.py`
```py

```

## File: `app\core\config.py`
```py
# ingest-service/app/core/config.py
# LLM: NO COMMENTS unless absolutely necessary for processing logic.
import logging
import os
from typing import Optional, List, Any, Dict, Union
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import (
    RedisDsn, AnyHttpUrl, SecretStr, Field, field_validator, ValidationError,
    ValidationInfo
)
import sys
import json
from urllib.parse import urlparse

# --- Service Names en K8s ---
POSTGRES_K8S_SVC = "postgresql-service.nyro-develop.svc.cluster.local"
# MILVUS_K8S_SVC = "milvus-standalone.nyro-develop.svc.cluster.local" # No longer default for URI
REDIS_K8S_SVC = "redis-service-master.nyro-develop.svc.cluster.local"
EMBEDDING_SERVICE_K8S_SVC = "embedding-service.nyro-develop.svc.cluster.local"
DOCPROC_SERVICE_K8S_SVC = "docproc-service.nyro-develop.svc.cluster.local"

# --- Defaults ---
POSTGRES_K8S_PORT_DEFAULT = 5432
POSTGRES_K8S_DB_DEFAULT = "atenex"
POSTGRES_K8S_USER_DEFAULT = "postgres"
# MILVUS_K8S_PORT_DEFAULT = 19530 # No longer default for URI
ZILLIZ_ENDPOINT_DEFAULT = "https://in03-0afab716eb46d7f.serverless.gcp-us-west1.cloud.zilliz.com"
# DEFAULT_MILVUS_URI = f"http://{MILVUS_K8S_SVC}:{MILVUS_K8S_PORT_DEFAULT}" # Replaced by ZILLIZ_ENDPOINT_DEFAULT
MILVUS_DEFAULT_COLLECTION = "atenex_collection"
MILVUS_DEFAULT_INDEX_PARAMS = '{"metric_type": "IP", "index_type": "HNSW", "params": {"M": 16, "efConstruction": 256}}'
MILVUS_DEFAULT_SEARCH_PARAMS = '{"metric_type": "IP", "params": {"ef": 128}}'
DEFAULT_EMBEDDING_DIM = 384
DEFAULT_TIKTOKEN_ENCODING = "cl100k_base"

DEFAULT_EMBEDDING_SERVICE_URL = f"http://{EMBEDDING_SERVICE_K8S_SVC}:80/api/v1/embed"
DEFAULT_DOCPROC_SERVICE_URL = f"http://{DOCPROC_SERVICE_K8S_SVC}:80/api/v1/process"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file='.env', env_prefix='INGEST_', env_file_encoding='utf-8',
        case_sensitive=False, extra='ignore'
    )

    PROJECT_NAME: str = "Atenex Ingest Service"
    API_V1_STR: str = "/api/v1/ingest"
    LOG_LEVEL: str = "INFO"

    CELERY_BROKER_URL: RedisDsn = Field(default_factory=lambda: RedisDsn(f"redis://{REDIS_K8S_SVC}:6379/0"))
    CELERY_RESULT_BACKEND: RedisDsn = Field(default_factory=lambda: RedisDsn(f"redis://{REDIS_K8S_SVC}:6379/1"))

    POSTGRES_USER: str = POSTGRES_K8S_USER_DEFAULT
    POSTGRES_PASSWORD: SecretStr
    POSTGRES_SERVER: str = POSTGRES_K8S_SVC
    POSTGRES_PORT: int = POSTGRES_K8S_PORT_DEFAULT
    POSTGRES_DB: str = POSTGRES_K8S_DB_DEFAULT

    ZILLIZ_API_KEY: SecretStr = Field(description="API Key for Zilliz Cloud connection.")
    MILVUS_URI: str = Field(
        default=ZILLIZ_ENDPOINT_DEFAULT,
        description="Milvus connection URI (Zilliz Cloud HTTPS endpoint)."
    )
    MILVUS_COLLECTION_NAME: str = MILVUS_DEFAULT_COLLECTION
    MILVUS_GRPC_TIMEOUT: int = 10 # Default timeout, can be adjusted via ENV
    MILVUS_CONTENT_FIELD: str = "content"
    MILVUS_EMBEDDING_FIELD: str = "embedding"
    MILVUS_CONTENT_FIELD_MAX_LENGTH: int = 20000
    MILVUS_INDEX_PARAMS: Dict[str, Any] = Field(default_factory=lambda: json.loads(MILVUS_DEFAULT_INDEX_PARAMS))
    MILVUS_SEARCH_PARAMS: Dict[str, Any] = Field(default_factory=lambda: json.loads(MILVUS_DEFAULT_SEARCH_PARAMS))

    GCS_BUCKET_NAME: str = Field(default="atenex", description="Name of the Google Cloud Storage bucket for storing original files.")

    EMBEDDING_DIMENSION: int = Field(default=DEFAULT_EMBEDDING_DIM, description="Dimension of embeddings expected from the embedding service, used for Milvus schema.")
    EMBEDDING_SERVICE_URL: AnyHttpUrl = Field(default_factory=lambda: AnyHttpUrl(DEFAULT_EMBEDDING_SERVICE_URL), description="URL of the external embedding service.")
    DOCPROC_SERVICE_URL: AnyHttpUrl = Field(default_factory=lambda: AnyHttpUrl(DEFAULT_DOCPROC_SERVICE_URL), description="URL of the external document processing service.")

    TIKTOKEN_ENCODING_NAME: str = Field(default=DEFAULT_TIKTOKEN_ENCODING, description="Name of the tiktoken encoding to use for token counting.")

    HTTP_CLIENT_TIMEOUT: int = 60
    HTTP_CLIENT_MAX_RETRIES: int = 3
    HTTP_CLIENT_BACKOFF_FACTOR: float = 1.0

    SUPPORTED_CONTENT_TYPES: List[str] = Field(default=[
        "application/pdf",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/msword",
        "text/plain",
        "text/markdown",
        "text/html",        
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", # XLSX
        "application/vnd.ms-excel"
    ])

    @field_validator("LOG_LEVEL")
    @classmethod
    def check_log_level(cls, v: str) -> str:
        valid_levels = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
        normalized_v = v.upper()
        if normalized_v not in valid_levels: raise ValueError(f"Invalid LOG_LEVEL '{v}'. Must be one of {valid_levels}")
        return normalized_v

    @field_validator('EMBEDDING_DIMENSION')
    @classmethod
    def check_embedding_dimension(cls, v: int, info: ValidationInfo) -> int:
        if v <= 0: raise ValueError("EMBEDDING_DIMENSION must be a positive integer.")
        logging.debug(f"Using EMBEDDING_DIMENSION for Milvus schema: {v}")
        return v

    @field_validator('POSTGRES_PASSWORD', mode='before')
    @classmethod
    def check_required_secret_value_present(cls, v: Any, info: ValidationInfo) -> Any:
        if v is None or v == "":
             field_name = info.field_name if info.field_name else "Unknown Secret Field"
             raise ValueError(f"Required secret field '{field_name}' (mapped from INGEST_{field_name.upper()}) cannot be empty.")
        return v

    @field_validator('MILVUS_URI', mode='before')
    @classmethod
    def validate_milvus_uri(cls, v: Any) -> str:
        if not isinstance(v, str):
            raise ValueError("MILVUS_URI must be a string.")
        v_strip = v.strip()
        if not v_strip.startswith("https://"): 
            raise ValueError(f"Invalid Zilliz URI: Must start with https://. Received: '{v_strip}'")
        try:
            parsed = urlparse(v_strip)
            if not parsed.hostname:
                raise ValueError(f"Invalid URI: Missing hostname in '{v_strip}'")
            return v_strip 
        except Exception as e:
            raise ValueError(f"Invalid Milvus URI format '{v_strip}': {e}") from e

    @field_validator('ZILLIZ_API_KEY', mode='before')
    @classmethod
    def check_zilliz_key(cls, v: Any, info: ValidationInfo) -> Any:
         if v is None or v == "" or (isinstance(v, SecretStr) and not v.get_secret_value()):
            raise ValueError(f"Required secret field for Zilliz (expected as INGEST_ZILLIZ_API_KEY) cannot be empty.")
         return v

    @field_validator('EMBEDDING_SERVICE_URL', 'DOCPROC_SERVICE_URL', mode='before')
    @classmethod
    def assemble_service_url(cls, v: Optional[str], info: ValidationInfo) -> str:
        default_map = {
            "EMBEDDING_SERVICE_URL": DEFAULT_EMBEDDING_SERVICE_URL,
            "DOCPROC_SERVICE_URL": DEFAULT_DOCPROC_SERVICE_URL
        }
        default_url_key = str(info.field_name) 
        default_url = default_map.get(default_url_key, "")

        url_to_validate = v if v is not None else default_url 
        if not url_to_validate:
            raise ValueError(f"URL for {default_url_key} cannot be empty and no default is set.")

        try:
            validated_url = AnyHttpUrl(url_to_validate)
            return str(validated_url)
        except ValidationError as ve:
             raise ValueError(f"Invalid URL format for {default_url_key}: {ve}") from ve


temp_log = logging.getLogger("ingest_service.config.loader")
if not temp_log.handlers:
    handler = logging.StreamHandler(sys.stdout)
    formatter = logging.Formatter('%(levelname)-8s [%(asctime)s] [%(name)s] %(message)s', datefmt='%Y-%m-%d %H:%M:%S')
    handler.setFormatter(formatter)
    temp_log.addHandler(handler)
    temp_log.setLevel(logging.INFO) 

try:
    temp_log.info("Loading Ingest Service settings...")
    settings = Settings()
    temp_log.info("--- Ingest Service Settings Loaded ---")
    temp_log.info(f"  PROJECT_NAME:                 {settings.PROJECT_NAME}")
    temp_log.info(f"  LOG_LEVEL:                    {settings.LOG_LEVEL}")
    temp_log.info(f"  API_V1_STR:                   {settings.API_V1_STR}")
    temp_log.info(f"  CELERY_BROKER_URL:            {settings.CELERY_BROKER_URL}")
    temp_log.info(f"  CELERY_RESULT_BACKEND:        {settings.CELERY_RESULT_BACKEND}")
    temp_log.info(f"  POSTGRES_SERVER:              {settings.POSTGRES_SERVER}:{settings.POSTGRES_PORT}")
    temp_log.info(f"  POSTGRES_DB:                  {settings.POSTGRES_DB}")
    temp_log.info(f"  POSTGRES_USER:                {settings.POSTGRES_USER}")
    pg_pass_status = '*** SET ***' if settings.POSTGRES_PASSWORD and settings.POSTGRES_PASSWORD.get_secret_value() else '!!! NOT SET !!!'
    temp_log.info(f"  POSTGRES_PASSWORD:            {pg_pass_status}")
    temp_log.info(f"  MILVUS_URI (for Pymilvus):    {settings.MILVUS_URI}")
    zilliz_api_key_status = '*** SET ***' if settings.ZILLIZ_API_KEY and settings.ZILLIZ_API_KEY.get_secret_value() else '!!! NOT SET !!!'
    temp_log.info(f"  ZILLIZ_API_KEY:               {zilliz_api_key_status}")
    temp_log.info(f"  MILVUS_COLLECTION_NAME:       {settings.MILVUS_COLLECTION_NAME}")
    temp_log.info(f"  MILVUS_GRPC_TIMEOUT:          {settings.MILVUS_GRPC_TIMEOUT}")
    temp_log.info(f"  GCS_BUCKET_NAME:              {settings.GCS_BUCKET_NAME}")
    temp_log.info(f"  EMBEDDING_DIMENSION (Milvus): {settings.EMBEDDING_DIMENSION}")
    temp_log.info(f"  INGEST_EMBEDDING_SERVICE_URL (from env INGEST_EMBEDDING_SERVICE_URL): {settings.EMBEDDING_SERVICE_URL}")
    temp_log.info(f"  INGEST_DOCPROC_SERVICE_URL (from env INGEST_DOCPROC_SERVICE_URL):   {settings.DOCPROC_SERVICE_URL}")
    temp_log.info(f"  TIKTOKEN_ENCODING_NAME:       {settings.TIKTOKEN_ENCODING_NAME}")
    temp_log.info(f"  SUPPORTED_CONTENT_TYPES:      {settings.SUPPORTED_CONTENT_TYPES}")
    temp_log.info(f"------------------------------------")

except (ValidationError, ValueError) as e:
    error_details = ""
    if isinstance(e, ValidationError):
        try: error_details = f"\nValidation Errors:\n{e.json(indent=2)}"
        except Exception: error_details = f"\nRaw Errors: {e.errors()}"
    else: # ValueError
        error_details = f"\nError: {str(e)}"
    temp_log.critical("!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
    temp_log.critical(f"! FATAL: Ingest Service configuration validation failed:{error_details}")
    temp_log.critical(f"! Check environment variables (prefixed with INGEST_) or .env file.")
    temp_log.critical(f"! Original Error Type: {type(e).__name__}")
    temp_log.critical("!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
    sys.exit(1)
except Exception as e: 
    temp_log.exception(f"FATAL: Unexpected error loading Ingest Service settings: {e}")
    sys.exit(1)
```

## File: `app\core\logging_config.py`
```py
import logging
import sys
import structlog
from app.core.config import settings
import os

def setup_logging():
    """Configura el logging estructurado con structlog."""

    # Disable existing handlers if running in certain environments (like Uvicorn default)
    # to avoid duplicate logs. This might need adjustment based on deployment.
    # logging.getLogger().handlers.clear()

    # Determine if running inside Celery worker
    is_celery_worker = "celery" in sys.argv[0]

    shared_processors = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
    ]

    if settings.LOG_LEVEL == logging.DEBUG:
         # Add caller info only in debug mode for performance
         shared_processors.append(structlog.processors.CallsiteParameterAdder(
             {
                 structlog.processors.CallsiteParameter.FILENAME,
                 structlog.processors.CallsiteParameter.LINENO,
             }
         ))

    # Configure structlog
    structlog.configure(
        processors=shared_processors + [
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    # Configure the formatter for stdlib logging
    formatter = structlog.stdlib.ProcessorFormatter(
        # These run ONCE per log structuralization
        foreign_pre_chain=shared_processors,
         # These run on EVERY record
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.JSONRenderer(), # Render as JSON
        ],
    )

    # Configure root logger handler
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    # Avoid adding handler twice if already configured (e.g., by Uvicorn/Gunicorn)
    if not any(isinstance(h, logging.StreamHandler) for h in root_logger.handlers):
         root_logger.addHandler(handler)

    root_logger.setLevel(settings.LOG_LEVEL)

    # Silence verbose libraries
    logging.getLogger("uvicorn").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("asyncpg").setLevel(logging.WARNING)
    logging.getLogger("haystack").setLevel(logging.INFO) # Or DEBUG for more Haystack details
    logging.getLogger("milvus_haystack").setLevel(logging.INFO) # Adjust as needed

    log = structlog.get_logger("ingest_service")
    log.info("Logging configured", log_level=settings.LOG_LEVEL, is_celery_worker=is_celery_worker)
```

## File: `app\db\postgres_client.py`
```py

```

## File: `app\domain\__init__.py`
```py

```

## File: `app\domain\entities.py`
```py
import uuid
from datetime import datetime
from typing import Optional, Dict, Any, List
from pydantic import BaseModel, Field
from app.domain.enums import DocumentStatus, ChunkVectorStatus

class Document(BaseModel):
    id: uuid.UUID
    company_id: uuid.UUID
    file_name: str
    file_type: str
    file_path: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = Field(default_factory=dict)
    status: DocumentStatus
    chunk_count: Optional[int] = 0
    error_message: Optional[str] = None
    uploaded_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    gcs_exists: Optional[bool] = None
    milvus_chunk_count_live: Optional[int] = None

    class Config:
        from_attributes = True

class ChunkMetadata(BaseModel):
    page: Optional[int] = None
    title: Optional[str] = None
    tokens: Optional[int] = None
    content_hash: Optional[str] = Field(None, max_length=64)

class Chunk(BaseModel):
    id: Optional[uuid.UUID] = None
    document_id: uuid.UUID
    company_id: uuid.UUID
    chunk_index: int
    content: str
    metadata: ChunkMetadata = Field(default_factory=ChunkMetadata)
    embedding_id: Optional[str] = None
    vector_status: ChunkVectorStatus = ChunkVectorStatus.PENDING
    created_at: Optional[datetime] = None

    class Config:
        from_attributes = True

```

## File: `app\domain\enums.py`
```py
from enum import Enum

class DocumentStatus(str, Enum):
    UPLOADED = "uploaded"
    PROCESSING = "processing"
    PROCESSED = "processed"
    ERROR = "error"
    PENDING = "pending"

class ChunkVectorStatus(str, Enum):
    PENDING = "pending"     # Initial state before Milvus insertion attempt
    CREATED = "created"     # Successfully inserted into Milvus
    ERROR = "error"         # Failed to insert into Milvus

```

## File: `app\domain\exceptions.py`
```py
# app/domain/exceptions.py

class DomainException(Exception):
    """Base class for domain exceptions."""
    def __init__(self, message: str, status_code: int = 500):
        super().__init__(message)
        self.status_code = status_code
        self.detail = message

class DocumentNotFoundException(DomainException):
    def __init__(self, document_id: str = None, message: str = "Document not found."):
        super().__init__(message, status_code=404)
        self.document_id = document_id

class DuplicateDocumentException(DomainException):
    def __init__(self, filename: str = None, message: str = "Duplicate document found."):
        super().__init__(message, status_code=409)
        self.filename = filename

class OperationConflictException(DomainException):
    def __init__(self, message: str = "Operation cannot be completed due to a conflict."):
        super().__init__(message, status_code=409)

class InvalidInputException(DomainException):
    def __init__(self, message: str = "Invalid input provided."):
        super().__init__(message, status_code=400)

class ServiceDependencyException(DomainException):
    def __init__(self, service_name: str, original_error: str = "", message: str = "Error with a dependent service."):
        detailed_message = f"{message} Service: {service_name}. Original error: {original_error}"
        super().__init__(detailed_message, status_code=503)
        self.service_name = service_name
        self.original_error = original_error

class StorageException(DomainException):
    def __init__(self, message: str = "Storage operation failed."):
        super().__init__(message, status_code=503)

class DatabaseException(DomainException):
    def __init__(self, message: str = "Database operation failed."):
        super().__init__(message, status_code=503)

class VectorStoreException(DomainException):
    def __init__(self, message: str = "Vector store operation failed."):
        super().__init__(message, status_code=503)
```

## File: `app\infrastructure\__init__.py`
```py

```

## File: `app\infrastructure\clients\__init__.py`
```py

```

## File: `app\infrastructure\clients\remote_docproc_adapter.py`
```py
from typing import Optional, Dict, Any
from app.application.ports.docproc_service_port import DocProcServicePort
from app.services.clients.docproc_service_client import DocProcServiceClient
from app.domain.exceptions import ServiceDependencyException # Import corrected
import asyncio

class RemoteDocProcAdapter(DocProcServicePort):
    def __init__(self):
        # DocProcServiceClient now handles its own base URL from settings
        self.client = DocProcServiceClient() 

    async def process_document(self, file_bytes: bytes, original_filename: str, content_type: str, document_id: Optional[str], company_id: Optional[str]) -> Dict[str, Any]:
        # This method remains async for potential async use cases if any
        try:
            # Ensure the async client method is called
            return await self.client.process_document_async(file_bytes, original_filename, content_type, document_id, company_id)
        except ServiceDependencyException: # Catch specific exception from client
            raise
        except Exception as e: # Catch any other unexpected error
            raise ServiceDependencyException(service_name="DocProcService", original_error=str(e)) from e

    def process_document_sync(self, file_bytes: bytes, original_filename: str, content_type: str, document_id: Optional[str], company_id: Optional[str]) -> Dict[str, Any]:
        try:
            # Call the new synchronous method on the client
            return self.client.process_document_sync(file_bytes, original_filename, content_type, document_id, company_id)
        except ServiceDependencyException:
            raise
        except Exception as e:
            raise ServiceDependencyException(service_name="DocProcService", original_error=str(e)) from e
```

## File: `app\infrastructure\clients\remote_embedding_adapter.py`
```py
from typing import List, Dict, Tuple, Any
from app.application.ports.embedding_service_port import EmbeddingServicePort
from app.services.clients.embedding_service_client import EmbeddingServiceClient
from app.domain.exceptions import ServiceDependencyException # Import corrected
import asyncio

class RemoteEmbeddingAdapter(EmbeddingServicePort):
    def __init__(self):
        # EmbeddingServiceClient now handles its own base URL from settings
        self.client = EmbeddingServiceClient()

    async def get_embeddings(self, texts: List[str], text_type: str = "passage") -> Tuple[List[List[float]], Dict[str, Any]]:
        # This method remains async
        try:
            # Ensure the async client method is called
            return await self.client.get_embeddings_async(texts, text_type)
        except ServiceDependencyException:
            raise
        except Exception as e:
            raise ServiceDependencyException(service_name="EmbeddingService", original_error=str(e)) from e

    def get_embeddings_sync(self, texts: List[str], text_type: str = "passage") -> Tuple[List[List[float]], Dict[str, Any]]:
        try:
            # Call the new synchronous method on the client
            return self.client.get_embeddings_sync(texts, text_type)
        except ServiceDependencyException:
            raise
        except Exception as e:
            raise ServiceDependencyException(service_name="EmbeddingService", original_error=str(e)) from e
```

## File: `app\infrastructure\dependency_injection.py`
```py
"""
Centralized dependency injection for adapters and use cases in the ingest-service.
Provides factory functions that instantiate adapters and use cases as needed.
This avoids premature instantiation of singletons, especially for resources like DB connections or HTTP clients
that have a lifecycle or might need different configurations (e.g., sync vs async clients).
"""
from app.infrastructure.persistence.postgres_document_repository import PostgresDocumentRepositoryAdapter
from app.infrastructure.persistence.postgres_chunk_repository import PostgresChunkRepositoryAdapter
from app.infrastructure.storage.gcs_adapter import GCSAdapter
from app.infrastructure.vectorstore.milvus_adapter import MilvusAdapter
from app.infrastructure.clients.remote_docproc_adapter import RemoteDocProcAdapter
from app.infrastructure.clients.remote_embedding_adapter import RemoteEmbeddingAdapter
from app.infrastructure.tasks.celery_task_adapter import CeleryTaskAdapter

from app.application.ports.document_repository_port import DocumentRepositoryPort
from app.application.ports.chunk_repository_port import ChunkRepositoryPort
from app.application.ports.file_storage_port import FileStoragePort
from app.application.ports.vector_store_port import VectorStorePort
from app.application.ports.docproc_service_port import DocProcServicePort
from app.application.ports.embedding_service_port import EmbeddingServicePort
from app.application.ports.task_queue_port import TaskQueuePort

from app.application.use_cases.upload_document_use_case import UploadDocumentUseCase
from app.application.use_cases.get_document_status_use_case import GetDocumentStatusUseCase
from app.application.use_cases.list_documents_use_case import ListDocumentsUseCase
from app.application.use_cases.retry_document_use_case import RetryDocumentUseCase
from app.application.use_cases.delete_document_use_case import DeleteDocumentUseCase
from app.application.use_cases.get_document_stats_use_case import GetDocumentStatsUseCase
from app.application.use_cases.process_document_use_case import ProcessDocumentUseCase

# --- Adapter Factories ---
# These functions will be called by FastAPI's Depends or explicitly in the worker.

def get_document_repository() -> DocumentRepositoryPort:
    # PostgresDocumentRepositoryAdapter uses get_db_pool() and get_sync_engine() internally,
    # which manage their own lifecycle.
    return PostgresDocumentRepositoryAdapter()

def get_chunk_repository() -> ChunkRepositoryPort:
    # PostgresChunkRepositoryAdapter uses get_sync_engine() internally.
    return PostgresChunkRepositoryAdapter()

def get_file_storage() -> FileStoragePort:
    # GCSAdapter instantiates GCSClient which is stateless after init.
    return GCSAdapter()

def get_vector_store() -> VectorStorePort:
    # MilvusAdapter manages its connection and collection.
    return MilvusAdapter()

def get_docproc_service() -> DocProcServicePort:
    # RemoteDocProcAdapter instantiates DocProcServiceClient.
    return RemoteDocProcAdapter()

def get_embedding_service() -> EmbeddingServicePort:
    # RemoteEmbeddingAdapter instantiates EmbeddingServiceClient.
    return RemoteEmbeddingAdapter()

def get_task_queue() -> TaskQueuePort:
    # CeleryTaskAdapter uses the global celery_app.
    return CeleryTaskAdapter()


# --- Use Case Factories (for FastAPI Depends) ---

def get_upload_document_use_case(
    doc_repo: DocumentRepositoryPort = Depends(get_document_repository),
    file_storage: FileStoragePort = Depends(get_file_storage),
    task_queue: TaskQueuePort = Depends(get_task_queue)
) -> UploadDocumentUseCase:
    return UploadDocumentUseCase(
        document_repository=doc_repo,
        file_storage=file_storage,
        task_queue=task_queue
    )

def get_get_document_status_use_case(
    doc_repo: DocumentRepositoryPort = Depends(get_document_repository),
    file_storage: FileStoragePort = Depends(get_file_storage),
    vector_store: VectorStorePort = Depends(get_vector_store)
) -> GetDocumentStatusUseCase:
    return GetDocumentStatusUseCase(
        document_repository=doc_repo,
        file_storage=file_storage,
        vector_store=vector_store
    )

def get_list_documents_use_case(
    doc_repo: DocumentRepositoryPort = Depends(get_document_repository),
    file_storage: FileStoragePort = Depends(get_file_storage), # Needed for GetDocumentStatusUseCase dependency
    vector_store: VectorStorePort = Depends(get_vector_store)   # Needed for GetDocumentStatusUseCase dependency
) -> ListDocumentsUseCase:
    return ListDocumentsUseCase(
        document_repository=doc_repo,
        file_storage=file_storage,
        vector_store=vector_store
    )

def get_retry_document_use_case(
    doc_repo: DocumentRepositoryPort = Depends(get_document_repository),
    task_queue: TaskQueuePort = Depends(get_task_queue)
) -> RetryDocumentUseCase:
    return RetryDocumentUseCase(
        document_repository=doc_repo,
        task_queue=task_queue
    )

def get_delete_document_use_case(
    doc_repo: DocumentRepositoryPort = Depends(get_document_repository),
    file_storage: FileStoragePort = Depends(get_file_storage),
    vector_store: VectorStorePort = Depends(get_vector_store)
) -> DeleteDocumentUseCase:
    return DeleteDocumentUseCase(
        document_repository=doc_repo,
        file_storage=file_storage,
        vector_store=vector_store
    )

def get_get_document_stats_use_case(
    doc_repo: DocumentRepositoryPort = Depends(get_document_repository)
) -> GetDocumentStatsUseCase:
    return GetDocumentStatsUseCase(
        document_repository=doc_repo
    )

# For Celery Worker (instantiated directly in the task)
def create_process_document_use_case_for_worker() -> ProcessDocumentUseCase:
    """
    Factory function to create ProcessDocumentUseCase with its (synchronous) dependencies
    for use within the Celery worker context.
    """
    return ProcessDocumentUseCase(
        document_repository=get_document_repository(), # Will use sync methods internally
        chunk_repository=get_chunk_repository(),
        file_storage=get_file_storage(),         # Will use sync methods internally
        docproc_service=get_docproc_service(),     # Will use sync methods internally
        embedding_service=get_embedding_service(), # Will use sync methods internally
        vector_store=get_vector_store()          # Will use sync methods internally
    )
```

## File: `app\infrastructure\persistence\__init__.py`
```py

```

## File: `app\infrastructure\persistence\postgres_chunk_repository.py`
```py
from typing import List, Dict, Any
import uuid
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.domain.entities import Chunk
from app.application.ports.chunk_repository_port import ChunkRepositoryPort
from .postgres_connector import get_sync_engine, document_chunks_table
import structlog

log = structlog.get_logger(__name__)

class PostgresChunkRepositoryAdapter(ChunkRepositoryPort):
    def save_bulk(self, chunks: List[Chunk]) -> int:
        if not chunks:
            log.warning("PostgresChunkRepositoryAdapter.save_bulk called with empty list of chunks.")
            return 0

        engine = get_sync_engine()
        
        prepared_data: List[Dict[str, Any]] = []
        for chunk_obj in chunks:
            prepared_chunk = {
                "id": chunk_obj.id or uuid.uuid4(), # Generate UUID if not provided
                "document_id": chunk_obj.document_id,
                "company_id": chunk_obj.company_id,
                "chunk_index": chunk_obj.chunk_index,
                "content": chunk_obj.content,
                "metadata": chunk_obj.metadata.model_dump(mode='json') if chunk_obj.metadata else None, # Ensure metadata is dict
                "embedding_id": chunk_obj.embedding_id,
                "vector_status": chunk_obj.vector_status.value,
                # created_at is handled by server_default in DB schema
            }
            prepared_data.append(prepared_chunk)
        
        insert_log = log.bind(component="PostgresChunkRepo", num_chunks=len(prepared_data), document_id=str(chunks[0].document_id))
        insert_log.info("Attempting synchronous bulk insert of document chunks.")

        inserted_count = 0
        try:
            with engine.connect() as connection:
                with connection.begin(): # Start transaction
                    stmt = pg_insert(document_chunks_table).values(prepared_data)
                    # ON CONFLICT DO NOTHING to avoid errors if a chunk (doc_id, chunk_index) already exists
                    # This assumes 'uq_document_chunk_index' (document_id, chunk_index) is the unique constraint
                    stmt = stmt.on_conflict_do_update(
                        index_elements=[document_chunks_table.c.document_id, document_chunks_table.c.chunk_index],
                        set_={
                            'content': stmt.excluded.content,
                            'metadata': stmt.excluded.metadata,
                            'embedding_id': stmt.excluded.embedding_id,
                            'vector_status': stmt.excluded.vector_status,
                            # 'created_at' should not be updated on conflict typically
                        }
                    )
                    result = connection.execute(stmt)
                    # rowcount might not be reliable for INSERT ... ON CONFLICT for reflecting actual inserts vs updates.
                    # For simplicity, we'll assume if it runs without error, all intended operations (insert or update) were attempted.
                    # A more accurate count would require querying after or using RETURNING (more complex).
                inserted_count = len(prepared_data) 
                insert_log.info("Bulk insert/update statement for chunks executed successfully.", 
                                intended_count=len(prepared_data), reported_rowcount=result.rowcount if result else -1)
                return inserted_count
        except Exception as e:
            insert_log.error("Error during synchronous bulk chunk insert", error=str(e), exc_info=True)
            raise RuntimeError(f"Failed to bulk insert/update chunks: {e}") from e
```

## File: `app\infrastructure\persistence\postgres_connector.py`
```py
import asyncpg
from sqlalchemy import create_engine, MetaData, Column, Uuid as SqlAlchemyUuid, Integer, Text, String, DateTime, UniqueConstraint, ForeignKeyConstraint, Index, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import Engine
from typing import Optional
from app.core.config import settings
import json
import structlog

log = structlog.get_logger(__name__)

_db_pool: Optional[asyncpg.Pool] = None
_sync_engine: Optional[Engine] = None
_sync_engine_dsn: Optional[str] = None

async def _init_asyncpg_connection(conn):
    log.debug("Initializing asyncpg connection with type codecs.")
    await conn.set_type_codec('jsonb', encoder=json.dumps, decoder=json.loads, schema='pg_catalog', format='text')
    await conn.set_type_codec('json', encoder=json.dumps, decoder=json.loads, schema='pg_catalog', format='text')
    log.debug("Asyncpg type codecs set for json/jsonb.")

async def get_db_pool() -> asyncpg.Pool:
    global _db_pool
    if _db_pool is None or _db_pool._closed:
        dsn = f"postgresql://{settings.POSTGRES_USER}:{settings.POSTGRES_PASSWORD.get_secret_value()}@{settings.POSTGRES_SERVER}:{settings.POSTGRES_PORT}/{settings.POSTGRES_DB}"
        log.info("Creating PostgreSQL async connection pool.", dsn_host=settings.POSTGRES_SERVER, db_name=settings.POSTGRES_DB)
        try:
            _db_pool = await asyncpg.create_pool(
                dsn=dsn,
                init=_init_asyncpg_connection,
                min_size=2,
                max_size=10,
                timeout=30.0,
                command_timeout=60.0,
                statement_cache_size=0 
            )
            log.info("PostgreSQL async connection pool created successfully.")
        except Exception as e:
            log.critical("Failed to create PostgreSQL async connection pool", error=str(e), exc_info=True)
            _db_pool = None 
            raise
    return _db_pool

async def close_db_pool():
    global _db_pool
    if _db_pool and not _db_pool._closed:
        log.info("Closing PostgreSQL async connection pool.")
        await _db_pool.close()
        _db_pool = None
        log.info("PostgreSQL async connection pool closed.")

def get_sync_engine() -> Engine:
    global _sync_engine, _sync_engine_dsn
    if _sync_engine is None:
        if not _sync_engine_dsn:
            _sync_engine_dsn = f"postgresql+psycopg2://{settings.POSTGRES_USER}:{settings.POSTGRES_PASSWORD.get_secret_value()}@{settings.POSTGRES_SERVER}:{settings.POSTGRES_PORT}/{settings.POSTGRES_DB}"
        log.info("Creating SQLAlchemy synchronous engine.", dsn_host=settings.POSTGRES_SERVER, db_name=settings.POSTGRES_DB)
        try:
            _sync_engine = create_engine(
                _sync_engine_dsn,
                pool_size=5,
                max_overflow=5,
                pool_timeout=30,
                pool_recycle=1800,
                json_serializer=json.dumps,
                json_deserializer=json.loads
            )
            with _sync_engine.connect() as conn_test:
                conn_test.execute(text("SELECT 1"))
            log.info("SQLAlchemy synchronous engine created and tested successfully.")
        except Exception as e:
            log.critical("Failed to create SQLAlchemy synchronous engine", error=str(e), exc_info=True)
            _sync_engine = None
            raise
    return _sync_engine

def dispose_sync_engine():
    global _sync_engine
    if _sync_engine:
        log.info("Disposing SQLAlchemy synchronous engine pool.")
        _sync_engine.dispose()
        _sync_engine = None
        log.info("SQLAlchemy synchronous engine pool disposed.")

metadata_obj = MetaData()

document_chunks_table = Table(
    'document_chunks',
    metadata_obj,
    Column('id', SqlAlchemyUuid(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")),
    Column('document_id', SqlAlchemyUuid(as_uuid=True), nullable=False),
    Column('company_id', SqlAlchemyUuid(as_uuid=True), nullable=False),
    Column('chunk_index', Integer, nullable=False),
    Column('content', Text, nullable=False),
    Column('metadata', JSONB),
    Column('embedding_id', String(255)),
    Column('created_at', DateTime(timezone=True), server_default=text("timezone('utc', now())")),
    Column('vector_status', String(50), default='pending'),
    UniqueConstraint('document_id', 'chunk_index', name='uq_document_chunk_index'),
    ForeignKeyConstraint(['document_id'], ['documents.id'], name='fk_document_chunks_document', ondelete='CASCADE'),
    Index('idx_document_chunks_document_id', 'document_id'),
    Index('idx_document_chunks_company_id', 'company_id'),
    Index('idx_document_chunks_embedding_id', 'embedding_id'),
)
```

## File: `app\infrastructure\persistence\postgres_document_repository.py`
```py
import uuid
from typing import Optional, Tuple, List, Dict, Any
from datetime import datetime, timezone, date
from sqlalchemy import text, update, insert, delete, select, func, and_

from app.domain.entities import Document
from app.domain.enums import DocumentStatus
from app.application.ports.document_repository_port import DocumentRepositoryPort
from .postgres_connector import get_db_pool, get_sync_engine, metadata_obj
import structlog

log = structlog.get_logger(__name__)

documents_table = metadata_obj.tables.get('documents') # Assume table 'documents' is defined elsewhere or reflected

class PostgresDocumentRepositoryAdapter(DocumentRepositoryPort):

    async def save(self, document: Document) -> None:
        pool = await get_db_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO documents (id, company_id, file_name, file_type, file_path, metadata, status, chunk_count, error_message, uploaded_at, updated_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
                """,
                document.id, document.company_id, document.file_name, document.file_type, document.file_path,
                document.metadata or {}, document.status.value, document.chunk_count or 0, document.error_message,
                document.uploaded_at or datetime.now(timezone.utc),
                document.updated_at or datetime.now(timezone.utc)
            )
        log.info("Document saved (async)", document_id=document.id)


    async def find_by_id(self, doc_id: uuid.UUID, company_id: uuid.UUID) -> Optional[Document]:
        pool = await get_db_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM documents WHERE id = $1 AND company_id = $2", doc_id, company_id
            )
            return Document(**dict(row)) if row else None

    def find_by_id_sync(self, doc_id: uuid.UUID, company_id: uuid.UUID) -> Optional[Document]:
        engine = get_sync_engine()
        with engine.connect() as conn:
            # Ensure documents_table is available for sync operations if not reflected globally
            # This might require reflecting it or ensuring it's defined for SQLAlchemy sync context
            # For simplicity, assuming a direct text query if documents_table isn't readily usable here
            stmt = text("SELECT * FROM documents WHERE id = :doc_id AND company_id = :company_id")
            result = conn.execute(stmt, {"doc_id": doc_id, "company_id": company_id}).fetchone()
            return Document(**result._asdict()) if result else None


    async def find_by_name_and_company(self, filename: str, company_id: uuid.UUID) -> Optional[Document]:
        pool = await get_db_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM documents WHERE file_name = $1 AND company_id = $2", filename, company_id
            )
            return Document(**dict(row)) if row else None

    async def update_status(self, doc_id: uuid.UUID, status: DocumentStatus, chunk_count: Optional[int] = None, error_message: Optional[str] = None, updated_at: Optional[datetime] = None) -> bool:
        pool = await get_db_pool()
        async with pool.acquire() as conn:
            values_to_set = {"status": status.value, "updated_at": updated_at or datetime.now(timezone.utc)}
            if chunk_count is not None:
                values_to_set["chunk_count"] = chunk_count
            if status == DocumentStatus.ERROR:
                 values_to_set["error_message"] = error_message
            else: # Clear error message if not an error status
                 values_to_set["error_message"] = None

            set_clauses = [f"{key} = ${i+2}" for i, key in enumerate(values_to_set.keys())]
            params = [doc_id] + list(values_to_set.values())
            
            query = f"UPDATE documents SET {', '.join(set_clauses)} WHERE id = $1"
            result = await conn.execute(query, *params)
            log.info("Document status updated (async)", document_id=doc_id, new_status=status.value, result=result)
            return result == 'UPDATE 1'


    def update_status_sync(self, doc_id: uuid.UUID, status: DocumentStatus, chunk_count: Optional[int] = None, error_message: Optional[str] = None, updated_at: Optional[datetime] = None) -> bool:
        engine = get_sync_engine()
        with engine.connect() as conn:
            values_to_set = {"status": status.value, "updated_at": updated_at or datetime.now(timezone.utc)}
            if chunk_count is not None:
                values_to_set["chunk_count"] = chunk_count
            if status == DocumentStatus.ERROR:
                values_to_set["error_message"] = error_message
            else:
                values_to_set["error_message"] = None
            
            # Assuming documents_table is reflected or available for SQLAlchemy Core
            # If not, use text() query similar to async version but with SQLAlchemy
            stmt = update(metadata_obj.tables['documents']).where(metadata_obj.tables['documents'].c.id == doc_id).values(**values_to_set)
            result = conn.execute(stmt)
            conn.commit()
            log.info("Document status updated (sync)", document_id=doc_id, new_status=status.value, rowcount=result.rowcount)
            return result.rowcount == 1


    async def list_paginated(self, company_id: uuid.UUID, limit: int, offset: int) -> Tuple[List[Document], int]:
        pool = await get_db_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM documents WHERE company_id = $1 ORDER BY uploaded_at DESC LIMIT $2 OFFSET $3",
                company_id, limit, offset
            )
            count_row = await conn.fetchrow(
                "SELECT COUNT(*) as total FROM documents WHERE company_id = $1", company_id
            )
            total_count = count_row['total'] if count_row else 0
            return [Document(**dict(row)) for row in rows], total_count


    async def delete(self, doc_id: uuid.UUID, company_id: uuid.UUID) -> bool:
        pool = await get_db_pool()
        async with pool.acquire() as conn:
            result = await conn.execute(
                "DELETE FROM documents WHERE id = $1 AND company_id = $2", doc_id, company_id
            )
            log.info("Document deleted (async)", document_id=doc_id, result=result)
            return result == 'DELETE 1'

    def delete_sync(self, doc_id: uuid.UUID, company_id: uuid.UUID) -> bool:
        engine = get_sync_engine()
        with engine.connect() as conn:
            stmt = delete(metadata_obj.tables['documents']).where(
                and_(metadata_obj.tables['documents'].c.id == doc_id, metadata_obj.tables['documents'].c.company_id == company_id)
            )
            result = conn.execute(stmt)
            conn.commit()
            log.info("Document deleted (sync)", document_id=doc_id, rowcount=result.rowcount)
            return result.rowcount == 1

    async def get_stats(self, company_id: uuid.UUID, from_date: Optional[date], to_date: Optional[date], status_filter: Optional[DocumentStatus]) -> Dict[str, Any]:
        pool = await get_db_pool()
        async with pool.acquire() as conn:
            params: List[Any] = [company_id]
            param_idx = 2
            
            base_query_where = "company_id = $1"
            if from_date:
                base_query_where += f" AND uploaded_at >= ${param_idx}"
                params.append(from_date)
                param_idx += 1
            if to_date:
                base_query_where += f" AND DATE(uploaded_at) <= ${param_idx}"
                params.append(to_date)
                param_idx += 1
            if status_filter:
                base_query_where += f" AND status = ${param_idx}"
                params.append(status_filter.value)
                param_idx += 1

            total_docs_query = f"SELECT COUNT(*) FROM documents WHERE {base_query_where};"
            total_documents = await conn.fetchval(total_docs_query, *params) or 0

            chunks_where_sql = base_query_where
            chunks_params = list(params)
            current_param_idx_chunks = param_idx

            if not (status_filter and status_filter != DocumentStatus.PROCESSED):
                if not status_filter: # Add 'processed' if no status filter was applied originally
                    chunks_where_sql += f" AND status = ${current_param_idx_chunks}"
                    chunks_params.append(DocumentStatus.PROCESSED.value)
                total_chunks_query = f"SELECT SUM(chunk_count) FROM documents WHERE {chunks_where_sql};"
                total_chunks_processed = await conn.fetchval(total_chunks_query, *chunks_params) or 0
            else: # If filtered by a status that is not 'processed'
                total_chunks_processed = 0


            by_status_query = f"SELECT status, COUNT(*) as count FROM documents WHERE {base_query_where} GROUP BY status;"
            status_rows = await conn.fetch(by_status_query, *params)
            
            by_type_query = f"SELECT file_type, COUNT(*) as count FROM documents WHERE {base_query_where} GROUP BY file_type;"
            type_rows = await conn.fetch(by_type_query, *params)

            dates_query = f"SELECT MIN(uploaded_at) as oldest, MAX(uploaded_at) as newest FROM documents WHERE {base_query_where};"
            date_row = await conn.fetchrow(dates_query, *params)

            return {
                "total_documents": total_documents,
                "total_chunks_processed": total_chunks_processed,
                "status_rows": [dict(row) for row in status_rows], # Let use case format this
                "type_rows": [dict(row) for row in type_rows],     # Let use case format this
                "oldest_document_date": date_row['oldest'] if date_row else None,
                "newest_document_date": date_row['newest'] if date_row else None,
            }
```

## File: `app\infrastructure\storage\__init__.py`
```py

```

## File: `app\infrastructure\storage\gcs_adapter.py`
```py
import asyncio
from typing import Optional
from app.application.ports.file_storage_port import FileStoragePort
from app.services.gcs_client import GCSClient, GCSClientError # Assuming GCSClient is in app/services/
import structlog

log = structlog.get_logger(__name__)

class GCSAdapter(FileStoragePort):
    def __init__(self, bucket_name: Optional[str] = None):
        # The actual GCSClient from app/services/gcs_client.py handles settings.GCS_BUCKET_NAME
        self.client = GCSClient(bucket_name=bucket_name) 
        log.info("GCSAdapter initialized", configured_bucket=self.client.bucket_name)

    async def upload(self, object_name: str, data: bytes, content_type: str) -> str:
        log.debug("GCSAdapter: upload (async)", object_name=object_name)
        return await self.client.upload_file_async(object_name, data, content_type)

    def download(self, object_name: str, target_path: str) -> None:
        log.debug("GCSAdapter: download (sync)", object_name=object_name, target_path=target_path)
        # GCSClient already has download_file_sync
        self.client.download_file_sync(object_name, target_path)

    async def exists(self, object_name: str) -> bool:
        log.debug("GCSAdapter: exists (async)", object_name=object_name)
        return await self.client.check_file_exists_async(object_name)
    
    def exists_sync(self, object_name: str) -> bool:
        log.debug("GCSAdapter: exists_sync (sync)", object_name=object_name)
        # GCSClient already has check_file_exists_sync
        return self.client.check_file_exists_sync(object_name)

    async def delete(self, object_name: str) -> None:
        log.debug("GCSAdapter: delete (async)", object_name=object_name)
        await self.client.delete_file_async(object_name)

    def delete_sync(self, object_name: str) -> None:
        log.debug("GCSAdapter: delete_sync (sync)", object_name=object_name)
        # GCSClient already has delete_file_sync
        self.client.delete_file_sync(object_name)
```

## File: `app\infrastructure\tasks\__init__.py`
```py

```

## File: `app\infrastructure\tasks\celery_app_config.py`
```py
from celery import Celery
from app.core.config import settings

celery_app = Celery(
    'ingest_service',
    broker=settings.CELERY_BROKER_URL,
    backend=settings.CELERY_RESULT_BACKEND
)
celery_app.conf.update(
    task_serializer='json',
    result_serializer='json',
    accept_content=['json'],
    timezone='UTC',
    enable_utc=True,
)

```

## File: `app\infrastructure\tasks\celery_task_adapter.py`
```py
from app.application.ports.task_queue_port import TaskQueuePort
from app.infrastructure.tasks.celery_app_config import celery_app

class CeleryTaskAdapter(TaskQueuePort):
    async def enqueue_process_document_task(self, document_id: str, company_id: str, filename: str, content_type: str) -> str:
        # Celery tasks are always called in a background thread/process, so we use apply_async
        result = celery_app.send_task(
            'app.infrastructure.tasks.celery_worker.process_document_task',
            args=[document_id, company_id, filename, content_type]
        )
        return result.id

```

## File: `app\infrastructure\tasks\celery_worker.py`
```py
from celery import Task # Import Task for self type hint
from app.infrastructure.tasks.celery_app_config import celery_app
from app.infrastructure.dependency_injection import create_process_document_use_case_for_worker
from app.infrastructure.persistence.postgres_connector import get_sync_engine, dispose_sync_engine # For init/shutdown
from app.services.gcs_client import GCSClient # Assuming GCSClient is globally managed for worker for now
from app.core.config import settings # To initialize GCSClient with correct bucket
from celery.signals import worker_process_init, worker_process_shutdown
import uuid
import structlog

# Global resources for worker process, initialized by signals
sync_db_engine_global = None
gcs_client_global = None # GCSClient can be shared if bucket is fixed per worker process


@worker_process_init.connect(weak=False)
def init_worker_resources(**kwargs):
    global sync_db_engine_global, gcs_client_global
    log = structlog.get_logger("celery_worker.init")
    log.info("Worker process initializing resources...")
    try:
        if sync_db_engine_global is None:
            sync_db_engine_global = get_sync_engine() # From postgres_connector
            log.info("Synchronous DB engine initialized for Celery worker.")
        
        if gcs_client_global is None:
            # GCSClient can be instantiated once per worker process
            # It's generally thread-safe for read operations.
            gcs_client_global = GCSClient(bucket_name=settings.GCS_BUCKET_NAME)
            log.info("GCS client initialized for Celery worker.", bucket=settings.GCS_BUCKET_NAME)
            
        # Potentially initialize other shared resources like Milvus connection for VectorStorePort
        # if MilvusAdapter is made to use a global connection.
        # For now, MilvusAdapter handles its own connection.

        log.info("Celery worker resources initialization complete.")
    except Exception as e:
        log.critical("CRITICAL FAILURE during Celery worker resource initialization!", error=str(e), exc_info=True)
        # Decide if worker should exit or try to continue without resources
        raise # Re-raising will likely stop the worker process if critical

@worker_process_shutdown.connect(weak=False)
def shutdown_worker_resources(**kwargs):
    log = structlog.get_logger("celery_worker.shutdown")
    log.info("Worker process shutting down resources...")
    if sync_db_engine_global:
        dispose_sync_engine() # From postgres_connector
        log.info("Synchronous DB engine disposed for Celery worker.")
    # GCSClient doesn't have an explicit close/dispose method in the provided code.
    # HTTP clients used by service adapters (docproc, embedding) will be closed by their BaseServiceClient.
    log.info("Celery worker resources shutdown complete.")


@celery_app.task(name='ingest_service.process_document_task', bind=True) # Ensure task name is unique
def process_document_task(self: Task, document_id_str: str, company_id_str: str, filename: str, content_type: str):
    task_log = structlog.get_logger("celery.task.process_document").bind(
        task_id=str(self.request.id), 
        document_id=document_id_str,
        company_id=company_id_str
    )
    task_log.info("Celery task process_document_task started.", filename=filename, content_type=content_type)

    # Ensure resources are available (they should be from worker_process_init)
    if sync_db_engine_global is None or gcs_client_global is None:
        task_log.error("Critical worker resources not initialized. Task cannot proceed.")
        # This situation indicates a problem with worker_process_init
        # Update status to error manually or re-raise to let Celery handle retry/failure
        # For now, let Celery's retry mechanism handle it if configured, or it will fail.
        raise Exception("Worker resources (DB engine or GCS client) not available.")

    try:
        # Use the factory from dependency_injection to get the use case
        # This ensures all dependencies are correctly wired for the worker context
        use_case = create_process_document_use_case_for_worker()
        
        # Execute the use case
        # The execute method of ProcessDocumentUseCase is synchronous
        result = use_case.execute(
            document_id_str=document_id_str, # Pass as string
            company_id_str=company_id_str,   # Pass as string
            filename=filename,               # Use original filename
            content_type=content_type
        )
        task_log.info("Celery task process_document_task completed successfully.", result=result)
        return result
    except Exception as e:
        task_log.error("Exception in Celery task process_document_task.", error=str(e), exc_info=True)
        # Celery will handle retries/failure based on task configuration and exception type
        raise # Re-raise the exception for Celery to handle
```

## File: `app\infrastructure\vectorstore\__init__.py`
```py

```

## File: `app\infrastructure\vectorstore\milvus_adapter.py`
```py
from __future__ import annotations
import os
import json
import uuid
import hashlib
import structlog
from typing import List, Dict, Any, Optional, Tuple

from app.core.config import settings
log = structlog.get_logger(__name__)

try:
    import tiktoken
    tiktoken_enc = tiktoken.get_encoding(settings.TIKTOKEN_ENCODING_NAME)
    log.info(f"Tiktoken encoder loaded for MilvusAdapter: {settings.TIKTOKEN_ENCODING_NAME}")
except ImportError:
    log.warning("tiktoken not installed, token count for MilvusAdapter will be unavailable.")
    tiktoken_enc = None
except Exception as e:
    log.warning(f"Failed to load tiktoken encoder '{settings.TIKTOKEN_ENCODING_NAME}' for MilvusAdapter", error=str(e))
    tiktoken_enc = None

from pymilvus import (
    Collection, CollectionSchema, FieldSchema, DataType, connections,
    utility, MilvusException
)

from app.application.ports.vector_store_port import VectorStorePort
from app.domain.entities import ChunkMetadata # For preparing PG data

# Milvus constants from original ingest_pipeline.py
MILVUS_COLLECTION_NAME = settings.MILVUS_COLLECTION_NAME
MILVUS_EMBEDDING_DIM = settings.EMBEDDING_DIMENSION
MILVUS_PK_FIELD = "pk_id"
MILVUS_VECTOR_FIELD = settings.MILVUS_EMBEDDING_FIELD
MILVUS_CONTENT_FIELD = settings.MILVUS_CONTENT_FIELD
MILVUS_COMPANY_ID_FIELD = "company_id"
MILVUS_DOCUMENT_ID_FIELD = "document_id"
MILVUS_FILENAME_FIELD = "file_name"
MILVUS_PAGE_FIELD = "page"
MILVUS_TITLE_FIELD = "title"
MILVUS_TOKENS_FIELD = "tokens"
MILVUS_CONTENT_HASH_FIELD = "content_hash"


class MilvusAdapter(VectorStorePort):
    _milvus_collection: Optional[Collection] = None # Class variable for shared collection object

    def __init__(self, alias: str = "default_ingest_milvus"):
        self.alias = alias
        self.log = log.bind(milvus_adapter_alias=self.alias, collection_name=MILVUS_COLLECTION_NAME)
        self._ensure_connection()
        self.ensure_collection_and_indexes() # This will set self._milvus_collection

    def _ensure_connection(self):
        if self.alias not in connections.list_connections() or not connections.has_connection(self.alias):
            self.log.info("Connecting to Milvus (Zilliz)...", uri=settings.MILVUS_URI)
            try:
                connections.connect(
                    alias=self.alias,
                    uri=settings.MILVUS_URI,
                    timeout=settings.MILVUS_GRPC_TIMEOUT, # Ensure this is in settings
                    token=settings.ZILLIZ_API_KEY.get_secret_value() if settings.ZILLIZ_API_KEY else None
                )
                self.log.info("Connected to Milvus (Zilliz).")
            except Exception as e:
                self.log.error("Failed to connect to Milvus (Zilliz)", error=str(e), exc_info=True)
                raise ConnectionError(f"Milvus (Zilliz) connection failed: {e}") from e
        else:
             self.log.debug("Milvus connection alias already exists and seems active.", alias=self.alias)


    def _create_milvus_collection(self) -> Collection:
        create_log = self.log.bind(embedding_dim=MILVUS_EMBEDDING_DIM)
        create_log.info("Defining schema for new Milvus collection.")
        fields = [
            FieldSchema(name=MILVUS_PK_FIELD, dtype=DataType.VARCHAR, max_length=255, is_primary=True),
            FieldSchema(name=MILVUS_VECTOR_FIELD, dtype=DataType.FLOAT_VECTOR, dim=MILVUS_EMBEDDING_DIM),
            FieldSchema(name=MILVUS_CONTENT_FIELD, dtype=DataType.VARCHAR, max_length=settings.MILVUS_CONTENT_FIELD_MAX_LENGTH),
            FieldSchema(name=MILVUS_COMPANY_ID_FIELD, dtype=DataType.VARCHAR, max_length=64),
            FieldSchema(name=MILVUS_DOCUMENT_ID_FIELD, dtype=DataType.VARCHAR, max_length=64),
            FieldSchema(name=MILVUS_FILENAME_FIELD, dtype=DataType.VARCHAR, max_length=512),
            FieldSchema(name=MILVUS_PAGE_FIELD, dtype=DataType.INT64, default_value=-1),
            FieldSchema(name=MILVUS_TITLE_FIELD, dtype=DataType.VARCHAR, max_length=512, default_value=""),
            FieldSchema(name=MILVUS_TOKENS_FIELD, dtype=DataType.INT64, default_value=-1),
            FieldSchema(name=MILVUS_CONTENT_HASH_FIELD, dtype=DataType.VARCHAR, max_length=64)
        ]
        schema = CollectionSchema(fields, description="Atenex Document Chunks", enable_dynamic_field=False)
        create_log.info("Schema defined. Creating collection...")
        collection = Collection(name=MILVUS_COLLECTION_NAME, schema=schema, using=self.alias, consistency_level="Strong")
        create_log.info(f"Collection '{MILVUS_COLLECTION_NAME}' created. Creating indexes...")
        
        index_params = settings.MILVUS_INDEX_PARAMS
        create_log.info("Creating HNSW index for vector field", field_name=MILVUS_VECTOR_FIELD, index_params=index_params)
        collection.create_index(field_name=MILVUS_VECTOR_FIELD, index_params=index_params, index_name=f"{MILVUS_VECTOR_FIELD}_hnsw_idx")
        
        scalar_fields_to_index = [MILVUS_COMPANY_ID_FIELD, MILVUS_DOCUMENT_ID_FIELD, MILVUS_CONTENT_HASH_FIELD]
        for field_name in scalar_fields_to_index:
            create_log.info(f"Creating scalar index for {field_name} field...")
            collection.create_index(field_name=field_name, index_name=f"{field_name}_idx")
        
        create_log.info("All required indexes created successfully.")
        return collection

    def _check_and_create_indexes(self, collection: Collection):
        check_log = self.log
        existing_indexes = collection.indexes
        existing_index_fields = {idx.field_name for idx in existing_indexes}
        required_indexes_map = {
            MILVUS_VECTOR_FIELD: (settings.MILVUS_INDEX_PARAMS, f"{MILVUS_VECTOR_FIELD}_hnsw_idx"),
            MILVUS_COMPANY_ID_FIELD: (None, f"{MILVUS_COMPANY_ID_FIELD}_idx"),
            MILVUS_DOCUMENT_ID_FIELD: (None, f"{MILVUS_DOCUMENT_ID_FIELD}_idx"),
            MILVUS_CONTENT_HASH_FIELD: (None, f"{MILVUS_CONTENT_HASH_FIELD}_idx"),
        }
        for field_name, (index_params, index_name) in required_indexes_map.items():
            if field_name not in existing_index_fields:
                check_log.warning(f"Index missing for field '{field_name}'. Creating '{index_name}'...")
                collection.create_index(field_name=field_name, index_params=index_params, index_name=index_name)
                check_log.info(f"Index '{index_name}' created for field '{field_name}'.")

    def ensure_collection_and_indexes(self) -> None:
        if MilvusAdapter._milvus_collection is None or MilvusAdapter._milvus_collection.name != MILVUS_COLLECTION_NAME:
            self.log.info("Ensuring Milvus collection and indexes...")
            try:
                if not utility.has_collection(MILVUS_COLLECTION_NAME, using=self.alias):
                    self.log.warning(f"Milvus collection '{MILVUS_COLLECTION_NAME}' not found. Creating.")
                    MilvusAdapter._milvus_collection = self._create_milvus_collection()
                else:
                    self.log.debug(f"Using existing Milvus collection '{MILVUS_COLLECTION_NAME}'.")
                    MilvusAdapter._milvus_collection = Collection(name=MILVUS_COLLECTION_NAME, using=self.alias)
                    self._check_and_create_indexes(MilvusAdapter._milvus_collection)
                
                self.log.info("Loading Milvus collection into memory for indexing...", collection_name=MilvusAdapter._milvus_collection.name)
                MilvusAdapter._milvus_collection.load()
                self.log.info("Milvus collection loaded into memory for indexing.")
            except MilvusException as e:
                self.log.error("Failed during Milvus collection access/load", error=str(e), exc_info=True)
                MilvusAdapter._milvus_collection = None
                raise RuntimeError(f"Milvus collection access error: {e}") from e
        if not isinstance(MilvusAdapter._milvus_collection, Collection):
            self.log.critical("Milvus collection object is unexpectedly None or invalid after ensure_collection_and_indexes.")
            raise RuntimeError("Failed to obtain a valid Milvus collection object.")


    def index_chunks(
        self,
        chunks_with_embeddings: List[Dict[str, Any]],
        filename: str,
        company_id: str,
        document_id: str,
        delete_existing: bool
    ) -> Tuple[int, List[str], List[Dict[str, Any]]]:
        
        index_log = self.log.bind(company_id=company_id, document_id=document_id, filename=filename)
        index_log.info("Starting Milvus indexing and PG data preparation", num_input_chunks=len(chunks_with_embeddings))

        if not chunks_with_embeddings:
            index_log.warning("No chunks_with_embeddings to process for Milvus/PG indexing.")
            return 0, [], []

        milvus_pk_ids: List[str] = []
        milvus_embeddings: List[List[float]] = []
        milvus_contents: List[str] = []
        milvus_company_ids: List[str] = []
        milvus_document_ids: List[str] = []
        milvus_filenames: List[str] = []
        milvus_pages: List[int] = []
        milvus_titles: List[str] = []
        milvus_tokens_counts: List[int] = []
        milvus_content_hashes: List[str] = []
        
        chunks_for_pg: List[Dict[str, Any]] = []

        for i, item in enumerate(chunks_with_embeddings):
            chunk_text = item.get('text', '')
            chunk_embedding = item.get('embedding')
            source_metadata = item.get('source_metadata', {}) # From docproc

            if not chunk_text or not chunk_text.strip() or not chunk_embedding:
                index_log.warning("Skipping chunk due to empty text or missing embedding.", original_chunk_index=i, has_text=bool(chunk_text), has_embedding=bool(chunk_embedding))
                continue
            
            tokens = len(tiktoken_enc.encode(chunk_text)) if tiktoken_enc else -1
            content_hash = hashlib.sha256(chunk_text.encode('utf-8', errors='ignore')).hexdigest()
            page_number = source_metadata.get('page_number', source_metadata.get('page', -1)) # Accommodate different metadata keys
            
            milvus_pk_id = f"{document_id}_{i}" # Use original index from docproc as part of PK
            title_for_chunk = f"{filename[:30]}... (Page {page_number if page_number != -1 else 'N/A'}, Chunk {i + 1})"

            milvus_pk_ids.append(milvus_pk_id)
            milvus_embeddings.append(chunk_embedding)
            milvus_contents.append(chunk_text[:settings.MILVUS_CONTENT_FIELD_MAX_LENGTH]) # Truncate
            milvus_company_ids.append(company_id)
            milvus_document_ids.append(document_id)
            milvus_filenames.append(filename)
            milvus_pages.append(page_number if page_number is not None else -1)
            milvus_titles.append(title_for_chunk[:512]) # Truncate
            milvus_tokens_counts.append(tokens)
            milvus_content_hashes.append(content_hash)

            # Prepare data for PostgreSQL, linking with Milvus PK
            pg_metadata_obj = ChunkMetadata(page=page_number, title=title_for_chunk, tokens=tokens, content_hash=content_hash)
            chunks_for_pg.append({
                "id": uuid.uuid4(), # Generate UUID for PG table pk
                "document_id": uuid.UUID(document_id),
                "company_id": uuid.UUID(company_id),
                "chunk_index": i, # Use original index from docproc
                "content": chunk_text, # Store full content in PG
                "metadata": pg_metadata_obj.model_dump(mode='json'),
                "embedding_id": milvus_pk_id, # This is the Milvus PK
                "vector_status": "pending" # Will be updated to 'created' if Milvus insert is successful
            })

        if not milvus_pk_ids:
            index_log.warning("No valid chunks remained after preparation for Milvus.")
            return 0, [], []

        if delete_existing:
            index_log.info("Attempting to delete existing Milvus chunks before insertion...")
            try:
                deleted_count = self.delete_document_chunks(company_id, document_id)
                index_log.info(f"Deleted {deleted_count} existing Milvus chunks.")
            except Exception as del_err:
                index_log.error("Failed to delete existing Milvus chunks, proceeding with insert.", error=str(del_err))

        data_to_insert_milvus = [
            milvus_pk_ids, milvus_embeddings, milvus_contents, milvus_company_ids,
            milvus_document_ids, milvus_filenames, milvus_pages, milvus_titles,
            milvus_tokens_counts, milvus_content_hashes
        ]
        index_log.debug(f"Inserting {len(milvus_pk_ids)} chunks into Milvus collection...")
        
        try:
            if MilvusAdapter._milvus_collection is None:
                 self.ensure_collection_and_indexes() # Should re-init _milvus_collection
                 if MilvusAdapter._milvus_collection is None: # Check again
                    raise RuntimeError("Milvus collection could not be initialized.")

            mutation_result = MilvusAdapter._milvus_collection.insert(data_to_insert_milvus)
            inserted_milvus_count = mutation_result.insert_count
            returned_milvus_pks = [str(pk) for pk in mutation_result.primary_keys]

            if inserted_milvus_count != len(milvus_pk_ids):
                index_log.error("Milvus insert count mismatch!", expected=len(milvus_pk_ids), inserted=inserted_milvus_count, errors=mutation_result.err_indices)
                # Partial failure, update status for successfully inserted chunks in PG list
                # This is complex, for now, we assume all or nothing for simplicity or log the error
                # Mark all as 'error' if mismatch is significant or pks are not reliable
                for chunk_pg_data in chunks_for_pg: chunk_pg_data["vector_status"] = "error"

            else:
                index_log.info(f"Successfully inserted {inserted_milvus_count} chunks into Milvus.")
                # Update vector_status for PG data
                for chunk_pg_data in chunks_for_pg:
                    # Ensure embedding_id (Milvus PK) matches one of the returned PKs.
                    # This check is crucial if order is not guaranteed or if there were partial inserts.
                    # For now, assuming order is preserved and all inserts were successful if counts match.
                    if chunk_pg_data["embedding_id"] in returned_milvus_pks:
                         chunk_pg_data["vector_status"] = "created"
                    else: # Should not happen if counts match and PKs are reliable
                         index_log.warning("Milvus PK mismatch for PG data. Chunk might not be in Milvus.", embedding_id=chunk_pg_data["embedding_id"])
                         chunk_pg_data["vector_status"] = "error"


            index_log.debug("Flushing Milvus collection...")
            MilvusAdapter._milvus_collection.flush()
            index_log.info("Milvus collection flushed.")

            return inserted_milvus_count, returned_milvus_pks, chunks_for_pg

        except MilvusException as e:
            index_log.error("Failed to insert data into Milvus", error=str(e), exc_info=True)
            for chunk_pg_data in chunks_for_pg: chunk_pg_data["vector_status"] = "error"
            # Do not re-raise immediately, allow PG to be updated with error status potentially
            return 0, [], chunks_for_pg # Indicate 0 Milvus inserts, but return PG data for error state
        except Exception as e:
            index_log.exception("Unexpected error during Milvus insertion")
            for chunk_pg_data in chunks_for_pg: chunk_pg_data["vector_status"] = "error"
            return 0, [], chunks_for_pg


    def delete_document_chunks(self, company_id: str, document_id: str) -> int:
        del_log = self.log.bind(company_id=company_id, document_id=document_id)
        expr = f'{MILVUS_COMPANY_ID_FIELD} == "{company_id}" and {MILVUS_DOCUMENT_ID_FIELD} == "{document_id}"'
        
        try:
            if MilvusAdapter._milvus_collection is None: self.ensure_collection_and_indexes()
            del_log.info("Attempting to delete chunks from Milvus.", filter_expr=expr)
            # Query for PKs first to get an accurate count of what *should* be deleted
            query_res = MilvusAdapter._milvus_collection.query(expr=expr, output_fields=[MILVUS_PK_FIELD], consistency_level="Strong")
            pks_to_delete = [item[MILVUS_PK_FIELD] for item in query_res if MILVUS_PK_FIELD in item]

            if not pks_to_delete:
                del_log.info("No matching primary keys found in Milvus for deletion.")
                return 0
            
            delete_expr = f'{MILVUS_PK_FIELD} in {json.dumps(pks_to_delete)}'
            delete_result = MilvusAdapter._milvus_collection.delete(expr=delete_expr)
            MilvusAdapter._milvus_collection.flush()
            del_log.info("Milvus delete operation executed and flushed.", delete_count=delete_result.delete_count, expected_to_delete=len(pks_to_delete))
            return delete_result.delete_count
        except MilvusException as e:
            del_log.error("Milvus delete error", error=str(e), exc_info=True)
            return 0 # Indicate failure or 0 deleted
        except Exception as e:
            del_log.exception("Unexpected error during Milvus chunk deletion")
            return 0

    def _get_chunk_count_internal(self, company_id: str, document_id: str) -> int:
        count_log = self.log.bind(company_id=company_id, document_id=document_id)
        try:
            if MilvusAdapter._milvus_collection is None: self.ensure_collection_and_indexes()
            expr = f'{MILVUS_COMPANY_ID_FIELD} == "{company_id}" and {MILVUS_DOCUMENT_ID_FIELD} == "{document_id}"'
            count_log.debug("Querying Milvus chunk count", filter_expr=expr)
            
            # Use query with output_fields=[MILVUS_PK_FIELD] for more robust counting
            # The result of collection.num_entities might not be immediately consistent without a flush
            # or if consistency level isn't strong enough for the use case.
            query_res = MilvusAdapter._milvus_collection.query(expr=expr, output_fields=[MILVUS_PK_FIELD], consistency_level="Strong")
            count = len(query_res)
            count_log.info("Milvus chunk count successful", count=count)
            return count
        except MilvusException as e:
            count_log.error("Milvus query error during count", error=str(e), exc_info=True)
            return -1 # Indicate error
        except Exception as e:
            count_log.exception("Unexpected error during Milvus count", error=str(e))
            return -1

    def count_document_chunks(self, company_id: str, document_id: str) -> int:
        # This is the synchronous version
        return self._get_chunk_count_internal(company_id, document_id)

    async def count_document_chunks_async(self, company_id: str, document_id: str) -> int:
        # For async, run the sync method in an executor
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._get_chunk_count_internal, company_id, document_id)
```

## File: `app\main.py`
```py
# ingest-service/app/main.py
from fastapi import FastAPI, HTTPException, status as fastapi_status, Request
from fastapi.exceptions import RequestValidationError # ResponseValidationError removed as we catch DomainException now
from fastapi.responses import JSONResponse, PlainTextResponse
import structlog
import uvicorn
import logging
import sys
import asyncio
import time
import uuid
from contextlib import asynccontextmanager

from app.core.logging_config import setup_logging
setup_logging() # Initialize logging first

from app.core.config import settings
log = structlog.get_logger("ingest_service.main")
# Corrected import to use the new ingest_endpoint
from app.api.v1.endpoints import ingest_endpoint # LLM_CORRECTION: Use ingest_endpoint
from app.infrastructure.persistence import postgres_connector
from app.infrastructure.vectorstore.milvus_adapter import MilvusAdapter # For Milvus init
from app.domain.exceptions import DomainException # Import base domain exception

SERVICE_READY = False
DB_CONNECTION_OK = False
MILVUS_CONNECTION_OK = False

@asynccontextmanager
async def lifespan(app: FastAPI):
    global SERVICE_READY, DB_CONNECTION_OK, MILVUS_CONNECTION_OK
    log.info("Executing Ingest Service startup sequence...")
    
    # Initialize PostgreSQL
    try:
        pool = await postgres_connector.get_db_pool()
        async with pool.acquire() as conn:
            await conn.execute('SELECT 1')
        log.info("PostgreSQL connection pool initialized and verified successfully.")
        DB_CONNECTION_OK = True
    except Exception as db_exc:
        log.critical("PostgreSQL connection FAILED during startup.", error=str(db_exc), exc_info=True)
        DB_CONNECTION_OK = False

    # Initialize Milvus
    try:
        # Instantiate adapter to trigger its connection and collection setup
        milvus_adapter = MilvusAdapter() 
        # A simple check could be trying to get collection info or count
        # For now, successful instantiation of adapter implies connection was attempted
        # and collection/indexes were ensured.
        # A more robust check might be needed if ensure_collection_and_indexes can fail silently
        # or if a specific health check method is added to VectorStorePort/MilvusAdapter.
        if milvus_adapter._milvus_collection is not None : # Accessing internal for check, ideally use a port method
            log.info("Milvus connection and collection initialized successfully via MilvusAdapter.")
            MILVUS_CONNECTION_OK = True
        else:
            log.critical("Milvus initialization via MilvusAdapter FAILED (collection is None).")
            MILVUS_CONNECTION_OK = False
    except Exception as milvus_exc:
        log.critical("Milvus connection/collection FAILED during startup.", error=str(milvus_exc), exc_info=True)
        MILVUS_CONNECTION_OK = False

    SERVICE_READY = DB_CONNECTION_OK and MILVUS_CONNECTION_OK

    if SERVICE_READY:
        log.info("Ingest Service startup successful. SERVICE IS READY.")
    else:
        log.error("Ingest Service startup FAILED. SERVICE IS NOT READY.", db_ok=DB_CONNECTION_OK, milvus_ok=MILVUS_CONNECTION_OK)

    yield 

    log.info("Executing Ingest Service shutdown sequence...")
    await postgres_connector.close_db_pool()
    # Add Milvus disconnect if necessary, though Pymilvus might handle this on process exit
    # connections.disconnect(alias_name_used_in_adapter) # If MilvusAdapter uses a specific alias
    log.info("Shutdown sequence complete.")


app = FastAPI(
    title=settings.PROJECT_NAME,
    openapi_url=f"{settings.API_V1_STR}/openapi.json",
    version="1.0.0", # Updated version
    description="Atenex Ingest Service with Hexagonal Architecture.",
    lifespan=lifespan
)

@app.middleware("http")
async def add_request_context_timing_logging(request: Request, call_next):
    start_time = time.perf_counter()
    request_id = request.headers.get("x-request-id", str(uuid.uuid4()))
    structlog.contextvars.bind_contextvars(request_id=request_id)
    req_log = log.bind(method=request.method, path=request.url.path)
    req_log.info("Request received")
    request.state.request_id = request_id

    response = None
    try:
        response = await call_next(request)
        process_time_ms = (time.perf_counter() - start_time) * 1000
        resp_log = req_log.bind(status_code=response.status_code, duration_ms=round(process_time_ms, 2))
        log_level_method = "warning" if 400 <= response.status_code < 500 else "error" if response.status_code >= 500 else "info"
        getattr(resp_log, log_level_method)("Request finished")
        response.headers["X-Request-ID"] = request_id
        response.headers["X-Process-Time-Ms"] = f"{process_time_ms:.2f}"
    except Exception as e:
        process_time_ms = (time.perf_counter() - start_time) * 1000
        exc_log = req_log.bind(status_code=500, duration_ms=round(process_time_ms, 2))
        
        if isinstance(e, DomainException): # Handle our custom domain exceptions
            exc_log.warning("Domain exception occurred.", detail=str(e), status_code=e.status_code)
            response = JSONResponse(
                status_code=e.status_code,
                content={"detail": str(e)}
            )
        elif isinstance(e, HTTPException): # Handle FastAPI's HTTPExceptions
            exc_log.warning("HTTPException occurred.", detail=e.detail, status_code=e.status_code)
            response = JSONResponse(
                status_code=e.status_code,
                content={"detail": e.detail},
                headers=getattr(e, "headers", None)
            )
        else: # Handle other unexpected exceptions
            exc_log.exception("Unhandled exception during request processing")
            response = JSONResponse(
                status_code=fastapi_status.HTTP_500_INTERNAL_SERVER_ERROR,
                content={"detail": "Internal Server Error"}
            )
        response.headers["X-Request-ID"] = request_id
        response.headers["X-Process-Time-Ms"] = f"{process_time_ms:.2f}"
    finally:
         structlog.contextvars.clear_contextvars()
    return response


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    log.warning("Request Validation Error", errors=exc.errors(), path=request.url.path)
    return JSONResponse(
        status_code=fastapi_status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={"detail": "Error de validación en la petición", "errors": exc.errors()},
    )

# Removed specific HTTPException and general Exception handlers as they are now covered by the middleware.

app.include_router(ingest_endpoint.router, prefix=settings.API_V1_STR, tags=["Ingestion V2 (Hexagonal)"])
log.info(f"Included NEW hexagonal ingestion router with prefix: {settings.API_V1_STR}")

@app.get("/health", tags=["Health Check"], summary="Service Health Check")
async def health_check_endpoint(request: Request):
    if SERVICE_READY:
        return PlainTextResponse("OK", status_code=fastapi_status.HTTP_200_OK)
    else:
        log.error("Health check failed: Service not ready.", db_ok=DB_CONNECTION_OK, milvus_ok=MILVUS_CONNECTION_OK)
        raise HTTPException(
            status_code=fastapi_status.HTTP_503_SERVICE_UNAVAILABLE, 
            detail=f"Service not ready (DB: {'OK' if DB_CONNECTION_OK else 'FAIL'}, Milvus: {'OK' if MILVUS_CONNECTION_OK else 'FAIL'})"
        )

@app.get("/", tags=["Root"], include_in_schema=False)
async def root_endpoint():
    return {"message": f"{settings.PROJECT_NAME} is running."}

if __name__ == "__main__":
    port = int(os.getenv("PORT", 8001)) # Use PORT env var if available
    log_level_str = settings.LOG_LEVEL.lower()
    print(f"----- Starting {settings.PROJECT_NAME} (Hexagonal) locally on port {port} -----")
    uvicorn.run("app.main:app", host="0.0.0.0", port=port, reload=True, log_level=log_level_str)
```

## File: `app\models\domain.py`
```py

```

## File: `app\services\__init__.py`
```py

```

## File: `app\services\base_client.py`
```py
import httpx
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type, before_sleep_log
import structlog
from typing import Any, Dict, Optional
import logging

from app.core.config import settings

log = structlog.get_logger(__name__)

class BaseServiceClient:
    def __init__(self, base_url: str, service_name: str, client_timeout: Optional[int] = None):
        self.base_url = base_url.rstrip('/')
        self.service_name = service_name
        self.timeout = client_timeout or settings.HTTP_CLIENT_TIMEOUT
        self._async_client: Optional[httpx.AsyncClient] = None
        self._sync_client: Optional[httpx.Client] = None
        self.log = log.bind(service_name=self.service_name, base_url=self.base_url)

    @property
    def async_client(self) -> httpx.AsyncClient:
        if self._async_client is None or self._async_client.is_closed:
            self.log.debug(f"Initializing httpx.AsyncClient for {self.service_name}")
            self._async_client = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=self.timeout
            )
        return self._async_client

    @property
    def sync_client(self) -> httpx.Client:
        if self._sync_client is None: # httpx.Client doesn't have an is_closed property
            self.log.debug(f"Initializing httpx.Client for {self.service_name}")
            self._sync_client = httpx.Client(
                base_url=self.base_url,
                timeout=self.timeout
            )
        return self._sync_client

    async def close_async(self):
        if self._async_client and not self._async_client.is_closed:
            self.log.info(f"Closing async client for {self.service_name}")
            await self._async_client.aclose()
            self._async_client = None

    def close_sync(self):
        if self._sync_client:
            self.log.info(f"Closing sync client for {self.service_name}")
            self._sync_client.close()
            self._sync_client = None
    
    async def close(self): # Generic close for lifespan manager if needed
        await self.close_async()
        self.close_sync()


    @retry(
        stop=stop_after_attempt(settings.HTTP_CLIENT_MAX_RETRIES),
        wait=wait_exponential(multiplier=settings.HTTP_CLIENT_BACKOFF_FACTOR, min=1, max=10),
        retry=retry_if_exception_type(httpx.RequestError),
        before_sleep=before_sleep_log(log, logging.WARNING),
        reraise=True
    )
    async def _request_async(
        self,
        method: str,
        endpoint: str,
        params: Optional[Dict[str, Any]] = None,
        json_payload: Optional[Dict[str, Any]] = None, # Renamed from json to avoid conflict
        data: Optional[Dict[str, Any]] = None,
        files: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> httpx.Response:
        request_log = self.log.bind(method=method, endpoint=endpoint, type="async")
        request_log.debug("Requesting service", params=params)
        try:
            response = await self.async_client.request(
                method=method,
                url=endpoint,
                params=params,
                json=json_payload,
                data=data,
                files=files,
                headers=headers,
            )
            response.raise_for_status()
            request_log.info("Received response from service", status_code=response.status_code)
            return response
        except httpx.HTTPStatusError as e:
            request_log.error("HTTP error from service", status_code=e.response.status_code, detail=e.response.text, exc_info=True)
            raise
        except (httpx.TimeoutException, httpx.NetworkError) as e:
            request_log.error("Network error when calling service", error=str(e), exc_info=True)
            raise
        except Exception as e:
            request_log.error("Unexpected error when calling service", error=str(e), exc_info=True)
            raise

    @retry(
        stop=stop_after_attempt(settings.HTTP_CLIENT_MAX_RETRIES),
        wait=wait_exponential(multiplier=settings.HTTP_CLIENT_BACKOFF_FACTOR, min=1, max=10),
        retry=retry_if_exception_type(httpx.RequestError),
        before_sleep=before_sleep_log(log, logging.WARNING),
        reraise=True
    )
    def _request_sync(
        self,
        method: str,
        endpoint: str,
        params: Optional[Dict[str, Any]] = None,
        json_payload: Optional[Dict[str, Any]] = None, # Renamed
        data: Optional[Dict[str, Any]] = None,
        files: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> httpx.Response:
        request_log = self.log.bind(method=method, endpoint=endpoint, type="sync")
        request_log.debug("Requesting service", params=params)
        try:
            response = self.sync_client.request(
                method=method,
                url=endpoint,
                params=params,
                json=json_payload,
                data=data,
                files=files,
                headers=headers,
            )
            response.raise_for_status()
            request_log.info("Received response from service", status_code=response.status_code)
            return response
        except httpx.HTTPStatusError as e:
            request_log.error("HTTP error from service", status_code=e.response.status_code, detail=e.response.text, exc_info=True)
            raise
        except (httpx.TimeoutException, httpx.NetworkError) as e:
            request_log.error("Network error when calling service", error=str(e), exc_info=True)
            raise
        except Exception as e:
            request_log.error("Unexpected error when calling service", error=str(e), exc_info=True)
            raise
```

## File: `app\services\clients\docproc_service_client.py`
```py
# ingest-service/app/services/clients/docproc_service_client.py
from typing import List, Dict, Any, Tuple, Optional
import httpx
import structlog

from app.core.config import settings
from app.services.base_client import BaseServiceClient 
from app.domain.exceptions import ServiceDependencyException

log = structlog.get_logger(__name__)


class DocProcServiceClient(BaseServiceClient):
    def __init__(self, base_url: Optional[str] = None):
        effective_base_url = base_url or str(settings.DOCPROC_SERVICE_URL).rstrip('/')
        parsed_url = httpx.URL(effective_base_url)
        self.service_endpoint_path = parsed_url.path or "/"
        client_base_url = f"{parsed_url.scheme}://{parsed_url.host}"
        if parsed_url.port:
            client_base_url += f":{parsed_url.port}"
        
        super().__init__(base_url=client_base_url, service_name="DocProcService")
        self.log = log.bind(service_client="DocProcServiceClient", base_url=self.base_url, endpoint_path=self.service_endpoint_path)

    async def process_document_async(
        self,
        file_bytes: bytes,
        original_filename: str,
        content_type: str,
        document_id: Optional[str] = None,
        company_id: Optional[str] = None
    ) -> Dict[str, Any]:
        
        if not file_bytes:
            self.log.error("process_document_async called with empty file_bytes.")
            raise ServiceDependencyException(service_name=self.service_name, original_error="File content cannot be empty.", message="DocProc submission error")

        files = {'file': (original_filename, file_bytes, content_type)}
        data: Dict[str, Any] = {'original_filename': original_filename, 'content_type': content_type}
        if document_id: data['document_id'] = document_id
        if company_id: data['company_id'] = company_id
        
        self.log.debug("Requesting document processing (async)", filename=original_filename, content_type=content_type, size_len=len(file_bytes))
        
        try:
            response = await self._request_async(method="POST", endpoint=self.service_endpoint_path, files=files, data=data)
            response_data = response.json()
            if "data" not in response_data or "chunks" not in response_data.get("data", {}):
                self.log.error("Invalid response format from DocProc Service (async)", response_data_preview=str(response_data)[:200])
                raise ServiceDependencyException(service_name=self.service_name, message="Invalid response format from DocProc Service.", original_error=str(response_data)[:200])
            self.log.info("Successfully processed document via DocProc Service (async).", filename=original_filename, num_chunks=len(response_data["data"]["chunks"]))
            return response_data
        except httpx.HTTPStatusError as e:
            err_msg = f"DocProc Service HTTP error: {e.response.status_code}"
            self.log.error(err_msg, response_text=e.response.text, filename=original_filename)
            raise ServiceDependencyException(service_name=self.service_name, message=err_msg, original_error=e.response.text) from e
        except Exception as e:
            self.log.exception("Unexpected error in DocProcServiceClient (async)", filename=original_filename)
            raise ServiceDependencyException(service_name=self.service_name, message=f"Unexpected error: {type(e).__name__}", original_error=str(e)) from e

    def process_document_sync(
        self,
        file_bytes: bytes,
        original_filename: str,
        content_type: str,
        document_id: Optional[str] = None,
        company_id: Optional[str] = None
    ) -> Dict[str, Any]:

        if not file_bytes:
            self.log.error("process_document_sync called with empty file_bytes.")
            raise ServiceDependencyException(service_name=self.service_name, original_error="File content cannot be empty.", message="DocProc submission error")

        files = {'file': (original_filename, file_bytes, content_type)}
        data: Dict[str, Any] = {'original_filename': original_filename, 'content_type': content_type}
        if document_id: data['document_id'] = document_id
        if company_id: data['company_id'] = company_id
        
        self.log.debug("Requesting document processing (sync)", filename=original_filename, content_type=content_type, size_len=len(file_bytes))
        
        try:
            response = self._request_sync(method="POST", endpoint=self.service_endpoint_path, files=files, data=data)
            response_data = response.json()
            if "data" not in response_data or "chunks" not in response_data.get("data", {}):
                self.log.error("Invalid response format from DocProc Service (sync)", response_data_preview=str(response_data)[:200])
                raise ServiceDependencyException(service_name=self.service_name, message="Invalid response format from DocProc Service.", original_error=str(response_data)[:200])
            self.log.info("Successfully processed document via DocProc Service (sync).", filename=original_filename, num_chunks=len(response_data["data"]["chunks"]))
            return response_data
        except httpx.HTTPStatusError as e:
            err_msg = f"DocProc Service HTTP error: {e.response.status_code}"
            self.log.error(err_msg, response_text=e.response.text, filename=original_filename)
            raise ServiceDependencyException(service_name=self.service_name, message=err_msg, original_error=e.response.text) from e
        except Exception as e:
            self.log.exception("Unexpected error in DocProcServiceClient (sync)", filename=original_filename)
            raise ServiceDependencyException(service_name=self.service_name, message=f"Unexpected error: {type(e).__name__}", original_error=str(e)) from e
```

## File: `app\services\clients\embedding_service_client.py`
```py
# ingest-service/app/services/clients/embedding_service_client.py
from typing import List, Dict, Any, Tuple, Optional
import httpx
import structlog

from app.core.config import settings
from app.services.base_client import BaseServiceClient
from app.domain.exceptions import ServiceDependencyException

log = structlog.get_logger(__name__)


class EmbeddingServiceClient(BaseServiceClient):
    def __init__(self, base_url: Optional[str] = None):
        effective_base_url = base_url or str(settings.EMBEDDING_SERVICE_URL).rstrip('/')
        parsed_url = httpx.URL(effective_base_url)
        self.service_endpoint_path = parsed_url.path or "/"
        client_base_url = f"{parsed_url.scheme}://{parsed_url.host}"
        if parsed_url.port:
            client_base_url += f":{parsed_url.port}"
            
        super().__init__(base_url=client_base_url, service_name="EmbeddingService")
        self.log = log.bind(service_client="EmbeddingServiceClient", base_url=self.base_url, endpoint_path=self.service_endpoint_path)

    async def get_embeddings_async(self, texts: List[str], text_type: str = "passage") -> Tuple[List[List[float]], Dict[str, Any]]:
        if not texts:
            self.log.warning("get_embeddings_async called with an empty list of texts.")
            return [], {"model_name": "unknown", "dimension": 0, "provider": "unknown"}

        request_payload = {"texts": texts, "text_type": text_type}
        self.log.debug(f"Requesting embeddings (async) for {len(texts)} texts", num_texts=len(texts), text_type=text_type)

        try:
            response = await self._request_async(method="POST", endpoint=self.service_endpoint_path, json_payload=request_payload)
            response_data = response.json()
            if "embeddings" not in response_data or "model_info" not in response_data:
                self.log.error("Invalid response format from Embedding Service (async)", response_data_preview=str(response_data)[:200])
                raise ServiceDependencyException(service_name=self.service_name, message="Invalid response format from Embedding Service.", original_error=str(response_data)[:200])
            
            embeddings = response_data["embeddings"]
            model_info = response_data["model_info"]
            self.log.info(f"Successfully retrieved {len(embeddings)} embeddings (async).", model_name=model_info.get("model_name"))
            return embeddings, model_info
        except httpx.HTTPStatusError as e:
            err_msg = f"Embedding Service HTTP error: {e.response.status_code}"
            self.log.error(err_msg, response_text=e.response.text)
            raise ServiceDependencyException(service_name=self.service_name, message=err_msg, original_error=e.response.text) from e
        except Exception as e:
            self.log.exception("Unexpected error in EmbeddingServiceClient (async)")
            raise ServiceDependencyException(service_name=self.service_name, message=f"Unexpected error: {type(e).__name__}", original_error=str(e)) from e

    def get_embeddings_sync(self, texts: List[str], text_type: str = "passage") -> Tuple[List[List[float]], Dict[str, Any]]:
        if not texts:
            self.log.warning("get_embeddings_sync called with an empty list of texts.")
            return [], {"model_name": "unknown", "dimension": 0, "provider": "unknown"}

        request_payload = {"texts": texts, "text_type": text_type}
        self.log.debug(f"Requesting embeddings (sync) for {len(texts)} texts", num_texts=len(texts), text_type=text_type)

        try:
            response = self._request_sync(method="POST", endpoint=self.service_endpoint_path, json_payload=request_payload)
            response_data = response.json()
            if "embeddings" not in response_data or "model_info" not in response_data:
                self.log.error("Invalid response format from Embedding Service (sync)", response_data_preview=str(response_data)[:200])
                raise ServiceDependencyException(service_name=self.service_name, message="Invalid response format from Embedding Service.", original_error=str(response_data)[:200])

            embeddings = response_data["embeddings"]
            model_info = response_data["model_info"]
            self.log.info(f"Successfully retrieved {len(embeddings)} embeddings (sync).", model_name=model_info.get("model_name"))
            return embeddings, model_info
        except httpx.HTTPStatusError as e:
            err_msg = f"Embedding Service HTTP error: {e.response.status_code}"
            self.log.error(err_msg, response_text=e.response.text)
            raise ServiceDependencyException(service_name=self.service_name, message=err_msg, original_error=e.response.text) from e
        except Exception as e:
            self.log.exception("Unexpected error in EmbeddingServiceClient (sync)")
            raise ServiceDependencyException(service_name=self.service_name, message=f"Unexpected error: {type(e).__name__}", original_error=str(e)) from e
```

## File: `app\services\gcs_client.py`
```py
import structlog
import asyncio
from typing import Optional
from google.cloud import storage
from google.api_core.exceptions import NotFound, GoogleAPIError
from app.core.config import settings

log = structlog.get_logger(__name__)

class GCSClientError(Exception):
    """Custom exception for GCS related errors."""
    def __init__(self, message: str, original_exception: Optional[Exception] = None):
        self.message = message
        self.original_exception = original_exception
        super().__init__(message)

    def __str__(self):
        if self.original_exception:
            return f"{self.message}: {type(self.original_exception).__name__} - {str(self.original_exception)}"
        return self.message

class GCSClient:
    """Client to interact with Google Cloud Storage using configured settings."""
    def __init__(self, bucket_name: Optional[str] = None):
        self.bucket_name = bucket_name or settings.GCS_BUCKET_NAME
        self._client = storage.Client()
        self._bucket = self._client.bucket(self.bucket_name)
        self.log = log.bind(gcs_bucket=self.bucket_name)

    async def upload_file_async(self, object_name: str, data: bytes, content_type: str) -> str:
        self.log.info("Uploading file to GCS...", object_name=object_name, content_type=content_type, length=len(data))
        loop = asyncio.get_running_loop()
        def _upload():
            blob = self._bucket.blob(object_name)
            blob.upload_from_string(data, content_type=content_type)
            return object_name
        try:
            uploaded_object_name = await loop.run_in_executor(None, _upload)
            self.log.info("File uploaded successfully to GCS", object_name=object_name)
            return uploaded_object_name
        except GoogleAPIError as e:
            self.log.error("GCS upload failed", error=str(e))
            raise GCSClientError(f"GCS error uploading {object_name}", e) from e
        except Exception as e:
            self.log.exception("Unexpected error during GCS upload", error=str(e))
            raise GCSClientError(f"Unexpected error uploading {object_name}", e) from e

    async def download_file_async(self, object_name: str, file_path: str):
        self.log.info("Downloading file from GCS...", object_name=object_name, target_path=file_path)
        loop = asyncio.get_running_loop()
        def _download():
            blob = self._bucket.blob(object_name)
            blob.download_to_filename(file_path)
        try:
            await loop.run_in_executor(None, _download)
            self.log.info("File downloaded successfully from GCS", object_name=object_name)
        except NotFound as e:
            self.log.error("Object not found in GCS", object_name=object_name)
            raise GCSClientError(f"Object not found in GCS: {object_name}", e) from e
        except GoogleAPIError as e:
            self.log.error("GCS download failed", error=str(e))
            raise GCSClientError(f"GCS error downloading {object_name}", e) from e
        except Exception as e:
            self.log.exception("Unexpected error during GCS download", error=str(e))
            raise GCSClientError(f"Unexpected error downloading {object_name}", e) from e

    async def check_file_exists_async(self, object_name: str) -> bool:
        self.log.debug("Checking file existence in GCS", object_name=object_name)
        loop = asyncio.get_running_loop()
        def _exists():
            blob = self._bucket.blob(object_name)
            return blob.exists()
        try:
            exists = await loop.run_in_executor(None, _exists)
            self.log.debug("File existence check completed in GCS", object_name=object_name, exists=exists)
            return exists
        except Exception as e:
            self.log.exception("Unexpected error during GCS existence check", error=str(e))
            return False

    async def delete_file_async(self, object_name: str):
        self.log.info("Deleting file from GCS...", object_name=object_name)
        loop = asyncio.get_running_loop()
        def _delete():
            blob = self._bucket.blob(object_name)
            blob.delete()
        try:
            await loop.run_in_executor(None, _delete)
            self.log.info("File deleted successfully from GCS", object_name=object_name)
        except NotFound:
            self.log.info("Object already deleted or not found in GCS", object_name=object_name)
        except GoogleAPIError as e:
            self.log.error("GCS delete failed", error=str(e))
            raise GCSClientError(f"GCS error deleting {object_name}", e) from e
        except Exception as e:
            self.log.exception("Unexpected error during GCS delete", error=str(e))
            raise GCSClientError(f"Unexpected error deleting {object_name}", e) from e

    # Synchronous methods for worker compatibility
    def download_file_sync(self, object_name: str, file_path: str):
        self.log.info("Downloading file from GCS (sync)...", object_name=object_name, target_path=file_path)
        try:
            blob = self._bucket.blob(object_name)
            blob.download_to_filename(file_path)
            self.log.info("File downloaded successfully from GCS (sync)", object_name=object_name)
        except NotFound as e:
            self.log.error("Object not found in GCS (sync)", object_name=object_name)
            raise GCSClientError(f"Object not found in GCS: {object_name}", e) from e
        except GoogleAPIError as e:
            self.log.error("GCS download failed (sync)", error=str(e))
            raise GCSClientError(f"GCS error downloading {object_name}", e) from e
        except Exception as e:
            self.log.exception("Unexpected error during GCS download (sync)", error=str(e))
            raise GCSClientError(f"Unexpected error downloading {object_name}", e) from e

    def check_file_exists_sync(self, object_name: str) -> bool:
        self.log.debug("Checking file existence in GCS (sync)", object_name=object_name)
        try:
            blob = self._bucket.blob(object_name)
            exists = blob.exists()
            self.log.debug("File existence check completed in GCS (sync)", object_name=object_name, exists=exists)
            return exists
        except Exception as e:
            self.log.exception("Unexpected error during GCS existence check (sync)", error=str(e))
            return False

    def delete_file_sync(self, object_name: str):
        self.log.info("Deleting file from GCS (sync)...", object_name=object_name)
        try:
            blob = self._bucket.blob(object_name)
            blob.delete()
            self.log.info("File deleted successfully from GCS (sync)", object_name=object_name)
        except NotFound:
            self.log.info("Object already deleted or not found in GCS (sync)", object_name=object_name)
        except GoogleAPIError as e:
            self.log.error("GCS delete failed (sync)", error=str(e))
            raise GCSClientError(f"GCS error deleting {object_name}", e) from e
        except Exception as e:
            self.log.exception("Unexpected error during GCS delete (sync)", error=str(e))
            raise GCSClientError(f"Unexpected error deleting {object_name}", e) from e

```

## File: `app\services\ingest_pipeline.py`
```py

```

## File: `app\tasks\process_document.py`
```py

```

## File: `pyproject.toml`
```toml
[tool.poetry]
name = "ingest-service"
version = "1.3.2"
description = "Ingest service for Atenex B2B SaaS (Postgres/GCS/Milvus/Remote Embedding & DocProc Services - CPU - Prefork)"
authors = ["Atenex Team <dev@atenex.com>"]
readme = "README.md"

[tool.poetry.dependencies]
python = ">=3.10,<3.13"
fastapi = "^0.110.0"
uvicorn = {extras = ["standard"], version = "^0.28.0"}
gunicorn = "^21.2.0"
pydantic = {extras = ["email"], version = "^2.6.4"}
pydantic-settings = "^2.2.1"
celery = {extras = ["redis"], version = "^5.3.6"}
asyncpg = "^0.29.0"
tenacity = "^8.2.3"
python-multipart = "^0.0.9"
structlog = "^24.1.0"

google-cloud-storage = "^2.16.0"

# --- Core Processing Dependencies (v0.3.2) ---
pymilvus = "==2.5.3"
tiktoken = "^0.7.0"


# --- HTTP Client (API & Service Client - Keep) ---
httpx = {extras = ["http2"], version = "^0.27.0"}
h2 = "^4.1.0"

# --- Synchronous DB Dependencies (Worker - Keep) ---
sqlalchemy = "^2.0.28"
psycopg2-binary = "^2.9.9"


[tool.poetry.group.dev.dependencies]
pytest = "^7.4.4"
pytest-asyncio = "^0.21.1"

[build-system]
requires = ["poetry-core>=1.0.0"]
build-backend = "poetry.core.masonry.api"
```
