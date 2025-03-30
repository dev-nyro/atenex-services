# ./app/core/config.py
import logging
import os
from typing import Optional, List, Any, Dict
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import AnyHttpUrl, SecretStr, Field, validator, ValidationError, HttpUrl
import sys

# --- Supabase Connection Defaults (Usando Session Pooler) ---
SUPABASE_SESSION_POOLER_HOST = "aws-0-sa-east-1.pooler.supabase.com"
SUPABASE_SESSION_POOLER_PORT_INT = 5432
SUPABASE_SESSION_POOLER_USER_TEMPLATE = "postgres.{project_ref}"
SUPABASE_DEFAULT_DB = "postgres"

# --- Milvus Kubernetes Defaults ---
# *** CORRECCIÓN: Usar el nombre y namespace correctos del servicio Milvus ***
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
    POSTGRES_USER: str = Field(default_factory=lambda: SUPABASE_SESSION_POOLER_USER_TEMPLATE.format(project_ref=os.getenv("SUPABASE_PROJECT_REF", "YOUR_PROJECT_REF_HERE")))
    POSTGRES_PASSWORD: SecretStr
    POSTGRES_SERVER: str = SUPABASE_SESSION_POOLER_HOST
    POSTGRES_PORT: int = SUPABASE_SESSION_POOLER_PORT_INT
    POSTGRES_DB: str = SUPABASE_DEFAULT_DB

    # --- Milvus ---
    # Usar el default corregido
    MILVUS_URI: AnyHttpUrl = AnyHttpUrl(MILVUS_K8S_DEFAULT_URI)
    MILVUS_COLLECTION_NAME: str = "document_chunks_haystack"
    # *** CORRECCIÓN: Asegurar que estos nombres coincidan con cómo ingest-service los guardó en Milvus ***
    # (Los nombres por defecto de milvus-haystack suelen ser 'content' y 'embedding')
    MILVUS_EMBEDDING_FIELD: str = "embedding" # Mantener si ingest usó este nombre
    MILVUS_CONTENT_FIELD: str = "content"     # Mantener si ingest usó este nombre
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
    # (Sin cambios en validadores)
    @validator("EMBEDDING_DIMENSION", pre=True, always=True)
    def set_embedding_dimension(cls, v: Optional[int], values: dict[str, Any]) -> int:
        model = values.get("OPENAI_EMBEDDING_MODEL")
        if model == "text-embedding-3-large": return 3072
        elif model in ["text-embedding-3-small", "text-embedding-ada-002"]: return 1536
        if v is None or v == 0:
            return 1536
        return v

    @validator("POSTGRES_USER", pre=True, always=True)
    def check_postgres_user(cls, v: str) -> str:
        if "YOUR_PROJECT_REF_HERE" in v:
            print("WARNING: SUPABASE_PROJECT_REF environment variable not set. Using placeholder for POSTGRES_USER.")
        return v

# --- Instancia Global ---
# (Sin cambios en la lógica de instanciación)
try:
    settings = Settings()
    print("DEBUG: Query Service Settings loaded successfully.")
    print(f"DEBUG: Using Postgres Server: {settings.POSTGRES_SERVER}:{settings.POSTGRES_PORT}")
    print(f"DEBUG: Using Postgres User: {settings.POSTGRES_USER}")
    print(f"DEBUG: Using Milvus URI: {settings.MILVUS_URI}") # Ahora mostrará la URI corregida
    print(f"DEBUG: Using Milvus Collection: {settings.MILVUS_COLLECTION_NAME}")
    print(f"DEBUG: Using OpenAI Embedding Model: {settings.OPENAI_EMBEDDING_MODEL} (Dim: {settings.EMBEDDING_DIMENSION})")
    print(f"DEBUG: Using Gemini Model: {settings.GEMINI_MODEL_NAME}")

except (ValidationError, ValueError) as e:
    error_details = ""
    if isinstance(e, ValidationError):
        try: error_details = f"\nValidation Errors:\n{e.json(indent=2)}"
        except Exception: error_details = f"\nRaw Errors: {e.errors()}"
    print(f"FATAL: Configuration validation failed:{error_details}\nOriginal Error: {e}")
    sys.exit(1)
except Exception as e:
    print(f"FATAL: Unexpected error during Settings instantiation:\n{e}")
    import traceback; traceback.print_exc()
    sys.exit(1)