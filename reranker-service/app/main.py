# reranker-service/app/main.py
from fastapi import FastAPI, HTTPException, Request, status as fastapi_status
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.exceptions import RequestValidationError
from contextlib import asynccontextmanager
import structlog
import uvicorn 
import asyncio
import uuid 

from app.core.config import settings
from app.core.logging_config import setup_logging

setup_logging()
logger = structlog.get_logger(settings.PROJECT_NAME.lower().replace(" ", "-") + ".main")

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
        log_level=settings.LOG_LEVEL
    )
    
    model_adapter = SentenceTransformerRerankerAdapter()
    
    try:
        await asyncio.to_thread(SentenceTransformerRerankerAdapter.load_model) 
        
        if model_adapter.is_ready(): 
            logger.info(
                "Reranker model adapter initialized and model loaded successfully.",
                model_name=model_adapter.get_model_name()
            )
            rerank_use_case = RerankDocumentsUseCase(reranker_model=model_adapter)
            set_dependencies(model_adapter=model_adapter, use_case=rerank_use_case)
            SERVICE_IS_READY = True
            logger.info(f"{settings.PROJECT_NAME} is ready to serve requests.")
        else:
            logger.error(
                "Reranker model failed to load during startup. Service will be unhealthy.",
                model_name=settings.MODEL_NAME 
            )
            SERVICE_IS_READY = False
            set_dependencies(model_adapter=model_adapter, use_case=None) 

    except Exception as e:
        logger.fatal(
            "Critical error during reranker model adapter initialization or loading in lifespan.", 
            error_message=str(e), 
            exc_info=True
        )
        SERVICE_IS_READY = False
        set_dependencies(model_adapter=model_adapter if 'model_adapter' in locals() else None, use_case=None)

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
        
        # Reducir logs de health checks exitosos a DEBUG
        log_method = logger.info
        is_health_check = request.url.path == "/health"
        if is_health_check and status_code_for_log == 200:
            log_method = logger.debug # Loguear health checks OK en DEBUG
        elif status_code_for_log >= 500:
            log_method = logger.error
        elif status_code_for_log >= 400:
            log_method = logger.warning
        
        # Para otros endpoints que no sean /health o si /health falla, usar el nivel correspondiente
        if not (is_health_check and status_code_for_log == 200 and log_method == logger.debug):
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
        # No loguear INFO para health checks OK aquí; el middleware lo hará en DEBUG
        return HealthCheckResponse(
            status="ok",
            service=settings.PROJECT_NAME,
            model_status=model_status,
            model_name=current_model_name
        )
    else:
        unhealthy_reason = "Service dependencies not fully initialized."
        if model_status != "loaded":
            unhealthy_reason = f"Model status is '{model_status}'."
        
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
    logger.info(f"Starting {settings.PROJECT_NAME} locally with Uvicorn...")
    uvicorn.run(
        "app.main:app",
        host="0.0.0.0", 
        port=settings.PORT,
        log_level=settings.LOG_LEVEL.lower(), 
        reload=True 
    )