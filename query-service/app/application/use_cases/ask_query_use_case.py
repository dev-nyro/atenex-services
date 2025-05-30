# query-service/app/application/use_cases/ask_query_use_case.py
import structlog
import asyncio
import uuid
import re
from typing import Dict, Any, List, Tuple, Optional, Type, Set
from datetime import datetime, timezone, timedelta
import httpx
import os 
import json 
from pydantic import ValidationError 

# Import Ports and Domain Models
from app.application.ports import (
    ChatRepositoryPort, LogRepositoryPort, VectorStorePort, LLMPort,
    SparseRetrieverPort, DiversityFilterPort, ChunkContentRepositoryPort,
    EmbeddingPort, RerankerPort 
)
from app.domain.models import RetrievedChunk, ChatMessage, RespuestaEstructurada, FuenteCitada 
from haystack.components.builders.prompt_builder import PromptBuilder
from haystack import Document

from app.core.config import settings
from app.api.v1.schemas import RetrievedDocument as RetrievedDocumentSchema 
from app.utils.helpers import truncate_text
from fastapi import HTTPException, status

log = structlog.get_logger(__name__)

GREETING_REGEX = re.compile(r"^\s*(hola|hello|hi|buenos días|buenas tardes|buenas noches|hey|qué tal|hi there)\s*[\.,!?]*\s*$", re.IGNORECASE)
RRF_K = 60 

MAP_REDUCE_NO_RELEVANT_INFO = "No hay información relevante en este fragmento."


def format_time_delta(dt: datetime) -> str:
    now = datetime.now(timezone.utc)
    delta = now - dt
    if delta < timedelta(minutes=1):
        return "justo ahora"
    elif delta < timedelta(hours=1):
        minutes = int(delta.total_seconds() / 60)
        return f"hace {minutes} min" if minutes > 1 else "hace 1 min"
    elif delta < timedelta(days=1):
        hours = int(delta.total_seconds() / 3600)
        return f"hace {hours} h" if hours > 1 else "hace 1 h"
    else:
        days = delta.days
        return f"hace {days} días" if days > 1 else "hace 1 día"


class AskQueryUseCase:
    def __init__(self,
                 chat_repo: ChatRepositoryPort,
                 log_repo: LogRepositoryPort,
                 vector_store: VectorStorePort,
                 llm: LLMPort,
                 embedding_adapter: EmbeddingPort,
                 http_client: httpx.AsyncClient, 
                 sparse_retriever: Optional[SparseRetrieverPort] = None, 
                 chunk_content_repo: Optional[ChunkContentRepositoryPort] = None, 
                 diversity_filter: Optional[DiversityFilterPort] = None):
        self.chat_repo = chat_repo
        self.log_repo = log_repo
        self.vector_store = vector_store
        self.llm = llm
        self.embedding_adapter = embedding_adapter
        self.http_client = http_client 
        self.settings = settings
        
        self.sparse_retriever = sparse_retriever if settings.BM25_ENABLED and sparse_retriever else None
        
        self.chunk_content_repo = chunk_content_repo 
        self.diversity_filter = diversity_filter if settings.DIVERSITY_FILTER_ENABLED else None

        self._prompt_builder_rag = self._initialize_prompt_builder_from_path(settings.RAG_PROMPT_TEMPLATE_PATH)
        self._prompt_builder_general = self._initialize_prompt_builder_from_path(settings.GENERAL_PROMPT_TEMPLATE_PATH)
        self._prompt_builder_map = self._initialize_prompt_builder_from_path(settings.MAP_PROMPT_TEMPLATE_PATH)
        self._prompt_builder_reduce = self._initialize_prompt_builder_from_path(settings.REDUCE_PROMPT_TEMPLATE_PATH)

        log_params = {
            "embedding_adapter_type": type(self.embedding_adapter).__name__,
            "bm25_enabled_setting": settings.BM25_ENABLED, 
            "sparse_retriever_active": bool(self.sparse_retriever), 
            "sparse_retriever_type": type(self.sparse_retriever).__name__ if self.sparse_retriever else "None",
            "reranker_enabled": settings.RERANKER_ENABLED,
            "reranker_service_url": str(settings.RERANKER_SERVICE_URL) if settings.RERANKER_ENABLED else "N/A",
            "diversity_filter_enabled": settings.DIVERSITY_FILTER_ENABLED,
            "diversity_filter_type": type(self.diversity_filter).__name__ if self.diversity_filter else "None",
            "map_reduce_enabled": settings.MAPREDUCE_ENABLED,
            "map_reduce_threshold": settings.MAPREDUCE_ACTIVATION_THRESHOLD,
            "map_reduce_batch_size": settings.MAPREDUCE_CHUNK_BATCH_SIZE,
            "rag_prompt_path": settings.RAG_PROMPT_TEMPLATE_PATH,
            "general_prompt_path": settings.GENERAL_PROMPT_TEMPLATE_PATH,
            "map_prompt_path": settings.MAP_PROMPT_TEMPLATE_PATH,
            "reduce_prompt_path": settings.REDUCE_PROMPT_TEMPLATE_PATH,
            "gemini_model_name": settings.GEMINI_MODEL_NAME,
            "max_context_chunks": settings.MAX_CONTEXT_CHUNKS,
            "max_prompt_tokens": settings.MAX_PROMPT_TOKENS,
        }
        log.info("AskQueryUseCase Initialized", **log_params)
        if settings.BM25_ENABLED and not self.sparse_retriever:
             log.error("BM25_ENABLED in settings, but sparse_retriever instance is NOT available (init failed or service unavailable). Sparse search will be skipped.")
        if self.sparse_retriever and not self.chunk_content_repo:
            log.error("SparseRetriever is active but ChunkContentRepository is missing. Content fetching for sparse results will fail.")


    def _initialize_prompt_builder_from_path(self, template_path: str) -> PromptBuilder:
        init_log = log.bind(action="_initialize_prompt_builder_from_path", path=template_path)
        init_log.debug("Initializing PromptBuilder from path...")
        try:
            path_to_template = template_path
            
            if not os.path.exists(path_to_template):
                init_log.error("Prompt template file not found at absolute path.")
                raise FileNotFoundError(f"Prompt template file not found at {template_path}")

            with open(path_to_template, "r", encoding="utf-8") as f:
                template_content = f.read()
            
            if not template_content.strip():
                init_log.error("Prompt template file is empty.")
                raise ValueError(f"Prompt template file is empty: {template_path}")

            builder = PromptBuilder(template=template_content)
            init_log.info("PromptBuilder initialized successfully from file.")
            return builder
        except FileNotFoundError:
            init_log.error("Prompt template file not found.")
            default_fallback_template = "Query: {{ query }}\n{% if documents %}Context: {{documents}}{% endif %}\nAnswer:"
            init_log.warning(f"Falling back to basic template due to missing file: {template_path}")
            return PromptBuilder(template=default_fallback_template)
        except Exception as e:
            init_log.exception("Failed to load or initialize PromptBuilder from path.")
            raise RuntimeError(f"Critical error loading prompt template from {template_path}: {e}") from e

    async def _embed_query(self, query: str) -> List[float]:
        embed_log = log.bind(action="_embed_query_use_case_call_remote")
        try:
            embedding = await self.embedding_adapter.embed_query(query)
            if not embedding or not isinstance(embedding, list) or not all(isinstance(f, float) for f in embedding):
                embed_log.error("Invalid embedding received from adapter", received_embedding_type=type(embedding).__name__)
                raise ValueError("Embedding adapter returned invalid or empty vector.")
            embed_log.debug("Query embedded successfully via remote adapter", vector_dim=len(embedding))
            return embedding
        except ConnectionError as e: 
            embed_log.error("Embedding failed: Connection to embedding service failed.", error=str(e), exc_info=False)
            raise ConnectionError(f"Embedding service error: {e}") from e
        except ValueError as e: 
            embed_log.error("Embedding failed: Invalid data from embedding service.", error=str(e), exc_info=False)
            raise ValueError(f"Embedding service data error: {e}") from e
        except Exception as e: 
            embed_log.error("Unexpected error during query embedding via adapter", error=str(e), exc_info=True)
            raise ConnectionError(f"Unexpected error contacting embedding service: {e}") from e

    def _format_chat_history(self, messages: List[ChatMessage]) -> str:
        if not messages:
            return ""
        history_str = []
        for msg in reversed(messages): 
            role = "Usuario" if msg.role == 'user' else "Atenex"
            time_mark = format_time_delta(msg.created_at)
            history_str.append(f"{role} ({time_mark}): {msg.content}")
        return "\n".join(reversed(history_str))

    async def _build_prompt(self, query: str, documents: List[Document], chat_history: Optional[str] = None, builder: Optional[PromptBuilder] = None, prompt_data_override: Optional[Dict[str,Any]] = None) -> str:
        
        final_prompt_data = {"query": query}
        if prompt_data_override:
            final_prompt_data.update(prompt_data_override)
        else: 
            if documents:
                final_prompt_data["documents"] = documents
            if chat_history:
                final_prompt_data["chat_history"] = chat_history
        
        effective_builder = builder
        if not effective_builder: 
            if documents or "documents" in final_prompt_data or "mapped_responses" in final_prompt_data: 
                effective_builder = self._prompt_builder_rag 
                if "mapped_responses" in final_prompt_data: effective_builder = self._prompt_builder_reduce
            else: 
                effective_builder = self._prompt_builder_general
        
        log.debug("Building prompt", builder_type=type(effective_builder).__name__, data_keys=list(final_prompt_data.keys()))

        try:
            result = await asyncio.to_thread(effective_builder.run, **final_prompt_data)
            prompt = result.get("prompt")
            if not prompt: raise ValueError("Prompt generation returned empty.")
            log.debug("Prompt built successfully.", length=len(prompt))
            return prompt
        except Exception as e:
            log.error("Prompt building failed", error=str(e), data_keys=list(final_prompt_data.keys()), exc_info=True)
            raise ValueError(f"Prompt building error: {e}") from e

    def _reciprocal_rank_fusion(self,
                                dense_results: List[RetrievedChunk],
                                sparse_results: List[Tuple[str, float]], 
                                k: int = RRF_K) -> Dict[str, float]:
        fused_scores: Dict[str, float] = {}
        for rank, chunk in enumerate(dense_results):
            if chunk.id: 
                fused_scores[chunk.id] = fused_scores.get(chunk.id, 0.0) + 1.0 / (k + rank + 1)
        
        for rank, (chunk_id, _) in enumerate(sparse_results):
            if chunk_id: 
                fused_scores[chunk_id] = fused_scores.get(chunk_id, 0.0) + 1.0 / (k + rank + 1)
        return fused_scores

    async def _fetch_content_for_fused_results(
        self,
        fused_scores: Dict[str, float], 
        dense_map: Dict[str, RetrievedChunk], 
        top_n: int
        ) -> List[RetrievedChunk]:
        fetch_log = log.bind(action="fetch_content_for_fused", top_n=top_n, fused_count=len(fused_scores))
        if not fused_scores: return []

        sorted_chunk_ids_with_scores = sorted(fused_scores.items(), key=lambda item: item[1], reverse=True)
        top_ids_with_scores_tuples: List[Tuple[str, float]] = sorted_chunk_ids_with_scores[:top_n]
        
        fetch_log.debug("Top IDs after fusion", top_ids_count=len(top_ids_with_scores_tuples))

        chunks_with_content: List[RetrievedChunk] = []
        ids_needing_content: List[str] = []
        placeholder_map: Dict[str, RetrievedChunk] = {}

        for cid, fused_score_val in top_ids_with_scores_tuples:
            if not cid:
                 fetch_log.warning("Skipping invalid chunk ID found during fusion processing.")
                 continue
            
            original_chunk_from_dense = dense_map.get(cid)

            if original_chunk_from_dense and original_chunk_from_dense.content:
                # Crucial: Ensure embedding from dense result is preserved
                original_chunk_from_dense.score = fused_score_val 
                chunks_with_content.append(original_chunk_from_dense)
            else: 
                # If chunk was not in dense_map or had no content, it's a placeholder
                # Its embedding will be None unless it was in dense_map but content was missing.
                chunk_placeholder = RetrievedChunk(
                    id=cid,
                    score=fused_score_val, 
                    content=None, 
                    metadata=original_chunk_from_dense.metadata if original_chunk_from_dense and original_chunk_from_dense.metadata else {"retrieval_source": "sparse_or_fused_no_initial_meta"},
                    embedding=original_chunk_from_dense.embedding if original_chunk_from_dense and original_chunk_from_dense.embedding else None,
                    document_id=original_chunk_from_dense.document_id if original_chunk_from_dense else None,
                    file_name=original_chunk_from_dense.file_name if original_chunk_from_dense else None,
                    company_id=original_chunk_from_dense.company_id if original_chunk_from_dense else None
                )
                chunks_with_content.append(chunk_placeholder) 
                placeholder_map[cid] = chunk_placeholder 
                ids_needing_content.append(cid)

        if ids_needing_content and self.chunk_content_repo:
             fetch_log.info("Fetching content for chunks missing content", count=len(ids_needing_content))
             try:
                 content_map = await self.chunk_content_repo.get_chunk_contents_by_ids(ids_needing_content)
                 for cid_item, content_val in content_map.items():
                     if cid_item in placeholder_map: 
                          placeholder_map[cid_item].content = content_val
                          if placeholder_map[cid_item].metadata.get("retrieval_source") == "sparse_or_fused_no_initial_meta":
                            placeholder_map[cid_item].metadata["content_fetched_for_sparse"] = True
                          else:
                            placeholder_map[cid_item].metadata["content_fetched"] = True
                 missing_after_fetch = [cid_item_check for cid_item_check in ids_needing_content if cid_item_check not in content_map or not content_map[cid_item_check]]
                 if missing_after_fetch:
                      fetch_log.warning("Content not found or empty for some chunks after fetch", missing_ids=missing_after_fetch)
             except Exception as e_content_fetch:
                 fetch_log.exception("Failed to fetch content for fused results", error=str(e_content_fetch))
        elif ids_needing_content:
            fetch_log.warning("Cannot fetch content for sparse/fused results, ChunkContentRepository not available.")

        final_chunks_with_content = [c for c in chunks_with_content if c.content and c.content.strip()]
        fetch_log.debug("Chunks remaining after content check and fetch", count=len(final_chunks_with_content))
        
        final_chunks_with_content.sort(key=lambda c: c.score or 0.0, reverse=True)
        return final_chunks_with_content
        
    async def _handle_llm_response(
        self,
        json_answer_str: str,
        query: str,
        company_id: uuid.UUID,
        user_id: uuid.UUID,
        final_chat_id: uuid.UUID,
        original_chunks_for_citation: List[RetrievedChunk], 
        pipeline_stages_used: List[str],
        map_reduce_used: bool = False,
        retriever_k_effective: int = 0,
        fusion_fetch_k_effective: int = 0,
        num_chunks_after_rerank_or_fusion_fetch_effective: int = 0,
        num_final_chunks_sent_to_llm_effective: int = 0,
        num_history_messages_effective: int = 0
    ) -> Tuple[str, List[RetrievedChunk], Optional[uuid.UUID]]:
        
        llm_handler_log = log.bind(action="_handle_llm_response", chat_id=str(final_chat_id))
        answer_for_user: str
        retrieved_chunks_for_response: List[RetrievedChunk] = []
        assistant_sources_for_db: List[Dict[str, Any]] = []
        log_id: Optional[uuid.UUID] = None
        
        try:
            structured_answer_obj = RespuestaEstructurada.model_validate_json(json_answer_str)
            answer_for_user = structured_answer_obj.respuesta_detallada
            
            llm_handler_log.info("LLM response successfully parsed and validated into RespuestaEstructurada.",
                                 has_summary=bool(structured_answer_obj.resumen_ejecutivo),
                                 num_fuentes_citadas_by_llm=len(structured_answer_obj.fuentes_citadas),
                                 siguiente_pregunta_sugerida=structured_answer_obj.siguiente_pregunta_sugerida)

            assistant_sources_for_db = [f.model_dump(exclude_none=True) for f in structured_answer_obj.fuentes_citadas]
            
            map_chunk_id_to_original = {chunk.id: chunk for chunk in original_chunks_for_citation}
            
            processed_chunk_ids_for_response = set()

            for cited_source_by_llm in structured_answer_obj.fuentes_citadas:
                if cited_source_by_llm.id_documento and cited_source_by_llm.id_documento in map_chunk_id_to_original:
                    original_chunk = map_chunk_id_to_original[cited_source_by_llm.id_documento]
                    if original_chunk.id not in processed_chunk_ids_for_response:
                       retrieved_chunks_for_response.append(original_chunk)
                       processed_chunk_ids_for_response.add(original_chunk.id)

            if not retrieved_chunks_for_response and structured_answer_obj.fuentes_citadas:
                llm_handler_log.warning("LLM cited sources, but no direct match found by id_documento. Using filename as fallback or top N.")
                for cited_source_by_llm in structured_answer_obj.fuentes_citadas:
                    if len(retrieved_chunks_for_response) >= self.settings.NUM_SOURCES_TO_SHOW: break
                    found_by_name = False
                    for orig_chunk in original_chunks_for_citation:
                        if orig_chunk.id not in processed_chunk_ids_for_response and \
                           orig_chunk.file_name == cited_source_by_llm.nombre_archivo:
                             retrieved_chunks_for_response.append(orig_chunk)
                             processed_chunk_ids_for_response.add(orig_chunk.id)
                             found_by_name = True
                             break 
                    if not found_by_name:
                         llm_handler_log.info("LLM cited source not found by filename either", cited_source_name=cited_source_by_llm.nombre_archivo)
            
            if len(retrieved_chunks_for_response) < self.settings.NUM_SOURCES_TO_SHOW and original_chunks_for_citation:
                llm_handler_log.debug("Filling remaining source slots with top original chunks provided to LLM/MapReduce.")
                for chunk in original_chunks_for_citation:
                    if len(retrieved_chunks_for_response) >= self.settings.NUM_SOURCES_TO_SHOW: break
                    if chunk.id not in processed_chunk_ids_for_response:
                        retrieved_chunks_for_response.append(chunk)
                        processed_chunk_ids_for_response.add(chunk.id)


        except ValidationError as pydantic_err:
            llm_handler_log.error("LLM JSON response failed Pydantic validation", raw_response=truncate_text(json_answer_str, 500), errors=pydantic_err.errors())
            answer_for_user = "La respuesta del asistente no tuvo el formato esperado. Por favor, intenta de nuevo."
            assistant_sources_for_db = [{"error": "Pydantic validation failed", "details": pydantic_err.errors()}]
            retrieved_chunks_for_response = original_chunks_for_citation[:self.settings.NUM_SOURCES_TO_SHOW] 
        except json.JSONDecodeError as json_err:
            llm_handler_log.error("Failed to parse JSON response from LLM", raw_response=truncate_text(json_answer_str, 500), error=str(json_err))
            answer_for_user = f"Hubo un error al procesar la respuesta del asistente (JSON malformado): {truncate_text(json_answer_str,100)}. Por favor, intenta de nuevo."
            assistant_sources_for_db = [{"error": "JSON decode error", "details": str(json_err)}]
            retrieved_chunks_for_response = original_chunks_for_citation[:self.settings.NUM_SOURCES_TO_SHOW] 

        await self.chat_repo.save_message(
            chat_id=final_chat_id, role='assistant',
            content=answer_for_user, 
            sources=assistant_sources_for_db[:self.settings.NUM_SOURCES_TO_SHOW] if assistant_sources_for_db else None
        )
        llm_handler_log.info(f"Assistant message saved with up to {self.settings.NUM_SOURCES_TO_SHOW} sources.")

        try:
            docs_for_log_summary = [
                RetrievedDocumentSchema(**chunk.model_dump(exclude={'embedding'}, exclude_none=True)).model_dump(exclude_none=True) 
                for chunk in retrieved_chunks_for_response 
            ]
            log_metadata_details = {
                "pipeline_stages": pipeline_stages_used,
                "map_reduce_used": map_reduce_used,
                "retriever_k_initial": retriever_k_effective,
                "fusion_fetch_k": fusion_fetch_k_effective,
                "max_context_chunks_limit_for_llm": self.settings.MAX_CONTEXT_CHUNKS, 
                "num_chunks_after_rerank_or_fusion_content_fetch": num_chunks_after_rerank_or_fusion_fetch_effective,
                "num_final_chunks_sent_to_llm": num_final_chunks_sent_to_llm_effective,
                "num_sources_shown_to_user": len(assistant_sources_for_db), 
                "num_retrieved_docs_in_api_response": len(retrieved_chunks_for_response),
                "chat_history_messages_included_in_prompt": num_history_messages_effective,
                "diversity_filter_enabled_in_settings": self.settings.DIVERSITY_FILTER_ENABLED,
                "reranker_enabled_in_settings": self.settings.RERANKER_ENABLED,
                "bm25_enabled_in_settings": self.settings.BM25_ENABLED,
            }
            log_id = await self.log_repo.log_query_interaction(
                company_id=company_id, user_id=user_id, query=query, answer=answer_for_user,
                retrieved_documents_data=docs_for_log_summary, 
                chat_id=final_chat_id, metadata=log_metadata_details
            )
            llm_handler_log.info("Interaction logged successfully", db_log_id=str(log_id))
        except Exception as log_err_final:
            llm_handler_log.error("Failed to log RAG interaction", error=str(log_err_final), exc_info=False)

        return answer_for_user, retrieved_chunks_for_response, log_id


    async def execute(
        self, query: str, company_id: uuid.UUID, user_id: uuid.UUID,
        chat_id: Optional[uuid.UUID] = None, top_k: Optional[int] = None
    ) -> Tuple[str, List[RetrievedChunk], Optional[uuid.UUID], uuid.UUID]:
        
        exec_log = log.bind(use_case="AskQueryUseCase", company_id=str(company_id), user_id=str(user_id), query_preview=truncate_text(query, 50))
        retriever_k_effective = top_k if top_k is not None and 0 < top_k <= self.settings.RETRIEVER_TOP_K else self.settings.RETRIEVER_TOP_K
        exec_log = exec_log.bind(effective_retriever_k=retriever_k_effective)

        pipeline_stages_used: List[str] = []
        final_chat_id: uuid.UUID
        chat_history_str: Optional[str] = None
        history_messages: List[ChatMessage] = []

        try:
            if chat_id:
                if not await self.chat_repo.check_chat_ownership(chat_id, user_id, company_id):
                    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Chat not found or access denied.")
                final_chat_id = chat_id
                if self.settings.MAX_CHAT_HISTORY_MESSAGES > 0:
                    history_messages = await self.chat_repo.get_chat_messages(
                        chat_id=final_chat_id, user_id=user_id, company_id=company_id,
                        limit=self.settings.MAX_CHAT_HISTORY_MESSAGES, offset=0
                    )
                    chat_history_str = self._format_chat_history(history_messages)
                    exec_log.info("Chat history retrieved", num_messages=len(history_messages))
            else:
                initial_title = f"Chat: {truncate_text(query, 40)}"
                final_chat_id = await self.chat_repo.create_chat(user_id=user_id, company_id=company_id, title=initial_title)
            exec_log = exec_log.bind(chat_id=str(final_chat_id))
            await self.chat_repo.save_message(chat_id=final_chat_id, role='user', content=query)
            exec_log.info("Chat state managed and user message saved", is_new_chat=(not chat_id))

            if GREETING_REGEX.match(query):
                answer = "¡Hola! ¿En qué puedo ayudarte hoy con la información de tus documentos?"
                await self.chat_repo.save_message(chat_id=final_chat_id, role='assistant', content=answer, sources=None)
                exec_log.info("Greeting detected, responded directly.")
                simple_log_id = await self.log_repo.log_query_interaction(
                    user_id=user_id, company_id=company_id, query=query, answer=answer,
                    retrieved_documents_data=[], metadata={"type": "greeting"}, chat_id=final_chat_id
                )
                return answer, [], simple_log_id, final_chat_id

            pipeline_stages_used.append("query_embedding (remote)")
            query_embedding = await self._embed_query(query)

            pipeline_stages_used.append("dense_retrieval (milvus)")
            dense_task = self.vector_store.search(query_embedding, str(company_id), retriever_k_effective)
            
            sparse_results_tuples: List[Tuple[str, float]] = [] 
            sparse_task_placeholder = asyncio.create_task(asyncio.sleep(0)) 

            if self.sparse_retriever: 
                 pipeline_stages_used.append("sparse_retrieval (remote_sparse_search_service)")
                 sparse_task = self.sparse_retriever.search(query, company_id, retriever_k_effective)
            else:
                 pipeline_stages_used.append(f"sparse_retrieval ({'disabled_no_adapter_instance' if self.settings.BM25_ENABLED else 'disabled_in_settings'})")
                 sparse_task = sparse_task_placeholder 

            dense_chunks_domain, sparse_results_maybe_tuples = await asyncio.gather(dense_task, sparse_task)
            
            if self.sparse_retriever and isinstance(sparse_results_maybe_tuples, list):
                sparse_results_tuples = sparse_results_maybe_tuples 
            
            exec_log.info("Retrieval phase done", dense_count=len(dense_chunks_domain), sparse_count=len(sparse_results_tuples))

            pipeline_stages_used.append("fusion (rrf)")
            dense_map = {c.id: c for c in dense_chunks_domain if c.id and c.embedding is not None} # Ensure embedding exists for dense_map entries used in MMR
            
            fused_scores: Dict[str, float] = self._reciprocal_rank_fusion(dense_chunks_domain, sparse_results_tuples)
            
            fusion_fetch_k_effective = self.settings.MAX_CONTEXT_CHUNKS 
            if self.settings.RERANKER_ENABLED or self.settings.DIVERSITY_FILTER_ENABLED : 
                 fusion_fetch_k_effective = max(self.settings.MAX_CONTEXT_CHUNKS + 20, int(self.settings.MAX_CONTEXT_CHUNKS * 1.2) ) 
            
            combined_chunks_with_content: List[RetrievedChunk] = await self._fetch_content_for_fused_results(
                fused_scores, dense_map, fusion_fetch_k_effective
            )
            exec_log.info("Fusion & content fetch done", fused_results_count=len(combined_chunks_with_content), fetch_k=fusion_fetch_k_effective)
            num_chunks_after_fusion_fetch = len(combined_chunks_with_content)

            if not combined_chunks_with_content:
                exec_log.warning("No chunks with content after fusion/fetch. Using general prompt.")
                general_prompt = await self._build_prompt(query, [], chat_history=chat_history_str, builder=self._prompt_builder_general)
                answer_str = await self.llm.generate(general_prompt) 
                
                await self.chat_repo.save_message(chat_id=final_chat_id, role='assistant', content=answer_str, sources=None)
                no_docs_log_id = await self.log_repo.log_query_interaction(
                    company_id=company_id, user_id=user_id, query=query, answer=answer_str, 
                    retrieved_documents_data=[], chat_id=final_chat_id, 
                    metadata={"pipeline_stages": pipeline_stages_used, "result_type": "no_docs_for_rag", "map_reduce_used": False}
                )
                return answer_str, [], no_docs_log_id, final_chat_id
            
            chunks_for_llm_or_mapreduce = combined_chunks_with_content 
            if self.settings.RERANKER_ENABLED and chunks_for_llm_or_mapreduce:
                pipeline_stages_used.append("reranking (remote_reranker_service)")
                rerank_log = exec_log.bind(action="rerank_remote", num_chunks_to_rerank=len(chunks_for_llm_or_mapreduce))
                
                documents_for_reranker = []
                map_id_to_original_chunk_before_rerank = {c.id: c for c in chunks_for_llm_or_mapreduce}


                for chk_id, original_chunk_obj in map_id_to_original_chunk_before_rerank.items():
                    if original_chunk_obj.content and original_chunk_obj.id: 
                        documents_for_reranker.append({
                            "id": original_chunk_obj.id,
                            "text": original_chunk_obj.content, 
                            "metadata": original_chunk_obj.metadata or {} 
                        })
                
                if documents_for_reranker:
                    reranker_payload = {
                        "query": query,
                        "documents": documents_for_reranker,
                        "top_n": self.settings.MAX_CONTEXT_CHUNKS 
                    }
                    try:
                        rerank_log.debug("Sending request to reranker service...")
                        base_reranker_url = str(settings.RERANKER_SERVICE_URL).rstrip('/')
                        reranker_url = f"{base_reranker_url}/api/v1/rerank"
                        if base_reranker_url.endswith("/api/v1"):
                            reranker_url = f"{base_reranker_url.rsplit('/api/v1',1)[0]}/api/v1/rerank"
                        elif base_reranker_url.endswith("/api"): 
                            reranker_url = f"{base_reranker_url.rsplit('/api',1)[0]}/api/v1/rerank"


                        rerank_log.debug(f"Final Reranker URL: {reranker_url}")

                        reranker_response = await self.http_client.post(
                            reranker_url,
                            json=reranker_payload,
                            timeout=self.settings.RERANKER_CLIENT_TIMEOUT
                        )
                        reranker_response.raise_for_status()
                        reranked_data = reranker_response.json()

                        if "data" in reranked_data and "reranked_documents" in reranked_data["data"]:
                            reranked_docs_from_service = reranked_data["data"]["reranked_documents"]
                                                        
                            updated_reranked_chunks = []
                            for reranked_item_data in reranked_docs_from_service: 
                                chunk_id = reranked_item_data.get("id")
                                new_score = reranked_item_data.get("score")
                                
                                if chunk_id in map_id_to_original_chunk_before_rerank:
                                    original_retrieved_chunk = map_id_to_original_chunk_before_rerank[chunk_id]
                                    
                                    updated_chunk = RetrievedChunk(
                                        id=original_retrieved_chunk.id,
                                        content=original_retrieved_chunk.content, 
                                        score=new_score, 
                                        metadata={
                                            **(original_retrieved_chunk.metadata or {}),
                                            **(reranked_item_data.get("metadata", {})), 
                                            "reranked_score": new_score 
                                        },
                                        embedding=original_retrieved_chunk.embedding, 
                                        document_id=original_retrieved_chunk.document_id,
                                        file_name=original_retrieved_chunk.file_name,
                                        company_id=original_retrieved_chunk.company_id
                                    )
                                    updated_reranked_chunks.append(updated_chunk)
                                else:
                                    rerank_log.warning("Reranked chunk ID not found in original map.", reranked_id=chunk_id)

                            if updated_reranked_chunks: 
                                chunks_for_llm_or_mapreduce = updated_reranked_chunks
                                rerank_log.info(f"Reranking successful. {len(chunks_for_llm_or_mapreduce)} chunks after reranking.")
                            else:
                                rerank_log.warning("Reranking seemed successful but no chunks could be mapped back. Using pre-reranked chunks.")
                        else:
                            rerank_log.warning("Reranker service response format invalid.", response_data=reranked_data)
                    except httpx.HTTPStatusError as http_err:
                        rerank_log.error("HTTP error from Reranker service", status_code=http_err.response.status_code, response_text=http_err.response.text, exc_info=False)
                    except httpx.RequestError as req_err: 
                        rerank_log.error("Request error contacting Reranker service", error_details=str(req_err), exc_info=False) 
                    except Exception as e_rerank:
                        rerank_log.exception("Unexpected error during reranking call.")
                else:
                    rerank_log.warning("No valid documents with content/id to send for reranking.")

            else: 
                pipeline_stages_used.append(f"reranking ({'disabled' if not self.settings.RERANKER_ENABLED else 'skipped_no_chunks_or_content'})")
            
            num_chunks_after_rerank = len(chunks_for_llm_or_mapreduce)

            # --- BEGIN CORRECTION FOR MMRDiversityFilter EMBEDDING ---
            # Populate embeddings for chunks before sending to MMRDiversityFilter
            if chunks_for_llm_or_mapreduce and self.diversity_filter : # Only if there are chunks and filter is active
                pipeline_stages_used.append("embedding_population_for_mmr")
                mmr_prep_log = exec_log.bind(action="mmr_embedding_population")
                
                chunk_ids_for_mmr_filter = [doc.id for doc in chunks_for_llm_or_mapreduce if doc.id]
                mmr_prep_log.debug("Preparing embeddings for MMR", num_chunks_input=len(chunks_for_llm_or_mapreduce), num_ids_to_fetch=len(chunk_ids_for_mmr_filter))

                vectors_from_milvus_by_id: Dict[str, List[float]] = {}
                if chunk_ids_for_mmr_filter:
                    try:
                        vectors_from_milvus_by_id = await self.vector_store.fetch_vectors_by_ids(chunk_ids_for_mmr_filter)
                        mmr_prep_log.info(f"Fetched {len(vectors_from_milvus_by_id)} vectors from Milvus for MMR.")
                    except Exception as e_milvus_fetch:
                         mmr_prep_log.error("Failed to fetch vectors from Milvus for MMR, fallback may occur.", error=str(e_milvus_fetch))

                texts_needing_embedding_generation: List[str] = []
                # Keep a mapping from original index of text to chunk object to update it later
                map_text_index_to_chunk_obj: Dict[int, RetrievedChunk] = {} 
                
                # Create a new list for chunks that will go to MMR, preserving original order as much as possible
                # but ensuring they are reconstructed with the embedding field.
                chunks_ready_for_mmr: List[RetrievedChunk] = []

                for original_chunk in chunks_for_llm_or_mapreduce:
                    retrieved_embedding = vectors_from_milvus_by_id.get(original_chunk.id)
                    
                    if retrieved_embedding is None and original_chunk.content: # If Milvus didn't have it, AND content exists
                        if original_chunk.embedding is None: # Only add to fallback if no embedding was ever present
                            map_text_index_to_chunk_obj[len(texts_needing_embedding_generation)] = original_chunk
                            texts_needing_embedding_generation.append(original_chunk.content)
                            
                    # Reconstruct the chunk. If embedding was already there (e.g. from dense_map earlier),
                    # or fetched from Milvus now, it will be included. Otherwise, it's None for now.
                    chunks_ready_for_mmr.append(
                        RetrievedChunk(
                            id=original_chunk.id,
                            content=original_chunk.content,
                            score=original_chunk.score,
                            metadata=original_chunk.metadata,
                            embedding=retrieved_embedding if retrieved_embedding else original_chunk.embedding, # Prioritize fresh Milvus fetch, then existing
                            document_id=original_chunk.document_id,
                            file_name=original_chunk.file_name,
                            company_id=original_chunk.company_id
                        )
                    )
                
                if texts_needing_embedding_generation:
                    mmr_prep_log.info(f"Requesting {len(texts_needing_embedding_generation)} missing embeddings from embedding_adapter for MMR.")
                    try:
                        # Call the embedding adapter to get embeddings for texts
                        generated_embeddings_list = await self.embedding_adapter.embed_texts(texts_needing_embedding_generation)
                        
                        if len(generated_embeddings_list) == len(texts_needing_embedding_generation):
                            for idx, generated_emb_vector in enumerate(generated_embeddings_list):
                                chunk_to_update = map_text_index_to_chunk_obj[idx] 
                                # Find the corresponding chunk in chunks_ready_for_mmr and update its embedding
                                for ch_ready in chunks_ready_for_mmr:
                                    if ch_ready.id == chunk_to_update.id:
                                        ch_ready.embedding = generated_emb_vector
                                        break
                            mmr_prep_log.info("Successfully updated chunks with newly generated embeddings for MMR.")
                        else:
                            mmr_prep_log.error("Mismatch in count of generated embeddings and requested texts for MMR fallback.",
                                               requested_count=len(texts_needing_embedding_generation),
                                               generated_count=len(generated_embeddings_list))
                    except Exception as e_embed_fallback:
                        mmr_prep_log.error("Failed to generate embeddings via adapter for MMR fallback.", error=str(e_embed_fallback))
                
                # The list chunks_for_llm_or_mapreduce now contains chunks with embeddings populated
                chunks_for_llm_or_mapreduce = chunks_ready_for_mmr
            # --- END CORRECTION FOR MMRDiversityFilter EMBEDDING ---


            # Log para depurar embeddings antes del filtro MMR
            num_with_embeddings_before_mmr = sum(1 for c_chk in chunks_for_llm_or_mapreduce if c_chk.embedding is not None)
            exec_log.debug(
                "Chunks before diversity filter",
                total_chunks=len(chunks_for_llm_or_mapreduce),
                chunks_with_embeddings=num_with_embeddings_before_mmr,
                first_few_ids_and_embedding_status=[(c.id, c.embedding is not None) for c in chunks_for_llm_or_mapreduce[:min(5, len(chunks_for_llm_or_mapreduce))]]
            )

            if self.diversity_filter and chunks_for_llm_or_mapreduce:
                 k_final_diversity = self.settings.MAX_CONTEXT_CHUNKS 
                 filter_type = type(self.diversity_filter).__name__
                 pipeline_stages_used.append(f"diversity_filter ({filter_type})")
                 exec_log.debug(f"Applying {filter_type} k={k_final_diversity}...", count=len(chunks_for_llm_or_mapreduce))
                 chunks_for_llm_or_mapreduce = await self.diversity_filter.filter(chunks_for_llm_or_mapreduce, k_final_diversity)
                 exec_log.info(f"{filter_type} applied.", final_count=len(chunks_for_llm_or_mapreduce))
            else: 
                 pipeline_stages_used.append(f"diversity_filter ({'disabled' if not self.settings.DIVERSITY_FILTER_ENABLED else 'skipped_no_chunks'})")
                 chunks_for_llm_or_mapreduce = chunks_for_llm_or_mapreduce[:self.settings.MAX_CONTEXT_CHUNKS]
                 exec_log.info(f"Diversity filter not applied or no chunks. Truncating to MAX_CONTEXT_CHUNKS.", 
                               count=len(chunks_for_llm_or_mapreduce), limit=self.settings.MAX_CONTEXT_CHUNKS)

            final_chunks_for_processing = [c for c in chunks_for_llm_or_mapreduce if c.content and c.content.strip()]
            num_final_chunks_for_llm_or_mapreduce = len(final_chunks_for_processing)

            if not final_chunks_for_processing: 
                exec_log.warning("No chunks with content after reranking/filtering. Using general prompt.")
                general_prompt = await self._build_prompt(query, [], chat_history=chat_history_str, builder=self._prompt_builder_general)
                answer_str = await self.llm.generate(general_prompt, response_pydantic_schema=None)
                await self.chat_repo.save_message(chat_id=final_chat_id, role='assistant', content=answer_str, sources=None)
                no_docs_final_log_id = await self.log_repo.log_query_interaction(
                    company_id=company_id, user_id=user_id, query=query, answer=answer_str, 
                    retrieved_documents_data=[], chat_id=final_chat_id, 
                    metadata={"pipeline_stages": pipeline_stages_used, "result_type": "no_docs_after_postprocessing", "map_reduce_used": False}
                )
                return answer_str, [], no_docs_final_log_id, final_chat_id

            map_reduce_active = False
            json_answer_str: str

            if self.settings.MAPREDUCE_ENABLED and len(final_chunks_for_processing) > self.settings.MAPREDUCE_ACTIVATION_THRESHOLD:
                exec_log.info(f"Activating MapReduce. Chunks: {len(final_chunks_for_processing)}, Threshold: {self.settings.MAPREDUCE_ACTIVATION_THRESHOLD}", flow_type="MapReduce")
                pipeline_stages_used.append("map_reduce_flow")
                map_reduce_active = True

                pipeline_stages_used.append("map_phase")
                mapped_responses_parts = []
                haystack_docs_for_map = [
                    Document(
                        id=c.id, 
                        content=c.content, 
                        meta={ 
                            "file_name": c.file_name, 
                            "page": c.metadata.get("page"), 
                            "title": c.metadata.get("title") 
                        },
                        score=c.score
                    ) for c in final_chunks_for_processing
                ]
                
                map_tasks = []
                for i in range(0, len(haystack_docs_for_map), self.settings.MAPREDUCE_CHUNK_BATCH_SIZE):
                    batch_docs = haystack_docs_for_map[i:i + self.settings.MAPREDUCE_CHUNK_BATCH_SIZE]
                    map_prompt_data = {
                        "original_query": query, 
                        "documents": batch_docs, 
                        "document_index": i, 
                        "total_documents": len(haystack_docs_for_map) 
                    }
                    map_prompt_str_task = self._build_prompt(query="", documents=[], prompt_data_override=map_prompt_data, builder=self._prompt_builder_map)
                    map_tasks.append(map_prompt_str_task)
                
                map_prompts = await asyncio.gather(*map_tasks)
                
                llm_map_tasks = []
                for idx, map_prompt in enumerate(map_prompts):
                    llm_map_tasks.append(self.llm.generate(map_prompt, response_pydantic_schema=None)) 
                
                map_phase_results = await asyncio.gather(*[asyncio.shield(task) for task in llm_map_tasks], return_exceptions=True)

                for idx, result in enumerate(map_phase_results):
                    map_log = exec_log.bind(map_batch_index=idx)
                    if isinstance(result, Exception):
                        map_log.error("LLM call failed for map batch", error=str(result), exc_info=True)
                    elif result and MAP_REDUCE_NO_RELEVANT_INFO not in result:
                        mapped_responses_parts.append(f"--- Extracto del Lote de Documentos {idx + 1} ---\n{result}\n")
                        map_log.info("Map request processed for batch.", response_length=len(result), is_relevant=True)
                    else:
                         map_log.info("Map request processed for batch, no relevant info found by LLM.", response_length=len(result or ""))


                if not mapped_responses_parts:
                    exec_log.warning("MapReduce: All map steps reported no relevant information or failed.")
                    concatenated_mapped_responses = "Todos los fragmentos procesados indicaron no tener información relevante para la consulta o hubo errores en su procesamiento."
                else:
                    concatenated_mapped_responses = "\n".join(mapped_responses_parts)

                pipeline_stages_used.append("reduce_phase")
                reduce_prompt_data = {
                    "original_query": query,
                    "chat_history": chat_history_str,
                    "mapped_responses": concatenated_mapped_responses,
                    "original_documents_for_citation": haystack_docs_for_map 
                }
                reduce_prompt_str = await self._build_prompt(query="", documents=[], prompt_data_override=reduce_prompt_data, builder=self._prompt_builder_reduce)
                
                exec_log.info("Sending reduce request to LLM for final JSON response.")
                json_answer_str = await self.llm.generate(reduce_prompt_str, response_pydantic_schema=RespuestaEstructurada)

            else: 
                exec_log.info(f"Using Direct RAG strategy. Chunks: {len(final_chunks_for_processing)}", flow_type="DirectRAG")
                pipeline_stages_used.append("direct_rag_flow")
                
                haystack_docs_for_prompt = [
                    Document(
                        id=c.id, 
                        content=c.content, 
                        meta={ 
                            "file_name": c.file_name, 
                            "page": c.metadata.get("page"),
                            "title": c.metadata.get("title")
                        },
                        score=c.score
                    ) for c in final_chunks_for_processing
                ]
                direct_rag_prompt = await self._build_prompt(query, haystack_docs_for_prompt, chat_history_str, builder=self._prompt_builder_rag)
                
                exec_log.info("Sending direct RAG request to LLM for JSON response.")
                json_answer_str = await self.llm.generate(direct_rag_prompt, response_pydantic_schema=RespuestaEstructurada)

            answer_text, relevant_chunks_for_api, final_log_id = await self._handle_llm_response(
                json_answer_str=json_answer_str,
                query=query,
                company_id=company_id,
                user_id=user_id,
                final_chat_id=final_chat_id,
                original_chunks_for_citation=final_chunks_for_processing, 
                pipeline_stages_used=pipeline_stages_used,
                map_reduce_used=map_reduce_active,
                retriever_k_effective=retriever_k_effective,
                fusion_fetch_k_effective=fusion_fetch_k_effective,
                num_chunks_after_rerank_or_fusion_fetch_effective=num_chunks_after_rerank, 
                num_final_chunks_sent_to_llm_effective=num_final_chunks_for_llm_or_mapreduce,
                num_history_messages_effective=len(history_messages)
            )
            
            exec_log.info("Use case execution finished successfully.")
            return answer_text, relevant_chunks_for_api, final_log_id, final_chat_id

        except ConnectionError as ce: 
            exec_log.error("Connection error during use case execution", error=str(ce), exc_info=False)
            detail_message = "A required external service is unavailable. Please try again later."
            if "Embedding service" in str(ce): detail_message = "The embedding service is currently unavailable."
            elif "Reranker service" in str(ce): detail_message = "The reranking service is currently unavailable."
            elif "Sparse search service" in str(ce): detail_message = "The sparse search service is currently unavailable."
            elif "Gemini API" in str(ce): detail_message = "The language model service (Gemini) is currently unavailable."
            elif "Vector DB" in str(ce): detail_message = "The vector database service is currently unavailable."
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=detail_message) from ce
        except ValueError as ve: 
            exec_log.error("Value error during use case execution", error=str(ve), exc_info=True) 
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Data processing error: {ve}") from ve
        except HTTPException as http_exc: 
            exec_log.warning("HTTPException caught in use case", status_code=http_exc.status_code, detail=http_exc.detail)
            raise http_exc
        except Exception as e: 
            exec_log.exception("Unexpected error during use case execution") 
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"An internal server error occurred: {type(e).__name__}. Please contact support if this persists.") from e