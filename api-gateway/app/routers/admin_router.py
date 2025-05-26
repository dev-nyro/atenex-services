# File: app/routers/admin_router.py
# api-gateway/app/routers/admin_router.py
from fastapi import APIRouter, Depends, HTTPException, status, Request, Path, Query
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
    UserCreateRequest, UserResponse, # UserResponse será usado por el nuevo endpoint
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
    admin_user_payload: AdminAuth # Protegido: Solo admins pueden acceder
):
    admin_id = admin_user_payload.get("sub")
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
    admin_user_payload: AdminAuth # Protegido
):
    admin_id = admin_user_payload.get("sub")
    req_id = getattr(request.state, 'request_id', 'N/A')
    company_log = log.bind(request_id=req_id, admin_user_id=admin_id, company_name=company_data.name)
    company_log.info("Admin attempting to create a new company.")

    try:
        new_company = await postgres_client.create_company(name=company_data.name)
        company_log.info("Company created successfully", new_company_id=str(new_company['id']))
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
    admin_user_payload: AdminAuth # Protegido
):
    admin_id = admin_user_payload.get("sub")
    req_id = getattr(request.state, 'request_id', 'N/A')
    list_log = log.bind(request_id=req_id, admin_user_id=admin_id)
    list_log.info("Admin requested list of active companies for select.")

    try:
        companies = await postgres_client.get_active_companies_select()
        list_log.info(f"Retrieved {len(companies)} companies for select.")
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
    admin_user_payload: AdminAuth # Protegido
):
    admin_id = admin_user_payload.get("sub")
    req_id = getattr(request.state, 'request_id', 'N/A')
    user_log = log.bind(request_id=req_id, admin_user_id=admin_id, new_user_email=user_data.email, target_company_id=str(user_data.company_id))
    user_log.info("Admin attempting to create a new user.")

    try:
        email_exists = await postgres_client.check_email_exists(user_data.email)
        if email_exists:
            user_log.warning("User creation failed: Email already exists.")
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"Email '{user_data.email}' already registered.")
    except Exception as e:
        user_log.exception("Error checking email existence during user creation", error=str(e))
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Error checking email availability.")

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

    try:
        hashed_password = get_password_hash(user_data.password)
        new_user = await postgres_client.create_user(
            email=user_data.email,
            hashed_password=hashed_password,
            name=user_data.name,
            company_id=user_data.company_id,
            roles=user_data.roles
        )
        user_log.info("User created successfully", new_user_id=str(new_user['id']))
        return UserResponse.model_validate(new_user)

    except asyncpg.UniqueViolationError:
         user_log.warning("User creation conflict (likely email).")
         raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"Email '{user_data.email}' already registered.")
    except asyncpg.ForeignKeyViolationError:
         user_log.warning("User creation failed due to non-existent company ID.")
         raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Company with ID '{user_data.company_id}' not found.")
    except Exception as e:
        user_log.exception("Error creating user in database", error=str(e))
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to create user.")

# --- NUEVO ENDPOINT ---
@router.get(
    "/users/by_company/{companyId}",
    response_model=List[UserResponse],
    summary="Listar usuarios por ID de compañía",
    description="Obtiene una lista paginada de usuarios pertenecientes a una compañía específica. Requiere rol de administrador."
)
async def list_users_by_company(
    request: Request,
    admin_user_payload: AdminAuth, # Moved before parameters with default values
    companyId: uuid.UUID = Path(..., description="ID de la compañía cuyos usuarios se desean listar."),
    limit: int = Query(50, ge=1, le=200, description="Número máximo de usuarios a devolver."),
    offset: int = Query(0, ge=0, description="Offset para paginación.")
):
    admin_id_from_token = admin_user_payload.get("sub")
    # X-Company-ID del admin (opcional para auditoría), viene del token si existe.
    admin_company_id_for_audit = admin_user_payload.get("company_id")
    # X-User-ID header es el ID del admin, que es admin_id_from_token
    # X-Company-ID header (opcional para auditoria) es admin_company_id_for_audit

    req_id = getattr(request.state, 'request_id', 'N/A')

    endpoint_log = log.bind(
        request_id=req_id,
        admin_user_id=admin_id_from_token,
        admin_company_id_for_audit=str(admin_company_id_for_audit) if admin_company_id_for_audit else "N/A",
        target_company_id_path=str(companyId),
        limit_query=limit,
        offset_query=offset
    )
    endpoint_log.info("Admin request to list users by company.")

    # 1. Verificar que la compañía (del path param) exista
    target_company = await postgres_client.get_company_by_id(companyId)
    if not target_company:
        endpoint_log.warning("Target company specified in path not found.", company_id_path=str(companyId))
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Company with ID '{companyId}' not found."
        )
    
    # (Opcional) Podrías querer verificar si la compañía está activa si es un requisito.
    # if not target_company.get('is_active', False):
    #     endpoint_log.warning("Target company is not active.")
    #     raise HTTPException(
    #         status_code=status.HTTP_400_BAD_REQUEST,
    #         detail=f"Company with ID '{companyId}' is not active. Cannot list users for inactive company."
    #     )

    # 2. Obtener usuarios de la compañía especificada en el path
    try:
        users_data = await postgres_client.get_users_by_company_id_paginated(
            company_id=companyId, # Usar el companyId del path
            limit=limit,
            offset=offset
        )
        endpoint_log.info(f"Successfully retrieved {len(users_data)} users from DB for target company.")
        
        # FastAPI se encargará de validar cada item de la lista contra UserResponse
        # y serializarlo correctamente.
        return users_data
        
    except Exception as e:
        endpoint_log.exception("Error retrieving users by company from database.", error=str(e))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to retrieve users for the company."
        )