# query-service/app/pipelines/rag_pipeline.py
import structlog
import asyncio
import uuid
from typing import Dict, Any, List, Tuple, Optional

# *** CORRECCIÓN: Usar MilvusException para tipo correcto ***
from pymilvus.exceptions import MilvusException, CollectionNotDefinedException
from fastapi import HTTPException, status

from haystack import Pipeline, Document
from haystack.components.embedders import OpenAITextEmbedder
from haystack.components.builders.prompt_builder import PromptBuilder
from milvus_haystack import MilvusDocumentStore, MilvusEmbeddingRetriever # Asegúrate de tener milvus-haystack instalado
from haystack.utils import Secret

from app.core.config import settings
from app.db import postgres_client # Importar cliente DB
from app.services.gemini_client import gemini_client # Importar cliente Gemini
# Importar schema para formatear documentos
from app.api.v1.schemas import RetrievedDocument

log = structlog.get_logger(__name__)

# --- Component Initialization Functions ---
def get_milvus_document_store() -> MilvusDocumentStore:
    connection_uri = str(settings.MILVUS_URI)
    connection_timeout = 30.0 # Aumentado ligeramente
    log.debug("Initializing MilvusDocumentStore...", uri=connection_uri, collection=settings.MILVUS_COLLECTION_NAME)
    try:
        # --- CORRECCIÓN: Eliminar argumentos inválidos para Haystack 2.x ---
        # embedding_field, content_field, metadata_fields no son argumentos aquí.
        # Se manejan por la estructura del objeto Document de Haystack.
        store = MilvusDocumentStore(
            connection_args={"uri": connection_uri, "timeout": connection_timeout},
            collection_name=settings.MILVUS_COLLECTION_NAME,
            search_params=settings.MILVUS_SEARCH_PARAMS,
            consistency_level="Strong",
            # dim = settings.EMBEDDING_DIMENSION # Dimensión no es un argumento directo aquí, Milvus lo infiere o usa el índice.
        )
        # ----------------------------------------------------
        log.info("MilvusDocumentStore parameters configured.", uri=connection_uri, collection=settings.MILVUS_COLLECTION_NAME)
        return store
    except MilvusException as e:
        log.error("Failed to initialize MilvusDocumentStore", error_code=e.code, error_message=e.message, exc_info=True)
        raise RuntimeError(f"Milvus connection/initialization failed: {e.message}") from e
    except TypeError as te:
        log.error("TypeError during MilvusDocumentStore initialization (likely invalid argument)", error=str(te), exc_info=True)
        raise RuntimeError(f"Milvus initialization error (Invalid argument): {te}") from te
    except Exception as e:
        log.error("Unexpected error during MilvusDocumentStore initialization", error=str(e), exc_info=True)
        raise RuntimeError(f"Unexpected error initializing Milvus: {e}") from e

def get_openai_text_embedder() -> OpenAITextEmbedder:
    log.debug("Initializing OpenAITextEmbedder", model=settings.OPENAI_EMBEDDING_MODEL)
    api_key_value = settings.OPENAI_API_KEY.get_secret_value()
    if not api_key_value: log.warning("QUERY_OPENAI_API_KEY is missing or empty!")
    return OpenAITextEmbedder(
        api_key=Secret.from_token(api_key_value or "dummy-key"),
        model=settings.OPENAI_EMBEDDING_MODEL
    )

def get_milvus_retriever(document_store: MilvusDocumentStore) -> MilvusEmbeddingRetriever:
    log.debug("Initializing MilvusEmbeddingRetriever")
    return MilvusEmbeddingRetriever(document_store=document_store)

def get_prompt_builder() -> PromptBuilder:
    log.debug("Initializing PromptBuilder")
    return PromptBuilder(template=settings.RAG_PROMPT_TEMPLATE)

# --- Pipeline Construction ---
_rag_pipeline_instance: Optional[Pipeline] = None
def build_rag_pipeline() -> Pipeline:
    global _rag_pipeline_instance
    if _rag_pipeline_instance: return _rag_pipeline_instance
    log.info("Building Haystack RAG pipeline...")
    rag_pipeline = Pipeline()
    try:
        doc_store = get_milvus_document_store() # Llamar a la función corregida
        text_embedder = get_openai_text_embedder()
        retriever = get_milvus_retriever(document_store=doc_store)
        prompt_builder = get_prompt_builder()
        rag_pipeline.add_component("text_embedder", text_embedder)
        rag_pipeline.add_component("retriever", retriever)
        rag_pipeline.add_component("prompt_builder", prompt_builder)
        rag_pipeline.connect("text_embedder.embedding", "retriever.query_embedding")
        rag_pipeline.connect("retriever.documents", "prompt_builder.documents")
        log.info("Haystack RAG pipeline built successfully.")
        _rag_pipeline_instance = rag_pipeline
        return rag_pipeline
    except Exception as e:
        log.error("Failed to build Haystack RAG pipeline", error=str(e), exc_info=True)
        raise RuntimeError("Could not build the RAG pipeline") from e

# --- Pipeline Execution (sin cambios lógicos aquí) ---
async def run_rag_pipeline(
    query: str,
    company_id: str,
    user_id: Optional[str],
    top_k: Optional[int] = None,
    chat_id: Optional[uuid.UUID] = None
) -> Tuple[str, List[Document], Optional[uuid.UUID]]:
    """
    Ejecuta el pipeline RAG, llama a Gemini, y loguea la interacción.
    """
    run_log = log.bind(query=query, company_id=company_id, user_id=user_id or "N/A", chat_id=str(chat_id) if chat_id else "N/A")
    run_log.info("Running RAG pipeline...")
    try:
        pipeline = build_rag_pipeline()
    except Exception as build_err:
         run_log.error("Failed to get or build RAG pipeline for execution", error=str(build_err))
         raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="RAG pipeline is not available.")
    retriever_top_k = top_k if top_k is not None else settings.RETRIEVER_TOP_K
    retriever_filters = {"company_id": company_id}
    run_log.debug("Retriever filters set", filters=retriever_filters, top_k=retriever_top_k)
    pipeline_input = {
        "text_embedder": {"text": query},
        "retriever": {"filters": retriever_filters, "top_k": retriever_top_k},
        "prompt_builder": {"query": query}
    }
    try:
        loop = asyncio.get_running_loop()
        pipeline_result = await loop.run_in_executor(None, pipeline.run, pipeline_input)
        run_log.info("Haystack pipeline (embed, retrieve, prompt) executed.")
        retrieved_docs: List[Document] = pipeline_result.get("retriever", {}).get("documents", [])
        prompt_builder_output = pipeline_result.get("prompt_builder", {})
        generated_prompt: Optional[str] = prompt_builder_output.get("prompt")
        if not retrieved_docs: run_log.warning("No relevant documents found by retriever.")
        if not generated_prompt:
             run_log.error("Failed to extract prompt from pipeline output", output=prompt_builder_output)
             generated_prompt = f"Pregunta: {query}\n\nNo se encontraron documentos relevantes. Por favor responde basado en conocimiento general si es posible, o indica que no tienes información específica."
        run_log.debug("Generated prompt for LLM", prompt_length=len(generated_prompt))
        answer = await gemini_client.generate_answer(generated_prompt)
        run_log.info("Answer generated by Gemini", answer_length=len(answer))
        log_id: Optional[uuid.UUID] = None
        try:
            formatted_docs_for_log = [
                RetrievedDocument.from_haystack_doc(doc).model_dump(exclude_none=True)
                for doc in retrieved_docs
            ]
            user_uuid = uuid.UUID(user_id) if user_id else None
            company_uuid = uuid.UUID(company_id)
            log_id = await postgres_client.log_query_interaction(
                company_id=company_uuid, user_id=user_uuid, query=query, answer=answer,
                retrieved_documents_data=formatted_docs_for_log, chat_id=chat_id,
                metadata={"retriever_top_k": retriever_top_k, "llm_model": settings.GEMINI_MODEL_NAME}
            )
        except Exception as log_err:
             run_log.error("Failed to log query interaction to database", error=str(log_err), exc_info=True)
        return answer, retrieved_docs, log_id
    except HTTPException as http_exc: raise http_exc
    except MilvusException as milvus_err:
        run_log.error("Milvus error during pipeline execution", error_code=milvus_err.code, error_message=milvus_err.message, exc_info=True)
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=f"Vector database error: {milvus_err.message}")
    except Exception as e:
        run_log.exception("Error occurred during RAG pipeline execution")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Error processing query: {type(e).__name__}")

# --- CORRECCIÓN: Usar store.count() en lugar de describe_collection ---
async def check_pipeline_dependencies() -> Dict[str, str]:
    results = {"milvus_connection": "pending", "openai_api": "pending", "gemini_api": "pending"}
    try:
        store = get_milvus_document_store()
        # Verificar conexión contando documentos (más fiable que describe_collection)
        count = await asyncio.to_thread(store.count_documents)
        results["milvus_connection"] = "ok"
        log.debug("Milvus dependency check successful.", document_count=count)
    except CollectionNotDefinedException:
         results["milvus_connection"] = "ok (collection not found yet)" # Colección vacía/no creada no es error de conexión
         log.info("Milvus dependency check: Collection not found (will be created on write).")
    except Exception as e:
        # Capturar cualquier error de Milvus u otro error durante la comprobación
        results["milvus_connection"] = f"error: {type(e).__name__}"
        log.warning("Milvus dependency check failed", error=str(e), exc_info=False) # Log menos verboso aquí
    if settings.OPENAI_API_KEY.get_secret_value(): results["openai_api"] = "key_present"
    else: results["openai_api"] = "key_missing"; log.warning("OpenAI API Key missing in config.")
    if settings.GEMINI_API_KEY.get_secret_value(): results["gemini_api"] = "key_present"
    else: results["gemini_api"] = "key_missing"; log.warning("Gemini API Key missing in config.")
    return results