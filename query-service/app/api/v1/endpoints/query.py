# ./app/api/v1/endpoints/query.py
import uuid
from typing import Dict, Any, Optional
import structlog
import asyncio

# --- Añadir imports necesarios ---
from jose import jwt, JWTError
from jose.exceptions import ExpiredSignatureError, JWTClaimsError, JWSError

from fastapi import APIRouter, Depends, HTTPException, status, Header, Body, Request # Request puede ser útil si se usa state

from app.api.v1 import schemas
from app.core.config import settings
from app.db import postgres_client # Para logging
from app.pipelines import rag_pipeline # Importar funciones del pipeline
from haystack import Document # Para type hints

log = structlog.get_logger(__name__)

router = APIRouter()

# --- Dependency for Company ID ---
# (Sin cambios)
async def get_current_company_id(x_company_id: Optional[str] = Header(None)) -> uuid.UUID:
    """Obtiene y valida el X-Company-ID del header."""
    if not x_company_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing X-Company-ID header",
        )
    try:
        company_uuid = uuid.UUID(x_company_id)
        return company_uuid
    except ValueError:
         raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid X-Company-ID header format (must be UUID)",
        )

# --- Dependency for User ID (IMPLEMENTACIÓN MVP) ---
async def get_current_user_id(
    # request: Request, # Descomentar si el Gateway inyecta user en request.state
    authorization: Optional[str] = Header(None)
) -> Optional[uuid.UUID]:
    """
    Extrae el User ID (sub o user_id) del token JWT en el header Authorization.

    MVP Assumption: El API Gateway ya ha validado la firma y expiración del token.
    Esta función solo decodifica para leer el payload sin re-validar firma.
    Si la validación completa fuera necesaria aquí, se necesitaría la clave pública/secreto.
    """
    # --- Opción 1: Leer desde request.state (si el Gateway lo inyecta) ---
    # user_payload = getattr(request.state, "user", None)
    # if user_payload and isinstance(user_payload, dict):
    #     user_id_str = user_payload.get("sub") or user_payload.get("user_id")
    #     if user_id_str:
    #         try:
    #             return uuid.UUID(user_id_str)
    #         except ValueError:
    #             log.warning("User ID claim from request.state is not a valid UUID", claim_value=user_id_str)
    #             # Podrías lanzar un error aquí si el user_id es mandatorio y malformado
    #             return None # O manejar como anónimo/error
    #     else:
    #         log.warning("request.state.user found but missing 'sub' or 'user_id' claim")
    #         # Continuar para intentar leer desde el header si no se encontró aquí

    # --- Opción 2: Decodificar desde el Header Authorization (Implementación actual) ---
    if not authorization or not authorization.startswith("Bearer "):
        log.debug("No Authorization header found or not Bearer type.")
        return None # Usuario anónimo o no autenticado

    token = authorization.split(" ")[1]

    try:
        # Decodificar SIN verificar firma (asumiendo que el Gateway ya lo hizo)
        # PERO SÍ verificando claims básicos como expiración si es posible/deseado
        # Nota: python-jose podría requerir una key incluso para decodificación sin verificación
        #       si ese es el caso y no tienes la key, la decodificación fallará.
        #       Alternativamente, usar librerías que permitan decodificación forzada sin key.
        #       Aquí intentamos decodificar sin verificar firma explícitamente.
        #       Si la librería aún requiere una key (aunque sea dummy), esto fallará.
        #       Ajustar 'options' según sea necesario y el comportamiento de la librería.
        payload = jwt.decode(
            token,
            key="dummy", # Proporcionar una clave dummy si la librería lo requiere incluso sin verificar
            options={
                "verify_signature": False,
                "verify_aud": False, # No verificar audiencia
                "verify_iss": False, # No verificar issuer
                "verify_exp": True,  # ¡SÍ verificar expiración! Es una buena práctica.
                # "verify_nbf": True, # Verificar not before si se usa
                # "verify_iat": True, # Verificar issued at si se usa
                # "require": ["sub"] # Exigir claims específicos si es necesario
            }
        )

        # Extraer 'sub' (estándar) o 'user_id' (personalizado)
        user_id_str = payload.get("sub") or payload.get("user_id")

        if not user_id_str:
            log.warning("Token payload decoded but missing 'sub' or 'user_id' claim", token_payload_keys=list(payload.keys()))
            return None # Claim necesario no encontrado

        # Convertir a UUID
        try:
            user_uuid = uuid.UUID(user_id_str)
            log.debug("Successfully extracted user ID from token", user_id=str(user_uuid))
            return user_uuid
        except ValueError:
            log.warning("Claim 'sub' or 'user_id' is not a valid UUID", claim_value=user_id_str)
            return None # ID malformado

    except ExpiredSignatureError:
        log.warning("JWT token has expired")
        # Podrías lanzar HTTPException 401 aquí si un token expirado debe ser rechazado
        # raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token has expired")
        return None # O tratar como anónimo/no válido
    except JWTClaimsError as e:
        log.warning("JWT claims verification failed", error=str(e))
        # raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=f"Invalid token claims: {e}")
        return None
    except JWTError as e: # Error genérico de JWT (formato inválido, etc.)
        log.warning("JWT processing error", error=str(e), token_snippet=token[:10]+"...")
        # raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=f"Invalid token: {e}")
        return None
    except Exception as e: # Capturar cualquier otro error inesperado
        log.exception("Unexpected error during user ID extraction from token")
        # raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Error processing token")
        return None # Fallback a anónimo o error interno


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
    # --- Usar la dependencia actualizada ---
    user_id: Optional[uuid.UUID] = Depends(get_current_user_id),
):
    """
    Endpoint principal para procesar consultas de usuario.
    """
    endpoint_log = log.bind(
        company_id=str(company_id),
        # --- Loguear el user_id extraído o 'anonymous' ---
        user_id=str(user_id) if user_id else "anonymous",
        query=request_body.query[:100] + "..." if len(request_body.query) > 100 else request_body.query
    )
    endpoint_log.info("Received query request")

    # --- Validación adicional (opcional): Requerir user_id si es mandatorio ---
    # if not user_id:
    #     endpoint_log.warning("Query request rejected: User ID could not be determined from token.")
    #     raise HTTPException(
    #         status_code=status.HTTP_401_UNAUTHORIZED,
    #         detail="Could not identify user from token. Ensure a valid token is provided."
    #     )

    try:
        # Ejecutar el pipeline RAG
        answer, retrieved_docs, log_id = await rag_pipeline.run_rag_pipeline(
            query=request_body.query,
            company_id=str(company_id),
            # --- Pasar el user_id (UUID) convertido a string o None ---
            user_id=str(user_id) if user_id else None,
            top_k=request_body.retriever_top_k
        )

        # Formatear documentos recuperados para la respuesta (sin cambios)
        response_docs = []
        for doc in retrieved_docs:
            doc_meta = doc.meta or {}
            original_doc_id = doc_meta.get("document_id")
            file_name = doc_meta.get("file_name")

            response_docs.append(schemas.RetrievedDocument(
                id=doc.id,
                score=doc.score,
                content_preview=(doc.content[:150] + '...') if doc.content and len(doc.content) > 150 else doc.content,
                metadata=doc_meta,
                document_id=original_doc_id,
                file_name=file_name
            ))

        endpoint_log.info("Query processed successfully", log_id=str(log_id) if log_id else "Log Failed", num_retrieved=len(response_docs))

        return schemas.QueryResponse(
            answer=answer,
            retrieved_documents=response_docs,
            query_log_id=log_id
        )

    except ValueError as ve:
        endpoint_log.warning("Value error during query processing", error=str(ve))
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(ve))
    except ConnectionError as ce:
         endpoint_log.error("Connection error during query processing", error=str(ce), exc_info=True)
         raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=f"Service dependency unavailable: {ce}")
    except HTTPException as http_exc: # Re-lanzar HTTPExceptions que puedan venir de las dependencias
        raise http_exc
    except Exception as e:
        endpoint_log.exception("Unhandled exception during query processing")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"An internal error occurred: {type(e).__name__}"
        )