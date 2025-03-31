# api-gateway/app/core/config.py
import os
from pydantic_settings import BaseSettings, SettingsConfigDict
from functools import lru_cache
import sys
import logging
from typing import Optional

# Usar nombres de servicio DNS de Kubernetes como defaults
# Asegúrate que los nombres ('ingest-api-service', 'query-service') y namespace ('nyro-develop') son correctos
# El puerto es 80 porque el Service de K8s mapeará este puerto al containerPort (8000, 8001, etc.)
K8S_INGEST_SVC_URL_DEFAULT = "http://ingest-api-service.nyro-develop.svc.cluster.local:80"
K8S_QUERY_SVC_URL_DEFAULT = "http://query-service.nyro-develop.svc.cluster.local:80"
# K8S_AUTH_SVC_URL_DEFAULT = "http://auth-service.nyro-develop.svc.cluster.local:80" # Si tuvieras un servicio de Auth separado

class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file='.env', # Busca un archivo .env
        env_prefix='GATEWAY_', # Prefijo para variables de entorno del Gateway
        case_sensitive=False,
        env_file_encoding='utf-8',
        extra='ignore' # Ignora variables de entorno extra
    )

    PROJECT_NAME: str = "Nyro API Gateway"
    API_V1_STR: str = "/api/v1" # Prefijo común para rutas proxificadas

    # URLs de los servicios downstream (leer de env vars o usar defaults de K8s)
    INGEST_SERVICE_URL: str = os.getenv("GATEWAY_INGEST_SERVICE_URL", K8S_INGEST_SVC_URL_DEFAULT)
    QUERY_SERVICE_URL: str = os.getenv("GATEWAY_QUERY_SERVICE_URL", K8S_QUERY_SVC_URL_DEFAULT)
    # AUTH_SERVICE_URL: Optional[str] = os.getenv("GATEWAY_AUTH_SERVICE_URL") # Descomentar si hay Auth Service

    # JWT settings (Debe ser el mismo secreto que usan los microservicios o el servicio de Auth)
    # ¡¡¡ ESTA VARIABLE DEBE SER CONFIGURADA EN EL ENTORNO O SECRETO K8S !!!
    JWT_SECRET: str = "YOUR_DEFAULT_JWT_SECRET_KEY_CHANGE_ME"
    JWT_ALGORITHM: str = "HS256"

    # Logging level (DEBUG, INFO, WARNING, ERROR, CRITICAL)
    LOG_LEVEL: str = "INFO"

    # HTTP Client settings para llamadas downstream
    HTTP_CLIENT_TIMEOUT: int = 60 # Timeout en segundos para llamadas a microservicios
    HTTP_CLIENT_MAX_KEEPALIAS_CONNECTIONS: int = 20 # Pool de conexiones keep-alive
    HTTP_CLIENT_MAX_CONNECTIONS: int = 100 # Conexiones totales máximas

# --- Instancia Global ---
@lru_cache()
def get_settings() -> Settings:
    # Usar logger estándar aquí porque structlog se configura después
    log = logging.getLogger(__name__)
    log.setLevel(logging.INFO) # Asegurar que vemos los logs iniciales
    log.addHandler(logging.StreamHandler(sys.stdout))

    log.info("Loading Gateway settings...")
    try:
        settings_instance = Settings()
        # Log settings (¡cuidado con los secretos!)
        log.info("Gateway Settings Loaded:")
        log.info(f"  PROJECT_NAME: {settings_instance.PROJECT_NAME}")
        log.info(f"  INGEST_SERVICE_URL: {settings_instance.INGEST_SERVICE_URL}")
        log.info(f"  QUERY_SERVICE_URL: {settings_instance.QUERY_SERVICE_URL}")
        # log.info(f"  AUTH_SERVICE_URL: {settings_instance.AUTH_SERVICE_URL}") # Si existe
        # *** IMPORTANTE: Verificar y advertir si el secreto JWT no está configurado ***
        if settings_instance.JWT_SECRET == "YOUR_DEFAULT_JWT_SECRET_KEY_CHANGE_ME":
            log.critical("!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
            log.critical("! FATAL: GATEWAY_JWT_SECRET is using the default insecure value!")
            log.critical("! Please set GATEWAY_JWT_SECRET environment variable or in secrets.")
            log.critical("!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
            # Considera salir si es crítico: sys.exit("FATAL: GATEWAY_JWT_SECRET not configured securely.")
        else:
             log.info(f"  JWT_SECRET: *** SET ***") # No loguear el secreto real
        log.info(f"  JWT_ALGORITHM: {settings_instance.JWT_ALGORITHM}")
        log.info(f"  LOG_LEVEL: {settings_instance.LOG_LEVEL}")
        log.info(f"  HTTP_CLIENT_TIMEOUT: {settings_instance.HTTP_CLIENT_TIMEOUT}")

        return settings_instance
    except Exception as e:
        log.exception(f"FATAL: Error loading Gateway settings: {e}")
        sys.exit(f"FATAL: Error loading Gateway settings: {e}")

settings = get_settings()