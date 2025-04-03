# api-gateway/app/routers/auth_router.py
from fastapi import APIRouter, Depends, HTTPException, status, Request, Body
from pydantic import BaseModel, EmailStr, Field
from typing import Annotated, Dict, Any, Optional
import structlog
import uuid

# Dependencias (Aunque no se usen ahora, las dejamos por si se añaden otras rutas)
from app.utils.supabase_admin import get_supabase_admin_client
from supabase import Client as SupabaseClient
from gotrue.errors import AuthApiError
from postgrest.exceptions import APIError as PostgrestError

from app.core.config import settings

log = structlog.get_logger(__name__)
# Cambiado el prefijo o eliminarlo si ya no hay rutas de auth manejadas aquí
# router = APIRouter(prefix="/api/v1/auth", tags=["Authentication"])
router = APIRouter() # Sin prefijo, o comenta la inclusión en main.py

# --- Pydantic Models (Ya no se usan para registro) ---
# class RegisterPayload(BaseModel):
#    email: EmailStr
#    password: str = Field(..., min_length=8)
#    name: Optional[str] = Field(None, min_length=2)
#
# class RegisterResponse(BaseModel):
#    message: str
#    user_id: Optional[uuid.UUID] = None


# --- Endpoint de Registro ELIMINADO ---
# La ruta POST /api/v1/auth/register ya no existirá o devolverá 404/501
# si el router está montado pero la ruta no está definida,
# o si el Auth Service Proxy (si estaba habilitado) ya no se usa.

# Puedes añadir aquí otros endpoints de auth si los gestiona el gateway
# (ej: refrescar token, obtener perfil propio), pero si no, este
# archivo puede quedar vacío o eliminarse junto con su inclusión en main.py.

log.info("Auth router loaded (registration endpoint removed/disabled).")