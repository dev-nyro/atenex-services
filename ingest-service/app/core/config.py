# ingest-service/app/core/config.py
import logging
import os
from typing import Optional, List, Any, Dict, Union
from pydantic_settings import BaseSettings, SettingsConfigDict
# *** CORREGIDO: Importar sólo lo necesario de Pydantic ***
from pydantic import (
    RedisDsn, AnyHttpUrl, SecretStr, Field, validator, ValidationError,
    ValidationInfo # Importar ValidationInfo para V2 si fuera necesario (no lo es aquí)
)
import sys
import json # Para parsear MILVUS_INDEX_PARAMS/SEARCH_PARAMS

# --- Service Names en K8s ---
POSTGRES_K8S_SVC = "postgresql.nyro-develop.svc.cluster.local"           # Namespace: nyro-develop
MINIO_K8S_SVC = "minio-service.nyro-develop.svc.cluster.local"            # Namespace: nyro-develop
MILVUS_K8S_SVC = "milvus-milvus.default.svc.cluster.local"                # Namespace: default
REDIS_K8S_SVC = "redis-service-master.nyro-develop.svc.cluster.local"     # Namespace: nyro-develop

# --- Defaults ---
POSTGRES_K8S_PORT_DEFAULT = 5432
POSTGRES_K8S_DB_DEFAULT = "atenex" # Base de datos para Atenex
POSTGRES_K8S_USER_DEFAULT = "postgres" # Usuario por defecto
MINIO_K8S_PORT_DEFAULT = 9000
MINIO_BUCKET_DEFAULT = "atenex" # Bucket específico
MILVUS_K8S_PORT_DEFAULT = 19530
REDIS_K8S_PORT_DEFAULT = 6379
MILVUS_DEFAULT_COLLECTION = "atenex_doc_chunks" # Nombre de colección más específico
MILVUS_DEFAULT_INDEX_PARAMS = '{"metric_type": "COSINE", "index_type": "HNSW", "params": {"M": 16, "efConstruction": 256}}'
MILVUS_DEFAULT_SEARCH_PARAMS = '{"metric_type": "COSINE", "params": {"ef": 128}}'
OPENAI_DEFAULT_EMBEDDING_MODEL = "text-embedding-3-small"
DEFAULT_EMBEDDING_DIM = 1536 # Para text-embedding-3-small / ada-002

class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file='.env',
        env_prefix='INGEST_',
        env_file_encoding='utf-8',
        case_sensitive=False,
        extra='ignore'
    )

    # --- General ---
    PROJECT_NAME: str = "Atenex Ingest Service"
    API_V1_STR: str = "/api/v1/ingest" # Prefijo base para este servicio
    LOG_LEVEL: str = "INFO"

    # --- Celery (Usando Redis en K8s 'nyro-develop') ---
    CELERY_BROKER_URL: RedisDsn = RedisDsn(f"redis://{REDIS_K8S_SVC}:{REDIS_K8S_PORT_DEFAULT}/0")
    CELERY_RESULT_BACKEND: RedisDsn = RedisDsn(f"redis://{REDIS_K8S_SVC}:{REDIS_K8S_PORT_DEFAULT}/1")

    # --- Database (PostgreSQL Directo en K8s 'nyro-develop') ---
    POSTGRES_USER: str = POSTGRES_K8S_USER_DEFAULT
    POSTGRES_PASSWORD: SecretStr
    POSTGRES_SERVER: str = POSTGRES_K8S_SVC
    POSTGRES_PORT: int = POSTGRES_K8S_PORT_DEFAULT
    POSTGRES_DB: str = POSTGRES_K8S_DB_DEFAULT

    # --- Milvus (en K8s 'default') ---
    MILVUS_URI: str = f"http://{MILVUS_K8S_SVC}:{MILVUS_K8S_PORT_DEFAULT}"
    MILVUS_COLLECTION_NAME: str = MILVUS_DEFAULT_COLLECTION
    MILVUS_INDEX_PARAMS: Dict[str, Any] = Field(default_factory=lambda: json.loads(MILVUS_DEFAULT_INDEX_PARAMS)) # Usar default_factory
    MILVUS_SEARCH_PARAMS: Dict[str, Any] = Field(default_factory=lambda: json.loads(MILVUS_DEFAULT_SEARCH_PARAMS)) # Usar default_factory
    MILVUS_CONTENT_FIELD: str = "content"
    MILVUS_EMBEDDING_FIELD: str = "embedding"
    MILVUS_METADATA_FIELDS: List[str] = Field(default=[
        "company_id", "document_id", "file_name", "file_type",
    ])

    # --- MinIO Storage (en K8s 'nyro-develop', bucket 'atenex') ---
    MINIO_ENDPOINT: str = f"{MINIO_K8S_SVC}:{MINIO_K8S_PORT_DEFAULT}"
    MINIO_ACCESS_KEY: SecretStr
    MINIO_SECRET_KEY: SecretStr
    MINIO_BUCKET_NAME: str = MINIO_BUCKET_DEFAULT
    MINIO_USE_SECURE: bool = False

    # --- External Services (OpenAI) ---
    OPENAI_API_KEY: SecretStr
    OPENAI_EMBEDDING_MODEL: str = OPENAI_DEFAULT_EMBEDDING_MODEL
    EMBEDDING_DIMENSION: int = DEFAULT_EMBEDDING_DIM

    # --- Service Client Config (Genérico) ---
    HTTP_CLIENT_TIMEOUT: int = 60
    HTTP_CLIENT_MAX_RETRIES: int = 2
    HTTP_CLIENT_BACKOFF_FACTOR: float = 1.0

    # --- File Processing & Haystack ---
    SUPPORTED_CONTENT_TYPES: List[str] = Field(default=[
        "application/pdf",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document", # DOCX
        "text/plain",
        "text/markdown",
        "text/html",
    ])
    SPLITTER_CHUNK_SIZE: int = 500
    SPLITTER_CHUNK_OVERLAP: int = 50
    SPLITTER_SPLIT_BY: str = "word"

    # --- Validadores (Pydantic V2 Style) ---

    # No se necesita un validador para el nivel de log si se usan Enums o Literal,
    # pero si se usa string, este validador es útil.
    @validator("LOG_LEVEL")
    def check_log_level(cls, v: str) -> str:
        valid_levels = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
        normalized_v = v.upper()
        if normalized_v not in valid_levels:
            raise ValueError(f"Invalid LOG_LEVEL '{v}'. Must be one of {valid_levels}")
        return normalized_v

    # Usar root_validator o model_validator para parsear params de Milvus si vienen como JSON string
    # O definir los campos como string y parsearlos donde se usen.
    # El enfoque con Field(default_factory=...) es más simple si los defaults son fijos.
    # Si necesitas leerlos como JSON string de env vars, un model_validator es mejor:
    # from pydantic import model_validator
    # @model_validator(mode='before')
    # def parse_milvus_json_params(cls, values: Dict[str, Any]) -> Dict[str, Any]:
    #     for key in ['MILVUS_INDEX_PARAMS', 'MILVUS_SEARCH_PARAMS']:
    #         if key in values and isinstance(values[key], str):
    #             try: values[key] = json.loads(values[key])
    #             except json.JSONDecodeError: raise ValueError(f"{key} must be a valid JSON string")
    #     return values

    @validator('EMBEDDING_DIMENSION', pre=True, always=True)
    def set_embedding_dimension(cls, v: Optional[int], values: Dict[str, Any]) -> int:
        # Esta forma V1 de acceder a 'values' puede funcionar con always=True,
        # pero es menos robusta que usar un model_validator en V2.
        # Sin embargo, no causa el error específico de los logs.
        model = values.get('OPENAI_EMBEDDING_MODEL', OPENAI_DEFAULT_EMBEDDING_MODEL)
        if model == "text-embedding-3-large": calculated_dim = 3072
        elif model in ["text-embedding-3-small", "text-embedding-ada-002"]: calculated_dim = 1536
        else: return v if v is not None else DEFAULT_EMBEDDING_DIM

        if v is not None and v != calculated_dim:
             # Usar logging estándar aquí porque el logger de structlog aún no está configurado
             logging.warning(f"Provided EMBEDDING_DIMENSION {v} conflicts with model {model} ({calculated_dim} expected). Using calculated value: {calculated_dim}")
             return calculated_dim
        return calculated_dim

    # *** CORREGIDO: Validador para secretos compatible con Pydantic V2 ***
    # Aplicar individualmente a cada campo secreto
    @validator('POSTGRES_PASSWORD', 'MINIO_ACCESS_KEY', 'MINIO_SECRET_KEY', 'OPENAI_API_KEY', mode='before')
    def check_secret_value_present(cls, v: Any) -> Any:
        # Este validador se ejecuta antes de que el valor se convierta a SecretStr
        # Verifica que la variable de entorno o el valor del .env no esté vacío.
        # Pydantic se encargará de validar si es un SecretStr válido después.
        if v is None or v == "":
             # Es difícil obtener el nombre del campo aquí sin `info` (que no está disponible en mode='before')
             # o sin hacerlo específico por campo. Lanzar un error genérico.
             raise ValueError("Required secret field cannot be empty.")
        return v


# --- Instancia Global ---
temp_log = logging.getLogger("ingest_service.config.loader")
# (resto del código de inicialización y logging de config sin cambios) ...
try:
    temp_log.info("Loading Ingest Service settings...")
    settings = Settings()
    temp_log.info("Ingest Service Settings Loaded Successfully:")
    # (mensajes de log sin cambios)...
except (ValidationError, ValueError) as e:
    error_details = ""
    if isinstance(e, ValidationError):
        try: error_details = f"\nValidation Errors:\n{e.json(indent=2)}"
        except Exception: error_details = f"\nRaw Errors: {e.errors()}"
    temp_log.critical(f"!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
    temp_log.critical(f"! FATAL: Ingest Service configuration validation failed:{error_details}")
    temp_log.critical(f"! Check environment variables (prefixed with INGEST_) or .env file.")
    temp_log.critical(f"! Original Error: {e}")
    temp_log.critical(f"!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
    sys.exit(1)
except Exception as e:
    temp_log.exception(f"FATAL: Unexpected error loading Ingest Service settings: {e}")
    sys.exit(1)