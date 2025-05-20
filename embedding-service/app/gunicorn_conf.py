# embedding-service/app/gunicorn_conf.py
import os
import multiprocessing
from app.core.config import settings # Load settings to access configured WORKERS

# Gunicorn config variables
# `settings.WORKERS` is already validated in config.py based on device
workers = settings.WORKERS
worker_class = "uvicorn.workers.UvicornWorker"

# Bind to 0.0.0.0 to be accessible from outside the container
host = os.getenv("EMBEDDING_HOST", "0.0.0.0")
port = os.getenv("EMBEDDING_PORT", str(settings.PORT)) # Use settings.PORT as default
bind = f"{host}:{port}"

# Logging
# Gunicorn logging will be handled by structlog through FastAPI's setup
# accesslog = "-" # Log to stdout
# errorlog = "-"  # Log to stderr
# loglevel = settings.LOG_LEVEL.lower() # Already handled by app

# Worker timeout
timeout = int(os.getenv("EMBEDDING_GUNICORN_TIMEOUT", "120"))

# Other settings
# preload_app = True # Consider if model loading time is very long and you want to load before forking workers.
                     # However, for GPU models, it's often better to load in each worker if workers > 1,
                     # but we force workers=1 for GPU SentenceTransformer for now.

# Print effective Gunicorn configuration for clarity
print("--- Gunicorn Configuration ---")
print(f"Workers: {workers}")
print(f"Worker Class: {worker_class}")
print(f"Bind: {bind}")
print(f"Timeout: {timeout}")
# print(f"Preload App: {preload_app}")
print("----------------------------")