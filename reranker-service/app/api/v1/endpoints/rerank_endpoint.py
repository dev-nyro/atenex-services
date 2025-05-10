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