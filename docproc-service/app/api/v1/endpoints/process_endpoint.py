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