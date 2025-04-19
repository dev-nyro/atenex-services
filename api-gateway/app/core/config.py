# File: app/core/config.py
# api-gateway/app/core/config.py
import os
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field, validator, ValidationError, HttpUrl, SecretStr
from functools import lru_cache
import sys
import logging
from typing import Optional, List
import uuid # Para validar UUID

# URLs por defecto si no se especifican en el entorno (usando el namespace 'nyro-develop')
K8S_INGEST_SVC_URL_DEFAULT = "http://ingest-api-service.nyro-develop.svc.cluster.local:80"
K8S_QUERY_SVC_URL_DEFAULT = "http://query-service.atenex-develop.svc.cluster.local:80"
K8S_AUTH_SVC_URL_DEFAULT = None # No hay auth service por defecto

# PostgreSQL Kubernetes Default (usando el namespace 'atenex-develop')
POSTGRES_K8S_HOST_DEFAULT = "postgresql.atenex-develop.svc.cluster.local" # <-- K8s Service Name
POSTGRES_K8S_PORT_DEFAULT = 5432
POSTGRES_K8S_DB_DEFAULT = "atenex" # <-- Nombre de DB (asegúrate que coincida)
POSTGRES_K8S_USER_DEFAULT = "postgres" # <-- Usuario DB (asegúrate que coincida)

class Settings(BaseSettings):
    # Configuración de Pydantic-Settings
    model_config = SettingsConfigDict(
        env_file='.env',
        env_prefix='GATEWAY_',
        case_sensitive=False,
        env_file_encoding='utf-8',
        extra='ignore'
    )

    # Información del Proyecto
    PROJECT_NAME: str = "Atenex API Gateway" # <-- Nombre actualizado
    API_V1_STR: str = "/api/v1"

    # URLs de Servicios Backend
    INGEST_SERVICE_URL: HttpUrl = K8S_INGEST_SVC_URL_DEFAULT
    QUERY_SERVICE_URL: HttpUrl = K8S_QUERY_SVC_URL_DEFAULT
    AUTH_SERVICE_URL: Optional[HttpUrl] = K8S_AUTH_SVC_URL_DEFAULT

    # Configuración JWT (Obligatoria)
    JWT_SECRET: SecretStr # Obligatorio, usar SecretStr para seguridad
    JWT_ALGORITHM: str = "HS256"

    # Configuración PostgreSQL (Obligatoria)
    POSTGRES_USER: str = POSTGRES_K8S_USER_DEFAULT
    POSTGRES_PASSWORD: SecretStr # Obligatorio desde Secrets
    POSTGRES_SERVER: str = POSTGRES_K8S_HOST_DEFAULT
    POSTGRES_PORT: int = POSTGRES_K8S_PORT_DEFAULT
    POSTGRES_DB: str = POSTGRES_K8S_DB_DEFAULT

    # Configuración de Asociación de Compañía
    DEFAULT_COMPANY_ID: Optional[str] = None # UUID de compañía por defecto

    # Configuración General
    LOG_LEVEL: str = "INFO"
    HTTP_CLIENT_TIMEOUT: int = 60
    # Corregido: KEEPALIVE en lugar de KEEPALIAS
    HTTP_CLIENT_MAX_KEEPALIVE_CONNECTIONS: int = 100
    HTTP_CLIENT_MAX_CONNECTIONS: int = 200

    # CORS (Opcional - URLs de ejemplo, ajusta según necesites)
    VERCEL_FRONTEND_URL: Optional[str] = "https://atenex-frontend.vercel.app"
    # NGROK_URL: Optional[str] = None

    # Validadores Pydantic
    @validator('JWT_SECRET')
    def check_jwt_secret(cls, v):
        # get_secret_value() se usa al *usar* el secreto, aquí solo verificamos que no esté vacío
        if not v or v.get_secret_value() == "YOUR_DEFAULT_JWT_SECRET_KEY_CHANGE_ME_IN_ENV_OR_SECRET":
            raise ValueError("GATEWAY_JWT_SECRET is not set or uses an insecure default value.")
        return v

    @validator('DEFAULT_COMPANY_ID', always=True)
    def check_default_company_id_format(cls, v):
        if v is not None:
            try:
                uuid.UUID(str(v))
            except ValueError:
                raise ValueError(f"GATEWAY_DEFAULT_COMPANY_ID ('{v}') is not a valid UUID.")
        return v

    @validator('LOG_LEVEL')
    def check_log_level(cls, v):
        valid_levels = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
        normalized_v = v.upper()
        if normalized_v not in valid_levels:
            raise ValueError(f"Invalid LOG_LEVEL '{v}'. Must be one of {valid_levels}")
        return normalized_v

# Usar lru_cache para asegurar que las settings se cargan una sola vez
@lru_cache()
def get_settings() -> Settings:
    temp_log = logging.getLogger("atenex_api_gateway.config.loader")
    if not temp_log.handlers:
        handler = logging.StreamHandler(sys.stdout)
        formatter = logging.Formatter('%(levelname)s: %(message)s')
        handler.setFormatter(formatter)
        temp_log.addHandler(handler)
        temp_log.setLevel(logging.INFO)

    temp_log.info("Loading Atenex Gateway settings...")
    try:
        settings_instance = Settings()

        # Loguear valores cargados (excepto secretos)
        temp_log.info("Atenex Gateway Settings Loaded Successfully:")
        temp_log.info(f"  PROJECT_NAME: {settings_instance.PROJECT_NAME}")
        temp_log.info(f"  INGEST_SERVICE_URL: {str(settings_instance.INGEST_SERVICE_URL)}")
        temp_log.info(f"  QUERY_SERVICE_URL: {str(settings_instance.QUERY_SERVICE_URL)}")
        temp_log.info(f"  AUTH_SERVICE_URL: {str(settings_instance.AUTH_SERVICE_URL) if settings_instance.AUTH_SERVICE_URL else 'Not Set'}")
        temp_log.info(f"  JWT_SECRET: *** SET (Validated) ***")
        temp_log.info(f"  JWT_ALGORITHM: {settings_instance.JWT_ALGORITHM}")
        temp_log.info(f"  POSTGRES_SERVER: {settings_instance.POSTGRES_SERVER}")
        temp_log.info(f"  POSTGRES_PORT: {settings_instance.POSTGRES_PORT}")
        temp_log.info(f"  POSTGRES_DB: {settings_instance.POSTGRES_DB}")
        temp_log.info(f"  POSTGRES_USER: {settings_instance.POSTGRES_USER}")
        temp_log.info(f"  POSTGRES_PASSWORD: *** SET ***")
        if settings_instance.DEFAULT_COMPANY_ID:
            temp_log.info(f"  DEFAULT_COMPANY_ID: {settings_instance.DEFAULT_COMPANY_ID} (Validated as UUID if set)")
        else:
            temp_log.warning("  DEFAULT_COMPANY_ID: Not Set (Ensure-company endpoint requires this)")
        temp_log.info(f"  LOG_LEVEL: {settings_instance.LOG_LEVEL}")
        temp_log.info(f"  HTTP_CLIENT_TIMEOUT: {settings_instance.HTTP_CLIENT_TIMEOUT}")
        temp_log.info(f"  HTTP_CLIENT_MAX_CONNECTIONS: {settings_instance.HTTP_CLIENT_MAX_CONNECTIONS}")
        temp_log.info(f"  HTTP_CLIENT_MAX_KEEPALIVE_CONNECTIONS: {settings_instance.HTTP_CLIENT_MAX_KEEPALIVE_CONNECTIONS}")
        temp_log.info(f"  VERCEL_FRONTEND_URL: {settings_instance.VERCEL_FRONTEND_URL or 'Not Set'}")
        # temp_log.info(f"  NGROK_URL: {settings_instance.NGROK_URL or 'Not Set'}")

        return settings_instance

    except ValidationError as e:
        temp_log.critical("!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
        temp_log.critical("! FATAL: Error validating Atenex Gateway settings:")
        for error in e.errors():
            loc = " -> ".join(map(str, error['loc'])) if error.get('loc') else 'N/A'
            temp_log.critical(f"!  - {loc}: {error['msg']}")
        temp_log.critical("! Check your Kubernetes ConfigMap/Secrets or .env file.")
        temp_log.critical("!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
        sys.exit("FATAL: Invalid Atenex Gateway configuration. Check logs.")
    except Exception as e:
        temp_log.exception(f"FATAL: Unexpected error loading Atenex Gateway settings: {e}")
        sys.exit(f"FATAL: Unexpected error loading Atenex Gateway settings: {e}")

# Crear instancia global de settings
settings = get_settings()