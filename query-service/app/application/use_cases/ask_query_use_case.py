# query-service/app/application/use_cases/ask_query_use_case.py
import structlog
import asyncio
import uuid
import re
from typing import Dict, Any, List, Tuple, Optional, Type, Set
from datetime import datetime, timezone, timedelta
import httpx

# Import Ports and Domain Models
from app.application.ports import (
    ChatRepositoryPort, LogRepositoryPort, VectorStorePort, LLMPort,
    SparseRetrieverPort, DiversityFilterPort, ChunkContentRepositoryPort,
    EmbeddingPort, RerankerPort # RerankerPort might be removed if not used as interface
)
from app.domain.models import RetrievedChunk, ChatMessage


try:
    import bm2s
except ImportError:
    bm2s = None

from haystack.components.builders.prompt_builder import PromptBuilder
from haystack import Document

from app.core.config import settings
from app.api.v1.schemas import RetrievedDocument as RetrievedDocumentSchema # For logging
from app.utils.helpers import truncate_text
from fastapi import HTTPException, status

log = structlog.get_logger(__name__)

GREETING_REGEX = re.compile(r"^\s*(hola|hello|hi|buenos días|buenas tardes|buenas noches|hey|qué tal|hi there)\s*[\.,!?]*\s*$", re.IGNORECASE)
RRF_K = 60

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
                 # Optional components
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
        self.sparse_retriever = sparse_retriever if settings.BM25_ENABLED and bm2s is not None else None
        self.chunk_content_repo = chunk_content_repo
        self.diversity_filter = diversity_filter if settings.DIVERSITY_FILTER_ENABLED else None

        self._prompt_builder_rag = self._initialize_prompt_builder(settings.RAG_PROMPT_TEMPLATE)
        self._prompt_builder_general = self._initialize_prompt_builder(settings.GENERAL_PROMPT_TEMPLATE)

        log.info("AskQueryUseCase Initialized",
                 embedding_adapter_type=type(self.embedding_adapter).__name__,
                 bm25_enabled=bool(self.sparse_retriever),
                 reranker_enabled=self.settings.RERANKER_ENABLED,
                 reranker_service_url=str(self.settings.RERANKER_SERVICE_URL) if self.settings.RERANKER_ENABLED else "N/A",
                 diversity_filter_enabled=settings.DIVERSITY_FILTER_ENABLED,
                 diversity_filter_type=type(self.diversity_filter).__name__ if self.diversity_filter else "None"
                 )
        if settings.BM25_ENABLED and bm2s is None:
             log.error("BM25_ENABLED is true in settings, but 'bm2s' library is not installed. Sparse retrieval will be skipped.")
        if self.sparse_retriever and not self.chunk_content_repo:
            log.error("SparseRetriever is enabled but ChunkContentRepositoryPort is missing!")


    def _initialize_prompt_builder(self, template: str) -> PromptBuilder:
        log.debug("Initializing PromptBuilder...", template_start=template[:100]+"...")
        return PromptBuilder(template=template)

    async def _embed_query(self, query: str) -> List[float]:
        embed_log = log.bind(action="_embed_query_use_case_call_remote")
        try:
            embedding = await self.embedding_adapter.embed_query(query)
            if not embedding or not isinstance(embedding, list) or not all(isinstance(f, float) for f in embedding):
                embed_log.error("Invalid embedding received from adapter", received_embedding_type=type(embedding).__name__)
                raise ValueError("Embedding adapter returned invalid or empty vector.")
            embed_log.debug("Query embedded successfully via remote adapter", vector_dim=len(embedding))
            return embedding
        except ConnectionError as e: # Specific error from adapter if service is down
            embed_log.error("Embedding failed: Connection to embedding service failed.", error=str(e), exc_info=False)
            raise ConnectionError(f"Embedding service error: {e}") from e
        except ValueError as e: # Specific error from adapter for bad response
            embed_log.error("Embedding failed: Invalid data from embedding service.", error=str(e), exc_info=False)
            raise ValueError(f"Embedding service data error: {e}") from e
        except Exception as e: # Catch-all for other unexpected issues
            embed_log.error("Unexpected error during query embedding via adapter", error=str(e), exc_info=True)
            raise ConnectionError(f"Unexpected error contacting embedding service: {e}") from e


    def _format_chat_history(self, messages: List[ChatMessage]) -> str:
        if not messages:
            return ""
        history_str = []
        for msg in reversed(messages): # Show newest first in internal processing, then reverse for prompt
            role = "Usuario" if msg.role == 'user' else "Atenex"
            time_mark = format_time_delta(msg.created_at)
            history_str.append(f"{role} ({time_mark}): {msg.content}")
        # The prompt template expects history oldest to newest
        return "\n".join(reversed(history_str))

    async def _build_prompt(self, query: str, documents: List[Document], chat_history: Optional[str] = None) -> str:
        builder_log = log.bind(action="build_prompt_use_case", num_docs=len(documents), history_included=bool(chat_history))
        prompt_data = {"query": query}
        try:
            if documents:
                prompt_builder = self._prompt_builder_rag
                prompt_data["documents"] = documents
                builder_log.debug("Using RAG prompt template.")
            else:
                prompt_builder = self._prompt_builder_general
                builder_log.debug("Using General prompt template.")

            if chat_history:
                prompt_data["chat_history"] = chat_history

            result = await asyncio.to_thread(prompt_builder.run, **prompt_data)
            prompt = result.get("prompt")
            if not prompt: raise ValueError("Prompt generation returned empty.")

            builder_log.debug("Prompt built successfully.")
            return prompt
        except Exception as e:
            builder_log.error("Prompt building failed", error=str(e), exc_info=True)
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

        sorted_chunk_ids = sorted(fused_scores.items(), key=lambda item: item[1], reverse=True)
        top_ids = [cid for cid, score in sorted_chunk_ids[:top_n]]
        fetch_log.debug("Top IDs after fusion", top_ids_count=len(top_ids))

        chunks_with_content: List[RetrievedChunk] = []
        ids_needing_content: List[str] = []
        final_scores: Dict[str, float] = dict(sorted_chunk_ids[:top_n])
        placeholder_map: Dict[str, RetrievedChunk] = {}

        for cid in top_ids:
            if not cid:
                 fetch_log.warning("Skipping invalid chunk ID found during fusion processing.")
                 continue
            if cid in dense_map and dense_map[cid].content:
                chunk = dense_map[cid]
                chunk.score = final_scores[cid] # Update score with fused score
                chunks_with_content.append(chunk)
            else: # Chunk from sparse results or dense results missing content
                # Create a placeholder, content will be fetched
                # Preserve original metadata if available (e.g., from dense_map but content was None)
                original_metadata = dense_map[cid].metadata if cid in dense_map and dense_map[cid].metadata else {"retrieval_source": "sparse/fused"}
                document_id = dense_map[cid].document_id if cid in dense_map and dense_map[cid].document_id else None
                file_name = dense_map[cid].file_name if cid in dense_map and dense_map[cid].file_name else None
                company_id_val = dense_map[cid].company_id if cid in dense_map and dense_map[cid].company_id else None
                
                chunk_placeholder = RetrievedChunk(
                    id=cid,
                    score=final_scores[cid],
                    content=None, # To be fetched
                    metadata=original_metadata,
                    document_id=document_id,
                    file_name=file_name,
                    company_id=company_id_val
                )
                chunks_with_content.append(chunk_placeholder)
                placeholder_map[cid] = chunk_placeholder # Keep track for content update
                ids_needing_content.append(cid)


        if ids_needing_content and self.chunk_content_repo:
             fetch_log.info("Fetching content for chunks missing content", count=len(ids_needing_content))
             try:
                 content_map = await self.chunk_content_repo.get_chunk_contents_by_ids(ids_needing_content)
                 for cid_item, content_val in content_map.items():
                     if cid_item in placeholder_map:
                          placeholder_map[cid_item].content = content_val
                          placeholder_map[cid_item].metadata["content_fetched"] = True # Mark as fetched
                 missing_after_fetch = [cid_item_check for cid_item_check in ids_needing_content if cid_item_check not in content_map or not content_map[cid_item_check]]
                 if missing_after_fetch:
                      fetch_log.warning("Content not found or empty for some chunks after fetch", missing_ids=missing_after_fetch)
             except Exception as e_content_fetch:
                 fetch_log.exception("Failed to fetch content for fused results", error=str(e_content_fetch))
        elif ids_needing_content:
            fetch_log.warning("Cannot fetch content for sparse/fused results, ChunkContentRepository not available.")

        # Filter out any chunks that still don't have content after attempting fetch
        final_chunks = [c for c in chunks_with_content if c.content and c.content.strip()]
        fetch_log.debug("Chunks remaining after content check and fetch", count=len(final_chunks))
        # Re-sort by score as the list might have been reordered during content fetching
        final_chunks.sort(key=lambda c: c.score or 0.0, reverse=True)
        return final_chunks

    async def execute(
        self, query: str, company_id: uuid.UUID, user_id: uuid.UUID,
        chat_id: Optional[uuid.UUID] = None, top_k: Optional[int] = None
    ) -> Tuple[str, List[RetrievedChunk], Optional[uuid.UUID], uuid.UUID]:
        exec_log = log.bind(use_case="AskQueryUseCase", company_id=str(company_id), user_id=str(user_id), query=truncate_text(query, 50))
        retriever_k = top_k if top_k is not None and 0 < top_k <= self.settings.RETRIEVER_TOP_K else self.settings.RETRIEVER_TOP_K
        exec_log = exec_log.bind(effective_retriever_k=retriever_k)

        pipeline_stages_used = ["remote_embedding", "dense_retrieval", "llm_generation"]
        final_chat_id: uuid.UUID
        log_id: Optional[uuid.UUID] = None
        chat_history_str: Optional[str] = None
        history_messages: List[ChatMessage] = []

        try:
            if chat_id:
                if not await self.chat_repo.check_chat_ownership(chat_id, user_id, company_id):
                    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Chat not found or access denied.")
                final_chat_id = chat_id
                if self.settings.MAX_CHAT_HISTORY_MESSAGES > 0:
                    try:
                        history_messages = await self.chat_repo.get_chat_messages(
                            chat_id=final_chat_id, user_id=user_id, company_id=company_id,
                            limit=self.settings.MAX_CHAT_HISTORY_MESSAGES, offset=0
                        )
                        chat_history_str = self._format_chat_history(history_messages)
                        exec_log.info("Chat history retrieved and formatted", num_messages=len(history_messages))
                    except Exception as hist_err:
                        exec_log.error("Failed to retrieve chat history", error=str(hist_err)) # Non-critical, proceed without history
            else:
                initial_title = f"Chat: {truncate_text(query, 40)}"
                final_chat_id = await self.chat_repo.create_chat(user_id=user_id, company_id=company_id, title=initial_title)
            exec_log = exec_log.bind(chat_id=str(final_chat_id))
            exec_log.info("Chat state managed", is_new=(not chat_id))

            await self.chat_repo.save_message(chat_id=final_chat_id, role='user', content=query)

            if GREETING_REGEX.match(query):
                answer = "¡Hola! ¿En qué puedo ayudarte hoy con la información de tus documentos?"
                await self.chat_repo.save_message(chat_id=final_chat_id, role='assistant', content=answer, sources=None)
                exec_log.info("Use case finished (greeting).")
                return answer, [], log_id, final_chat_id

            exec_log.info("Proceeding with RAG pipeline...")
            query_embedding = await self._embed_query(query) # Uses remote embedding adapter

            dense_task = self.vector_store.search(query_embedding, str(company_id), retriever_k)
            sparse_task = asyncio.create_task(asyncio.sleep(0)) # Placeholder if sparse is disabled
            sparse_results: List[Tuple[str, float]] = []

            if self.sparse_retriever:
                 pipeline_stages_used.append("sparse_retrieval (bm2s)")
                 sparse_task = self.sparse_retriever.search(query, str(company_id), retriever_k)
            elif self.settings.BM25_ENABLED: # Log if enabled but instance not available
                 pipeline_stages_used.append("sparse_retrieval (skipped_no_lib_or_init_fail)")
                 exec_log.warning("BM25 enabled in settings but sparse_retriever instance is not available.")
            else: # Log if explicitly disabled
                 pipeline_stages_used.append("sparse_retrieval (disabled_in_settings)")


            dense_chunks, sparse_results_maybe = await asyncio.gather(dense_task, sparse_task)
            if self.sparse_retriever and isinstance(sparse_results_maybe, list): # Ensure sparse_results_maybe is the actual result
                sparse_results = sparse_results_maybe
            exec_log.info("Retrieval phase completed", dense_count=len(dense_chunks), sparse_count=len(sparse_results), retriever_k=retriever_k)


            pipeline_stages_used.append("fusion (rrf)")
            dense_map = {c.id: c for c in dense_chunks if c.id} # Map for easy lookup
            fused_scores = self._reciprocal_rank_fusion(dense_chunks, sparse_results)
            fusion_fetch_k = self.settings.MAX_CONTEXT_CHUNKS + 10 # Fetch slightly more for reranking/filtering
            combined_chunks_with_content = await self._fetch_content_for_fused_results(fused_scores, dense_map, fusion_fetch_k)
            exec_log.info("Fusion & Content Fetch completed", initial_fused_count=len(fused_scores), chunks_with_content=len(combined_chunks_with_content), fetch_limit=fusion_fetch_k)


            if not combined_chunks_with_content:
                 exec_log.warning("No chunks with content available after fusion/fetch for RAG.")
                 # Proceed to LLM without documents
                 final_prompt = await self._build_prompt(query, [], chat_history=chat_history_str)
                 answer = await self.llm.generate(final_prompt)
                 await self.chat_repo.save_message(chat_id=final_chat_id, role='assistant', content=answer, sources=None)
                 try: # Best effort logging
                    log_id = await self.log_repo.log_query_interaction(company_id=company_id, user_id=user_id, query=query, answer=answer, retrieved_documents_data=[], chat_id=final_chat_id, metadata={"pipeline_stages": pipeline_stages_used, "result": "no_docs_found_after_fusion"})
                 except Exception as log_exc_no_docs: exec_log.error("Failed to log interaction for no_docs_after_fusion case", error=str(log_exc_no_docs))
                 return answer, [], log_id, final_chat_id

            chunks_to_process_further = combined_chunks_with_content

            # --- Remote Reranking Step ---
            if self.settings.RERANKER_ENABLED:
                rerank_log = exec_log.bind(action="remote_rerank")
                rerank_log.debug(f"Performing remote reranking with {self.settings.RERANKER_SERVICE_URL}...", count=len(chunks_to_process_further))
                
                # Prepare payload for reranker service
                documents_to_rerank_payload = []
                for doc_chunk in chunks_to_process_further:
                    if doc_chunk.content and doc_chunk.id: # Ensure content and ID exist
                        documents_to_rerank_payload.append(
                            {"id": doc_chunk.id, "text": doc_chunk.content, "metadata": doc_chunk.metadata or {}}
                        )
                
                if documents_to_rerank_payload:
                    reranker_request_payload = {
                        "query": query,
                        "documents": documents_to_rerank_payload,
                        "top_n": self.settings.MAX_CONTEXT_CHUNKS + 5 # Ask reranker for slightly more than needed for LLM
                    }
                    try:
                        response = await self.http_client.post(
                            str(self.settings.RERANKER_SERVICE_URL).rstrip('/') + "/api/v1/rerank",
                            json=reranker_request_payload,
                            timeout=self.settings.RERANKER_CLIENT_TIMEOUT
                        )
                        response.raise_for_status() # Raise HTTPStatusError for 4xx/5xx
                        
                        reranker_response_json = response.json()
                        # The reranker-service wraps its response in a "data" key
                        api_response_data = reranker_response_json.get("data", {})
                        reranked_docs_data = api_response_data.get("reranked_documents", [])
                        
                        # Map response back to domain.RetrievedChunk, preserving original embeddings if any
                        original_chunks_map = {chunk.id: chunk for chunk in chunks_to_process_further}
                        reranked_domain_chunks = []
                        for reranked_doc_item in reranked_docs_data:
                            original_chunk = original_chunks_map.get(reranked_doc_item["id"])
                            if original_chunk:
                                # Create new RetrievedChunk instances with updated scores
                                updated_chunk = RetrievedChunk(
                                    id=original_chunk.id,
                                    content=original_chunk.content, # Use original fetched content
                                    score=reranked_doc_item["score"], # New score from reranker
                                    metadata=reranked_doc_item.get("metadata", original_chunk.metadata), # Prefer reranker's metadata if returned, else original
                                    embedding=original_chunk.embedding, # Preserve original embedding
                                    document_id=original_chunk.document_id,
                                    file_name=original_chunk.file_name,
                                    company_id=original_chunk.company_id
                                )
                                reranked_domain_chunks.append(updated_chunk)
                        
                        chunks_to_process_further = reranked_domain_chunks
                        model_name_from_reranker = api_response_data.get("model_info",{}).get("model_name","unknown_remote")
                        pipeline_stages_used.append(f"reranking (remote:{model_name_from_reranker})")
                        rerank_log.info("Remote reranking successful.", count=len(chunks_to_process_further), model_used=model_name_from_reranker)

                    except httpx.HTTPStatusError as http_err:
                        rerank_log.error("Reranker service request failed (HTTPStatusError)", status_code=http_err.response.status_code, response_text=http_err.response.text, exc_info=False)
                        pipeline_stages_used.append("reranking (remote_http_error)")
                        # Optionally, could raise ConnectionError here or proceed with non-reranked chunks
                    except httpx.RequestError as req_err:
                        rerank_log.error("Reranker service request failed (RequestError)", error=str(req_err), exc_info=False)
                        pipeline_stages_used.append("reranking (remote_request_error)")
                        # Raise ConnectionError to signal service unavailability
                        raise ConnectionError(f"Reranker service connection error: {req_err}") from req_err
                    except Exception as e_rerank: # Catch other errors like JSONDecodeError
                        rerank_log.error("Error processing reranker service response", error_message=str(e_rerank), exc_info=True)
                        pipeline_stages_used.append("reranking (remote_client_error)")
                else:
                    rerank_log.warning("No documents with content to send to reranker service.")
                    pipeline_stages_used.append("reranking (skipped_no_content_for_remote)")
            else:
                 pipeline_stages_used.append("reranking (disabled_in_settings)")


            final_chunks_for_llm = chunks_to_process_further
            if self.diversity_filter:
                 k_final_diversity = self.settings.MAX_CONTEXT_CHUNKS
                 filter_type = type(self.diversity_filter).__name__
                 pipeline_stages_used.append(f"diversity_filter ({filter_type})")
                 exec_log.debug(f"Applying {filter_type} k={k_final_diversity}...", count=len(chunks_to_process_further))
                 final_chunks_for_llm = await self.diversity_filter.filter(chunks_to_process_further, k_final_diversity)
                 exec_log.info(f"{filter_type} applied.", final_count=len(final_chunks_for_llm))
            else: # If diversity filter is not enabled, just truncate
                 final_chunks_for_llm = chunks_to_process_further[:self.settings.MAX_CONTEXT_CHUNKS]
                 exec_log.info(f"Diversity filter disabled. Truncating to MAX_CONTEXT_CHUNKS.", final_count=len(final_chunks_for_llm), limit=self.settings.MAX_CONTEXT_CHUNKS)

            # Ensure chunks passed to LLM have content
            final_chunks_for_llm = [c for c in final_chunks_for_llm if c.content and c.content.strip()]
            if not final_chunks_for_llm:
                 exec_log.warning("No chunks with content remaining after final limiting/filtering for RAG.")
                 final_prompt = await self._build_prompt(query, [], chat_history=chat_history_str)
                 answer = await self.llm.generate(final_prompt)
                 await self.chat_repo.save_message(chat_id=final_chat_id, role='assistant', content=answer, sources=None)
                 try:
                    log_id = await self.log_repo.log_query_interaction(company_id=company_id, user_id=user_id, query=query, answer=answer, retrieved_documents_data=[], chat_id=final_chat_id, metadata={"pipeline_stages": pipeline_stages_used, "result": "no_docs_after_final_processing"})
                 except Exception as log_exc_final_no_docs: exec_log.error("Failed to log interaction for no_docs_after_final_processing case", error=str(log_exc_final_no_docs))
                 return answer, [], log_id, final_chat_id

            haystack_docs_for_prompt = [
                Document(id=c.id, content=c.content, meta=c.metadata, score=c.score)
                for c in final_chunks_for_llm
            ]
            exec_log.info(f"Building RAG prompt with {len(haystack_docs_for_prompt)} final chunks and history.")
            final_prompt = await self._build_prompt(query, haystack_docs_for_prompt, chat_history=chat_history_str)

            answer = await self.llm.generate(final_prompt)
            exec_log.info("LLM answer generated.", length=len(answer))

            sources_to_show_count = self.settings.NUM_SOURCES_TO_SHOW
            assistant_sources = [
                {"chunk_id": c.id, "document_id": c.document_id, "file_name": c.file_name, "score": c.score, "preview": truncate_text(c.content, 150) if c.content else "Error: Contenido no disponible"}
                for c in final_chunks_for_llm[:sources_to_show_count]
            ]
            await self.chat_repo.save_message(chat_id=final_chat_id, role='assistant', content=answer, sources=assistant_sources or None)
            exec_log.info(f"Assistant message saved with top {len(assistant_sources)} sources.")

            try:
                docs_for_log_dump = [RetrievedDocumentSchema(**c.model_dump(exclude_none=True)).model_dump(exclude_none=True) for c in final_chunks_for_llm[:sources_to_show_count]]
                log_metadata_details = {
                    "pipeline_stages": pipeline_stages_used,
                    "retriever_k_initial": retriever_k,
                    "fusion_fetch_k": fusion_fetch_k,
                    "max_context_chunks_limit_for_llm": self.settings.MAX_CONTEXT_CHUNKS,
                    "num_chunks_after_rerank_or_fusion_content_fetch": len(chunks_to_process_further),
                    "num_final_chunks_sent_to_llm": len(final_chunks_for_llm),
                    "num_sources_shown_to_user": len(assistant_sources),
                    "chat_history_messages_included_in_prompt": len(history_messages),
                    "diversity_filter_enabled_in_settings": self.settings.DIVERSITY_FILTER_ENABLED,
                    "reranker_enabled_in_settings": self.settings.RERANKER_ENABLED,
                    "bm25_enabled_in_settings": self.settings.BM25_ENABLED,
                 }
                log_id = await self.log_repo.log_query_interaction(
                    company_id=company_id, user_id=user_id, query=query, answer=answer,
                    retrieved_documents_data=docs_for_log_dump, chat_id=final_chat_id, metadata=log_metadata_details
                )
                exec_log.info("Interaction logged successfully", db_log_id=str(log_id))
            except Exception as log_err_final:
                exec_log.error("Failed to log RAG interaction", error=str(log_err_final), exc_info=False)

            exec_log.info("Use case execution finished successfully.")
            return answer, final_chunks_for_llm[:sources_to_show_count], log_id, final_chat_id

        except ConnectionError as ce: # Catch specific ConnectionErrors from sub-components
            exec_log.error("Connection error during use case execution", error=str(ce), exc_info=True)
            detail_message = "A required external service is unavailable. Please try again later."
            # More specific messages based on error content
            if "Embedding service" in str(ce) or "embedding service" in str(ce).lower():
                detail_message = "The embedding service is currently unavailable. Please try again later."
            elif "Reranker service" in str(ce) or "reranker service" in str(ce).lower():
                detail_message = "The reranking service is currently unavailable. Please try again later."
            elif "Gemini API" in str(ce) or "gemini" in str(ce).lower():
                detail_message = "The language model service (Gemini) is currently unavailable. Please try again later."
            elif "Vector DB" in str(ce) or "Milvus" in str(ce).lower():
                detail_message = "The vector database service is currently unavailable. Please try again later."
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=detail_message) from ce
        except ValueError as ve: # Catch specific ValueErrors (e.g., from prompt building, bad embedding data)
            exec_log.error("Value error during use case execution", error=str(ve), exc_info=True) # Log with traceback for ValueError
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Data processing error: {ve}") from ve
        except HTTPException as http_exc: # Re-raise HTTPExceptions from chat ownership check etc.
            raise http_exc
        except Exception as e: # Catch-all for any other unhandled error
            exec_log.exception("Unexpected error during use case execution") # Log with full traceback
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"An internal error occurred: {type(e).__name__}") from e