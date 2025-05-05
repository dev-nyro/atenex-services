# query-service/app/core/config.py
import logging
import os
from typing import Optional, List, Any, Dict
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import AnyHttpUrl, SecretStr, Field, field_validator, ValidationError, ValidationInfo
import sys
import json

# --- Default Values ---
# PostgreSQL
POSTGRES_K8S_HOST_DEFAULT = "postgresql-service.nyro-develop.svc.cluster.local"
POSTGRES_K8S_PORT_DEFAULT = 5432
POSTGRES_K8S_DB_DEFAULT = "atenex"
POSTGRES_K8S_USER_DEFAULT = "postgres"
# Milvus
MILVUS_K8S_DEFAULT_URI = "http://milvus-standalone.nyro-develop.svc.cluster.local:19530"
MILVUS_DEFAULT_COLLECTION = "document_chunks_haystack" # LLM_FLAG: Consider aligning collection name if ingest changes
MILVUS_DEFAULT_EMBEDDING_FIELD = "embedding"
MILVUS_DEFAULT_CONTENT_FIELD = "content"
MILVUS_DEFAULT_COMPANY_ID_FIELD = "company_id"
MILVUS_DEFAULT_DOCUMENT_ID_FIELD = "document_id"
MILVUS_DEFAULT_FILENAME_FIELD = "file_name"
MILVUS_DEFAULT_GRPC_TIMEOUT = 10
# RAG Prompts
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
# Models
DEFAULT_FASTEMBED_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
DEFAULT_FASTEMBED_QUERY_PREFIX = "query: "
DEFAULT_EMBEDDING_DIMENSION = 384
DEFAULT_GEMINI_MODEL = "gemini-1.5-flash-latest"
# LLM_REFACTOR_STEP_4: Add default for reranker
DEFAULT_RERANKER_MODEL = "BAAI/bge-reranker-base"
# RAG Pipeline Parameters
DEFAULT_RETRIEVER_TOP_K = 5
# LLM_REFACTOR_STEP_4: Add defaults for new RAG components
DEFAULT_BM25_ENABLED: bool = True # Control if BM25 is used
DEFAULT_RERANKER_ENABLED: bool = True # Control if Reranker is used
DEFAULT_DIVERSITY_FILTER_ENABLED: bool = False # Control if Diversity Filter is used (start disabled)
DEFAULT_DIVERSITY_K_FINAL: int = 10 # Number of docs after diversity filter
DEFAULT_HYBRID_ALPHA: float = 0.5 # Weight for dense vs sparse fusion (0=sparse only, 1=dense only)

class Settings(BaseSettings):
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
    POSTGRES_USER: str = Field(default=POSTGRES_K8S_USER_DEFAULT)
    POSTGRES_PASSWORD: SecretStr
    POSTGRES_SERVER: str = Field(default=POSTGRES_K8S_HOST_DEFAULT)
    POSTGRES_PORT: int = Field(default=POSTGRES_K8S_PORT_DEFAULT)
    POSTGRES_DB: str = Field(default=POSTGRES_K8S_DB_DEFAULT)

    # --- Vector Store (Milvus) ---
    MILVUS_URI: AnyHttpUrl = Field(default=AnyHttpUrl(MILVUS_K8S_DEFAULT_URI))
    MILVUS_COLLECTION_NAME: str = Field(default=MILVUS_DEFAULT_COLLECTION)
    MILVUS_EMBEDDING_FIELD: str = Field(default=MILVUS_DEFAULT_EMBEDDING_FIELD)
    MILVUS_CONTENT_FIELD: str = Field(default=MILVUS_DEFAULT_CONTENT_FIELD)
    MILVUS_COMPANY_ID_FIELD: str = Field(default=MILVUS_DEFAULT_COMPANY_ID_FIELD)
    MILVUS_DOCUMENT_ID_FIELD: str = Field(default=MILVUS_DEFAULT_DOCUMENT_ID_FIELD)
    MILVUS_FILENAME_FIELD: str = Field(default=MILVUS_DEFAULT_FILENAME_FIELD)
    MILVUS_METADATA_FIELDS: List[str] = Field(default=["company_id", "document_id", "file_name", "file_type"])
    MILVUS_GRPC_TIMEOUT: int = Field(default=MILVUS_DEFAULT_GRPC_TIMEOUT)
    # Index/Search params are usually static, loaded from defaults or env vars as JSON strings
    # Pydantic v1 style: MILVUS_INDEX_PARAMS: str = MILVUS_DEFAULT_INDEX_PARAMS
    # Pydantic v1 style: MILVUS_SEARCH_PARAMS: str = MILVUS_DEFAULT_SEARCH_PARAMS

    # --- Embedding Model (FastEmbed) ---
    FASTEMBED_MODEL_NAME: str = Field(default=DEFAULT_FASTEMBED_MODEL)
    EMBEDDING_DIMENSION: int = Field(default=DEFAULT_EMBEDDING_DIMENSION)
    FASTEMBED_QUERY_PREFIX: str = Field(default=DEFAULT_FASTEMBED_QUERY_PREFIX)

    # --- LLM (Google Gemini) ---
    GEMINI_API_KEY: SecretStr
    GEMINI_MODEL_NAME: str = Field(default=DEFAULT_GEMINI_MODEL)

    # --- Reranker Model ---
    # LLM_REFACTOR_STEP_4: Add Reranker config
    RERANKER_ENABLED: bool = Field(default=DEFAULT_RERANKER_ENABLED)
    RERANKER_MODEL_NAME: str = Field(default=DEFAULT_RERANKER_MODEL, description="Sentence Transformer model name/path for reranking.")
    # RERANKER_ONNX_PATH: Optional[str] = Field(None, description="Path to ONNX version of reranker model (optional).")
    # RERANKER_DEVICE: Optional[str] = Field(None, description="Device for reranker ('cpu', 'cuda', 'mps'). Auto-detected if None.")

    # --- Sparse Retriever (BM25) ---
    # LLM_REFACTOR_STEP_4: Add BM25 config
    BM25_ENABLED: bool = Field(default=DEFAULT_BM25_ENABLED)
    # BM25_INDEX_PATH: Optional[str] = Field(None, description="Path to pre-built BM25 index (if using Pyserini).")

    # --- Diversity Filter ---
    # LLM_REFACTOR_STEP_4: Add Diversity config
    DIVERSITY_FILTER_ENABLED: bool = Field(default=DEFAULT_DIVERSITY_FILTER_ENABLED)
    # DIVERSITY_FILTER_METHOD: str = Field(default="stub", description="Diversity method ('stub', 'mmr', 'dartboard').")
    DIVERSITY_K_FINAL: int = Field(default=DEFAULT_DIVERSITY_K_FINAL, gt=0, description="Target number of documents after diversity filtering.")
    # DIVERSITY_LAMBDA: float = Field(default=0.5, ge=0.0, le=1.0, description="Lambda parameter for MMR diversity (if used).")

    # --- RAG Pipeline Parameters ---
    RETRIEVER_TOP_K: int = Field(default=DEFAULT_RETRIEVER_TOP_K, gt=0, le=50)
    # LLM_REFACTOR_STEP_4: Add fusion parameter
    HYBRID_FUSION_ALPHA: float = Field(default=DEFAULT_HYBRID_ALPHA, ge=0.0, le=1.0, description="Weighting factor for dense vs sparse fusion (0=sparse, 1=dense). Used for simple linear fusion.")
    RAG_PROMPT_TEMPLATE: str = Field(default=DEFAULT_RAG_PROMPT_TEMPLATE)
    GENERAL_PROMPT_TEMPLATE: str = Field(default=DEFAULT_GENERAL_PROMPT_TEMPLATE)
    MAX_PROMPT_TOKENS: Optional[int] = Field(default=7000)

    # --- Service Client Config ---
    HTTP_CLIENT_TIMEOUT: int = Field(default=60)
    HTTP_CLIENT_MAX_RETRIES: int = Field(default=2)
    HTTP_CLIENT_BACKOFF_FACTOR: float = Field(default=1.0)

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
        if v is None or (isinstance(v, str) and v == ""): # Check for None or empty string
            field_name = info.field_name if info.field_name else "Unknown Secret Field"
            # Raise ValueError for config loading phase
            raise ValueError(f"Required secret field 'QUERY_{field_name.upper()}' cannot be empty.")
        # Return as SecretStr implicitly by Pydantic
        return v

    @field_validator('EMBEDDING_DIMENSION')
    @classmethod
    def check_embedding_dimension(cls, v: int, info: ValidationInfo) -> int:
        if v <= 0:
            raise ValueError("EMBEDDING_DIMENSION must be a positive integer.")
        # LLM_FLAG: This validation logic relies on specific model names. Might need updates if models change frequently.
        model_name = info.data.get('FASTEMBED_MODEL_NAME', DEFAULT_FASTEMBED_MODEL)
        expected_dim = -1
        if 'all-MiniLM-L6-v2' in model_name: expected_dim = 384
        elif 'bge-small-en-v1.5' in model_name: expected_dim = 384
        elif 'bge-large-en-v1.5' in model_name: expected_dim = 1024

        if expected_dim != -1 and v != expected_dim:
            logging.warning(f"Configured EMBEDDING_DIMENSION ({v}) differs from standard dimension ({expected_dim}) for model '{model_name}'. Ensure this is intentional.")
        elif expected_dim == -1:
            logging.warning(f"Unknown standard embedding dimension for model '{model_name}'. Using configured dimension {v}. Verify this matches the actual model output.")
        logging.debug(f"Using EMBEDDING_DIMENSION: {v} for model: {model_name}")
        return v

# --- Global Settings Instance ---
# Setup temporary logger for loading phase
temp_log = logging.getLogger("query_service.config.loader")
if not temp_log.handlers:
    handler = logging.StreamHandler(sys.stdout)
    formatter = logging.Formatter('%(levelname)s: [%(name)s] %(message)s')
    handler.setFormatter(formatter)
    temp_log.addHandler(handler)
    temp_log.setLevel(logging.INFO) # Set to INFO or DEBUG as needed

try:
    temp_log.info("Loading Query Service settings...")
    settings = Settings()
    # Log loaded settings (omitting secrets)
    temp_log.info("Query Service Settings Loaded Successfully:")
    log_data = settings.model_dump(exclude={'POSTGRES_PASSWORD', 'GEMINI_API_KEY'})
    for key, value in log_data.items():
        temp_log.info(f"  {key}: {value}")
    temp_log.info(f"  POSTGRES_PASSWORD: *** SET ***")
    temp_log.info(f"  GEMINI_API_KEY: *** SET ***")

except (ValidationError, ValueError) as e:
    # Log detailed validation errors
    error_details = ""
    if isinstance(e, ValidationError):
        try:
            # Use Pydantic v2 error formatting
             error_details = f"\nValidation Errors:\n{json.dumps(e.errors(), indent=2)}"
        except Exception:
             # Fallback for unexpected formatting issues
             error_details = f"\nRaw Errors: {e}" # Simple string representation
    else: # Handle plain ValueError from custom validators
        error_details = f"\nError: {e}"

    temp_log.critical(f"!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
    temp_log.critical(f"! FATAL: Query Service configuration validation failed!{error_details}")
    temp_log.critical(f"! Check environment variables (prefixed with QUERY_) or .env file.")
    # temp_log.critical(f"! Original Error Traceback:", exc_info=True) # Optional: include traceback
    temp_log.critical(f"!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
    sys.exit(1) # Exit if configuration fails
except Exception as e:
    temp_log.exception(f"FATAL: Unexpected error loading Query Service settings: {e}")
    sys.exit(1)