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
MILVUS_DEFAULT_COLLECTION = "document_chunks_minilm"
MILVUS_DEFAULT_EMBEDDING_FIELD = "embedding"
MILVUS_DEFAULT_CONTENT_FIELD = "content"
MILVUS_DEFAULT_COMPANY_ID_FIELD = "company_id"
MILVUS_DEFAULT_DOCUMENT_ID_FIELD = "document_id"
MILVUS_DEFAULT_FILENAME_FIELD = "file_name"
MILVUS_DEFAULT_GRPC_TIMEOUT = 15
MILVUS_DEFAULT_SEARCH_PARAMS = {"metric_type": "IP", "params": {"nprobe": 10}}
MILVUS_DEFAULT_METADATA_FIELDS = ["company_id", "document_id", "file_name", "page", "title"]


# ==========================================================================================
#  ATENEX PROMPT TEMPLATES – v1.2  (sustituir esta sección en config.py o prompt_builder.py)
# ==========================================================================================

ATENEX_RAG_PROMPT_TEMPLATE = r"""
Eres **Atenex**, el Gestor de Conocimiento Empresarial.
Actúa como un analista experto que lee, sintetiza y razona **solo** con los
documentos proporcionados. **Nunca** utilices conocimiento externo ni inventes
hechos.

PENSAMIENTO INTERNO (no lo muestres):
1. Lee cada documento y extrae los datos clave pertinentes a la pregunta.
2. Si la pregunta es demasiado amplia/ambigua (ej. “toda la información”),
   identifica los temas principales y prepara 1‑3 preguntas aclaratorias.
3. Decide si puedes responder o necesitas clarificar.
4. Planifica la respuesta siguiendo el FORMATO DE SALIDA.
5. Redacta la respuesta (sin revelar estos pasos).

──────────────────────── DOCUMENTOS ────────────────────────
{% for doc in documents %}
[Doc {{ loop.index }}] «{{ doc.meta.file_name | default("sin_nombre") }}»
· Título : {{ doc.meta.title | default("sin título") }}
· Página : {{ doc.meta.page | default("?") }}
· Extracto:
{{ doc.content }}
{% endfor %}
────────────────────────────────────────────────────────────

PREGUNTA DEL USUARIO: {{ query }}

──────────────────────── INSTRUCCIONES ─────────────────────
• Utiliza **únicamente** la información de los documentos.
• Si la respuesta no está, contesta:
  “No dispongo de información suficiente en los documentos proporcionados.”
• Si la pregunta es muy amplia/ambigüa, **no respondas aún**; formula las
  preguntas aclaratorias definidas en el paso 2 e indica los temas que puedes
  cubrir.
• En otro caso, responde siguiendo el FORMATO DE SALIDA.

────────────────────── FORMATO DE SALIDA ───────────────────
1. **Respuesta directa** – Clara, concisa y alineada al negocio.
2. **Resumen ejecutivo** (≤ 80 palabras) – *solo* si la respuesta supera
   160 palabras o el usuario lo solicita.
3. **Siguiente acción sugerida** – Pregunta o paso recomendado para avanzar.
4. **Fuentes** – Lista numerada (por relevancia):
     · Nombre de archivo · Título (si existe) · Página.

(Mantén exactamente este orden; no agregues secciones adicionales.)
"""

# ------------------------------------------------------------------------------

ATENEX_GENERAL_PROMPT_TEMPLATE = r"""
Eres **Atenex**, el Gestor de Conocimiento Empresarial.
Responde de forma útil y concisa.
Si la consulta requiere datos que aún no te han sido proporcionados mediante
RAG, indícalo amablemente y sugiere al usuario subir un documento o precisar
su pregunta.

Pregunta del usuario:
{{ query }}

Respuesta:
"""
# Models
DEFAULT_FASTEMBED_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
DEFAULT_FASTEMBED_QUERY_PREFIX = "query: "
DEFAULT_EMBEDDING_DIMENSION = 384
# --- LLM_CORRECTION: Ensure correct model name ---
DEFAULT_GEMINI_MODEL = "gemini-2.5-flash-preview-04-17"
DEFAULT_RERANKER_MODEL = "BAAI/bge-reranker-base"

# RAG Pipeline Parameters
# --- LLM_CORRECTION: Increase retrieval K substantially ---
DEFAULT_RETRIEVER_TOP_K = 100 # Increased from 5/10
DEFAULT_BM25_ENABLED: bool = True
DEFAULT_RERANKER_ENABLED: bool = True
DEFAULT_DIVERSITY_FILTER_ENABLED: bool = False # Keep disabled by default for max context
# --- LLM_CORRECTION: Rename and increase final context chunk limit ---
DEFAULT_MAX_CONTEXT_CHUNKS: int = 75 # Increased from 7/10 (Renamed from DIVERSITY_K_FINAL)
DEFAULT_HYBRID_ALPHA: float = 0.5
DEFAULT_DIVERSITY_LAMBDA: float = 0.5
# --- LLM_CORRECTION: Increase max prompt tokens significantly ---
DEFAULT_MAX_PROMPT_TOKENS: int = 500000 # Increased from 7000 for Gemini Flash 1.5 (aiming for 500k target)

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
    MILVUS_METADATA_FIELDS: List[str] = Field(default=MILVUS_DEFAULT_METADATA_FIELDS)
    MILVUS_GRPC_TIMEOUT: int = Field(default=MILVUS_DEFAULT_GRPC_TIMEOUT)
    MILVUS_SEARCH_PARAMS: Dict[str, Any] = Field(default=MILVUS_DEFAULT_SEARCH_PARAMS)

    # --- Embedding Model (FastEmbed) ---
    FASTEMBED_MODEL_NAME: str = Field(default=DEFAULT_FASTEMBED_MODEL)
    EMBEDDING_DIMENSION: int = Field(default=DEFAULT_EMBEDDING_DIMENSION)
    FASTEMBED_QUERY_PREFIX: str = Field(default=DEFAULT_FASTEMBED_QUERY_PREFIX)

    # --- LLM (Google Gemini) ---
    GEMINI_API_KEY: SecretStr
    GEMINI_MODEL_NAME: str = Field(default=DEFAULT_GEMINI_MODEL)

    # --- Reranker Model ---
    RERANKER_ENABLED: bool = Field(default=DEFAULT_RERANKER_ENABLED)
    RERANKER_MODEL_NAME: str = Field(default=DEFAULT_RERANKER_MODEL, description="Sentence Transformer model name/path for reranking.")

    # --- Sparse Retriever (BM25) ---
    BM25_ENABLED: bool = Field(default=DEFAULT_BM25_ENABLED)

    # --- Diversity Filter ---
    DIVERSITY_FILTER_ENABLED: bool = Field(default=DEFAULT_DIVERSITY_FILTER_ENABLED)
    # --- LLM_CORRECTION: Use renamed setting ---
    MAX_CONTEXT_CHUNKS: int = Field(default=DEFAULT_MAX_CONTEXT_CHUNKS, gt=0, description="Max number of retrieved/reranked chunks to pass to LLM context.")
    QUERY_DIVERSITY_LAMBDA: float = Field(default=DEFAULT_DIVERSITY_LAMBDA, ge=0.0, le=1.0, description="Lambda for MMR diversity (0=max diversity, 1=max relevance).")


    # --- RAG Pipeline Parameters ---
    # --- LLM_CORRECTION: Use updated default ---
    RETRIEVER_TOP_K: int = Field(default=DEFAULT_RETRIEVER_TOP_K, gt=0, le=500) # Allow up to 500 retrieval
    HYBRID_FUSION_ALPHA: float = Field(default=DEFAULT_HYBRID_ALPHA, ge=0.0, le=1.0, description="Weighting factor for dense vs sparse fusion (0=sparse, 1=dense). Used for simple linear fusion.")
    RAG_PROMPT_TEMPLATE: str = Field(default=ATENEX_RAG_PROMPT_TEMPLATE)
    GENERAL_PROMPT_TEMPLATE: str = Field(default=ATENEX_GENERAL_PROMPT_TEMPLATE)
    # --- LLM_CORRECTION: Use updated default ---
    MAX_PROMPT_TOKENS: Optional[int] = Field(default=DEFAULT_MAX_PROMPT_TOKENS)

    # --- Service Client Config ---
    HTTP_CLIENT_TIMEOUT: int = Field(default=60)
    HTTP_CLIENT_MAX_RETRIES: int = Field(default=2)
    HTTP_CLIENT_BACKOFF_FACTOR: float = Field(default=1.0)

    # --- Validators (No changes needed here) ---
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
        if v is None or (isinstance(v, str) and v == ""):
            field_name = info.field_name if info.field_name else "Unknown Secret Field"
            raise ValueError(f"Required secret field 'QUERY_{field_name.upper()}' cannot be empty.")
        return v

    @field_validator('EMBEDDING_DIMENSION')
    @classmethod
    def check_embedding_dimension(cls, v: int, info: ValidationInfo) -> int:
        if v <= 0:
            raise ValueError("EMBEDDING_DIMENSION must be a positive integer.")
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

    # --- LLM_CORRECTION: Add validator for max context chunks ---
    @field_validator('MAX_CONTEXT_CHUNKS')
    @classmethod
    def check_max_context_chunks(cls, v: int, info: ValidationInfo) -> int:
        retriever_k = info.data.get('RETRIEVER_TOP_K', DEFAULT_RETRIEVER_TOP_K)
        # fusion_fetch_k is retriever_k * 2 in the code logic
        max_possible_after_fusion = retriever_k * 2
        if v > max_possible_after_fusion:
            logging.warning(f"MAX_CONTEXT_CHUNKS ({v}) is greater than the maximum possible chunks after fusion ({max_possible_after_fusion} based on RETRIEVER_TOP_K={retriever_k}). Effective limit will be {max_possible_after_fusion}.")
            # We don't strictly need to cap 'v' here, the code logic will handle it, but warning is good.
        if v <= 0:
             raise ValueError("MAX_CONTEXT_CHUNKS must be a positive integer.")
        return v

# --- Global Settings Instance ---
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
    log_data = settings.model_dump(exclude={'POSTGRES_PASSWORD', 'GEMINI_API_KEY'})
    for key, value in log_data.items():
        temp_log.info(f"  {key}: {value}")
    temp_log.info(f"  POSTGRES_PASSWORD: *** SET ***")
    temp_log.info(f"  GEMINI_API_KEY: *** SET ***")

except (ValidationError, ValueError) as e:
    error_details = ""
    if isinstance(e, ValidationError):
        try: error_details = f"\nValidation Errors:\n{json.dumps(e.errors(), indent=2)}"
        except Exception: error_details = f"\nRaw Errors: {e}"
    else: error_details = f"\nError: {e}"
    temp_log.critical(f"!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
    temp_log.critical(f"! FATAL: Query Service configuration validation failed!{error_details}")
    temp_log.critical(f"! Check environment variables (prefixed with QUERY_) or .env file.")
    temp_log.critical(f"!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
    sys.exit(1)
except Exception as e:
    temp_log.exception(f"FATAL: Unexpected error loading Query Service settings: {e}")
    sys.exit(1)