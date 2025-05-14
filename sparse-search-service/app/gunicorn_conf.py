# sparse-search-service/app/gunicorn_conf.py
import os
import multiprocessing

# --- Server Mechanics ---
# bind = f"0.0.0.0:{os.environ.get('PORT', '8004')}" # FastAPI/Uvicorn main.py reads PORT
# Gunicorn will use the PORT env var by default if not specified with -b

# --- Worker Processes ---
# Autotune based on CPU cores if GUNICORN_PROCESSES is not set
default_workers = (multiprocessing.cpu_count() * 2) + 1
workers = int(os.environ.get('GUNICORN_PROCESSES', str(default_workers)))
if workers <= 0: workers = default_workers

# Threads per worker (UvicornWorker is async, so threads are less critical but can help with blocking I/O)
threads = int(os.environ.get('GUNICORN_THREADS', '1')) # Default to 1 for async workers, can be increased.

worker_class = 'uvicorn.workers.UvicornWorker'
worker_tmp_dir = "/dev/shm" # Use shared memory for worker temp files

# --- Logging ---
# Gunicorn's log level for its own messages. App logs are handled by structlog.
loglevel = os.environ.get('GUNICORN_LOG_LEVEL', 'info').lower()
accesslog = '-' # Log to stdout
errorlog = '-'  # Log to stderr

# --- Process Naming ---
# proc_name = 'sparse-search-service' # Set a process name

# --- Timeouts ---
timeout = int(os.environ.get('GUNICORN_TIMEOUT', '120')) # Default worker timeout
graceful_timeout = int(os.environ.get('GUNICORN_GRACEFUL_TIMEOUT', '30')) # Timeout for graceful shutdown
keepalive = int(os.environ.get('GUNICORN_KEEPALIVE', '5')) # HTTP Keep-Alive header timeout

# --- Security ---
# forward_allow_ips = '*' # Trust X-Forwarded-* headers from all proxies (common in K8s)

# --- Raw Environment Variables for Workers ---
# Pass application-specific log level to Uvicorn workers
# This ensures Uvicorn itself respects the log level set for the application.
# The app's structlog setup will use SPARSE_LOG_LEVEL from the environment.
# This raw_env is for Gunicorn to pass to Uvicorn workers if Uvicorn uses it.
raw_env = [
    f"SPARSE_LOG_LEVEL={os.environ.get('SPARSE_LOG_LEVEL', 'INFO')}",
    # Add other env vars if needed by workers specifically at this stage
]

# Example of print statements to verify Gunicorn config during startup (remove for production)
print(f"[Gunicorn Config] Workers: {workers}")
print(f"[Gunicorn Config] Threads: {threads}")
print(f"[Gunicorn Config] Log Level (Gunicorn): {loglevel}")
print(f"[Gunicorn Config] App Log Level (SPARSE_LOG_LEVEL for Uvicorn worker): {os.environ.get('SPARSE_LOG_LEVEL', 'INFO')}")