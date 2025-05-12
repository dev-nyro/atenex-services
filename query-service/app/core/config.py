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
# MILVUS_K8S_DEFAULT_URI = "http://milvus-standalone.nyro-develop.svc.cluster.local:19530" # No longer default
ZILLIZ_ENDPOINT_DEFAULT = "https://in03-0afab716eb46d7f.serverless.gcp-us-west1.cloud.zilliz.com"
MILVUS_DEFAULT_COLLECTION = "document_chunks_minilm"
MILVUS_DEFAULT_EMBEDDING_FIELD = "embedding"
MILVUS_DEFAULT_CONTENT_FIELD = "content"
MILVUS_DEFAULT_COMPANY_ID_FIELD = "company_id"
MILVUS_DEFAULT_DOCUMENT_ID_FIELD = "document_id"
MILVUS_DEFAULT_FILENAME_FIELD = "file_name"
MILVUS_DEFAULT_GRPC_TIMEOUT = 15
MILVUS_DEFAULT_SEARCH_PARAMS = {"metric_type": "IP", "params": {"nprobe": 10}}
MILVUS_DEFAULT_METADATA_FIELDS = ["company_id", "document_id", "file_name", "page", "title"]

# Embedding Service
EMBEDDING_SERVICE_K8S_URL_DEFAULT = "http://embedding-service.nyro-develop.svc.cluster.local:8003"
# Reranker Service
RERANKER_SERVICE_K8S_URL_DEFAULT = "http://reranker-service.nyro-develop.svc.cluster.local:80"


# ==========================================================================================
#  ATENEX SYSTEM PROMPT ADAPTADO - v1.3 (Mayo 2025)
# ==========================================================================================
# Adaptado del System Prompt detallado, enfocado en instrucciones para el LLM en tiempo de ejecución.

ATENEX_RAG_PROMPT_TEMPLATE = r"""
════════════════════════════════════════════════════════════════════
A T E N E X · INSTRUCCIONES DE GENERACIÓN (Gemini 2.5 Flash)
════════════════════════════════════════════════════════════════════

1 · IDENTIDAD Y TONO
Eres **Atenex**, un asistente de IA experto en consulta de documentación empresarial (PDFs, manuales, etc.). Eres profesional, directo, verificable y empático. Escribe en **español latino** claro y conciso. Prioriza la precisión y la seguridad.

2 · PRINCIPIOS CLAVE
   - **BASATE SOLO EN EL CONTEXTO:** Usa *únicamente* la información del HISTORIAL RECIENTE y los DOCUMENTOS RECUPERADOS a continuación. **No inventes**, especules ni uses conocimiento externo. Si la respuesta no está explícitamente en el contexto, indícalo claramente.
   - **CITACIÓN:** Cuando uses información de un documento recuperado, añade una cita al final de la frase o párrafo relevante indicando el documento y página. Formato preferido: `[Doc N]`. El formato detallado de la fuente se listará al final.
   - **NO ESPECULACIÓN:** Si la información solicitada no se encuentra, responde claramente: "No encontré información específica sobre eso en los documentos proporcionados ni en el historial reciente." y, si es posible, sugiere cómo el usuario podría encontrarla (ej. probar otros términos, revisar un documento específico si parece muy relevante).
   - **NO CÓDIGO:** No generes bloques de código ejecutable. Puedes explicar conceptos o pseudocódigo si es útil.
   - **PREGUNTAS AMPLIAS:** Si la pregunta es muy general (ej. "dame todo sobre X"), en lugar de intentar resumir todo, identifica 1-3 temas clave cubiertos en los documentos y formula preguntas aclaratorias al usuario para enfocar la consulta. Ejemplo: "Los documentos mencionan X en relación a Y y Z. ¿Te interesa algún aspecto en particular?".
   - **PETICIONES DE ARCHIVOS:** Si el usuario pide explícitamente "el PDF" o "el documento" y varios chunks recuperados pertenecen al mismo archivo (identificable por `doc.meta.file_name`), tu respuesta debe indicar que tienes información *proveniente* de ese archivo específico, resumir lo más relevante según la consulta, y citarlo. **No puedes entregar el archivo binario**, solo responder basado en su contenido textual.

3 · CONTEXTO PROPORCIONADO
{% if chat_history %}
───────────────────── HISTORIAL RECIENTE (Más antiguo a más nuevo) ─────────────────────
{{ chat_history }}
────────────────────────────────────────────────────────────────────────────────────────
{% endif %}
──────────────────── DOCUMENTOS RECUPERADOS (Ordenados por relevancia) ──────────────────
{% for doc in documents %}
[Doc {{ loop.index }}] «{{ doc.meta.file_name | default("Archivo Desconocido") }}»
 Título : {{ doc.meta.title | default("Sin Título") }}
 Página : {{ doc.meta.page | default("?") }}
 Score  : {{ "%.3f"|format(doc.score) if doc.score is not none else "N/A" }}
 Extracto: {{ doc.content | trim }}
--------------------------------------------------------------------------------------------{% endfor %}
────────────────────────────────────────────────────────────────────────────────────────

4 · PREGUNTA ACTUAL DEL USUARIO
{{ query }}

5 · FORMATO DE RESPUESTA REQUERIDO
   - **Respuesta Principal:** Escribe una respuesta natural, fluida y conversacional directamente abordando la pregunta del usuario. Si la respuesta es extensa (más de ~160 palabras), puedes iniciar con un breve resumen ejecutivo (1-2 frases). Integra la información clave y las citas `[Doc N]` donde corresponda. Evita usar explícitamente etiquetas como "Respuesta directa:" o "Siguiente acción sugerida:". Concluye la respuesta sugiriendo un próximo paso lógico o una pregunta relacionada si tiene sentido para continuar la conversación productivamente.
   - **Sección de Fuentes:** Obligatoriamente después de tu respuesta principal, añade una sección titulada `**Fuentes:**`. Lista aquí *solo* los documentos que **efectivamente utilizaste** para construir tu respuesta (basándote en las citas `[Doc N]` que incluiste). Usa una lista numerada ordenada por relevancia (el Doc 1 suele ser el más relevante). Formato:
     `1. «Nombre Archivo» (Título: <Título si existe>, Pág: <Página>)`
     `2. «Otro Archivo» (Pág: <Página>)`
     ... (No incluyas documentos que no citaste en la respuesta principal).

════════════════════════════════════════════════════════════════════
RESPUESTA DE ATENEX (en español latino):
════════════════════════════════════════════════════════════════════
"""

# ------------------------------------------------------------------------------

ATENEX_GENERAL_PROMPT_TEMPLATE = r"""
Eres **Atenex**, el Gestor de Conocimiento Empresarial. Responde de forma útil, concisa y profesional en español latino.

{% if chat_history %}
───────────────────── HISTORIAL RECIENTE (Más antiguo a más nuevo) ─────────────────────
{{ chat_history }}
────────────────────────────────────────────────────────────────────────────────────────
{% endif %}

PREGUNTA ACTUAL DEL USUARIO:
{{ query }}

INSTRUCCIONES:
- Basándote únicamente en el HISTORIAL RECIENTE (si existe) y tu conocimiento general como asistente, responde la pregunta.
- Si la consulta requiere información específica de documentos que no te han sido proporcionados en esta interacción (porque no se activó el RAG), indica amablemente que no tienes acceso a documentos externos para responder y sugiere al usuario subir un documento relevante o precisar su pregunta si busca información documental.
- NO inventes información que no tengas.

RESPUESTA DE ATENEX (en español latino):
"""
# Models
DEFAULT_EMBEDDING_DIMENSION = 384
DEFAULT_GEMINI_MODEL = "gemini-1.5-flash-latest"

# RAG Pipeline Parameters
DEFAULT_RETRIEVER_TOP_K = 100
DEFAULT_BM25_ENABLED: bool = True
DEFAULT_RERANKER_ENABLED: bool = True
DEFAULT_DIVERSITY_FILTER_ENABLED: bool = False
DEFAULT_MAX_CONTEXT_CHUNKS: int = 75
DEFAULT_HYBRID_ALPHA: float = 0.5
DEFAULT_DIVERSITY_LAMBDA: float = 0.5
DEFAULT_MAX_PROMPT_TOKENS: int = 500000
DEFAULT_MAX_CHAT_HISTORY_MESSAGES = 10
DEFAULT_NUM_SOURCES_TO_SHOW = 7


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

    # --- Vector Store (Milvus/Zilliz) ---
    ZILLIZ_API_KEY: SecretStr = Field(description="API Key for Zilliz Cloud connection.")
    MILVUS_URI: AnyHttpUrl = Field(default_factory=lambda: AnyHttpUrl(ZILLIZ_ENDPOINT_DEFAULT))

    @field_validator('MILVUS_URI', mode='before')
    @classmethod
    def validate_milvus_uri(cls, v: Any) -> AnyHttpUrl:
        if not isinstance(v, str):
            raise ValueError("MILVUS_URI must be a string.")
        v_strip = v.strip()
        if not v_strip.startswith("https://"):
            raise ValueError(f"Invalid Zilliz URI: Must start with https://. Received: '{v_strip}'")
        try:
            validated_url = AnyHttpUrl(v_strip)
            return validated_url
        except Exception as e:
            raise ValueError(f"Invalid Milvus URI format '{v_strip}': {e}") from e

    @field_validator('ZILLIZ_API_KEY', mode='before')
    @classmethod
    def check_zilliz_key(cls, v: Any, info: ValidationInfo) -> Any:
        # Allow SecretStr to be passed, check its content
        if isinstance(v, SecretStr):
            secret_value = v.get_secret_value()
            if secret_value is None or secret_value == "":
                raise ValueError(f"Required secret field 'QUERY_ZILLIZ_API_KEY' cannot be empty.")
        elif v is None or v == "": # Handle direct string or None input before SecretStr conversion
            raise ValueError(f"Required secret field 'QUERY_ZILLIZ_API_KEY' cannot be empty.")
        return v

    MILVUS_COLLECTION_NAME: str = Field(default=MILVUS_DEFAULT_COLLECTION)
    MILVUS_EMBEDDING_FIELD: str = Field(default=MILVUS_DEFAULT_EMBEDDING_FIELD)
    MILVUS_CONTENT_FIELD: str = Field(default=MILVUS_DEFAULT_CONTENT_FIELD)
    MILVUS_COMPANY_ID_FIELD: str = Field(default=MILVUS_DEFAULT_COMPANY_ID_FIELD)
    MILVUS_DOCUMENT_ID_FIELD: str = Field(default=MILVUS_DEFAULT_DOCUMENT_ID_FIELD)
    MILVUS_FILENAME_FIELD: str = Field(default=MILVUS_DEFAULT_FILENAME_FIELD)
    MILVUS_METADATA_FIELDS: List[str] = Field(default=MILVUS_DEFAULT_METADATA_FIELDS)
    MILVUS_GRPC_TIMEOUT: int = Field(default=MILVUS_DEFAULT_GRPC_TIMEOUT)
    MILVUS_SEARCH_PARAMS: Dict[str, Any] = Field(default_factory=lambda: MILVUS_DEFAULT_SEARCH_PARAMS.copy())


    # --- Embedding Settings (General) ---
    EMBEDDING_DIMENSION: int = Field(default=DEFAULT_EMBEDDING_DIMENSION, description="Dimension of embeddings, used for Milvus and validation.")

    # --- External Embedding Service ---
    EMBEDDING_SERVICE_URL: AnyHttpUrl = Field(default_factory=lambda: AnyHttpUrl(EMBEDDING_SERVICE_K8S_URL_DEFAULT), description="URL of the Atenex Embedding Service.")
    EMBEDDING_CLIENT_TIMEOUT: int = Field(default=30, description="Timeout in seconds for calls to the Embedding Service.")


    # --- LLM (Google Gemini) ---
    GEMINI_API_KEY: SecretStr
    GEMINI_MODEL_NAME: str = Field(default=DEFAULT_GEMINI_MODEL)

    # --- Reranker Settings ---
    RERANKER_ENABLED: bool = Field(default=DEFAULT_RERANKER_ENABLED)
    RERANKER_SERVICE_URL: AnyHttpUrl = Field(default_factory=lambda: AnyHttpUrl(RERANKER_SERVICE_K8S_URL_DEFAULT), description="URL of the Atenex Reranker Service.")
    RERANKER_CLIENT_TIMEOUT: int = Field(default=30, description="Timeout in seconds for calls to the Reranker Service.")


    # --- Sparse Retriever (BM25) ---
    BM25_ENABLED: bool = Field(default=DEFAULT_BM25_ENABLED)

    # --- Diversity Filter ---
    DIVERSITY_FILTER_ENABLED: bool = Field(default=DEFAULT_DIVERSITY_FILTER_ENABLED)
    MAX_CONTEXT_CHUNKS: int = Field(default=DEFAULT_MAX_CONTEXT_CHUNKS, gt=0, description="Max number of retrieved/reranked chunks to pass to LLM context.")
    QUERY_DIVERSITY_LAMBDA: float = Field(default=DEFAULT_DIVERSITY_LAMBDA, ge=0.0, le=1.0, description="Lambda for MMR diversity (0=max diversity, 1=max relevance).")

    # --- RAG Pipeline Parameters ---
    RETRIEVER_TOP_K: int = Field(default=DEFAULT_RETRIEVER_TOP_K, gt=0, le=500)
    HYBRID_FUSION_ALPHA: float = Field(default=DEFAULT_HYBRID_ALPHA, ge=0.0, le=1.0)
    RAG_PROMPT_TEMPLATE: str = Field(default=ATENEX_RAG_PROMPT_TEMPLATE)
    GENERAL_PROMPT_TEMPLATE: str = Field(default=ATENEX_GENERAL_PROMPT_TEMPLATE)
    MAX_PROMPT_TOKENS: Optional[int] = Field(default=DEFAULT_MAX_PROMPT_TOKENS)
    MAX_CHAT_HISTORY_MESSAGES: int = Field(default=DEFAULT_MAX_CHAT_HISTORY_MESSAGES, ge=0)
    NUM_SOURCES_TO_SHOW: int = Field(default=DEFAULT_NUM_SOURCES_TO_SHOW, ge=0)

    # --- Service Client Config ---
    HTTP_CLIENT_TIMEOUT: int = Field(default=60) # General timeout for httpx client
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
         # Allow SecretStr to be passed, check its content
        if isinstance(v, SecretStr):
            secret_value = v.get_secret_value()
            if secret_value is None or secret_value == "":
                raise ValueError(f"Required secret field 'QUERY_{info.field_name.upper()}' cannot be empty.")
        elif v is None or v == "": # Handle direct string or None input
            raise ValueError(f"Required secret field 'QUERY_{info.field_name.upper()}' cannot be empty.")
        return v


    @field_validator('EMBEDDING_DIMENSION')
    @classmethod
    def check_embedding_dimension(cls, v: int, info: ValidationInfo) -> int:
        if v <= 0:
            raise ValueError("EMBEDDING_DIMENSION must be a positive integer.")
        logging.info(f"Configured EMBEDDING_DIMENSION: {v}. This will be used for Milvus and validated against the embedding service.")
        return v

    @field_validator('MAX_CONTEXT_CHUNKS')
    @classmethod
    def check_max_context_chunks(cls, v: int, info: ValidationInfo) -> int:
        retriever_k = info.data.get('RETRIEVER_TOP_K', DEFAULT_RETRIEVER_TOP_K)
        max_possible_after_fusion = retriever_k * 2 # Simplistic assumption, could be just retriever_k if no sparse
        if v > max_possible_after_fusion:
            logging.warning(f"MAX_CONTEXT_CHUNKS ({v}) is greater than a typical max after fusion based on RETRIEVER_TOP_K ({retriever_k}). Effective limit will be applied.")
        if v <= 0:
             raise ValueError("MAX_CONTEXT_CHUNKS must be a positive integer.")
        return v

    @field_validator('NUM_SOURCES_TO_SHOW')
    @classmethod
    def check_num_sources_to_show(cls, v: int, info: ValidationInfo) -> int:
        max_chunks = info.data.get('MAX_CONTEXT_CHUNKS', DEFAULT_MAX_CONTEXT_CHUNKS)
        if v > max_chunks:
             logging.warning(f"NUM_SOURCES_TO_SHOW ({v}) is greater than MAX_CONTEXT_CHUNKS ({max_chunks}). Will only show up to {max_chunks} sources.")
             return max_chunks
        if v < 0:
            raise ValueError("NUM_SOURCES_TO_SHOW cannot be negative.")
        return v


# --- Global Settings Instance ---
temp_log = logging.getLogger("query_service.config.loader")
if not temp_log.handlers:
    handler = logging.StreamHandler(sys.stdout)
    formatter = logging.Formatter('%(levelname)s: [%(asctime)s] [%(name)s] %(message)s') # Added asctime
    handler.setFormatter(formatter)
    temp_log.addHandler(handler)
    temp_log.setLevel(logging.INFO)

try:
    temp_log.info("Loading Query Service settings...")
    settings = Settings()
    temp_log.info("--- Query Service Settings Loaded ---")
    # Log settings, excluding sensitive ones and long templates by default
    excluded_fields = {'POSTGRES_PASSWORD', 'GEMINI_API_KEY', 'ZILLIZ_API_KEY', 'RAG_PROMPT_TEMPLATE', 'GENERAL_PROMPT_TEMPLATE'}
    log_data = settings.model_dump(exclude=excluded_fields)

    for key, value in log_data.items():
        temp_log.info(f"  {key.upper()}: {value}")

    # Log status of sensitive fields and templates
    pg_pass_status = '*** SET ***' if settings.POSTGRES_PASSWORD and settings.POSTGRES_PASSWORD.get_secret_value() else '!!! NOT SET !!!'
    temp_log.info(f"  POSTGRES_PASSWORD:            {pg_pass_status}")
    gemini_key_status = '*** SET ***' if settings.GEMINI_API_KEY and settings.GEMINI_API_KEY.get_secret_value() else '!!! NOT SET !!!'
    temp_log.info(f"  GEMINI_API_KEY:               {gemini_key_status}")
    zilliz_api_key_status = '*** SET ***' if settings.ZILLIZ_API_KEY and settings.ZILLIZ_API_KEY.get_secret_value() else '!!! NOT SET !!!'
    temp_log.info(f"  ZILLIZ_API_KEY:               {zilliz_api_key_status}") # ADDED LOG FOR ZILLIZ_API_KEY
    temp_log.info(f"  RAG_PROMPT_TEMPLATE:          Present (length: {len(settings.RAG_PROMPT_TEMPLATE)})")
    temp_log.info(f"  GENERAL_PROMPT_TEMPLATE:      Present (length: {len(settings.GENERAL_PROMPT_TEMPLATE)})")
    temp_log.info(f"------------------------------------")


except (ValidationError, ValueError) as e:
    error_details = ""
    if isinstance(e, ValidationError):
        try: error_details = f"\nValidation Errors:\n{json.dumps(e.errors(), indent=2)}"
        except Exception: error_details = f"\nRaw Errors: {e}" # Fallback for e.errors()
    else: error_details = f"\nError: {e}"
    temp_log.critical(f"!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
    temp_log.critical(f"! FATAL: Query Service configuration validation failed!{error_details}")
    temp_log.critical(f"! Check environment variables (prefixed with QUERY_) or .env file.")
    temp_log.critical(f"!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
    sys.exit(1)
except Exception as e:
    temp_log.exception(f"FATAL: Unexpected error loading Query Service settings: {e}")
    sys.exit(1)