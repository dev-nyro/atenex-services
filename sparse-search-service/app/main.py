# sparse-search-service/app/main.py
from fastapi import FastAPI, HTTPException, status as fastapi_status, Request
from fastapi.exceptions import RequestValidationError, ResponseValidationError
from fastapi.responses import JSONResponse, PlainTextResponse
import structlog
import uvicorn 
import logging 
import sys
import asyncio
import json
import uuid 
from contextlib import asynccontextmanager
from typing import Optional, Dict 

from app.core.config import settings 
from app.core.logging_config import setup_logging

setup_logging() 
main_log = structlog.get_logger("sparse_search_service.main") 


from app.api.v1.endpoints import search_endpoint

from app.api.v1 import schemas

from app.application.ports.repository_ports import ChunkContentRepositoryPort
from app.application.ports.sparse_search_port import SparseSearchPort
from app.application.ports.sparse_index_storage_port import SparseIndexStoragePort
from app.infrastructure.persistence.postgres_repositories import PostgresChunkContentRepository
from app.infrastructure.sparse_retrieval.bm25_adapter import BM25Adapter
from app.infrastructure.storage.gcs_index_storage_adapter import GCSIndexStorageAdapter, GCSIndexStorageError
from app.infrastructure.cache.index_lru_cache import IndexLRUCache
from app.application.use_cases.load_and_search_index_use_case import LoadAndSearchIndexUseCase


from app.infrastructure.persistence import postgres_connector
from app.dependencies import set_global_dependencies, get_service_status


SERVICE_NAME = settings.PROJECT_NAME
SERVICE_VERSION = settings.SERVICE_VERSION


@asynccontextmanager
async def lifespan(app: FastAPI):
    main_log.info(f"Starting up {SERVICE_NAME} v{SERVICE_VERSION}...")
    service_ready_final = False
    critical_startup_error_message = ""

    db_pool_ok: bool = False
    chunk_repo: Optional[ChunkContentRepositoryPort] = None
    bm25_engine: Optional[SparseSearchPort] = None
    gcs_adapter: Optional[SparseIndexStoragePort] = None
    index_cache: Optional[IndexLRUCache] = None
    load_search_uc: Optional[LoadAndSearchIndexUseCase] = None

    try:
        await postgres_connector.get_db_pool() 
        db_pool_ok = await postgres_connector.check_db_connection()
        if db_pool_ok:
            main_log.info("PostgreSQL connection pool initialized and verified.")
            chunk_repo = PostgresChunkContentRepository() 
        else:
            critical_startup_error_message = "Failed PostgreSQL connection verification during startup."
            main_log.critical(critical_startup_error_message)
    except ConnectionError as e:
        critical_startup_error_message = f"CRITICAL: Failed to connect to PostgreSQL: {e}"
        main_log.critical(critical_startup_error_message, error_details=str(e))
    except Exception as e:
        critical_startup_error_message = f"CRITICAL: Unexpected error initializing PostgreSQL pool: {e}"
        main_log.critical(critical_startup_error_message, error_details=str(e), exc_info=True)

    if not db_pool_ok: 
        main_log.error("Aborting further service initialization due to PostgreSQL connection failure.")
    else:
        try:
            gcs_adapter = GCSIndexStorageAdapter(bucket_name=settings.SPARSE_INDEX_GCS_BUCKET_NAME)
            main_log.info("GCSIndexStorageAdapter initialized.", bucket_name=settings.SPARSE_INDEX_GCS_BUCKET_NAME)
        except GCSIndexStorageError as e_gcs: 
            critical_startup_error_message = f"CRITICAL: Failed GCSIndexStorageAdapter initialization: {e_gcs}"
            main_log.critical(critical_startup_error_message, error_details=str(e_gcs))
            gcs_adapter = None
        except Exception as e_gcs_other:
            critical_startup_error_message = f"CRITICAL: Unexpected error initializing GCSIndexStorageAdapter: {e_gcs_other}"
            main_log.critical(critical_startup_error_message, error_details=str(e_gcs_other), exc_info=True)
            gcs_adapter = None

        try:
            bm25_engine = BM25Adapter()
            await bm25_engine.initialize_engine() 
            if not bm25_engine.is_available():
                 main_log.warning("BM25 engine (bm2s library) not available. Search functionality will be impaired/unavailable.")
            main_log.info("BM25Adapter (SparseSearchPort) initialized.")
        except Exception as e_bm25:
            main_log.error(f"Failed to initialize BM25Adapter: {e_bm25}", error_details=str(e_bm25), exc_info=True)
            bm25_engine = None 
        
        try:
            index_cache = IndexLRUCache(
                max_items=settings.SPARSE_INDEX_CACHE_MAX_ITEMS,
                ttl_seconds=settings.SPARSE_INDEX_CACHE_TTL_SECONDS
            )
            main_log.info("IndexLRUCache initialized.")
        except Exception as e_cache:
            main_log.error(f"Failed to initialize IndexLRUCache: {e_cache}", error_details=str(e_cache), exc_info=True)
            index_cache = None 

        if db_pool_ok and gcs_adapter is not None and bm25_engine is not None and index_cache is not None:
            try:
                load_search_uc = LoadAndSearchIndexUseCase(
                    index_cache=index_cache,
                    index_storage=gcs_adapter,
                    sparse_search_engine=bm25_engine 
                )
                main_log.info("LoadAndSearchIndexUseCase instantiated.")
                service_ready_final = True 
            except Exception as e_uc:
                critical_startup_error_message = f"Failed to instantiate LoadAndSearchIndexUseCase: {e_uc}"
                main_log.critical(critical_startup_error_message, error_details=str(e_uc), exc_info=True)
                service_ready_final = False
        else:
            main_log.warning("Not all components ready for LoadAndSearchIndexUseCase instantiation.",
                             db_ok=db_pool_ok, gcs_ok=bool(gcs_adapter), 
                             bm25_ok=bool(bm25_engine), cache_ok=(index_cache is not None))
            service_ready_final = False 

    set_global_dependencies(
        chunk_repo=chunk_repo, 
        search_engine=bm25_engine, 
        gcs_storage=gcs_adapter,
        index_cache=index_cache,
        use_case=load_search_uc, 
        service_ready=service_ready_final
    )

    if service_ready_final:
        main_log.info(f"{SERVICE_NAME} service components initialized. SERVICE IS READY.")
    else:
        final_startup_error_msg = critical_startup_error_message or "One or more components failed to initialize."
        main_log.critical(f"{SERVICE_NAME} startup finished. {final_startup_error_msg} SERVICE IS NOT READY.")

    yield 

    main_log.info(f"Shutting down {SERVICE_NAME}...")
    await postgres_connector.close_db_pool()
    if index_cache: index_cache.clear() 
    main_log.info(f"{SERVICE_NAME} shutdown complete.")


app = FastAPI(
    title=SERVICE_NAME,
    openapi_url=f"{settings.API_V1_STR}/openapi.json",
    version=SERVICE_VERSION, 
    description="Atenex microservice for performing sparse (keyword-based) search using BM25 with GCS-backed indexes.",
    lifespan=lifespan
)

@app.middleware("http")
async def request_context_middleware(request: Request, call_next):
    start_time = asyncio.get_event_loop().time()
    request_id = request.headers.get("x-request-id", str(uuid.uuid4()))

    structlog.contextvars.bind_contextvars(
        request_id=request_id,
        method=request.method,
        path=str(request.url.path),
        client_host=request.client.host if request.client else "unknown_client",
    )
    middleware_log = structlog.get_logger("sparse_search_service.request")
    middleware_log.info("Request received")
    
    response = None
    try:
        response = await call_next(request)
    except Exception as e_call_next: 
        process_time_ms = (asyncio.get_event_loop().time() - start_time) * 1000
        structlog.contextvars.bind_contextvars(duration_ms=round(process_time_ms, 2), status_code=500)
        middleware_log.exception("Unhandled exception during request processing pipeline.")
        response = JSONResponse(
            status_code=fastapi_status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"request_id": request_id, "detail": "Internal Server Error during request handling."}
        )
    finally:
        if response: 
            process_time_ms = (asyncio.get_event_loop().time() - start_time) * 1000
            structlog.contextvars.bind_contextvars(duration_ms=round(process_time_ms, 2), status_code=response.status_code)
            
            log_method = middleware_log.info
            if 400 <= response.status_code < 500: log_method = middleware_log.warning
            elif response.status_code >= 500: log_method = middleware_log.error
            log_method("Request finished")
            
            response.headers["X-Request-ID"] = request_id
            response.headers["X-Process-Time-Ms"] = f"{process_time_ms:.2f}"
        
        structlog.contextvars.clear_contextvars() 

    return response


@app.exception_handler(HTTPException)
async def http_exception_handler_custom(request: Request, exc: HTTPException):
    exc_log = structlog.get_logger("sparse_search_service.exception_handler")
    exc_log.error("HTTPException caught", detail=exc.detail, status_code=exc.status_code)
    return JSONResponse(
        status_code=exc.status_code,
        content={"request_id": structlog.contextvars.get_contextvars().get("request_id"), "detail": exc.detail}
    )

@app.exception_handler(RequestValidationError)
async def validation_exception_handler_custom(request: Request, exc: RequestValidationError):
    exc_log = structlog.get_logger("sparse_search_service.exception_handler")
    try:
        errors = exc.errors()
    except Exception: 
        errors = [{"loc": ["unknown"], "msg": "Error parsing validation details.", "type": "internal_error"}]
    exc_log.warning("RequestValidationError caught", validation_errors=errors)
    return JSONResponse(
        status_code=fastapi_status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={
            "request_id": structlog.contextvars.get_contextvars().get("request_id"),
            "detail": "Request validation failed.",
            "errors": errors
        },
    )

@app.exception_handler(ResponseValidationError) 
async def response_validation_exception_handler_custom(request: Request, exc: ResponseValidationError):
    exc_log = structlog.get_logger("sparse_search_service.exception_handler")
    exc_log.error("ResponseValidationError caught", validation_errors=exc.errors(), exc_info=True)
    return JSONResponse(
        status_code=fastapi_status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={
            "request_id": structlog.contextvars.get_contextvars().get("request_id"),
            "detail": "Internal Server Error: Response data failed validation."
        }
    )

@app.exception_handler(Exception) 
async def generic_exception_handler_custom(request: Request, exc: Exception):
    exc_log = structlog.get_logger("sparse_search_service.exception_handler")
    exc_log.exception("Unhandled generic Exception caught") 
    return JSONResponse(
        status_code=fastapi_status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={
            "request_id": structlog.contextvars.get_contextvars().get("request_id"),
            "detail": f"An unexpected internal server error occurred: {type(exc).__name__}"
        }
    )

app.include_router(search_endpoint.router, prefix=settings.API_V1_STR, tags=["Sparse Search"])
main_log.info(f"API router included under prefix '{settings.API_V1_STR}/search'")

@app.get(
    "/health",
    response_model=schemas.HealthCheckResponse,
    tags=["Health Check"],
    summary="Service Health and Dependencies Check"
)
async def health_check():
    health_log = structlog.get_logger("sparse_search_service.health_check")
    is_globally_ready = get_service_status() 
    
    dependencies_status_dict: Dict[str, str] = {}
    
    db_ok = await postgres_connector.check_db_connection()
    dependencies_status_dict["PostgreSQL"] = "ok" if db_ok else "error"
    
    bm2s_engine_available = False
    bm2s_adapter_details = "unavailable"
    try:
        from app.dependencies import get_sparse_search_engine
        engine_adapter = get_sparse_search_engine() 
        if engine_adapter and hasattr(engine_adapter, 'is_available'):
            bm2s_engine_available = engine_adapter.is_available()
            bm2s_adapter_details = "ok (bm2s library loaded and adapter initialized)" if bm2s_engine_available else "unavailable (bm2s library potentially missing)"
        else:
             bm2s_adapter_details = "adapter not fully initialized or is_available method missing"
    except HTTPException as http_exc_dep: 
        bm2s_adapter_details = f"adapter not ready ({http_exc_dep.status_code})"
        health_log.warning("Could not get search engine adapter for health check.", detail=str(http_exc_dep.detail))
    except Exception as e_bm2s_check:
        bm2s_adapter_details = f"error checking adapter: {type(e_bm2s_check).__name__}"
        health_log.error("Error checking BM2S engine adapter status.", error=str(e_bm2s_check))

    dependencies_status_dict["BM2S_Engine"] = bm2s_adapter_details

    gcs_adapter_ready = False
    gcs_adapter_details = "unavailable"
    try:
        from app.dependencies import get_gcs_index_storage
        gcs_storage_adapter = get_gcs_index_storage() 
        if gcs_storage_adapter: 
            gcs_adapter_ready = True 
            gcs_adapter_details = "ok (adapter initialized)"
        else:
            gcs_adapter_details = "adapter not initialized"
    except HTTPException as http_exc_gcs:
        gcs_adapter_details = f"adapter not ready ({http_exc_gcs.status_code})"
        health_log.warning("Could not get GCS storage adapter for health check.", detail=str(http_exc_gcs.detail))
    except Exception as e_gcs_check:
        gcs_adapter_details = f"error checking adapter: {type(e_gcs_check).__name__}"
        health_log.error("Error checking GCS adapter status.", error=str(e_gcs_check))
        
    dependencies_status_dict["GCS_Index_Storage"] = gcs_adapter_details
    
    final_http_status: int
    response_content: schemas.HealthCheckResponse

    if is_globally_ready and db_ok and gcs_adapter_ready : 
        final_http_status = fastapi_status.HTTP_200_OK
        response_content = schemas.HealthCheckResponse(
            status="ok",
            service=SERVICE_NAME,
            ready=True, 
            dependencies=dependencies_status_dict
        )
        health_log.debug("Health check successful.", **response_content.model_dump())
    else:
        final_http_status = fastapi_status.HTTP_503_SERVICE_UNAVAILABLE
        response_content = schemas.HealthCheckResponse(
            status="error",
            service=SERVICE_NAME,
            ready=False, 
            dependencies=dependencies_status_dict
        )
        health_log.error("Health check failed.", **response_content.model_dump())

    return JSONResponse(
        status_code=final_http_status,
        content=response_content.model_dump()
    )


@app.get("/", include_in_schema=False)
async def root():
    return PlainTextResponse(f"{SERVICE_NAME} v{SERVICE_VERSION} is running. Visit /docs for API documentation or /health for status.")


if __name__ == "__main__":
    port_to_use = settings.PORT
    log_level_str = settings.LOG_LEVEL.lower() 
    print(f"----- Starting {SERVICE_NAME} v{SERVICE_VERSION} locally on port {port_to_use} with log level {log_level_str} -----")
    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=port_to_use,
        reload=True, 
        log_level=log_level_str
    )

# JFU