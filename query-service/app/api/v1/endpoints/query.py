# ./app/api/v1/endpoints/query.py
import uuid
from typing import Dict, Any, Optional
import structlog
import asyncio

from fastapi import APIRouter, Depends, HTTPException, status, Header, Body

from app.api.v1 import schemas
from app.core.config import settings
from app.db import postgres_client # Para logging
from app.pipelines import rag_pipeline # Importar funciones del pipeline
from haystack import Document # Para type hints

log = structlog.get_logger(__name__)

router = APIRouter()

# --- Dependency for Company ID ---
# (Reutilizado de ingest-service, adaptado si es necesario)
async def get_current_company_id(x_company_id: Optional[str] = Header(None)) -> uuid.UUID:
    """Obtiene y valida el X-Company-ID del header."""
    if not x_company_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, # O 400 Bad Request si se considera error de cliente
            detail="Missing X-Company-ID header",
        )
    try:
        company_uuid = uuid.UUID(x_company_id)
        # Aquí podrías añadir una validación extra contra la tabla COMPANIES si fuera necesario
        # por ahora, solo validamos el formato UUID
        return company_uuid
    except ValueError:
         raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid X-Company-ID header format (must be UUID)",
        )

# --- Placeholder Dependency for User ID ---
# En una implementación real, esto extraería el user_id del token JWT
# validado por el API Gateway o un middleware de autenticación.
async def get_current_user_id(authorization: Optional[str] = Header(None)) -> Optional[uuid.UUID]:
    """Placeholder para obtener el User ID (ej: de un token JWT)."""
    if authorization and authorization.startswith("Bearer "):
        token = authorization.split(" ")[1]
        # Aquí iría la lógica para decodificar el token y extraer el user_id
        # Ejemplo hardcodeado (REEMPLAZAR CON LÓGICA REAL):
        try:
            # Simular extracción de un UUID del token
            # En un caso real, usarías python-jose u otra librería
            payload = {"user_id": "f47ac10b-58cc-4372-a567-0e02b2c3d479"} # Ejemplo
            user_id_str = payload.get("user_id")
            if user_id_str:
                return uuid.UUID(user_id_str)
        except Exception as e:
            log.warning("Failed to simulate JWT decoding", error=str(e))
            return None # O lanzar HTTPException si el token es inválido/requerido
    return None # Retorna None si no hay token o no se puede extraer


# --- Endpoint ---
@router.post(
    "/query",
    response_model=schemas.QueryResponse,
    status_code=status.HTTP_200_OK,
    summary="Process a user query using RAG pipeline",
    description="Receives a query, retrieves relevant documents for the company, generates an answer using an LLM, logs the interaction, and returns the result.",
)
async def process_query(
    request_body: schemas.QueryRequest = Body(...),
    company_id: uuid.UUID = Depends(get_current_company_id),
    user_id: Optional[uuid.UUID] = Depends(get_current_user_id), # Obtener user_id (puede ser None)
):
    """
    Endpoint principal para procesar consultas de usuario.
    """
    endpoint_log = log.bind(
        company_id=str(company_id),
        user_id=str(user_id) if user_id else "anonymous",
        query=request_body.query[:100] + "..." if len(request_body.query) > 100 else request_body.query
    )
    endpoint_log.info("Received query request")

    try:
        # Ejecutar el pipeline RAG
        answer, retrieved_docs, log_id = await rag_pipeline.run_rag_pipeline(
            query=request_body.query,
            company_id=str(company_id), # Pasar como string
            user_id=str(user_id) if user_id else None, # Pasar como string o None
            top_k=request_body.retriever_top_k # Pasar el top_k del request si existe
        )

        # Formatear documentos recuperados para la respuesta
        response_docs = []
        for doc in retrieved_docs:
            # Extraer metadatos relevantes si existen
            doc_meta = doc.meta or {}
            original_doc_id = doc_meta.get("document_id")
            file_name = doc_meta.get("file_name")

            response_docs.append(schemas.RetrievedDocument(
                id=doc.id, # ID del chunk de Milvus
                score=doc.score,
                # Generar un preview corto del contenido
                content_preview=(doc.content[:150] + '...') if doc.content and len(doc.content) > 150 else doc.content,
                metadata=doc_meta, # Incluir todos los metadatos recuperados
                document_id=original_doc_id,
                file_name=file_name
            ))

        endpoint_log.info("Query processed successfully", log_id=str(log_id) if log_id else "Log Failed", num_retrieved=len(response_docs))

        return schemas.QueryResponse(
            answer=answer,
            retrieved_documents=response_docs,
            query_log_id=log_id # Será None si el logging falló
        )

    except ValueError as ve:
        endpoint_log.warning("Value error during query processing", error=str(ve))
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(ve))
    except ConnectionError as ce: # Podría venir de Milvus o DB si fallan las conexiones
         endpoint_log.error("Connection error during query processing", error=str(ce), exc_info=True)
         raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=f"Service dependency unavailable: {ce}")
    except Exception as e:
        endpoint_log.exception("Unhandled exception during query processing")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"An internal error occurred: {type(e).__name__}"
        )