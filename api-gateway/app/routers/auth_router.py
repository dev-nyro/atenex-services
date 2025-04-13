# File: app/routers/auth_router.py
# api-gateway/app/routers/auth_router.py
from fastapi import APIRouter
import structlog

log = structlog.get_logger(__name__)

# Este router ya no define rutas activas.
# El login está en user_router.
# El proxy /api/v1/auth/* se define en gateway_router si settings.AUTH_SERVICE_URL está configurado.
router = APIRouter()

log.info("Auth router loaded (currently defines no active endpoints).")

# Puedes eliminar este archivo si no planeas añadir rutas específicas bajo /api/v1/auth
# que NO sean proxy al AUTH_SERVICE_URL. Si lo eliminas, asegúrate de quitar
# la línea 'app.include_router(auth_router.router)' en app/main.py.