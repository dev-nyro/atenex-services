# ./app/core/config.py
import logging
import os
from typing import Optional, List, Any, Dict
from pydantic_settings import BaseSettings, SettingsConfigDict
# LLM_COMMENT: Correct Pydantic v2 imports
from pydantic import AnyHttpUrl, SecretStr, Field, field_validator, ValidationError, ValidationInfo
import sys
import json # LLM_COMMENT: Added json import for default factories

# --- PostgreSQL Kubernetes Defaults ---
POSTGRES_K8S_HOST_DEFAULT = "postgresql.nyro-develop.svc.cluster.local"
POSTGRES_K8S_PORT_DEFAULT = 5432
POSTGRES_K8S_DB_DEFAULT = "atenex" # LLM_COMMENT: Verified database name
POSTGRES_K8S_USER_DEFAULT = "postgres"

# --- Milvus Kubernetes Defaults ---
MILVUS_K8S_DEFAULT_URI = "http://milvus-milvus.default.svc.cluster.local:19530" # LLM_COMMENT: Default Milvus URI in 'default' namespace
# LLM_COMMENT: Default Milvus collection name, should match ingest-service
MILVUS_DEFAULT_COLLECTION = "atenex_doc_chunks"
# LLM_COMMENT: Default Milvus index/search parameters, can be overridden by env vars
MILVUS_DEFAULT_INDEX_PARAMS = '{"metric_type": "COSINE", "index_type": "HNSW", "params": {"M": 16, "efConstruction": 256}}'
MILVUS_DEFAULT_SEARCH_PARAMS = '{"metric_type": "COSINE", "params": {"ef": 128}}'

# --- RAG Defaults ---
# LLM_COMMENT: Prompt for RAG when documents are retrieved. Uses Jinja2 syntax.
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

Respuesta concisa y directa:
"""
# LLM_COMMENT: Prompt for general conversation when NO documents are relevant.
DEFAULT_GENERAL_PROMPT_TEMPLATE = """
Eres un asistente de IA llamado Atenex. Responde a la siguiente pregunta del usuario de forma útil y conversacional.
Si no sabes la respuesta o la pregunta no está relacionada con tus capacidades, indícalo amablemente.

Pregunta: {{ query }}

Respuesta:
"""

# LLM_COMMENT: Define default FastEmbed model and query prefix
DEFAULT_FASTEMBED_MODEL = "BAAI/bge-small-en-v1.5"
DEFAULT_FASTEMBED_QUERY_PREFIX = "query: "
# LLM_COMMENT: Embedding dimension for the default FastEmbed model
DEFAULT_EMBEDDING_DIMENSION = 384

class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file='.env',
        env_prefix='QUERY_', # LLM_COMMENT: Keep prefix consistent
        env_file_encoding='utf-8',
        case_sensitive=False,
        extra='ignore'
    )

    # --- General ---
    PROJECT_NAME: str = "Atenex Query Service" # LLM_COMMENT: Standardized name
    API_V1_STR: str = "/api/v1"
    LOG_LEVEL: str = "INFO"

    # --- Database (PostgreSQL) ---
    POSTGRES_USER: str = POSTGRES_K8S_USER_DEFAULT
    POSTGRES_PASSWORD: SecretStr # LLM_COMMENT: Secret loaded from env/secret
    POSTGRES_SERVER: str = POSTGRES_K8S_HOST_DEFAULT
    POSTGRES_PORT: int = POSTGRES_K8S_PORT_DEFAULT
    POSTGRES_DB: str = POSTGRES_K8S_DB_DEFAULT

    # --- Vector Store (Milvus) ---
    MILVUS_URI: AnyHttpUrl = Field(default=AnyHttpUrl(MILVUS_K8S_DEFAULT_URI)) # LLM_COMMENT: Use Field for default
    MILVUS_COLLECTION_NAME: str = MILVUS_DEFAULT_COLLECTION
    # LLM_COMMENT: Define metadata fields expected in Milvus, MUST include 'company_id' for filtering
    MILVUS_METADATA_FIELDS: List[str] = Field(default=["company_id", "document_id", "file_name", "file_type"])
    MILVUS_EMBEDDING_FIELD: str = "embedding" # LLM_COMMENT: Field name for vectors in Milvus
    MILVUS_CONTENT_FIELD: str = "content" # LLM_COMMENT: Field name for text content in Milvus
    MILVUS_COMPANY_ID_FIELD: str = "company_id" # LLM_COMMENT: Field used for tenant filtering in Milvus
    # LLM_COMMENT: Use json.loads with default_factory for complex dict defaults
    MILVUS_INDEX_PARAMS: Dict[str, Any] = Field(default_factory=lambda: json.loads(MILVUS_DEFAULT_INDEX_PARAMS))
    MILVUS_SEARCH_PARAMS: Dict[str, Any] = Field(default_factory=lambda: json.loads(MILVUS_DEFAULT_SEARCH_PARAMS))

    # --- Embedding Model (FastEmbed) ---
    # LLM_COMMENT: Settings specific to FastEmbed replacing OpenAI embedding settings
    FASTEMBED_MODEL_NAME: str = DEFAULT_FASTEMBED_MODEL
    FASTEMBED_QUERY_PREFIX: str = DEFAULT_FASTEMBED_QUERY_PREFIX # LLM_COMMENT: Prefix for query embedding
    EMBEDDING_DIMENSION: int = DEFAULT_EMBEDDING_DIMENSION # LLM_COMMENT: Dimension matching the FastEmbed model

    # --- LLM (Google Gemini) ---
    GEMINI_API_KEY: SecretStr # LLM_COMMENT: Secret loaded from env/secret
    GEMINI_MODEL_NAME: str = "gemini-1.5-flash-latest"

    # --- RAG Pipeline Settings ---
    RETRIEVER_TOP_K: int = 5
    RAG_PROMPT_TEMPLATE: str = DEFAULT_RAG_PROMPT_TEMPLATE
    # LLM_COMMENT: Added separate template for non-RAG scenarios
    GENERAL_PROMPT_TEMPLATE: str = DEFAULT_GENERAL_PROMPT_TEMPLATE
    MAX_PROMPT_TOKENS: Optional[int] = 7000 # LLM_COMMENT: Optional token limit for prompts

    # --- Service Client Config ---
    HTTP_CLIENT_TIMEOUT: int = 60
    HTTP_CLIENT_MAX_RETRIES: int = 2
    HTTP_CLIENT_BACKOFF_FACTOR: float = 1.0

    # --- Validators ---
    # LLM_COMMENT: Validator for LOG_LEVEL (using Pydantic v2 syntax)
    @field_validator('LOG_LEVEL')
    @classmethod
    def check_log_level(cls, v: str) -> str:
        valid_levels = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
        normalized_v = v.upper()
        if normalized_v not in valid_levels:
            raise ValueError(f"Invalid LOG_LEVEL '{v}'. Must be one of {valid_levels}")
        return normalized_v

    # LLM_COMMENT: Validator to ensure required secrets are present (using Pydantic v2 syntax)
    @field_validator('POSTGRES_PASSWORD', 'GEMINI_API_KEY', mode='before')
    @classmethod
    def check_secret_value_present(cls, v: Any, info: ValidationInfo) -> Any:
        if v is None or v == "":
            field_name = info.field_name if info.field_name else "Unknown Secret Field"
            raise ValueError(f"Required secret field '{field_name}' cannot be empty.")
        return v

# --- Instancia Global ---
# LLM_COMMENT: Keep global settings instance pattern
temp_log = logging.getLogger("query_service.config.loader")
if not temp_log.handlers: # Avoid adding handler multiple times during reloads
    handler = logging.StreamHandler(sys.stdout)
    formatter = logging.Formatter('%(levelname)s: [%(name)s] %(message)s')
    handler.setFormatter(formatter)
    temp_log.addHandler(handler)
    temp_log.setLevel(logging.INFO) # Use INFO level for startup logs

try:
    temp_log.info("Loading Query Service settings...")
    settings = Settings()
    temp_log.info("Query Service Settings Loaded Successfully:")
    # LLM_COMMENT: Log key settings for verification (secrets are masked)
    temp_log.info(f"  PROJECT_NAME: {settings.PROJECT_NAME}")
    temp_log.info(f"  LOG_LEVEL: {settings.LOG_LEVEL}")
    temp_log.info(f"  API_V1_STR: {settings.API_V1_STR}")
    temp_log.info(f"  POSTGRES_SERVER: {settings.POSTGRES_SERVER}:{settings.POSTGRES_PORT}")
    temp_log.info(f"  POSTGRES_DB: {settings.POSTGRES_DB}")
    temp_log.info(f"  POSTGRES_USER: {settings.POSTGRES_USER}")
    temp_log.info(f"  POSTGRES_PASSWORD: *** SET ***")
    temp_log.info(f"  MILVUS_URI: {settings.MILVUS_URI}")
    temp_log.info(f"  MILVUS_COLLECTION_NAME: {settings.MILVUS_COLLECTION_NAME}")
    temp_log.info(f"  FASTEMBED_MODEL_NAME: {settings.FASTEMBED_MODEL_NAME}")
    temp_log.info(f"  EMBEDDING_DIMENSION: {settings.EMBEDDING_DIMENSION}")
    temp_log.info(f"  GEMINI_API_KEY: *** SET ***")
    temp_log.info(f"  GEMINI_MODEL_NAME: {settings.GEMINI_MODEL_NAME}")
    temp_log.info(f"  RETRIEVER_TOP_K: {settings.RETRIEVER_TOP_K}")

except (ValidationError, ValueError) as e:
    error_details = ""
    if isinstance(e, ValidationError):
        try: error_details = f"\nValidation Errors:\n{e.json(indent=2)}"
        except Exception: error_details = f"\nRaw Errors: {e.errors()}"
    temp_log.critical(f"!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
    temp_log.critical(f"! FATAL: Query Service configuration validation failed:{error_details}")
    temp_log.critical(f"! Check environment variables (prefixed with QUERY_) or .env file.")
    temp_log.critical(f"! Original Error: {e}")
    temp_log.critical(f"!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
    sys.exit(1)
except Exception as e:
    temp_log.exception(f"FATAL: Unexpected error loading Query Service settings: {e}")
    sys.exit(1)