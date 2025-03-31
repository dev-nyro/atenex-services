# api-gateway/app/core/config.py
import os
from pydantic_settings import BaseSettings, SettingsConfigDict
from functools import lru_cache
import sys
import logging

# Usar nombres de servicio DNS de Kubernetes como defaults
# Asegúrate que los nombres ('ingest-api-service', 'query-service') y namespace ('nyro-develop') son correctos
K8S_INGEST_SVC_URL = "http://ingest-api-service.nyro-develop.svc.cluster.local:80"
K8S_QUERY_SVC_URL = "http://query-service.nyro-develop.svc.cluster.local:80"
# K8S_AUTH_SVC_URL = "http://auth-service.nyro-develop.svc.cluster.local:80" # Si tuvieras un servicio de Auth separado

class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file='.env',
        env_prefix='GATEWAY_', # Prefijo para variables de entorno del Gateway
        case_sensitive=False,
        env_file_encoding='utf-8',
        extra='ignore'
    )

    PROJECT_NAME: str = "Nyro API Gateway"

    # URLs de los servicios downstream (usar defaults de K8s)
    INGEST_SERVICE_URL: str = os.getenv("GATEWAY_INGEST_SERVICE_URL", K8S_INGEST_SVC_URL)
    QUERY_SERVICE_URL: str = os.getenv("GATEWAY_QUERY_SERVICE_URL", K8S_QUERY_SVC_URL)
    # AUTH_SERVICE_URL: Optional[str] = os.getenv("GATEWAY_AUTH_SERVICE_URL") # Descomentar si hay Auth Service

    # JWT settings (Debe ser el mismo secreto que usan los microservicios o el servicio de Auth)
    JWT_SECRET: str = "YOUR_JWT_SECRET_KEY_NEEDS_TO_BE_SET_IN_ENV_OR_SECRET" # Obligatorio
    JWT_ALGORITHM: str = "HS256"
    # ACCESS_TOKEN_EXPIRE_MINUTES: int = 30 # No necesario si el Gateway no CREA tokens

    # Logging level
    LOG_LEVEL: str = "INFO"

    # HTTP Client settings para llamadas downstream
    HTTP_CLIENT_TIMEOUT: int = 30 # Timeout en segundos
    HTTP_CLIENT_MAX_KEEPALIAS_CONNECTIONS: int = 100
    HTTP_CLIENT_MAX_CONNECTIONS: int = 200

# --- Instancia Global ---
@lru_cache()
def get_settings() -> Settings:
    log = logging.getLogger(__name__) # Usar logger estándar antes de configurar structlog
    log.setLevel(logging.INFO)
    log.addHandler(logging.StreamHandler(sys.stdout))
    log.info("Loading Gateway settings...")
    try:
        settings_instance = Settings()
        # Ocultar secreto en logs
        log.info(f"Gateway Settings Loaded:")
        log.info(f"  INGEST_SERVICE_URL: {settings_instance.INGEST_SERVICE_URL}")
        log.info(f"  QUERY_SERVICE_URL: {settings_instance.QUERY_SERVICE_URL}")
        # log.info(f"  AUTH_SERVICE_URL: {settings_instance.AUTH_SERVICE_URL}") # Si existe
        log.info(f"  JWT_SECRET: {'*** SET ***' if settings_instance.JWT_SECRET != 'YOUR_JWT_SECRET_KEY_NEEDS_TO_BE_SET_IN_ENV_OR_SECRET' else '!!! NOT SET - USING DEFAULT PLACEHOLDER !!!'}")
        log.info(f"  JWT_ALGORITHM: {settings_instance.JWT_ALGORITHM}")
        log.info(f"  LOG_LEVEL: {settings_instance.LOG_LEVEL}")

        if settings_instance.JWT_SECRET == "YOUR_JWT_SECRET_KEY_NEEDS_TO_BE_SET_IN_ENV_OR_SECRET":
            log.critical("FATAL: GATEWAY_JWT_SECRET is not set. Please configure it via environment variables or a .env file.")
            sys.exit("FATAL: GATEWAY_JWT_SECRET is not set.")

        return settings_instance
    except Exception as e:
        log.exception(f"FATAL: Error loading Gateway settings: {e}")
        sys.exit(f"FATAL: Error loading Gateway settings: {e}")

settings = get_settings()