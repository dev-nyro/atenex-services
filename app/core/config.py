import logging
import os
from typing import Optional, List
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import RedisDsn, PostgresDsn, AnyHttpUrl, SecretStr, Field

# Define niveles de log para que Pydantic los reconozca
LogLevel = logging.getLevelName # type: ignore

class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file='.env',
        env_file_encoding='utf-8',
        case_sensitive=False,
        extra='ignore'
    )

    # --- General ---
    PROJECT_NAME: str = "Ingest Service (Haystack)"
    API_V1_STR: str = "/api/v1"
    LOG_LEVEL: LogLevel = logging.INFO # type: ignore

    # --- Celery ---
    CELERY_BROKER_URL: RedisDsn = RedisDsn("redis://localhost:6379/0") # type: ignore
    CELERY_RESULT_BACKEND: RedisDsn = RedisDsn("redis://localhost:6379/1") # type: ignore

    # --- Databases ---
    POSTGRES_DSN: PostgresDsn = PostgresDsn("postgresql+asyncpg://user:pass@host:port/db") # type: ignore
    MILVUS_URI: str = "http://localhost:19530"
    MILVUS_COLLECTION_NAME: str = "document_chunks_haystack" # Consider renaming
    MILVUS_INDEX_PARAMS: dict = Field(default={ # Default index params
        "metric_type": "COSINE", # OpenAI embeddings often use cosine similarity
        "index_type": "HNSW",
        "params": {"M": 16, "efConstruction": 256}
    })
    MILVUS_SEARCH_PARAMS: dict = Field(default={"metric_type": "COSINE", "params": {"ef": 128}})


    # --- External Services (Only Storage and potentially OCR remain) ---
    STORAGE_SERVICE_URL: AnyHttpUrl = AnyHttpUrl("http://localhost:8001/api/v1/storage") # type: ignore
    # Optional: Keep OCR service if needed for images/scanned PDFs
    OCR_SERVICE_URL: Optional[AnyHttpUrl] = None # type: ignore # Example: "http://localhost:8002/api/v1/ocr"

    # --- Service Client Config ---
    HTTP_CLIENT_TIMEOUT: int = 60 # Increased timeout for potentially longer calls
    HTTP_CLIENT_MAX_RETRIES: int = 2
    HTTP_CLIENT_BACKOFF_FACTOR: float = 1.0

    # --- File Processing & Haystack ---
    SUPPORTED_CONTENT_TYPES: List[str] = Field(default=[
        "application/pdf",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document", # docx
        "text/plain",
        "text/markdown",
        "text/html",
        "image/jpeg", # Keep if OCR service is used
        "image/png",  # Keep if OCR service is used
    ])
    # Content types definitely needing the external OCR service
    EXTERNAL_OCR_REQUIRED_CONTENT_TYPES: List[str] = Field(default=[
        "image/jpeg",
        "image/png",
    ])
    # Haystack DocumentSplitter settings
    SPLITTER_CHUNK_SIZE: int = 500
    SPLITTER_CHUNK_OVERLAP: int = 50
    SPLITTER_SPLIT_BY: str = "word" # or "sentence", "passage"

    # --- OpenAI ---
    OPENAI_API_KEY: SecretStr = SecretStr("sk-...") # Loaded from env var OPENAI_API_KEY
    OPENAI_EMBEDDING_MODEL: str = "text-embedding-3-small" # Or "text-embedding-ada-002", "text-embedding-3-large"
    # Dimension needs to match the model
    # text-embedding-3-small: 1536
    # text-embedding-ada-002: 1536
    # text-embedding-3-large: 3072
    EMBEDDING_DIMENSION: int = 1536 # Adjust based on OPENAI_EMBEDDING_MODEL

    # --- MilvusDocumentStore Fields ---
    # Ensure these match your Milvus schema and needs
    MILVUS_CONTENT_FIELD: str = "content"
    MILVUS_EMBEDDING_FIELD: str = "embedding"
    MILVUS_METADATA_FIELDS: List[str] = Field(default=["company_id", "document_id", "file_name", "file_type"]) # Fields from Haystack Doc.meta to store


settings = Settings()

# Override Milvus dimension based on selected model if needed (simple example)
if settings.OPENAI_EMBEDDING_MODEL == "text-embedding-3-large":
    settings.EMBEDDING_DIMENSION = 3072
elif settings.OPENAI_EMBEDDING_MODEL in ["text-embedding-3-small", "text-embedding-ada-002"]:
    settings.EMBEDDING_DIMENSION = 1536

# Add OPENAI_API_KEY to environment if not set, for Haystack Secret loading
# In production, this should absolutely be set externally (k8s secret, etc.)
if "OPENAI_API_KEY" not in os.environ and settings.OPENAI_API_KEY:
     os.environ["OPENAI_API_KEY"] = settings.OPENAI_API_KEY.get_secret_value()