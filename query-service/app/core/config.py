# ./app/core/config.py
import logging
import os
from typing import Optional, List, Any, Dict
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import AnyHttpUrl, SecretStr, Field, validator, ValidationError, HttpUrl
import sys

# --- PostgreSQL Kubernetes Defaults ---
POSTGRES_K8S_HOST_DEFAULT = "postgres-service.nyro-develop.svc.cluster.local"
POSTGRES_K8S_PORT_DEFAULT = 5432
POSTGRES_K8S_DB_DEFAULT = "nyro"
POSTGRES_K8S_USER_DEFAULT = "postgres"

# --- Milvus Kubernetes Defaults ---
MILVUS_K8S_DEFAULT_URI = "http://milvus-milvus.default.svc.cluster.local:19530"

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
"""

class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file='.env',
        env_prefix='QUERY_',
        env_file_encoding='utf-8',
        case_sensitive=False,
        extra='ignore'
    )

    # --- General ---
    PROJECT_NAME: str = "Query Service (Haystack RAG)"
    API_V1_STR: str = "/api/v1"
    LOG_LEVEL: str = "INFO"

    # --- Database ---
    POSTGRES_USER: str = POSTGRES_K8S_USER_DEFAULT
    POSTGRES_PASSWORD: SecretStr
    POSTGRES_SERVER: str = POSTGRES_K8S_HOST_DEFAULT
    POSTGRES_PORT: int = POSTGRES_K8S_PORT_DEFAULT
    POSTGRES_DB: str = POSTGRES_K8S_DB_DEFAULT

    # --- Milvus ---
    MILVUS_URI: AnyHttpUrl = AnyHttpUrl(MILVUS_K8S_DEFAULT_URI)
    MILVUS_COLLECTION_NAME: str = "document_chunks_haystack"
    MILVUS_EMBEDDING_FIELD: str = "embedding"
    MILVUS_CONTENT_FIELD: str = "content"
    MILVUS_METADATA_FIELDS: List[str] = Field(default=[
        "company_id", "document_id", "file_name", "file_type",
    ])
    MILVUS_SEARCH_PARAMS: Dict[str, Any] = Field(default={
        "metric_type": "COSINE", "params": {"ef": 128}
    })
    MILVUS_COMPANY_ID_FIELD: str = "company_id" # Campo usado para filtrar

    # --- OpenAI Embedding ---
    OPENAI_API_KEY: SecretStr
    OPENAI_EMBEDDING_MODEL: str = "text-embedding-3-small"
    EMBEDDING_DIMENSION: int = 1536

    # --- Gemini LLM ---
    GEMINI_API_KEY: SecretStr
    GEMINI_MODEL_NAME: str = "gemini-1.5-flash-latest"

    # --- RAG Pipeline Settings ---
    RETRIEVER_TOP_K: int = 5
    RAG_PROMPT_TEMPLATE: str = DEFAULT_RAG_PROMPT_TEMPLATE
    MAX_PROMPT_TOKENS: Optional[int] = 7000

    # --- Service Client Config ---
    HTTP_CLIENT_TIMEOUT: int = 60
    HTTP_CLIENT_MAX_RETRIES: int = 2
    HTTP_CLIENT_BACKOFF_FACTOR: float = 1.0

    # --- Validators ---
    @validator("EMBEDDING_DIMENSION", pre=True, always=True)
    def set_embedding_dimension(cls, v: Optional[int], values: dict[str, Any]) -> int:
        model = values.get("OPENAI_EMBEDDING_MODEL")
        if model == "text-embedding-3-large": return 3072
        elif model in ["text-embedding-3-small", "text-embedding-ada-002"]: return 1536
        if v is None or v == 0:
            return 1536
        return v

# --- Instancia Global ---
try:
    settings = Settings()
    # Mensajes de debug
    print("DEBUG: Settings loaded successfully.")
    print(f"DEBUG: Using Postgres Server: {settings.POSTGRES_SERVER}:{settings.POSTGRES_PORT}")
    print(f"DEBUG: Using Postgres User: {settings.POSTGRES_USER}")
    print(f"DEBUG: Using Milvus URI: {settings.MILVUS_URI}")
    print(f"DEBUG: Using OpenAI Model: {settings.OPENAI_EMBEDDING_MODEL}")
    print(f"DEBUG: Using Gemini Model: {settings.GEMINI_MODEL_NAME}")
except Exception as e:
    print(f"FATAL: Error loading settings: {e}")
    import traceback
    traceback.print_exc()
    sys.exit(1)