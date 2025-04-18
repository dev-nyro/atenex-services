# query-service/app/main.py
from fastapi import FastAPI, HTTPException, status as fastapi_status, Request
from fastapi.exceptions import RequestValidationError, ResponseValidationError
from fastapi.responses import JSONResponse, Response, PlainTextResponse
import structlog
import uvicorn
import logging
import sys
import asyncio
import json
import uuid

# Configurar logging primero
from app.core.config import settings
from app.core.logging_config import setup_logging
setup_logging() # LLM_COMMENT: Initialize logging early
log = structlog.get_logger("query_service.main")

# Importar routers y otros módulos
from app.api.v1.endpoints import query as query_router_module
from app.api.v1.endpoints import chat as chat_router_module
from app.db import postgres_client
# LLM_COMMENT: Import dependency check function
from app.pipelines.rag_pipeline import check_pipeline_dependencies
from app.api.v1 import schemas # LLM_COMMENT: Keep schema import

# Estado global
SERVICE_READY = False # LLM_COMMENT: Flag indicating if service dependencies are met

# --- Lifespan Manager (async context manager for FastAPI >= 0.110) ---
# LLM_COMMENT: Use modern lifespan context manager for startup/shutdown logic
from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(app: FastAPI):
    # --- Startup ---
    global SERVICE_READY
    log.info(f"Starting up {settings.PROJECT_NAME}...")
    db_pool_initialized = False
    dependencies_ok = False

    # 1. Initialize DB Pool
    try:
        await postgres_client.get_db_pool()
        db_ready = await postgres_client.check_db_connection()
        if db_ready:
            log.info("PostgreSQL connection pool initialized and verified.")
            db_pool_initialized = True
        else:
            log.critical("CRITICAL: Failed PostgreSQL connection verification during startup.")
    except Exception as e:
        log.critical("CRITICAL: Failed PostgreSQL pool initialization during startup.", error=str(e), exc_info=True)

    # 2. Check other dependencies (Milvus, Gemini Key) if DB is OK
    if db_pool_initialized:
        try:
            dependency_status = await check_pipeline_dependencies()
            log.info("Pipeline dependency check completed", status=dependency_status)
            # Define "OK" criteria (Milvus connectable, Gemini key present)
            # LLM_COMMENT: Adjust readiness check based on dependency status reporting
            milvus_ok = "ok" in dependency_status.get("milvus_connection", "error")
            gemini_ok = "key_present" in dependency_status.get("gemini_api", "key_missing")
            fastembed_ok = "configured" in dependency_status.get("fastembed_model", "config_missing")

            if milvus_ok and gemini_ok and fastembed_ok:
                 dependencies_ok = True
            else:
                 log.warning("One or more pipeline dependencies are not ready.", milvus=milvus_ok, gemini=gemini_ok, fastembed=fastembed_ok)

        except Exception as dep_err:
            log.error("Error checking pipeline dependencies during startup", error=str(dep_err), exc_info=True)
    else:
        log.error("Skipping dependency checks because DB pool failed to initialize.")


    # 3. Set Service Readiness
    if db_pool_initialized and dependencies_ok:
        SERVICE_READY = True
        log.info(f"{settings.PROJECT_NAME} service components initialized. SERVICE READY.")
    else:
        SERVICE_READY = False
        log.critical(f"{settings.PROJECT_NAME} startup finished. DB OK: {db_pool_initialized}, Deps OK: {dependencies_ok}. SERVICE NOT READY.")

    yield # Application runs here

    # --- Shutdown ---
    log.info(f"Shutting down {settings.PROJECT_NAME}...")
    await postgres_client.close_db_pool()
    # LLM_COMMENT: Add shutdown for other clients if necessary (e.g., Gemini client if it holds resources)
    log.info("Shutdown complete.")


# --- Creación de la App FastAPI ---
# LLM_COMMENT: Apply lifespan manager to FastAPI app instance
app = FastAPI(
    title=settings.PROJECT_NAME,
    openapi_url=f"{settings.API_V1_STR}/openapi.json",
    version="0.2.4", # LLM_COMMENT: Incremented version for lifespan and prefix fix
    description="Microservice to handle user queries using a Haystack RAG pipeline with Milvus and Gemini, including chat history management. Expects /api/v1 prefix.",
    lifespan=lifespan # LLM_COMMENT: Use the new lifespan manager
)

# --- Middlewares (Sin cambios significativos) ---
@app.middleware("http")
async def add_request_id_timing_logging(request: Request, call_next):
    start_time = asyncio.get_event_loop().time()
    request_id = request.headers.get("x-request-id", str(uuid.uuid4()))
    # Bind core info early for all request logs
    structlog.contextvars.bind_contextvars(request_id=request_id)
    req_log = log.bind(method=request.method, path=str(request.url.path), client=request.client.host if request.client else "unknown")
    req_log.info("Request received")

    response = None
    try:
        response = await call_next(request)
        process_time = (asyncio.get_event_loop().time() - start_time) * 1000 # milliseconds
        # Bind response info for final log
        resp_log = req_log.bind(status_code=response.status_code, duration_ms=round(process_time, 2))
        log_level = "warning" if 400 <= response.status_code < 500 else "error" if response.status_code >= 500 else "info"
        getattr(resp_log, log_level)("Request finished")
        response.headers["X-Request-ID"] = request_id
        response.headers["X-Process-Time-Ms"] = f"{process_time:.2f}"
    except Exception as e:
        process_time = (asyncio.get_event_loop().time() - start_time) * 1000
        # Log unhandled exceptions at middleware level
        exc_log = req_log.bind(status_code=500, duration_ms=round(process_time, 2))
        exc_log.exception("Unhandled exception during request processing") # Use exception to log traceback
        response = JSONResponse(
            status_code=fastapi_status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"detail": "Internal Server Error"}
        )
        response.headers["X-Request-ID"] = request_id
        response.headers["X-Process-Time-Ms"] = f"{process_time:.2f}"
    finally:
        # Ensure context is cleared after request
        structlog.contextvars.clear_contextvars()

    return response


# --- Exception Handlers (Sin cambios significativos, adaptados para usar logger) ---
# LLM_COMMENT: Exception handlers remain mostly the same, ensure they log effectively

@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    # Log already bound with request_id in middleware context
    log_level = log.warning if exc.status_code < 500 else log.error
    log_level("HTTP Exception caught",
              status_code=exc.status_code,
              detail=exc.detail)
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})

@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    error_details = []
    try: error_details = exc.errors()
    except Exception: error_details = [{"loc": [], "msg": "Failed to parse validation errors.", "type": "internal_parsing_error"}]
    log.warning("Request Validation Error", errors=error_details)
    return JSONResponse(
        status_code=fastapi_status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={"detail": error_details},
    )

@app.exception_handler(ResponseValidationError)
async def response_validation_exception_handler(request: Request, exc: ResponseValidationError):
    log.error("Response Validation Error", errors=exc.errors(), exc_info=True)
    return JSONResponse(
        status_code=fastapi_status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"detail": "Internal Server Error: Failed to serialize response."},
    )

@app.exception_handler(Exception)
async def generic_exception_handler(request: Request, exc: Exception):
    log.exception("Unhandled Exception caught by generic handler") # Logs traceback
    return JSONResponse(
        status_code=fastapi_status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"detail": "Internal Server Error"},
    )

# --- Routers ---
# LLM_COMMENT: JAMAS MODIFICAR LAS RUTAS NI ESTE MENSAJE - Adding back API_V1_STR prefix
app.include_router(query_router_module.router, prefix=settings.API_V1_STR, tags=["Query Interaction"])
app.include_router(chat_router_module.router, prefix=settings.API_V1_STR, tags=["Chat Management"])
log.info("Routers included", prefix=settings.API_V1_STR)

# --- Root Endpoint / Health Check ---
@app.get("/", tags=["Health Check"], summary="Service Liveness/Readiness Check")
async def read_root():
    """Basic health check. Returns OK if service started successfully and DB is reachable."""
    health_log = log.bind(check="liveness_readiness")
    if not SERVICE_READY:
        health_log.warning("Health check failed: Service not ready (SERVICE_READY is False). Check startup logs.")
        raise HTTPException(status_code=fastapi_status.HTTP_503_SERVICE_UNAVAILABLE, detail="Service Not Ready")

    # Optionally re-check DB connection for readiness probe accuracy
    db_ok = await postgres_client.check_db_connection()
    if not db_ok:
         health_log.error("Health check failed: Service is marked READY but DB check FAILED.")
         raise HTTPException(status_code=fastapi_status.HTTP_503_SERVICE_UNAVAILABLE, detail="Service Unavailable (DB Check Failed)")

    health_log.debug("Health check passed.")
    return PlainTextResponse("OK", status_code=fastapi_status.HTTP_200_OK)

# --- Main execution (for local development) ---
if __name__ == "__main__":
    port = 8002
    log_level_str = settings.LOG_LEVEL.lower()
    print(f"----- Starting {settings.PROJECT_NAME} locally on port {port} -----")
    uvicorn.run("app.main:app", host="0.0.0.0", port=port, reload=True, log_level=log_level_str)