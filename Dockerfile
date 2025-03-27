# Usar una imagen base de Python
FROM python:3.10-slim

# Establecer directorio de trabajo
WORKDIR /app

# Variables de entorno (pueden ser útiles para logs o config)
ENV PYTHONDONTWRITEBYTECODE 1
ENV PYTHONUNBUFFERED 1

# Instalar dependencias del sistema si son necesarias (ej. para OCR local)
# RUN apt-get update && apt-get install -y --no-install-recommends some-package && rm -rf /var/lib/apt/lists/*

# Instalar dependencias de Python usando Poetry
COPY pyproject.toml poetry.lock* ./
RUN pip install --no-cache-dir poetry==1.7.1 \
    && poetry config virtualenvs.create false \
    && poetry install --no-dev --no-interaction --no-ansi

# Copiar el código de la aplicación
COPY ./app /app/app

# Exponer el puerto que usa FastAPI
EXPOSE 8000

# Comando para correr la aplicación (se puede sobreescribir en GKE/docker-compose)
# Ejecutar con Gunicorn para producción
CMD ["gunicorn", "-k", "uvicorn.workers.UvicornWorker", "-c", "gunicorn_conf.py", "app.main:app"]

# Nota: Necesitarás un archivo gunicorn_conf.py
# Ejemplo gunicorn_conf.py:
# bind = "0.0.0.0:8000"
# workers = 4 # Ajustar según CPU
# worker_class = "uvicorn.workers.UvicornWorker"
# loglevel = "info"
# accesslog = "-" # Log a stdout
# errorlog = "-"  # Log a stdout