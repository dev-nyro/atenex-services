# ./app/db/__init__.py
# (Puede estar vacío o importar selectivamente)
from .postgres_client import get_db_pool, close_db_pool, log_query_interaction

__all__ = ["get_db_pool", "close_db_pool", "log_query_interaction"]