# ./app/core/config.py
import logging
import os
from typing import Optional, List, Any, Dict
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import AnyHttpUrl, SecretStr, Field, validator, ValidationError, HttpUrl
import sys

# --- PostgreSQL Kubernetes Defaults ---
POSTGRES_K8S_HOST_DEFAULT = "postgresql-service.nyro-develop.svc.cluster.local" # LLM_COMMENT: Keep DB Defaults
POSTGRES_K8S_PORT_DEFAULT = 5432
# LLM_COMMENT: Database name 'atenex' seems correct based on previous logs/configs, correcting default.
POSTGRES_K8S_DB_DEFAULT = "atenex"
POSTGRES_K8S_USER_DEFAULT = "postgres"

# --- Milvus Kubernetes Defaults ---
MILVUS_K8S_DEFAULT_URI = "http://milvus-milvus.default.svc.cluster.local:19530" # LLM_COMMENT: Keep Milvus Defaults

# --- RAG Defaults ---
DEFAULT_RAG_PROMPT_TEMPLATE = """
Basándote estrictamente en los siguientes documentos recuperados, responde a la pregunta del usuario.
Si los documentos no contienen la respuesta, indica explícitamente que no puedes responder con la información proporcionada.
No inventes información ni uses conocimiento externo.

Documentos:
{% for doc in documents %}
--- Documento {{ loop.index }} ---
{{ doc.content }}
--- Fin Documento {{ loop.index }} ---
{% endfor %}

Pregunta: {{ query }}

Respuesta:
""" # LLM_COMMENT: Keep Prompt Template

class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file='.env',
        env_prefix='QUERY_',
        env_file_encoding='utf-8',
        case_sensitive=False,
        extra='ignore'
    )

    # --- General ---
    PROJECT_NAME: str = "Query Service (Haystack RAG + FastEmbed)" # LLM_COMMENT: Update project name slightly
    API_V1_STR: str = "/api/v1"
    LOG_LEVEL: str = "INFO"

    # --- Database ---
    POSTGRES_USER: str = POSTGRES_K8S_USER_DEFAULT
    POSTGRES_PASSWORD: SecretStr
    POSTGRES_SERVER: str = POSTGRES_K8S_HOST_DEFAULT
    POSTGRES_PORT: int = POSTGRES_K8S_PORT_DEFAULT
    POSTGRES_DB: str = POSTGRES_K8S_DB_DEFAULT # LLM_COMMENT: Use the corrected default 'atenex'

    # --- Milvus ---
    MILVUS_URI: AnyHttpUrl = AnyHttpUrl(MILVUS_K8S_DEFAULT_URI)
    MILVUS_COLLECTION_NAME: str = "document_chunks_haystack" # LLM_COMMENT: Keep Milvus collection name consistent with ingest-service
    MILVUS_EMBEDDING_FIELD: str = "embedding"
    MILVUS_CONTENT_FIELD: str = "content"
    MILVUS_METADATA_FIELDS: List[str] = Field(default=[
        "company_id", "document_id", "file_name", "file_type",
    ])
    MILVUS_SEARCH_PARAMS: Dict[str, Any] = Field(default={
        "metric_type": "COSINE", "params": {"ef": 128}
    })
    MILVUS_COMPANY_ID_FIELD: str = "company_id"

    # --- Embedding (FastEmbed) ---
    # LLM_COMMENT: Removed OpenAI specific settings (API Key, Model Name).
    # LLM_COMMENT: Added settings for FastEmbed. Using BAAI/bge-small-en-v1.5 as default free model.
    FASTEMBED_MODEL_NAME: str = "BAAI/bge-small-en-v1.5"
    # LLM_COMMENT: BGE models often require specific prefixes for query/passage embedding.
    # LLM_COMMENT: Prefix for query embedding (used in query-service).
    FASTEMBED_QUERY_PREFIX: Optional[str] = "query: "
    # LLM_COMMENT: Set embedding dimension according to the chosen FastEmbed model.
    # LLM_COMMENT: BAAI/bge-small-en-v1.5 has 384 dimensions.
    EMBEDDING_DIMENSION: int = 384

    # --- Gemini LLM ---
    GEMINI_API_KEY: SecretStr
    GEMINI_MODEL_NAME: str = "gemini-1.5-flash-latest" # LLM_COMMENT: Keep Gemini settings

    # --- RAG Pipeline Settings ---
    RETRIEVER_TOP_K: int = 5
    RAG_PROMPT_TEMPLATE: str = DEFAULT_RAG_PROMPT_TEMPLATE
    MAX_PROMPT_TOKENS: Optional[int] = 7000

    # --- Service Client Config ---
    HTTP_CLIENT_TIMEOUT: int = 60
    HTTP_CLIENT_MAX_RETRIES: int = 2
    HTTP_CLIENT_BACKOFF_FACTOR: float = 1.0

    # LLM_COMMENT: Removed the validator that automatically set EMBEDDING_DIMENSION based on OpenAI models.

# --- Instancia Global ---
try:
    settings = Settings()
    # LLM_COMMENT: Updated debug print statements for new embedding config.
    print("DEBUG: Settings loaded successfully.")
    print(f"DEBUG: Using Postgres Server: {settings.POSTGRES_SERVER}:{settings.POSTGRES_PORT}")
    print(f"DEBUG: Using Postgres User: {settings.POSTGRES_USER}")
    print(f"DEBUG: Using Milvus URI: {settings.MILVUS_URI}")
    print(f"DEBUG: Using FastEmbed Model: {settings.FASTEMBED_MODEL_NAME}")
    print(f"DEBUG: Using Embedding Dimension: {settings.EMBEDDING_DIMENSION}")
    print(f"DEBUG: Using Gemini Model: {settings.GEMINI_MODEL_NAME}")
except Exception as e:
    print(f"FATAL: Error loading settings: {e}")
    import traceback
    traceback.print_exc()
    sys.exit(1)