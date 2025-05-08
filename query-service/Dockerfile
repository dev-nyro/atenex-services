# Usar imagen base Python
FROM python:3.10-slim

# Establecer variables de entorno como en ingest-service
ENV PYTHONUNBUFFERED=1 \
    PYTHONFAULTHANDLER=1 \
    POETRY_VERSION=1.7.1 \
    POETRY_HOME="/opt/poetry" \
    POETRY_VIRTUALENVS_CREATE=false \
    PATH="$POETRY_HOME/bin:$PATH" \
    PORT=8001

# Instalar dependencias del sistema (curl para instalar poetry)
# Combinar comandos para reducir capas
RUN apt-get update && \
    apt-get install -y --no-install-recommends curl build-essential && \
    rm -rf /var/lib/apt/lists/*

# Instalar Poetry usando el script oficial
RUN curl -sSL https://install.python-poetry.org | python3 -

# Establecer directorio de trabajo
WORKDIR /app

# Copiar solo pyproject.toml y poetry.lock primero
# --- LLM_CORRECTION: Copiar también poetry.lock ---
COPY pyproject.toml poetry.lock* ./
# --- END CORRECTION ---


# Instalar dependencias del proyecto usando Poetry
RUN /opt/poetry/bin/poetry install --no-root --no-interaction --no-ansi

# --- LLM_CORRECTION: Añadir paso de verificación para bm2s ---
RUN python -c "import sys; print('Verifying bm2s import...'); import bm2s; print('bm2s imported successfully!')"
# --- END CORRECTION ---


# Copiar el código de la aplicación al contenedor
COPY app ./app

# Exponer el puerto en el que correrá la aplicación
EXPOSE ${PORT}

# Comando para correr la aplicación con Gunicorn
# Timeout ya está en 120s, especificado en el Deployment YAML
CMD ["gunicorn", "-k", "uvicorn.workers.UvicornWorker", "-w", "2", "-b", "0.0.0.0:${PORT}", "app.main:app"]

# Comando alternativo para correr con Uvicorn directamente (útil para debug local)
# CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "${PORT}", "--reload"]