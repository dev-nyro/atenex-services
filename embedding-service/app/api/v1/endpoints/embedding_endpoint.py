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