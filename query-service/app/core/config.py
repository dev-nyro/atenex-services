# query-service/app/core/config.py
import logging
import os
from typing import Optional, List, Any, Dict
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import AnyHttpUrl, SecretStr, Field, field_validator, ValidationError, ValidationInfo
import sys
import json
from pathlib import Path 

# --- Default Values ---
# PostgreSQL
POSTGRES_K8S_HOST_DEFAULT = "postgresql-service.nyro-develop.svc.cluster.local"
POSTGRES_K8S_PORT_DEFAULT = 5432
POSTGRES_K8S_DB_DEFAULT = "atenex"
POSTGRES_K8S_USER_DEFAULT = "postgres"

# Milvus
ZILLIZ_ENDPOINT_DEFAULT = "https://in03-0afab716eb46d7f.serverless.gcp-us-west1.cloud.zilliz.com"
MILVUS_DEFAULT_COLLECTION = "atenex_collection"
MILVUS_DEFAULT_EMBEDDING_FIELD = "embedding"
MILVUS_DEFAULT_CONTENT_FIELD = "content"
MILVUS_DEFAULT_COMPANY_ID_FIELD = "company_id"
MILVUS_DEFAULT_DOCUMENT_ID_FIELD = "document_id"
MILVUS_DEFAULT_FILENAME_FIELD = "file_name"
MILVUS_DEFAULT_GRPC_TIMEOUT = 15
MILVUS_DEFAULT_SEARCH_PARAMS = {"metric_type": "IP", "params": {"nprobe": 10}}
MILVUS_DEFAULT_METADATA_FIELDS = ["company_id", "document_id", "file_name", "page", "title"]

# Embedding Service
EMBEDDING_SERVICE_K8S_URL_DEFAULT = "http://embedding-service.nyro-develop.svc.cluster.local:80" 
# Reranker Service
RERANKER_SERVICE_K8S_URL_DEFAULT = "http://reranker-service.nyro-develop.svc.cluster.local:80" 
# Sparse Search Service
SPARSE_SEARCH_SERVICE_K8S_URL_DEFAULT = "http://sparse-search-service.nyro-develop.svc.cluster.local:80" 


# --- Paths for Prompt Templates ---
PROMPT_DIR = Path(__file__).resolve().parent.parent / "prompts"
DEFAULT_RAG_PROMPT_TEMPLATE_PATH = str(PROMPT_DIR / "rag_template_gemini_v2.txt")
DEFAULT_GENERAL_PROMPT_TEMPLATE_PATH = str(PROMPT_DIR / "general_template_gemini_v2.txt")
DEFAULT_MAP_PROMPT_TEMPLATE_PATH = str(PROMPT_DIR / "map_prompt_template.txt")
DEFAULT_REDUCE_PROMPT_TEMPLATE_PATH = str(PROMPT_DIR / "reduce_prompt_template_v2.txt")


# Models
DEFAULT_EMBEDDING_DIMENSION = 1536
DEFAULT_GEMINI_MODEL = "gemini-2.5-flash-preview-04-17" 
DEFAULT_GEMINI_MAX_OUTPUT_TOKENS = 16384

# RAG Pipeline Parameters
DEFAULT_RETRIEVER_TOP_K = 200 
DEFAULT_BM25_ENABLED: bool = True
DEFAULT_RERANKER_ENABLED: bool = True
DEFAULT_DIVERSITY_FILTER_ENABLED: bool = False 
DEFAULT_MAX_CONTEXT_CHUNKS: int = 200 
DEFAULT_HYBRID_ALPHA: float = 0.5
DEFAULT_DIVERSITY_LAMBDA: float = 0.5
DEFAULT_MAX_PROMPT_TOKENS: int = 500000 
DEFAULT_MAX_CHAT_HISTORY_MESSAGES = 10 # Reducido de 20 para ser más conservador con el tamaño del prompt
DEFAULT_NUM_SOURCES_TO_SHOW = 7

# MapReduce settings
DEFAULT_MAPREDUCE_ENABLED: bool = True 
DEFAULT_MAPREDUCE_CHUNK_BATCH_SIZE: int = 5
DEFAULT_MAPREDUCE_ACTIVATION_THRESHOLD: int = 25 
DEFAULT_TIKTOKEN_ENCODING_NAME: str = "cl100k_base"


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
        if isinstance(v, SecretStr):
            secret_value = v.get_secret_value()
            if secret_value is None or secret_value == "":
                raise ValueError(f"Required secret field 'QUERY_ZILLIZ_API_KEY' cannot be empty.")
        elif v is None or v == "":
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
    GEMINI_MAX_OUTPUT_TOKENS: int = Field(default=DEFAULT_GEMINI_MAX_OUTPUT_TOKENS, gt=0)


    # --- Reranker Settings ---
    RERANKER_ENABLED: bool = Field(default=DEFAULT_RERANKER_ENABLED)
    RERANKER_SERVICE_URL: AnyHttpUrl = Field(default_factory=lambda: AnyHttpUrl(RERANKER_SERVICE_K8S_URL_DEFAULT), description="URL of the Atenex Reranker Service.")
    RERANKER_CLIENT_TIMEOUT: int = Field(default=30, description="Timeout in seconds for calls to the Reranker Service.")

    # --- Sparse Retriever (Remote Service) ---
    BM25_ENABLED: bool = Field(default=DEFAULT_BM25_ENABLED, description="Enables/disables the sparse search step (uses sparse-search-service).")
    SPARSE_SEARCH_SERVICE_URL: AnyHttpUrl = Field(default_factory=lambda: AnyHttpUrl(SPARSE_SEARCH_SERVICE_K8S_URL_DEFAULT), description="URL of the Atenex Sparse Search Service.")
    SPARSE_SEARCH_CLIENT_TIMEOUT: int = Field(default=30, description="Timeout for calls to Sparse Search Service.")


    # --- Diversity Filter ---
    DIVERSITY_FILTER_ENABLED: bool = Field(default=DEFAULT_DIVERSITY_FILTER_ENABLED)
    MAX_CONTEXT_CHUNKS: int = Field(default=DEFAULT_MAX_CONTEXT_CHUNKS, gt=0, description="Max number of retrieved/reranked chunks to pass to LLM context in Direct RAG, or to Diversity Filter.")
    QUERY_DIVERSITY_LAMBDA: float = Field(default=DEFAULT_DIVERSITY_LAMBDA, ge=0.0, le=1.0, description="Lambda for MMR diversity (0=max diversity, 1=max relevance).")

    # --- RAG Pipeline Parameters ---
    RETRIEVER_TOP_K: int = Field(default=DEFAULT_RETRIEVER_TOP_K, gt=0, le=500)
    HYBRID_FUSION_ALPHA: float = Field(default=DEFAULT_HYBRID_ALPHA, ge=0.0, le=1.0)
    
    # Prompt template paths
    RAG_PROMPT_TEMPLATE_PATH: str = Field(default=DEFAULT_RAG_PROMPT_TEMPLATE_PATH)
    GENERAL_PROMPT_TEMPLATE_PATH: str = Field(default=DEFAULT_GENERAL_PROMPT_TEMPLATE_PATH)
    MAP_PROMPT_TEMPLATE_PATH: str = Field(default=DEFAULT_MAP_PROMPT_TEMPLATE_PATH)
    REDUCE_PROMPT_TEMPLATE_PATH: str = Field(default=DEFAULT_REDUCE_PROMPT_TEMPLATE_PATH)

    MAX_PROMPT_TOKENS: Optional[int] = Field(default=DEFAULT_MAX_PROMPT_TOKENS, description="Token threshold to activate MapReduce if enabled. Also a general guide for LLM context window size.")
    MAX_CHAT_HISTORY_MESSAGES: int = Field(default=DEFAULT_MAX_CHAT_HISTORY_MESSAGES, ge=0)
    NUM_SOURCES_TO_SHOW: int = Field(default=DEFAULT_NUM_SOURCES_TO_SHOW, ge=0)

    # --- MapReduce Settings ---
    MAPREDUCE_ENABLED: bool = Field(default=DEFAULT_MAPREDUCE_ENABLED)
    MAPREDUCE_CHUNK_BATCH_SIZE: int = Field(default=DEFAULT_MAPREDUCE_CHUNK_BATCH_SIZE, gt=0)
    MAPREDUCE_ACTIVATION_THRESHOLD_CHUNKS: int = Field(default=DEFAULT_MAPREDUCE_ACTIVATION_THRESHOLD, gt=0, description="Chunk count threshold to activate MapReduce (secondary to token threshold).")
    TIKTOKEN_ENCODING_NAME: str = Field(default=DEFAULT_TIKTOKEN_ENCODING_NAME, description="Encoding name for tiktoken.")


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
        if isinstance(v, SecretStr):
            secret_value = v.get_secret_value()
            if secret_value is None or secret_value == "":
                raise ValueError(f"Required secret field 'QUERY_{info.field_name.upper()}' cannot be empty.")
        elif v is None or v == "":
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
        if v <= 0:
             raise ValueError("MAX_CONTEXT_CHUNKS must be a positive integer.")
        return v
    
    @field_validator('MAPREDUCE_CHUNK_BATCH_SIZE')
    @classmethod
    def check_mapreduce_batch_size(cls, v: int, info: ValidationInfo) -> int:
        if v <=0:
            raise ValueError("MAPREDUCE_CHUNK_BATCH_SIZE must be positive.")
        if v > 20: 
            logging.warning(f"MAPREDUCE_CHUNK_BATCH_SIZE ({v}) is quite large. Ensure LLM can handle this many docs in a single map prompt.")
        return v
        
    @field_validator('MAPREDUCE_ACTIVATION_THRESHOLD_CHUNKS') 
    @classmethod
    def check_mapreduce_activation_threshold_chunks(cls, v: int, info: ValidationInfo) -> int:
        if v <= 0:
            raise ValueError("MAPREDUCE_ACTIVATION_THRESHOLD_CHUNKS must be positive.")
        return v


    @field_validator('NUM_SOURCES_TO_SHOW')
    @classmethod
    def check_num_sources_to_show(cls, v: int, info: ValidationInfo) -> int:
        max_chunks = info.data.get('MAX_CONTEXT_CHUNKS', DEFAULT_MAX_CONTEXT_CHUNKS)
        if v > max_chunks:
             logging.warning(f"NUM_SOURCES_TO_SHOW ({v}) is greater than MAX_CONTEXT_CHUNKS ({max_chunks}). Will only show up to {max_chunks} sources if MapReduce is not used.")
        if v < 0:
            raise ValueError("NUM_SOURCES_TO_SHOW cannot be negative.")
        return v

    @field_validator('GEMINI_MAX_OUTPUT_TOKENS')
    @classmethod
    def check_gemini_max_output_tokens(cls, v: Optional[int]) -> Optional[int]:
        if v is not None and v <= 0:
            raise ValueError("GEMINI_MAX_OUTPUT_TOKENS, if set, must be a positive integer.")
        return v

# --- Global Settings Instance ---
temp_log = logging.getLogger("query_service.config.loader")
if not temp_log.handlers:
    handler = logging.StreamHandler(sys.stdout)
    formatter = logging.Formatter('%(levelname)s: [%(asctime)s] [%(name)s] %(message)s')
    handler.setFormatter(formatter)
    temp_log.addHandler(handler)
    temp_log.setLevel(logging.INFO)

try:
    temp_log.info("Loading Query Service settings...")
    settings = Settings()
    temp_log.info("--- Query Service Settings Loaded ---")
    
    excluded_fields = {'POSTGRES_PASSWORD', 'GEMINI_API_KEY', 'ZILLIZ_API_KEY'}
    log_data = settings.model_dump(exclude=excluded_fields)

    for key, value in log_data.items():
        if key.endswith("_PATH"): 
            try:
                path_obj = Path(value)
                status_msg = "Present and readable" if path_obj.is_file() and os.access(path_obj, os.R_OK) else "!!! NOT FOUND or UNREADABLE !!!"
                temp_log.info(f"  {key.upper()}: {value} (Status: {status_msg})")
            except Exception as path_e:
                temp_log.info(f"  {key.upper()}: {value} (Status: Error checking path: {path_e})")
        else:
            temp_log.info(f"  {key.upper()}: {value}")

    pg_pass_status = '*** SET ***' if settings.POSTGRES_PASSWORD and settings.POSTGRES_PASSWORD.get_secret_value() else '!!! NOT SET !!!'
    temp_log.info(f"  POSTGRES_PASSWORD:            {pg_pass_status}")
    gemini_key_status = '*** SET ***' if settings.GEMINI_API_KEY and settings.GEMINI_API_KEY.get_secret_value() else '!!! NOT SET !!!'
    temp_log.info(f"  GEMINI_API_KEY:               {gemini_key_status}")
    zilliz_api_key_status = '*** SET ***' if settings.ZILLIZ_API_KEY and settings.ZILLIZ_API_KEY.get_secret_value() else '!!! NOT SET !!!'
    temp_log.info(f"  ZILLIZ_API_KEY:               {zilliz_api_key_status}")
    temp_log.info(f"------------------------------------")

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