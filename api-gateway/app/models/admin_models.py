# File: app/models/admin_models.py
# api-gateway/app/models/admin_models.py
import uuid
from pydantic import BaseModel, EmailStr, Field, validator
from typing import List, Optional
from datetime import datetime

# --- Modelos para Compañías ---

class CompanyCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, description="Nombre de la nueva compañía.")

class CompanyResponse(BaseModel):
    id: uuid.UUID
    name: str
    created_at: datetime
    # Podríamos añadir is_active si fuera relevante en la respuesta
    # is_active: bool

    model_config = { # Anteriormente Config
        "from_attributes": True # Anteriormente orm_mode
    }

class CompanySelectItem(BaseModel):
    id: uuid.UUID
    name: str

    model_config = {
        "from_attributes": True
    }

# No necesitamos un modelo wrapper CompanyListSelectResponse, FastAPI puede devolver List[CompanySelectItem] directamente.

# --- Modelos para Usuarios ---

class UserCreateRequest(BaseModel):
    email: EmailStr = Field(..., description="Correo electrónico único del nuevo usuario.")
    password: str = Field(..., min_length=8, description="Contraseña del nuevo usuario (mínimo 8 caracteres).")
    name: Optional[str] = Field(None, description="Nombre completo del usuario (opcional).")
    company_id: uuid.UUID = Field(..., description="ID de la compañía a la que pertenece el usuario.")
    roles: List[str] = Field(default=['user'], description="Lista de roles asignados al usuario (ej. ['user', 'admin']).")

    @validator('roles', each_item=True)
    def check_role_values(cls, v):
        # Opcional: Validar que los roles sean de un conjunto permitido si es necesario
        allowed_roles = {'user', 'admin'} # Ejemplo
        if v not in allowed_roles:
             raise ValueError(f"Rol inválido: '{v}'. Roles permitidos: {allowed_roles}")
        return v

class UserResponse(BaseModel):
    id: uuid.UUID
    email: EmailStr
    name: Optional[str] = None
    company_id: uuid.UUID
    roles: List[str]
    is_active: bool
    created_at: datetime

    model_config = {
        "from_attributes": True
    }


# --- Modelos para Estadísticas ---

class UsersPerCompanyStat(BaseModel):
    company_id: uuid.UUID
    name: str
    user_count: int

    model_config = {
        "from_attributes": True
    }

class AdminStatsResponse(BaseModel):
    company_count: int = Field(..., description="Número total de compañías activas.")
    users_per_company: List[UsersPerCompanyStat] = Field(..., description="Lista de compañías activas con su conteo de usuarios activos.")