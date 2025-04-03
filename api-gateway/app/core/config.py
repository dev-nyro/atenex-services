# File: app/core/config.py
# api-gateway/app/core/config.py
import os
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field, validator, ValidationError # Importar validator y ValidationError
from functools import lru_cache
import sys
import logging
from typing import Optional, List # Añadir List
import uuid # Para validar UUID

# URLs por defecto si no se especifican en el entorno (típico para K8s)
K8S_INGEST_SVC_URL_DEFAULT = "http://ingest-api-service.nyro-develop.svc.cluster.local:80"
K8S_QUERY_SVC_URL_DEFAULT = "http://query-service.nyro-develop.svc.cluster.local:80"

# ----- ELIMINAR ESTA LÍNEA -----
# from app.core.config import settings # Importar settings ya parseadas y validadas <--- ¡¡¡ELIMINAR!!!
# -------------------------------

class Settings(BaseSettings):
    # Configuración de Pydantic-Settings
    model_config = SettingsConfigDict(
        env_file='.env', # Buscar archivo .env
        env_prefix='GATEWAY_', # Buscar variables de entorno con prefijo GATEWAY_
        case_sensitive=False, # Insensible a mayúsculas/minúsculas
        env_file_encoding='utf-8',
        extra='ignore' # Ignorar variables extra en el entorno/archivo .env
    )

    # Información del Proyecto
    PROJECT_NAME: str = "Nyro API Gateway"
    API_V1_STR: str = "/api/v1"

    # URLs de Servicios Backend (Obligatorias)
    # Pydantic-settings buscará GATEWAY_INGEST_SERVICE_URL y GATEWAY_QUERY_SERVICE_URL
    # Si no se encuentran y no hay default, lanzará ValidationError
    INGEST_SERVICE_URL: str = K8S_INGEST_SVC_URL_DEFAULT
    QUERY_SERVICE_URL: str = K8S_QUERY_SVC_URL_DEFAULT
    AUTH_SERVICE_URL: Optional[str] = None # Opcional (GATEWAY_AUTH_SERVICE_URL)

    # Configuración JWT (Obligatoria)
    # Buscará GATEWAY_JWT_SECRET y GATEWAY_JWT_ALGORITHM
    JWT_SECRET: str # Obligatorio, sin valor por defecto inseguro
    JWT_ALGORITHM: str = "HS256" # Valor por defecto común para Supabase

    # Configuración Supabase Admin (Obligatoria)
    # Buscará GATEWAY_SUPABASE_URL y GATEWAY_SUPABASE_SERVICE_ROLE_KEY
    SUPABASE_URL: str # Obligatorio
    SUPABASE_SERVICE_ROLE_KEY: str # Obligatorio, sin valor por defecto inseguro

    # Configuración de Asociación de Compañía
    # Buscará GATEWAY_DEFAULT_COMPANY_ID
    DEFAULT_COMPANY_ID: Optional[str] = None # Opcional, pero necesario para la asociación

    # Configuración General
    LOG_LEVEL: str = "INFO"
    HTTP_CLIENT_TIMEOUT: int = 60 # Timeout en segundos para llamadas downstream
    # Nombre corregido según logs iniciales (aunque el código usaba KEEPALIAS)
    # Mantendremos el nombre que Pydantic buscará (GATEWAY_HTTP_CLIENT_MAX_KEEPALIAS_CONNECTIONS)
    # Si el nombre real de la variable de entorno es diferente, Pydantic fallará.
    HTTP_CLIENT_MAX_KEEPALIAS_CONNECTIONS: int = 20 # Máximo conexiones keep-alive
    HTTP_CLIENT_MAX_CONNECTIONS: int = 100 # Máximo conexiones totales

    # Validadores Pydantic
    @validator('JWT_SECRET')
    def check_jwt_secret(cls, v):
        if not v or v == "YOUR_DEFAULT_JWT_SECRET_KEY_CHANGE_ME_IN_ENV_OR_SECRET":
            raise ValueError("GATEWAY_JWT_SECRET is not set or uses the insecure default value.")
        # Podría añadirse una validación de longitud mínima si se desea
        return v

    @validator('SUPABASE_SERVICE_ROLE_KEY')
    def check_supabase_service_key(cls, v):
        if not v or v == "YOUR_SUPABASE_SERVICE_ROLE_KEY_HERE":
            raise ValueError("GATEWAY_SUPABASE_SERVICE_ROLE_KEY is not set or uses the insecure default value.")
        # Podría añadirse validación de formato si Supabase tiene uno específico (ej. longitud)
        return v

    @validator('SUPABASE_URL')
    def check_supabase_url(cls, v):
        if not v or not v.startswith("https://"):
             raise ValueError("GATEWAY_SUPABASE_URL must be a valid HTTPS URL.")
        # Podría validarse el formato más estrictamente
        return v

    @validator('DEFAULT_COMPANY_ID', always=True) # always=True para que se ejecute incluso si es None
    def check_default_company_id(cls, v):
        if v is not None: # Solo validar si se proporciona un valor
            try:
                uuid.UUID(v)
            except ValueError:
                raise ValueError(f"GATEWAY_DEFAULT_COMPANY_ID ('{v}') is not a valid UUID.")
        # Si es None, es válido (aunque la lógica de asociación fallará si se necesita)
        return v

    @validator('LOG_LEVEL')
    def check_log_level(cls, v):
        valid_levels = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
        if v.upper() not in valid_levels:
            raise ValueError(f"Invalid LOG_LEVEL '{v}'. Must be one of {valid_levels}")
        return v.upper() # Normalizar a mayúsculas

# Usar lru_cache para asegurar que las settings se cargan una sola vez
@lru_cache()
def get_settings() -> Settings:
    # Configurar un logger temporal BÁSICO para la carga de settings
    # Esto evita problemas si el logging completo aún no está configurado
    temp_log = logging.getLogger(__name__)
    if not temp_log.handlers:
        temp_log.addHandler(logging.StreamHandler(sys.stdout))
        temp_log.setLevel(logging.INFO) # Usar INFO para ver mensajes de carga

    temp_log.info("Loading Gateway settings...")
    try:
        # Pydantic-settings leerá las variables de entorno/archivo .env
        # y ejecutará los validadores
        settings_instance = Settings()

        # Loguear valores cargados (excepto secretos)
        temp_log.info("Gateway Settings Loaded Successfully:")
        temp_log.info(f"  PROJECT_NAME: {settings_instance.PROJECT_NAME}")
        temp_log.info(f"  INGEST_SERVICE_URL: {settings_instance.INGEST_SERVICE_URL}")
        temp_log.info(f"  QUERY_SERVICE_URL: {settings_instance.QUERY_SERVICE_URL}")
        temp_log.info(f"  AUTH_SERVICE_URL: {settings_instance.AUTH_SERVICE_URL or 'Not Set'}")
        temp_log.info(f"  JWT_SECRET: *** SET (Validated) ***")
        temp_log.info(f"  JWT_ALGORITHM: {settings_instance.JWT_ALGORITHM}")
        temp_log.info(f"  SUPABASE_URL: {settings_instance.SUPABASE_URL}")
        temp_log.info(f"  SUPABASE_SERVICE_ROLE_KEY: *** SET (Validated) ***")
        if settings_instance.DEFAULT_COMPANY_ID:
            temp_log.info(f"  DEFAULT_COMPANY_ID: {settings_instance.DEFAULT_COMPANY_ID}")
        else:
            temp_log.warning("  DEFAULT_COMPANY_ID: Not Set (Company association endpoint will fail if called)")
        temp_log.info(f"  LOG_LEVEL: {settings_instance.LOG_LEVEL}")
        temp_log.info(f"  HTTP_CLIENT_TIMEOUT: {settings_instance.HTTP_CLIENT_TIMEOUT}")
        temp_log.info(f"  HTTP_CLIENT_MAX_CONNECTIONS: {settings_instance.HTTP_CLIENT_MAX_CONNECTIONS}")
        temp_log.info(f"  HTTP_CLIENT_MAX_KEEPALIAS_CONNECTIONS: {settings_instance.HTTP_CLIENT_MAX_KEEPALIAS_CONNECTIONS}")

        return settings_instance

    except ValidationError as e:
        # Captura errores de validación de Pydantic (incluyendo los validadores personalizados)
        temp_log.critical("!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
        temp_log.critical("! FATAL: Error validating Gateway settings:")
        for error in e.errors():
            temp_log.critical(f"!  - {' -> '.join(map(str, error['loc']))}: {error['msg']}")
        temp_log.critical("!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
        sys.exit("FATAL: Invalid Gateway configuration. Check logs.")
    except Exception as e:
        # Captura cualquier otro error durante la carga
        temp_log.exception(f"FATAL: Unexpected error loading Gateway settings: {e}")
        sys.exit(f"FATAL: Unexpected error loading Gateway settings: {e}")

# Crear instancia global de settings
settings = get_settings()