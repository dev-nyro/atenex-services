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