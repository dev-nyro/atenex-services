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
# import multiprocessing as mp # Ya no es necesario para mp.set_start_method
# import torch # Ya no es necesario aquí, config.py lo usa

from app.core.config import settings # settings ya tiene IS_CUDA_AVAILABLE
from app.core.logging_config import setup_logging

setup_logging() 
logger = structlog.get_logger(settings.PROJECT_NAME.lower().replace(" ", "-") + ".main")

# La configuración de multiprocessing ya no se manipula aquí.
# Se confía en que num_workers=0 para CUDA en el adaptador evitará problemas.

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
        model_name=settings.MODEL_NAME,
        model_device_configured=settings.MODEL_DEVICE,
        cuda_available_check=settings.IS_CUDA_AVAILABLE, # Usar el check de config
        effective_model_device=settings.MODEL_DEVICE, # Después del validador, este es el dispositivo real
        gunicorn_workers_configured=settings.WORKERS, # Valor después del validador
        tokenizer_workers_configured=settings.TOKENIZER_WORKERS, # Valor después del validador
        batch_size_configured=settings.BATCH_SIZE
    )
    
    model_adapter_instance = SentenceTransformerRerankerAdapter()
    
    try:
        await asyncio.to_thread(SentenceTransformerRerankerAdapter.load_model) 
        
        if model_adapter_instance.is_ready(): 
            logger.info(
                "Reranker model adapter initialized and model loaded successfully.",
                loaded_model_name=model_adapter_instance.get_model_name() # Usar el getter
            )
            rerank_use_case_instance = RerankDocumentsUseCase(reranker_model=model_adapter_instance)
            set_dependencies(model_adapter=model_adapter_instance, use_case=rerank_use_case_instance)
            SERVICE_IS_READY = True 
            logger.info(f"{settings.PROJECT_NAME} is ready to serve requests.")
        else:
            logger.error(
                "Reranker model failed to load during startup. Service will be unhealthy.",
                configured_model_name=settings.MODEL_NAME 
            )
            SERVICE_IS_READY = False
            use_case_on_failure = RerankDocumentsUseCase(reranker_model=model_adapter_instance)
            set_dependencies(model_adapter=model_adapter_instance, use_case=use_case_on_failure)

    except Exception as e:
        logger.fatal(
            "Critical error during reranker model adapter initialization or loading in lifespan.", 
            error_message=str(e), 
            exc_info=True
        )
        SERVICE_IS_READY = False
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
    # Usar settings.MODEL_NAME ya que es lo que se intentó cargar
    # y el get_model_name() del adapter devolverá esto si la carga falló.
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
    logger.info(f"Starting {settings.PROJECT_NAME} locally with Uvicorn (direct run)...")
    uvicorn.run(
        "app.main:app",
        host="0.0.0.0", 
        port=settings.PORT,
        log_level=settings.LOG_LEVEL.lower(), 
        reload=True
    )

# JFU