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
SUPABASE_SESSION_POOLER_USER_TEMPLATE = "postgres.{project_ref}" # Necesitarás el project_ref
SUPABASE_DEFAULT_DB = "postgres"

# --- Milvus Kubernetes Defaults ---
MILVUS_K8S_DEFAULT_URI = "http://milvus-service.nyro-develop.svc.cluster.local:19530" # Ajustar namespace si es diferente

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
        env_prefix='QUERY_', # Prefijo específico para Query Service
        env_file_encoding='utf-8',
        case_sensitive=False,
        extra='ignore'
    )

    # --- General ---
    PROJECT_NAME: str = "Query Service (Haystack RAG)"
    API_V1_STR: str = "/api/v1"
    LOG_LEVEL: str = "INFO"

    # --- Database (Supabase Session Pooler Settings for Logging) ---
    POSTGRES_USER: str = Field(default_factory=lambda: SUPABASE_SESSION_POOLER_USER_TEMPLATE.format(project_ref=os.getenv("SUPABASE_PROJECT_REF", "YOUR_PROJECT_REF_HERE"))) # Lee de ENV o usa placeholder
    POSTGRES_PASSWORD: SecretStr # Obligatorio desde Secrets (query-service-secrets -> postgres-password)
    POSTGRES_SERVER: str = SUPABASE_SESSION_POOLER_HOST
    POSTGRES_PORT: int = SUPABASE_SESSION_POOLER_PORT_INT
    POSTGRES_DB: str = SUPABASE_DEFAULT_DB

    # --- Milvus ---
    MILVUS_URI: AnyHttpUrl = AnyHttpUrl(MILVUS_K8S_DEFAULT_URI)
    MILVUS_COLLECTION_NAME: str = "document_chunks_haystack" # Debe coincidir con Ingest
    MILVUS_EMBEDDING_FIELD: str = "embedding" # Nombre del campo vectorial en Milvus (debe coincidir con Ingest)
    MILVUS_CONTENT_FIELD: str = "content"   # Nombre del campo de contenido en Milvus (debe coincidir con Ingest)
    MILVUS_METADATA_FIELDS: List[str] = Field(default=[ # Campos de metadatos indexados en Milvus (debe coincidir con Ingest)
        "company_id", "document_id", "file_name", "file_type",
    ])
    MILVUS_SEARCH_PARAMS: Dict[str, Any] = Field(default={ # Parámetros de búsqueda (ajustar según pruebas)
        "metric_type": "COSINE", "params": {"ef": 128}
    })
    # Añadimos el campo company_id específico para filtrado fácil
    MILVUS_COMPANY_ID_FIELD: str = "company_id"

    # --- OpenAI Embedding (for Query) ---
    OPENAI_API_KEY: SecretStr # Obligatorio desde Secrets (query-service-secrets -> openai-api-key)
    OPENAI_EMBEDDING_MODEL: str = "text-embedding-3-small" # Debe coincidir con Ingest
    EMBEDDING_DIMENSION: int = 1536 # Default para text-embedding-3-small/ada-002

    # --- Gemini LLM ---
    GEMINI_API_KEY: SecretStr # Obligatorio desde Secrets (query-service-secrets -> gemini-api-key)
    GEMINI_MODEL_NAME: str = "gemini-1.5-flash-latest" # Modelo a usar para generación

    # --- RAG Pipeline Settings ---
    RETRIEVER_TOP_K: int = 5 # Número de documentos a recuperar por defecto
    RAG_PROMPT_TEMPLATE: str = DEFAULT_RAG_PROMPT_TEMPLATE # Plantilla para el prompt
    MAX_PROMPT_TOKENS: Optional[int] = 7000 # Límite opcional para tokens de prompt (Gemini 1.5 tiene límites altos)

    # --- Service Client Config ---
    HTTP_CLIENT_TIMEOUT: int = 60
    HTTP_CLIENT_MAX_RETRIES: int = 2
    HTTP_CLIENT_BACKOFF_FACTOR: float = 1.0

    # --- Validators ---
    @validator("EMBEDDING_DIMENSION", pre=True, always=True)
    def set_embedding_dimension(cls, v: Optional[int], values: dict[str, Any]) -> int:
        model = values.get("OPENAI_EMBEDDING_MODEL")
        # Mantener consistencia con Ingest Service
        if model == "text-embedding-3-large": return 3072
        elif model in ["text-embedding-3-small", "text-embedding-ada-002"]: return 1536
        # Si no se especifica, intentar inferir o usar default
        if v is None or v == 0:
            if model:
                 if model == "text-embedding-3-large": return 3072
                 if model in ["text-embedding-3-small", "text-embedding-ada-002"]: return 1536
            return 1536 # Default general si no se puede inferir
        return v

    @validator("POSTGRES_USER", pre=True, always=True)
    def check_postgres_user(cls, v: str) -> str:
        if "YOUR_PROJECT_REF_HERE" in v:
            print("WARNING: SUPABASE_PROJECT_REF environment variable not set. Using placeholder for POSTGRES_USER.")
        return v

# --- Instancia Global ---
try:
    settings = Settings()
    print("DEBUG: Query Service Settings loaded successfully.")
    print(f"DEBUG: Using Postgres Server: {settings.POSTGRES_SERVER}:{settings.POSTGRES_PORT}")
    print(f"DEBUG: Using Postgres User: {settings.POSTGRES_USER}")
    print(f"DEBUG: Using Milvus URI: {settings.MILVUS_URI}")
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