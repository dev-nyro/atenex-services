# reranker-service/app/main.py
from fastapi import FastAPI, HTTPException, Request, status as fastapi_status
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.exceptions import RequestValidationError
from contextlib import asynccontextmanager
import structlog
import uvicorn 
import asyncio
import uuid 
import sys # Necesario para sys.argv
import multiprocessing as mp # Necesario para set_start_method

from app.core.config import settings
from app.core.logging_config import setup_logging

setup_logging() # Configurar logging primero
logger = structlog.get_logger(settings.PROJECT_NAME.lower().replace(" ", "-") + ".main")


# --- Configuración de Multiprocessing para CUDA con Gunicorn/Uvicorn ---
# Esto debe hacerse ANTES de que PyTorch/CUDA se inicialice en los workers.
# La condición verifica si se está ejecutando bajo Gunicorn o Uvicorn directamente.
# El módulo 'main' se carga una vez por worker de Gunicorn.
if "gunicorn" in sys.argv[0] or "uvicorn" in sys.argv[0] or __name__ == "app.main":
    if settings.MODEL_DEVICE.startswith("cuda"):
        current_start_method = mp.get_start_method(allow_none=True)
        if current_start_method != 'spawn':
            try:
                mp.set_start_method('spawn', force=True)
                logger.info(
                    "Successfully set multiprocessing start method to 'spawn' for CUDA compatibility.",
                    current_method_was=current_start_method
                )
            except RuntimeError as e:
                logger.warning(
                    f"Could not set multiprocessing start method to 'spawn': {e}. Current method: {current_start_method}",
                    exc_info=True
                )
        else:
            logger.info("Multiprocessing start method already set to 'spawn'.")


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
        tokenizer_workers=settings.TOKENIZER_WORKERS
    )
    
    model_adapter = SentenceTransformerRerankerAdapter()
    
    try:
        # La carga del modelo es bloqueante, ejecutar en thread pool
        await asyncio.to_thread(SentenceTransformerRerankerAdapter.load_model) 
        
        if model_adapter.is_ready(): 
            logger.info(
                "Reranker model adapter initialized and model loaded successfully.",
                model_name=model_adapter.get_model_name()
            )
            # El use case no necesita ser `None` si el adapter no está listo,
            # la lógica de `get_rerank_use_case` lo manejará.
            rerank_use_case = RerankDocumentsUseCase(reranker_model=model_adapter)
            set_dependencies(model_adapter=model_adapter, use_case=rerank_use_case)
            SERVICE_IS_READY = True # Solo si el modelo se carga bien.
            logger.info(f"{settings.PROJECT_NAME} is ready to serve requests.")
        else:
            logger.error(
                "Reranker model failed to load during startup. Service will be unhealthy.",
                model_name=settings.MODEL_NAME 
            )
            SERVICE_IS_READY = False
            # Aun así, configurar las dependencias para que el health check pueda obtener el estado del modelo.
            # Si rerank_use_case no se puede instanciar sin un modelo_adapter válido,
            # entonces el use_case podría ser None aquí y get_rerank_use_case fallaría.
            # Asumimos que el use_case puede tomar un adapter no listo.
            rerank_use_case_on_fail = RerankDocumentsUseCase(reranker_model=model_adapter)
            set_dependencies(model_adapter=model_adapter, use_case=rerank_use_case_on_fail)


    except Exception as e:
        logger.fatal(
            "Critical error during reranker model adapter initialization or loading in lifespan.", 
            error_message=str(e), 
            exc_info=True
        )
        SERVICE_IS_READY = False
        # Intentar configurar dependencias incluso en fallo para diagnósticos
        failed_adapter = model_adapter if 'model_adapter' in locals() else SentenceTransformerRerankerAdapter() # Crea una instancia si no existe
        failed_use_case = RerankDocumentsUseCase(reranker_model=failed_adapter)
        set_dependencies(model_adapter=failed_adapter, use_case=failed_use_case)


    yield 
    logger.info(f"{settings.PROJECT_NAME} service shutting down...")
    # Aquí podrías añadir lógica de limpieza si fuera necesario, como liberar recursos de GPU explícitamente.
    # Sin embargo, Python y PyTorch suelen manejar esto bien al salir.
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

# La ejecución con `if __name__ == "__main__":` es para desarrollo local con `python app/main.py`
# Gunicorn ejecutará el archivo directamente, por lo que el bloque de `set_start_method` anterior se ejecutará.
if __name__ == "__main__":
    # Para desarrollo local, también es bueno establecer el método de inicio si se usa CUDA
    if settings.MODEL_DEVICE.startswith("cuda"):
        current_start_method = mp.get_start_method(allow_none=True)
        if current_start_method != 'spawn':
            try:
                mp.set_start_method('spawn', force=True)
                logger.info(
                    "LOCAL DEV: Successfully set multiprocessing start method to 'spawn' for CUDA.",
                     current_method_was=current_start_method
                )
            except RuntimeError as e:
                 logger.warning(
                    f"LOCAL DEV: Could not set multiprocessing start method to 'spawn': {e}. Current: {current_start_method}",
                    exc_info=True
                )
        else:
            logger.info("LOCAL DEV: Multiprocessing start method already 'spawn'.")

    logger.info(f"Starting {settings.PROJECT_NAME} locally with Uvicorn...")
    uvicorn.run(
        "app.main:app",
        host="0.0.0.0", 
        port=settings.PORT,
        log_level=settings.LOG_LEVEL.lower(), 
        reload=True 
        # reload_dirs=["app"] # Opcional si el reload simple no es suficiente
    )

# JFU 