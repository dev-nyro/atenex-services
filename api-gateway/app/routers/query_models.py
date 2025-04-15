# File: app/routers/query_models.py (NUEVO ARCHIVO)
# api-gateway/app/routers/query_models.py
from pydantic import BaseModel, Field
from typing import Optional
import uuid

class QueryAskRequest(BaseModel):
    """
    Modelo para el cuerpo de la petición POST /api/v1/query/ask.
    Debe coincidir con lo que envía el frontend y espera (implícitamente) el Query Service.
    """
    query: str = Field(..., description="The user's query or message.")
    chat_id: Optional[str] = Field(None, description="Optional existing chat ID (UUID as string).")
    retriever_top_k: Optional[int] = Field(None, ge=1, le=20, description="Optional number of documents to retrieve.")

    # Opcional: Añadir validación para chat_id si se proporciona
    # @validator('chat_id')
    # def validate_chat_id_format(cls, v):
    #     if v is not None:
    #         try:
    #             uuid.UUID(v)
    #         except ValueError:
    #             raise ValueError("Provided chat_id is not a valid UUID")
    #     return v