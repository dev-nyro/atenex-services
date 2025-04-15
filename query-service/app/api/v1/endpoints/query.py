# query-service/app/api/v1/endpoints/query.py
import uuid
from typing import Dict, Any, Optional, List
import structlog
import asyncio

from fastapi import APIRouter, Depends, HTTPException, status, Header, Body, Request

from app.api.v1 import schemas
from app.core.config import settings
from app.db import postgres_client
from app.pipelines import rag_pipeline
from haystack import Document
from app.utils.helpers import truncate_text
# Importar dependencias definidas en chat.py (o módulo compartido)
from .chat import get_current_company_id, get_current_user_id

log = structlog.get_logger(__name__)

router = APIRouter()

# --- Endpoint Principal Modificado para usar dependencias de X- Headers ---
@router.post(
    "/query", # Mantenemos la ruta interna /query
    response_model=schemas.QueryResponse,
    status_code=status.HTTP_200_OK,
    summary="Process a user query using RAG pipeline and manage chat history",
    description="Receives query via API Gateway. Uses X-Company-ID and X-User-ID. Continues or creates chat.",
)
async def process_query(
    request_body: schemas.QueryRequest = Body(...),
    # *** CORRECCIÓN CLAVE: Usar las dependencias importadas/definidas ***
    company_id: uuid.UUID = Depends(get_current_company_id),
    user_id: uuid.UUID = Depends(get_current_user_id),
    request: Request = None
):
    request_id = request.headers.get("x-request-id") if request else str(uuid.uuid4())
    endpoint_log = log.bind(
        request_id=request_id,
        company_id=str(company_id),
        user_id=str(user_id),
        query=truncate_text(request_body.query, 100),
        provided_chat_id=str(request_body.chat_id) if request_body.chat_id else "None"
    )
    endpoint_log.info("Received query request with chat context")

    current_chat_id: uuid.UUID
    is_new_chat = False

    try:
        # Lógica de Chat ID (sin cambios, ya usa los user_id/company_id de Depends)
        if request_body.chat_id:
            if not await postgres_client.check_chat_ownership(request_body.chat_id, user_id, company_id):
                 raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Chat not found or access denied.")
            current_chat_id = request_body.chat_id
            endpoint_log = endpoint_log.bind(chat_id=str(current_chat_id))
            endpoint_log.info("Continuing existing chat")
        else:
            endpoint_log.info("Creating a new chat...")
            initial_title = f"Chat: {truncate_text(request_body.query, 50)}"
            current_chat_id = await postgres_client.create_chat(user_id=user_id, company_id=company_id, title=initial_title)
            is_new_chat = True
            endpoint_log = endpoint_log.bind(chat_id=str(current_chat_id))
            endpoint_log.info("New chat created", title=initial_title)

        # Guardar Mensaje Usuario (sin cambios, usa user_id de Depends)
        endpoint_log.info("Saving user message...")
        await postgres_client.save_message(
            chat_id=current_chat_id, role='user', content=request_body.query
        )
        endpoint_log.info("User message saved")

        # Ejecutar Pipeline RAG (sin cambios, usa user_id/company_id de Depends)
        endpoint_log.info("Running RAG pipeline...")
        answer, retrieved_docs_haystack, log_id = await rag_pipeline.run_rag_pipeline(
            query=request_body.query, company_id=str(company_id), user_id=str(user_id),
            top_k=request_body.retriever_top_k, chat_id=current_chat_id
        )
        endpoint_log.info("RAG pipeline finished")

        # Formatear Documentos y Fuentes (sin cambios)
        response_docs_schema: List[schemas.RetrievedDocument] = []
        assistant_sources: List[Dict[str, Any]] = []
        for doc in retrieved_docs_haystack:
            schema_doc = schemas.RetrievedDocument.from_haystack_doc(doc)
            response_docs_schema.append(schema_doc)
            source_info = { "chunk_id": schema_doc.id, "document_id": schema_doc.document_id, "file_name": schema_doc.file_name, "score": schema_doc.score, "preview": schema_doc.content_preview }
            assistant_sources.append(source_info)

        # Guardar Mensaje Asistente (sin cambios)
        endpoint_log.info("Saving assistant message...")
        await postgres_client.save_message(
            chat_id=current_chat_id, role='assistant', content=answer,
            sources=assistant_sources if assistant_sources else None
        )
        endpoint_log.info("Assistant message saved")

        endpoint_log.info("Query processed successfully", log_id=str(log_id) if log_id else "Log Failed", num_retrieved=len(response_docs_schema))

        # Devolver Respuesta (sin cambios)
        return schemas.QueryResponse(
            answer=answer, retrieved_documents=response_docs_schema,
            query_log_id=log_id, chat_id=current_chat_id
        )

    # Manejo de Errores (sin cambios)
    except ValueError as ve: endpoint_log.warning("Value error", error=str(ve)); raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(ve))
    except ConnectionError as ce: endpoint_log.error("Connection error", error=str(ce), exc_info=True); raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=f"Dependency unavailable: {ce}")
    except HTTPException as http_exc: raise http_exc
    except Exception as e: endpoint_log.exception("Unhandled exception"); raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Internal error: {type(e).__name__}")