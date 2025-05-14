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