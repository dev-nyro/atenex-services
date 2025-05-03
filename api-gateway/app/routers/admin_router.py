# File: app/routers/admin_router.py
# api-gateway/app/routers/admin_router.py
from fastapi import APIRouter, Depends, HTTPException, status, Request
from typing import List, Dict, Any
import structlog
import uuid
import asyncpg # Para capturar errores específicos de DB

# --- Dependencias y Servicios ---
from app.auth.auth_middleware import AdminAuth # Usar la dependencia AdminAuth
from app.db import postgres_client
from app.auth.auth_service import get_password_hash

# --- Modelos Pydantic ---
from app.models.admin_models import (
    CompanyCreateRequest, CompanyResponse, CompanySelectItem,
    UserCreateRequest, UserResponse,
    UsersPerCompanyStat, AdminStatsResponse
)

log = structlog.get_logger(__name__)
router = APIRouter() # No añadir prefijo aquí, se añade en main.py

# --- Endpoints de Administración ---

@router.get(
    "/stats",
    response_model=AdminStatsResponse,
    summary="Obtener estadísticas generales",
    description="Devuelve el número total de compañías activas y el número de usuarios activos por compañía activa."
)
async def get_admin_stats(
    request: Request,
    admin_user: AdminAuth # Protegido: Solo admins pueden acceder
):
    admin_id = admin_user.get("sub")
    req_id = getattr(request.state, 'request_id', 'N/A')
    stats_log = log.bind(request_id=req_id, admin_user_id=admin_id)
    stats_log.info("Admin requested platform statistics.")

    try:
        company_count = await postgres_client.count_active_companies()
        users_per_company = await postgres_client.count_active_users_per_active_company()

        stats_log.info("Statistics retrieved successfully.", company_count=company_count, companies_with_users=len(users_per_company))
        return AdminStatsResponse(
            company_count=company_count,
            users_per_company=users_per_company
        )
    except Exception as e:
        stats_log.exception("Error retrieving admin statistics", error=str(e))
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to retrieve statistics.")


@router.post(
    "/companies",
    response_model=CompanyResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Crear una nueva compañía",
    description="Crea un nuevo registro de compañía en la base de datos."
)
async def create_new_company(
    request: Request,
    company_data: CompanyCreateRequest,
    admin_user: AdminAuth # Protegido
):
    admin_id = admin_user.get("sub")
    req_id = getattr(request.state, 'request_id', 'N/A')
    company_log = log.bind(request_id=req_id, admin_user_id=admin_id, company_name=company_data.name)
    company_log.info("Admin attempting to create a new company.")

    # Validación del nombre ya la hace Pydantic (min_length=1)
    try:
        new_company = await postgres_client.create_company(name=company_data.name)
        company_log.info("Company created successfully", new_company_id=str(new_company['id']))
        # Convertir a CompanyResponse (Pydantic v2 usa model_validate)
        return CompanyResponse.model_validate(new_company)
    except asyncpg.UniqueViolationError:
         company_log.warning("Failed to create company: Name likely already exists.")
         raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"Company name '{company_data.name}' may already exist.")
    except Exception as e:
        company_log.exception("Error creating company", error=str(e))
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to create company.")


@router.get(
    "/companies/select",
    response_model=List[CompanySelectItem],
    summary="Listar compañías para selector",
    description="Devuelve una lista simplificada (ID, Nombre) de compañías activas, ordenadas por nombre."
)
async def list_companies_for_select(
    request: Request,
    admin_user: AdminAuth # Protegido
):
    admin_id = admin_user.get("sub")
    req_id = getattr(request.state, 'request_id', 'N/A')
    list_log = log.bind(request_id=req_id, admin_user_id=admin_id)
    list_log.info("Admin requested list of active companies for select.")

    try:
        companies = await postgres_client.get_active_companies_select()
        list_log.info(f"Retrieved {len(companies)} companies for select.")
        # FastAPI maneja la conversión a List[CompanySelectItem] si los dicts coinciden
        return companies
    except Exception as e:
        list_log.exception("Error retrieving companies for select", error=str(e))
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to retrieve company list.")


@router.post(
    "/users",
    response_model=UserResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Crear un nuevo usuario",
    description="Crea un nuevo usuario, lo asocia a una compañía y le asigna roles."
)
async def create_new_user(
    request: Request,
    user_data: UserCreateRequest,
    admin_user: AdminAuth # Protegido
):
    admin_id = admin_user.get("sub")
    req_id = getattr(request.state, 'request_id', 'N/A')
    user_log = log.bind(request_id=req_id, admin_user_id=admin_id, new_user_email=user_data.email, target_company_id=str(user_data.company_id))
    user_log.info("Admin attempting to create a new user.")

    # --- Validaciones Adicionales ---
    # 1. Verificar si el email ya existe
    try:
        email_exists = await postgres_client.check_email_exists(user_data.email)
        if email_exists:
            user_log.warning("User creation failed: Email already exists.")
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"Email '{user_data.email}' already registered.")
    except Exception as e:
        user_log.exception("Error checking email existence during user creation", error=str(e))
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Error checking email availability.")

    # 2. Verificar si la compañía existe
    try:
        company = await postgres_client.get_company_by_id(user_data.company_id)
        if not company:
            user_log.warning("User creation failed: Target company not found.", company_id=str(user_data.company_id))
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Company with ID '{user_data.company_id}' not found.")
        if not company.get('is_active', False):
             user_log.warning("User creation failed: Target company is inactive.", company_id=str(user_data.company_id))
             raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Cannot assign user to inactive company '{user_data.company_id}'.")
    except Exception as e:
        user_log.exception("Error checking company existence during user creation", error=str(e))
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Error verifying target company.")

    # --- Creación del Usuario ---
    try:
        # Hashear contraseña
        hashed_password = get_password_hash(user_data.password)

        # Crear usuario en DB
        new_user = await postgres_client.create_user(
            email=user_data.email,
            hashed_password=hashed_password,
            name=user_data.name,
            company_id=user_data.company_id,
            roles=user_data.roles # Pasa la lista de roles
        )
        user_log.info("User created successfully", new_user_id=str(new_user['id']))
        # Convertir a UserResponse (Pydantic v2 usa model_validate)
        return UserResponse.model_validate(new_user)

    except asyncpg.UniqueViolationError: # Ya controlado arriba, pero por si acaso
         user_log.warning("User creation conflict (likely email).")
         raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"Email '{user_data.email}' already registered.")
    except asyncpg.ForeignKeyViolationError: # Ya controlado arriba, pero por si acaso
         user_log.warning("User creation failed due to non-existent company ID.")
         raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Company with ID '{user_data.company_id}' not found.")
    except Exception as e:
        user_log.exception("Error creating user in database", error=str(e))
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to create user.")