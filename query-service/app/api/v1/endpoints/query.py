# ./app/api/v1/endpoints/query.py
import uuid
from typing import Dict, Any, Optional, List # Añadir List
import structlog
import asyncio

from jose import jwt, JWTError
from jose.exceptions import ExpiredSignatureError, JWTClaimsError, JWSError

from fastapi import APIRouter, Depends, HTTPException, status, Header, Body, Request

from app.api.v1 import schemas
from app.core.config import settings
from app.db import postgres_client # Para logging y ahora chat/messages
from app.pipelines import rag_pipeline # Importar funciones del pipeline
from haystack import Document # Para type hints
from app.utils.helpers import truncate_text # Importar helper si se usa

log = structlog.get_logger(__name__)

router = APIRouter()

# --- Dependency for Company ID (Sin cambios) ---
async def get_current_company_id(x_company_id: Optional[str] = Header(None)) -> uuid.UUID:
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

# --- Dependency for User ID (Sin cambios) ---
async def get_current_user_id(authorization: Optional[str] = Header(None)) -> Optional[uuid.UUID]:
    if not authorization or not authorization.startswith("Bearer "):
        log.debug("No Authorization header found or not Bearer type.")
        return None # Usuario anónimo o no autenticado

    token = authorization.split(" ")[1]
    try:
        payload = jwt.decode(
            token, key="dummy",
            options={
                "verify_signature": False, "verify_aud": False, "verify_iss": False, "verify_exp": True,
            }
        )
        user_id_str = payload.get("sub") or payload.get("user_id")
        if not user_id_str: return None
        try:
            user_uuid = uuid.UUID(user_id_str)
            log.debug("Successfully extracted user ID from token", user_id=str(user_uuid))
            return user_uuid
        except ValueError: return None
    except ExpiredSignatureError: return None
    except JWTError: return None
    except Exception: log.exception("Unexpected error during user ID extraction"); return None


# --- Endpoint /query Modificado ---
@router.post(
    "/ask",
    # --- Actualizar response_model ---
    response_model=schemas.QueryResponse,
    status_code=status.HTTP_200_OK,
    summary="Process a user query using RAG pipeline and manage chat history",
    description="Receives a query. If chat_id is provided, continues the chat. If not, creates a new chat. Saves user and assistant messages, runs RAG, logs the interaction, and returns the result including the chat_id.",
)
async def process_query(
    request_body: schemas.QueryRequest = Body(...),
    company_id: uuid.UUID = Depends(get_current_company_id),
    user_id: Optional[uuid.UUID] = Depends(get_current_user_id),
):
    """
    Endpoint principal para procesar consultas de usuario, integrado con el historial de chat.
    """
    # --- Validación de User ID (Ahora es mandatorio para chats) ---
    if not user_id:
        log.warning("Query request rejected: User ID could not be determined from token. Chat history requires authentication.")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required to use the chat feature."
        )

    endpoint_log = log.bind(
        company_id=str(company_id),
        user_id=str(user_id),
        query=truncate_text(request_body.query, 100), # Usar helper
        provided_chat_id=str(request_body.chat_id) if request_body.chat_id else "None"
    )
    endpoint_log.info("Received query request with chat context")

    current_chat_id: uuid.UUID
    is_new_chat = False

    try:
        # --- Lógica de Creación/Obtención de Chat ID ---
        if request_body.chat_id:
            # Verificar si el chat existente pertenece al usuario
            if not await postgres_client.check_chat_ownership(request_body.chat_id, user_id, company_id):
                 endpoint_log.warning("Attempt to use chat_id not owned by user or non-existent", chat_id=str(request_body.chat_id))
                 raise HTTPException(
                     status_code=status.HTTP_403_FORBIDDEN, # O 404 si preferimos
                     detail="Chat not found or access denied."
                 )
            current_chat_id = request_body.chat_id
            endpoint_log = endpoint_log.bind(chat_id=str(current_chat_id))
            endpoint_log.info("Continuing existing chat")
        else:
            # Crear nuevo chat
            endpoint_log.info("No chat_id provided, creating a new chat...")
            # Generar un título inicial simple (se puede mejorar)
            initial_title = f"Chat: {truncate_text(request_body.query, 50)}"
            current_chat_id = await postgres_client.create_chat(
                user_id=user_id,
                company_id=company_id,
                title=initial_title
            )
            is_new_chat = True
            endpoint_log = endpoint_log.bind(chat_id=str(current_chat_id))
            endpoint_log.info("New chat created", title=initial_title)

        # --- Guardar Mensaje del Usuario ---
        endpoint_log.info("Saving user message...")
        await postgres_client.add_message_to_chat(
            chat_id=current_chat_id,
            role='user',
            content=request_body.query
        )
        endpoint_log.info("User message saved")

        # --- Ejecutar el Pipeline RAG ---
        endpoint_log.info("Running RAG pipeline...")
        # Pasar chat_id a run_rag_pipeline para logging
        answer, retrieved_docs_haystack, log_id = await rag_pipeline.run_rag_pipeline(
            query=request_body.query,
            company_id=str(company_id),
            user_id=str(user_id), # run_rag_pipeline espera string
            top_k=request_body.retriever_top_k,
            chat_id=current_chat_id # Pasar el UUID
        )
        endpoint_log.info("RAG pipeline finished")

        # --- Formatear Documentos Recuperados y Extraer Fuentes ---
        response_docs_schema: List[schemas.RetrievedDocument] = []
        assistant_sources: List[Dict[str, Any]] = []
        for doc in retrieved_docs_haystack:
            schema_doc = schemas.RetrievedDocument.from_haystack_doc(doc)
            response_docs_schema.append(schema_doc)
            # Crear estructura de fuentes para guardar en messages.sources
            source_info = {
                "chunk_id": schema_doc.id,
                "document_id": schema_doc.document_id,
                "file_name": schema_doc.file_name,
                "score": schema_doc.score,
                "preview": schema_doc.content_preview # Guardar preview o content completo? Preview es más ligero.
            }
            assistant_sources.append(source_info)

        # --- Guardar Mensaje del Asistente ---
        endpoint_log.info("Saving assistant message...")
        await postgres_client.add_message_to_chat(
            chat_id=current_chat_id,
            role='assistant',
            content=answer,
            sources=assistant_sources if assistant_sources else None # Guardar fuentes si existen
        )
        endpoint_log.info("Assistant message saved")

        endpoint_log.info("Query processed successfully", log_id=str(log_id) if log_id else "Log Failed", num_retrieved=len(response_docs_schema))

        # --- Devolver Respuesta ---
        return schemas.QueryResponse(
            answer=answer,
            retrieved_documents=response_docs_schema,
            query_log_id=log_id,
            chat_id=current_chat_id # Devolver el chat_id usado o creado
        )

    except ValueError as ve: # Errores de validación o de lógica interna (e.g., chat no encontrado en add_message)
        endpoint_log.warning("Value error during query processing", error=str(ve))
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(ve))
    except ConnectionError as ce:
         endpoint_log.error("Connection error during query processing", error=str(ce), exc_info=True)
         raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=f"Service dependency unavailable: {ce}")
    except HTTPException as http_exc: # Re-lanzar HTTPExceptions controladas (e.g., 401, 403)
        raise http_exc
    except Exception as e:
        endpoint_log.exception("Unhandled exception during query processing")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"An internal error occurred: {type(e).__name__}"
        )