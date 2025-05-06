# query-service/app/dependencies.py
"""
Centralized dependency functions to avoid circular imports.
"""
from fastapi import HTTPException
from app.application.use_cases.ask_query_use_case import AskQueryUseCase

# These will be set by main.py at startup
ask_query_use_case_instance = None
SERVICE_READY = False

def set_ask_query_use_case_instance(instance, ready_flag):
    global ask_query_use_case_instance, SERVICE_READY
    ask_query_use_case_instance = instance
    SERVICE_READY = ready_flag

def get_ask_query_use_case() -> AskQueryUseCase:
    if not SERVICE_READY or not ask_query_use_case_instance:
        raise HTTPException(status_code=503, detail="Query processing service is not ready. Check startup logs.")
    return ask_query_use_case_instance
