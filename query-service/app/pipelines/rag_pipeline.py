# query-service/app/pipelines/rag_pipeline.py
import structlog
import asyncio
import uuid
from typing import Dict, Any, List, Tuple, Optional

# *** CORRECCIÓN: Usar MilvusException y ErrorCode ***
from pymilvus.exceptions import MilvusException, ErrorCode # Asegurarse que ErrorCode esté importado
from fastapi import HTTPException, status

from haystack import Pipeline, Document
from haystack.components.embedders import OpenAITextEmbedder
from haystack.components.builders.prompt_builder import PromptBuilder
from milvus_haystack import MilvusDocumentStore, MilvusEmbeddingRetriever # Asegurar que esté importado
from haystack.utils import Secret # Asegurar que esté importado

from app.core.config import settings
from app.db import postgres_client
from app.services.gemini_client import gemini_client
from app.api.v1.schemas import RetrievedDocument

log = structlog.get_logger(__name__)

# --- Component Initialization Functions (sin cambios) ---
def get_milvus_document_store() -> MilvusDocumentStore:
    connection_uri = str(settings.MILVUS_URI)
    connection_timeout = 30.0
    log.debug("Initializing MilvusDocumentStore...", uri=connection_uri, collection=settings.MILVUS_COLLECTION_NAME)
    try:
        store = MilvusDocumentStore(
            connection_args={"uri": connection_uri, "timeout": connection_timeout},
            collection_name=settings.MILVUS_COLLECTION_NAME,
            search_params=settings.MILVUS_SEARCH_PARAMS,
            consistency_level="Strong",
        )
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
        api_key=Secret.from_token(api_key_value or "dummy-key"), # Usar Secret.from_token
        model=settings.OPENAI_EMBEDDING_MODEL
    )

def get_milvus_retriever(document_store: MilvusDocumentStore) -> MilvusEmbeddingRetriever:
    log.debug("Initializing MilvusEmbeddingRetriever")
    return MilvusEmbeddingRetriever(document_store=document_store)

def get_prompt_builder() -> PromptBuilder:
    log.debug("Initializing PromptBuilder")
    # Pasar las variables requeridas explícitamente ayuda a la validación de Haystack
    return PromptBuilder(template=settings.RAG_PROMPT_TEMPLATE)


# --- Pipeline Construction (sin cambios) ---
_rag_pipeline_instance: Optional[Pipeline] = None
def build_rag_pipeline() -> Pipeline:
    global _rag_pipeline_instance
    if _rag_pipeline_instance: return _rag_pipeline_instance
    log.info("Building Haystack RAG pipeline...")
    rag_pipeline = Pipeline()
    try:
        doc_store = get_milvus_document_store()
        text_embedder = get_openai_text_embedder()
        retriever = get_milvus_retriever(document_store=doc_store)
        prompt_builder = get_prompt_builder()

        # Añadir componentes con nombres explícitos
        rag_pipeline.add_component("text_embedder", text_embedder)
        rag_pipeline.add_component("retriever", retriever)
        rag_pipeline.add_component("prompt_builder", prompt_builder)

        # Conectar los sockets
        # text_embedder output 'embedding' -> retriever input 'query_embedding'
        rag_pipeline.connect("text_embedder.embedding", "retriever.query_embedding")
        # retriever output 'documents' -> prompt_builder input 'documents'
        rag_pipeline.connect("retriever.documents", "prompt_builder.documents")

        log.info("Haystack RAG pipeline built successfully.")
        _rag_pipeline_instance = rag_pipeline
        return rag_pipeline
    except Exception as e:
        log.error("Failed to build Haystack RAG pipeline", error=str(e), exc_info=True)
        raise RuntimeError("Could not build the RAG pipeline") from e

# --- Pipeline Execution ---
# *** FUNCIÓN CORREGIDA: run_rag_pipeline ***
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
    run_log.info("Running RAG pipeline execution flow...") # Mensaje ligeramente diferente

    try:
        pipeline = build_rag_pipeline() # Obtener/construir el pipeline
    except Exception as build_err:
         run_log.error("Failed to get or build RAG pipeline for execution", error=str(build_err))
         raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="RAG pipeline is not available.")

    retriever_top_k = top_k if top_k is not None else settings.RETRIEVER_TOP_K
    # *** CORRECCIÓN: Filtro para retriever ***
    # Asegurarse que el filtro usa el nombre de campo correcto definido en settings
    retriever_filters = {"field": settings.MILVUS_COMPANY_ID_FIELD, "operator": "==", "value": company_id}
    # Milvus-Haystack retriever espera filtros en este formato ^^^ (o similar, verificar su doc)
    # Alternativa si Milvus-Haystack usa el formato directo de Pymilvus expr:
    # retriever_filters_expr = f"{settings.MILVUS_COMPANY_ID_FIELD} == '{company_id}'"


    run_log.debug("Pipeline execution parameters set", filters=retriever_filters, top_k=retriever_top_k) # Loguear filtros usados

    # *** CORRECCIÓN: Estructura de pipeline_input ***
    # Mapear nombres de componentes a diccionarios de {socket_name: value}
    pipeline_input = {
        "text_embedder": {"text": query},  # Input 'text' para el componente 'text_embedder'
        "retriever": {"filters": [retriever_filters], "top_k": retriever_top_k}, # Inputs 'filters' y 'top_k' para 'retriever'. Filters es una lista.
        "prompt_builder": {"query": query} # Input 'query' para 'prompt_builder'. 'documents' viene del retriever.
    }
    # Nota: Si se usara retriever_filters_expr: "retriever": {"filters": retriever_filters_expr, ...}

    run_log.debug("Constructed pipeline input dictionary", pipeline_input_structure=pipeline_input)

    try:
        loop = asyncio.get_running_loop()
        # Ejecutar el pipeline en un executor para no bloquear el loop (si alguna parte es síncrona)
        pipeline_result = await loop.run_in_executor(None, pipeline.run, pipeline_input)

        run_log.info("Haystack pipeline (embed, retrieve, prompt) executed successfully.")

        # Extraer resultados (con verificación robusta)
        retrieved_docs: List[Document] = pipeline_result.get("retriever", {}).get("documents", [])
        prompt_builder_output = pipeline_result.get("prompt_builder", {})
        generated_prompt: Optional[str] = prompt_builder_output.get("prompt") if isinstance(prompt_builder_output, dict) else None

        if not retrieved_docs:
             run_log.warning("No relevant documents found by retriever for the query.")
        else:
             run_log.info(f"Retriever found {len(retrieved_docs)} documents.")

        if not generated_prompt:
             run_log.error("Failed to extract prompt from prompt_builder output", component_output=prompt_builder_output)
             # Crear un prompt de fallback si falla la generación
             generated_prompt = f"Pregunta: {query}\n\n(No se pudo construir el prompt con documentos recuperados). Por favor responde a la pregunta."

        run_log.debug("Generated prompt for LLM", prompt_length=len(generated_prompt))

        # Llamar a Gemini (asíncrono)
        answer = await gemini_client.generate_answer(generated_prompt)
        run_log.info("Answer generated by Gemini", answer_length=len(answer))

        # Loguear la interacción en la BD (con manejo de errores)
        log_id: Optional[uuid.UUID] = None
        try:
            # Formatear documentos para el log
            formatted_docs_for_log = [
                RetrievedDocument.from_haystack_doc(doc).model_dump(exclude_none=True)
                for doc in retrieved_docs
            ]
            # Convertir IDs a UUID si es necesario
            user_uuid = uuid.UUID(user_id) if user_id and isinstance(user_id, str) else user_id
            company_uuid = uuid.UUID(company_id) if isinstance(company_id, str) else company_id

            log_id = await postgres_client.log_query_interaction(
                company_id=company_uuid,
                user_id=user_uuid, # Puede ser None si user_id no se pasó o es inválido
                query=query,
                answer=answer,
                retrieved_documents_data=formatted_docs_for_log,
                chat_id=chat_id,
                metadata={"retriever_top_k": retriever_top_k, "llm_model": settings.GEMINI_MODEL_NAME}
            )
            run_log.info("Query interaction logged to database", db_log_id=str(log_id))
        except Exception as log_err:
             # Loguear el error pero continuar, la respuesta al usuario es más crítica
             run_log.error("Failed to log query interaction to database after successful generation", error=str(log_err), exc_info=True)

        # Devolver la respuesta, documentos y log_id (puede ser None si el log falló)
        return answer, retrieved_docs, log_id

    except HTTPException as http_exc:
        # Relanzar excepciones HTTP conocidas (ej. 403, 404 de check_chat_ownership)
        raise http_exc
    except MilvusException as milvus_err:
        run_log.error("Milvus error during pipeline execution", error_code=milvus_err.code, error_message=milvus_err.message, exc_info=True)
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=f"Vector database error: {milvus_err.message}")
    except ValueError as val_err:
         # Capturar ValueErrors específicos (como el "Missing input" original)
         run_log.error("ValueError during pipeline execution", error=str(val_err), exc_info=True)
         # Podría ser un 400 si es por input inválido, o 500 si es error interno del pipeline
         raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Pipeline configuration or input error: {val_err}")
    except Exception as e:
        # Capturar cualquier otra excepción inesperada
        run_log.exception("Unexpected error occurred during RAG pipeline execution")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Error processing query: {type(e).__name__}")

# --- Dependency Check Function ---
# *** FUNCIÓN CORREGIDA: check_pipeline_dependencies (manejo de errores Milvus) ***
async def check_pipeline_dependencies() -> Dict[str, str]:
    results = {"milvus_connection": "pending", "openai_api": "pending", "gemini_api": "pending"}
    try:
        store = get_milvus_document_store()
        # Intentar verificar la conexión. count_documents() es una buena forma.
        # Ejecutar en thread porque puede ser bloqueante
        count = await asyncio.to_thread(store.count_documents)
        results["milvus_connection"] = "ok"
        log.debug("Milvus dependency check successful.", document_count=count)
    except MilvusException as e:
        # Verificar si el error es específicamente "Collection not found"
        # El código para esto puede variar, pero ErrorCode.COLLECTION_NOT_FOUND es común (valor 1)
        # Consultar documentación de Pymilvus para códigos de error exactos si es necesario.
        if e.code == ErrorCode.COLLECTION_NOT_FOUND: # Usar ErrorCode importado
            # Considerar esto como OK, ya que la colección se creará al escribir
            results["milvus_connection"] = "ok (collection not found yet)"
            log.info("Milvus dependency check: Collection not found (expected if empty, will be created on write).")
        elif e.code == ErrorCode.UNEXPECTED_ERROR and "connect failed" in e.message.lower():
            # Error de conexión específico
            results["milvus_connection"] = f"error: Connection Failed (code={e.code}, msg={e.message})"
            log.warning("Milvus dependency check failed: Connection Error", error_code=e.code, error_message=e.message, exc_info=False)
        else:
            # Otro error de Milvus (configuración, timeout, etc.)
            results["milvus_connection"] = f"error: MilvusException (code={e.code}, msg={e.message})"
            log.warning("Milvus dependency check failed with Milvus error", error_code=e.code, error_message=e.message, exc_info=False) # No mostrar exc_info por defecto
    except RuntimeError as rte:
         # Capturar errores de RuntimeError de get_milvus_document_store si falla la inicialización
         results["milvus_connection"] = f"error: Initialization Failed ({rte})"
         log.warning("Milvus dependency check failed during store initialization", error=str(rte), exc_info=False)
    except Exception as e:
        # Otro tipo de error inesperado durante la comprobación de Milvus
        results["milvus_connection"] = f"error: Unexpected {type(e).__name__}"
        log.warning("Milvus dependency check failed with unexpected error", error=str(e), exc_info=True) # Mostrar exc_info aquí puede ser útil

    # Comprobaciones de API keys (sin cambios)
    if settings.OPENAI_API_KEY.get_secret_value() and settings.OPENAI_API_KEY.get_secret_value() != "dummy-key":
        results["openai_api"] = "key_present"
    else:
        results["openai_api"] = "key_missing"
        log.warning("OpenAI API Key missing or is dummy key in config.")

    if settings.GEMINI_API_KEY.get_secret_value():
        results["gemini_api"] = "key_present"
    else:
        results["gemini_api"] = "key_missing"
        log.warning("Gemini API Key missing in config.")

    return results