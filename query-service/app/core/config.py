# query-service/app/core/config.py
import logging
import os
from typing import Optional, List, Any, Dict
from pydantic_settings import BaseSettings, SettingsConfigDict
# LLM_COMMENT: Correct Pydantic v2 imports
from pydantic import AnyHttpUrl, SecretStr, Field, field_validator, ValidationError, ValidationInfo
import sys
import json # LLM_COMMENT: Added json import for default factories

# --- PostgreSQL Kubernetes Defaults ---
POSTGRES_K8S_HOST_DEFAULT = "postgresql-service.nyro-develop.svc.cluster.local" # Corrected service name
POSTGRES_K8S_PORT_DEFAULT = 5432
POSTGRES_K8S_DB_DEFAULT = "atenex"
POSTGRES_K8S_USER_DEFAULT = "postgres"

# --- Milvus Kubernetes Defaults ---
MILVUS_K8S_DEFAULT_URI = "http://milvus-standalone.nyro-develop.svc.cluster.local:19530" # Corrected service name
# --- CORRECTION: Align collection name with ingest ---
MILVUS_DEFAULT_COLLECTION = "document_chunks_haystack" # Match ingest config
# --- CORRECTION: Align field names with ingest ---
MILVUS_DEFAULT_EMBEDDING_FIELD = "embedding"
MILVUS_DEFAULT_CONTENT_FIELD = "content"
MILVUS_DEFAULT_COMPANY_ID_FIELD = "company_id"
# ---------------------------------------------------
MILVUS_DEFAULT_INDEX_PARAMS = '{"metric_type": "COSINE", "index_type": "HNSW", "params": {"M": 16, "efConstruction": 256}}'
MILVUS_DEFAULT_SEARCH_PARAMS = '{"metric_type": "COSINE", "params": {"ef": 128}}'

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

Respuesta concisa y directa:
"""
DEFAULT_GENERAL_PROMPT_TEMPLATE = """
Eres un asistente de IA llamado Atenex. Responde a la siguiente pregunta del usuario de forma útil y conversacional.
Si no sabes la respuesta o la pregunta no está relacionada con tus capacidades, indícalo amablemente.

Pregunta: {{ query }}

Respuesta:
"""

# --- CORRECTION: Align Embedding Model and Dimension with ingest ---
DEFAULT_FASTEMBED_MODEL = "sentence-transformers/all-MiniLM-L6-v2" # Match ingest
DEFAULT_FASTEMBED_QUERY_PREFIX = "query: "
DEFAULT_EMBEDDING_DIMENSION = 384 # Match ingest (MiniLM dimension)
# ------------------------------------------------------------------

class Settings(BaseSettings):
    # --- Milvus gRPC Timeout ---
    MILVUS_GRPC_TIMEOUT: int = 10  # Seconds (match ingest-service default)
    model_config = SettingsConfigDict(
        env_file='.env',
        env_prefix='QUERY_',
        env_file_encoding='utf-8',
        case_sensitive=False,
        extra='ignore'
    )

    # --- General ---
    PROJECT_NAME: str = "Atenex Query Service"
    API_V1_STR: str = "/api/v1"
    LOG_LEVEL: str = "INFO"

    # --- Database (PostgreSQL) ---
    POSTGRES_USER: str = POSTGRES_K8S_USER_DEFAULT
    POSTGRES_PASSWORD: SecretStr
    POSTGRES_SERVER: str = POSTGRES_K8S_HOST_DEFAULT
    POSTGRES_PORT: int = POSTGRES_K8S_PORT_DEFAULT
    POSTGRES_DB: str = POSTGRES_K8S_DB_DEFAULT

    # --- Vector Store (Milvus) ---
    MILVUS_URI: AnyHttpUrl = Field(default=AnyHttpUrl(MILVUS_K8S_DEFAULT_URI))
    MILVUS_COLLECTION_NAME: str = MILVUS_DEFAULT_COLLECTION
    # --- CORRECTION: Use defaults aligned with ingest ---
    MILVUS_EMBEDDING_FIELD: str = Field(default=MILVUS_DEFAULT_EMBEDDING_FIELD) # Field name for vectors in Milvus
    MILVUS_CONTENT_FIELD: str = Field(default=MILVUS_DEFAULT_CONTENT_FIELD) # Field name for text content in Milvus
    MILVUS_COMPANY_ID_FIELD: str = Field(default=MILVUS_DEFAULT_COMPANY_ID_FIELD) # Field used for tenant filtering in Milvus
    # --------------------------------------------------
    # LLM_COMMENT: Define metadata fields expected in Milvus, MUST include 'company_id' for filtering
    MILVUS_METADATA_FIELDS: List[str] = Field(default=["company_id", "document_id", "file_name", "file_type"]) # Keep others if needed
    # LLM_COMMENT: Use json.loads with default_factory for complex dict defaults
    MILVUS_INDEX_PARAMS: Dict[str, Any] = Field(default_factory=lambda: json.loads(MILVUS_DEFAULT_INDEX_PARAMS))
    MILVUS_SEARCH_PARAMS: Dict[str, Any] = Field(default_factory=lambda: json.loads(MILVUS_DEFAULT_SEARCH_PARAMS))

    # --- Embedding Model (FastEmbed) ---
    # --- CORRECTION: Use aligned defaults ---
    FASTEMBED_MODEL_NAME: str = Field(default=DEFAULT_FASTEMBED_MODEL)
    EMBEDDING_DIMENSION: int = Field(default=DEFAULT_EMBEDDING_DIMENSION) # Dimension matching the FastEmbed model
    FASTEMBED_QUERY_PREFIX: str = Field(default=DEFAULT_FASTEMBED_QUERY_PREFIX) # Prefix for query embedding
    # --------------------------------------

    # --- LLM (Google Gemini) ---
    GEMINI_API_KEY: SecretStr
    GEMINI_MODEL_NAME: str = "gemini-1.5-flash-latest"

    # --- RAG Pipeline Settings ---
    RETRIEVER_TOP_K: int = 5
    RAG_PROMPT_TEMPLATE: str = DEFAULT_RAG_PROMPT_TEMPLATE
    GENERAL_PROMPT_TEMPLATE: str = DEFAULT_GENERAL_PROMPT_TEMPLATE
    MAX_PROMPT_TOKENS: Optional[int] = 7000

    # --- Service Client Config ---
    HTTP_CLIENT_TIMEOUT: int = 60
    HTTP_CLIENT_MAX_RETRIES: int = 2
    HTTP_CLIENT_BACKOFF_FACTOR: float = 1.0

    # --- Validators ---
    @field_validator('LOG_LEVEL')
    @classmethod
    def check_log_level(cls, v: str) -> str:
        valid_levels = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
        normalized_v = v.upper()
        if normalized_v not in valid_levels:
            raise ValueError(f"Invalid LOG_LEVEL '{v}'. Must be one of {valid_levels}")
        return normalized_v

    @field_validator('POSTGRES_PASSWORD', 'GEMINI_API_KEY', mode='before')
    @classmethod
    def check_secret_value_present(cls, v: Any, info: ValidationInfo) -> Any:
        if v is None or v == "":
            field_name = info.field_name if info.field_name else "Unknown Secret Field"
            raise ValueError(f"Required secret field '{field_name}' cannot be empty.")
        return v

    # --- CORRECTION: Add validator for embedding dimension vs model ---
    @field_validator('EMBEDDING_DIMENSION')
    @classmethod
    def check_embedding_dimension(cls, v: int, info: ValidationInfo) -> int:
        if v <= 0:
            raise ValueError("EMBEDDING_DIMENSION must be a positive integer.")
        # Check against the actual model being used (from defaults or env var)
        model_name = info.data.get('FASTEMBED_MODEL_NAME', DEFAULT_FASTEMBED_MODEL)
        expected_dim = -1
        if 'all-MiniLM-L6-v2' in model_name:
            expected_dim = 384
        elif 'bge-small-en-v1.5' in model_name:
            expected_dim = 384
        elif 'bge-large-en-v1.5' in model_name:
            expected_dim = 1024
        # Add other known models here

        if expected_dim != -1 and v != expected_dim:
            logging.warning(
                f"Configured EMBEDDING_DIMENSION ({v}) differs from standard dimension ({expected_dim}) "
                f"for model '{model_name}'. Ensure this is intentional."
            )
        elif expected_dim == -1:
            logging.warning(
                 f"Unknown embedding dimension for model '{model_name}'. Using configured dimension {v}. "
                 f"Verify this matches the actual model output."
            )

        logging.debug(f"Using EMBEDDING_DIMENSION: {v} for model: {model_name}")
        return v
    # ------------------------------------------------------------------


# --- Instancia Global ---
temp_log = logging.getLogger("query_service.config.loader")
if not temp_log.handlers:
    handler = logging.StreamHandler(sys.stdout)
    formatter = logging.Formatter('%(levelname)s: [%(name)s] %(message)s')
    handler.setFormatter(formatter)
    temp_log.addHandler(handler)
    temp_log.setLevel(logging.INFO)

try:
    temp_log.info("Loading Query Service settings...")
    settings = Settings()
    temp_log.info("Query Service Settings Loaded Successfully:")
    temp_log.info(f"  PROJECT_NAME: {settings.PROJECT_NAME}")
    temp_log.info(f"  LOG_LEVEL: {settings.LOG_LEVEL}")
    temp_log.info(f"  API_V1_STR: {settings.API_V1_STR}")
    temp_log.info(f"  POSTGRES_SERVER: {settings.POSTGRES_SERVER}:{settings.POSTGRES_PORT}")
    temp_log.info(f"  POSTGRES_DB: {settings.POSTGRES_DB}")
    temp_log.info(f"  POSTGRES_USER: {settings.POSTGRES_USER}")
    temp_log.info(f"  POSTGRES_PASSWORD: *** SET ***")
    temp_log.info(f"  MILVUS_URI: {settings.MILVUS_URI}")
    temp_log.info(f"  MILVUS_COLLECTION_NAME: {settings.MILVUS_COLLECTION_NAME}")
    # --- Log corrected fields ---
    temp_log.info(f"  MILVUS_EMBEDDING_FIELD: {settings.MILVUS_EMBEDDING_FIELD}")
    temp_log.info(f"  MILVUS_CONTENT_FIELD: {settings.MILVUS_CONTENT_FIELD}")
    temp_log.info(f"  MILVUS_COMPANY_ID_FIELD: {settings.MILVUS_COMPANY_ID_FIELD}")
    # --------------------------
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