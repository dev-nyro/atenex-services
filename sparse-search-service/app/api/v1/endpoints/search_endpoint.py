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