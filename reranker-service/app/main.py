# reranker-service/app/main.py
from fastapi import FastAPI, HTTPException, Request, status as fastapi_status
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.exceptions import RequestValidationError
from contextlib import asynccontextmanager
import structlog
import uvicorn 
import asyncio
import uuid 
import sys 
import multiprocessing as mp 
import torch # Para verificar disponibilidad de CUDA al inicio

from app.core.config import settings
from app.core.logging_config import setup_logging

setup_logging() 
logger = structlog.get_logger(settings.PROJECT_NAME.lower().replace(" ", "-") + ".main")

# --- Configuración de Multiprocessing para CUDA ---
# Debe ejecutarse ANTES de que PyTorch/CUDA se inicialice en los workers de Gunicorn/Uvicorn.
# Esta variable global ayuda a asegurar que solo se intente una vez por proceso.
_MP_START_METHOD_SET = False

def _configure_mp_for_cuda():
    global _MP_START_METHOD_SET
    if _MP_START_METHOD_SET:
        return

    if settings.MODEL_DEVICE.startswith("cuda"):
        # Solo intentar cambiar si el método actual no es 'spawn'.
        # En Windows, el default ya es 'spawn'. En Linux es 'fork'.
        current_start_method = mp.get_start_method(allow_none=True)
        if sys.platform != "win32" and current_start_method != 'spawn':
            try:
                mp.set_start_method('spawn', force=True)
                logger.info(
                    "Successfully set multiprocessing start method to 'spawn' for CUDA compatibility.",
                    previous_method=current_start_method
                )
            except RuntimeError as e:
                # Esto puede ocurrir si ya se ha configurado o si el contexto es incorrecto.
                logger.warning(
                    f"Could not set multiprocessing start method to 'spawn': {e}. Current method: {current_start_method}",
                    exc_info=False # No es necesario el traceback completo para una advertencia.
                )
        elif current_start_method == 'spawn':
            logger.info("Multiprocessing start method already set to 'spawn'.")
        else: # Plataforma Windows donde spawn ya es el default
             logger.info(f"Running on {sys.platform}, default start method is '{current_start_method}'. No change needed for 'spawn'.")
    _MP_START_METHOD_SET = True

# Llamar a la configuración aquí, se ejecutará cuando se importe main.py en cada worker.
_configure_mp_for_cuda()


from app.api.v1.endpoints import rerank_endpoint
from app.infrastructure.rerankers.sentence_transformer_adapter import SentenceTransformerRerankerAdapter
from app.application.use_cases.rerank_documents_use_case import RerankDocumentsUseCase
from app.dependencies import set_dependencies 
from app.api.v1.schemas import HealthCheckResponse 

SERVICE_IS_READY = False

@asynccontextmanager
async def lifespan(app: FastAPI):
    global SERVICE_IS_READY
    logger.info(
        f"{settings.PROJECT_NAME} service starting up...", 
        version="0.1.0", 
        port=settings.PORT,
        log_level=settings.LOG_LEVEL,
        model_device=settings.MODEL_DEVICE,
        gunicorn_workers=settings.WORKERS,
        tokenizer_workers=settings.TOKENIZER_WORKERS,
        batch_size=settings.BATCH_SIZE
    )
    
    # Asegurar que la config de MP se haya intentado (debería haberse ejecutado al importar)
    _configure_mp_for_cuda()

    model_adapter_instance = SentenceTransformerRerankerAdapter()
    
    try:
        await asyncio.to_thread(SentenceTransformerRerankerAdapter.load_model) 
        
        if model_adapter_instance.is_ready(): 
            logger.info(
                "Reranker model adapter initialized and model loaded successfully.",
                model_name=model_adapter_instance.get_model_name()
            )
            rerank_use_case_instance = RerankDocumentsUseCase(reranker_model=model_adapter_instance)
            set_dependencies(model_adapter=model_adapter_instance, use_case=rerank_use_case_instance)
            SERVICE_IS_READY = True 
            logger.info(f"{settings.PROJECT_NAME} is ready to serve requests.")
        else:
            logger.error(
                "Reranker model failed to load during startup. Service will be unhealthy.",
                model_name=settings.MODEL_NAME 
            )
            SERVICE_IS_READY = False
            # Configurar dependencias incluso si el modelo no está listo para que el health check pueda obtener el estado
            # Si RerankDocumentsUseCase requiere un adapter funcional, esto podría necesitar ajuste.
            # Por ahora, se asume que puede instanciarse con un adapter no listo.
            use_case_on_failure = RerankDocumentsUseCase(reranker_model=model_adapter_instance)
            set_dependencies(model_adapter=model_adapter_instance, use_case=use_case_on_failure)

    except Exception as e:
        logger.fatal(
            "Critical error during reranker model adapter initialization or loading in lifespan.", 
            error_message=str(e), 
            exc_info=True
        )
        SERVICE_IS_READY = False
        # En caso de error fatal, intentar establecer dependencias con el estado actual para diagnósticos
        # Esto podría fallar si model_adapter_instance no se inicializó, de ahí el try-except implícito.
        _sa = model_adapter_instance if 'model_adapter_instance' in locals() else SentenceTransformerRerankerAdapter()
        _ruc = RerankDocumentsUseCase(reranker_model=_sa)
        set_dependencies(model_adapter=_sa, use_case=_ruc)

    yield 
    logger.info(f"{settings.PROJECT_NAME} service shutting down...")
    logger.info(f"{settings.PROJECT_NAME} has been shut down.")

app = FastAPI(
    title=settings.PROJECT_NAME,
    version="0.1.0", 
    description="Microservice for reranking documents based on query relevance using CrossEncoder models.",
    lifespan=lifespan,
    openapi_url=f"{settings.API_V1_STR}/openapi.json",
    docs_url=f"{settings.API_V1_STR}/docs",
    redoc_url=f"{settings.API_V1_STR}/redoc"
)

@app.middleware("http")
async def request_context_middleware(request: Request, call_next):
    structlog.contextvars.clear_contextvars()
    request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
    structlog.contextvars.bind_contextvars(request_id=request_id)

    start_time = asyncio.get_event_loop().time()
    
    response = None
    try:
        response = await call_next(request)
    except Exception as e:
        logger.exception("Unhandled exception during request processing by middleware.") 
        response = JSONResponse(
            status_code=fastapi_status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"detail": "An unexpected internal server error occurred."}
        )
    finally:
        process_time_ms = (asyncio.get_event_loop().time() - start_time) * 1000
        status_code_for_log = response.status_code if response else 500 
        
        is_health_check = request.url.path == "/health"
        
        log_method = logger.info 
        if is_health_check and status_code_for_log == 200:
            log_method = logger.debug 
        elif status_code_for_log >= 500:
            log_method = logger.error
        elif status_code_for_log >= 400:
            log_method = logger.warning
        
        log_method(
            "Request finished", 
            http_method=request.method,
            http_path=str(request.url.path),
            http_status_code=status_code_for_log,
            http_duration_ms=round(process_time_ms, 2),
            client_host=request.client.host if request.client else "unknown_client"
        )
        
        if response: 
            response.headers["X-Request-ID"] = request_id
            response.headers["X-Process-Time-Ms"] = f"{process_time_ms:.2f}"
        
        structlog.contextvars.clear_contextvars()
    return response

@app.exception_handler(HTTPException)
async def http_exception_handler_custom(request: Request, exc: HTTPException):
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail}
    )

@app.exception_handler(RequestValidationError)
async def validation_exception_handler_custom(request: Request, exc: RequestValidationError):
    return JSONResponse(
        status_code=fastapi_status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={"detail": exc.errors()} 
    )

@app.exception_handler(Exception) 
async def generic_exception_handler_custom(request: Request, exc: Exception):
    return JSONResponse(
        status_code=fastapi_status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"detail": "An unexpected internal server error occurred."}
    )

app.include_router(rerank_endpoint.router, prefix=settings.API_V1_STR, tags=["Reranking Operations"])
logger.info("API routers included.", prefix=settings.API_V1_STR)

@app.get(
    "/health", 
    response_model=HealthCheckResponse, 
    tags=["Health"],
    summary="Service Health and Model Status Check"
)
async def health_check():
    model_status = SentenceTransformerRerankerAdapter.get_model_status()
    current_model_name = settings.MODEL_NAME 

    health_log = logger.bind(service_ready_flag=SERVICE_IS_READY, model_actual_status=model_status)

    if SERVICE_IS_READY and model_status == "loaded":
        return HealthCheckResponse(
            status="ok",
            service=settings.PROJECT_NAME,
            model_status=model_status,
            model_name=current_model_name
        )
    else:
        unhealthy_reason = "Service dependencies not fully initialized or model load failed."
        if not SERVICE_IS_READY: 
             unhealthy_reason = "Lifespan initialization incomplete or failed."
        elif model_status != "loaded": 
            unhealthy_reason = f"Model status is '{model_status}' (expected 'loaded')."
        
        health_log.warning("Health check: FAILED", reason=unhealthy_reason) 
        return JSONResponse(
            status_code=fastapi_status.HTTP_503_SERVICE_UNAVAILABLE,
            content={
                "status": "error",
                "service": settings.PROJECT_NAME,
                "model_status": model_status,
                "model_name": current_model_name,
                "message": f"Service is not ready. {unhealthy_reason}"
            }
        )

@app.get("/", include_in_schema=False)
async def root_redirect():
    return PlainTextResponse(
        f"{settings.PROJECT_NAME} is running. See {settings.API_V1_STR}/docs for API documentation."
    )

if __name__ == "__main__":
    # Para desarrollo local directo con `python app/main.py`,
    # _configure_mp_for_cuda() ya se habrá llamado al importar.
    # No es necesario llamarlo de nuevo aquí explícitamente si está al inicio del script.
    logger.info(f"Starting {settings.PROJECT_NAME} locally with Uvicorn (direct run)...")
    uvicorn.run(
        "app.main:app",
        host="0.0.0.0", 
        port=settings.PORT,
        log_level=settings.LOG_LEVEL.lower(), 
        reload=True
    )

# JFU