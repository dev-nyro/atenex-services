# Usar imagen base Python
FROM python:3.10-slim

# Establecer variables de entorno como en ingest-service
ENV PYTHONUNBUFFERED=1 \
    PYTHONFAULTHANDLER=1 \
    POETRY_VERSION=1.7.1 \
    POETRY_HOME="/opt/poetry" \
    POETRY_VENV="/opt/poetry/venv" \
    # Importante: Asegurar que poetry no cree un venv en el proyecto
    POETRY_VIRTUALENVS_CREATE=false \
    # Añadir poetry y venv al PATH
    PATH="$POETRY_HOME/bin:$POETRY_VENV/bin:$PATH" \
    # Puerto por defecto para query-service
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

# Copiar solo pyproject.toml primero
# No copiamos poetry.lock aquí, ya que puede no existir en el repo.
# 'poetry install' lo usará si existe en el contexto de build, o lo generará si no.
COPY pyproject.toml ./

# Instalar dependencias del proyecto usando Poetry
# --no-root: No instala el paquete del proyecto en sí (query-service)
# --no-dev: Excluye dependencias de desarrollo
# Esto instalará los paquetes en el site-packages del sistema debido a POETRY_VIRTUALENVS_CREATE=false
# Poetry leerá pyproject.toml y generará/usará un lock file internamente si es necesario.
RUN poetry install --no-root --no-dev --no-interaction --no-ansi

# Copiar el código de la aplicación al contenedor
COPY app ./app

# Exponer el puerto en el que correrá la aplicación
EXPOSE ${PORT}

# Comando para correr la aplicación con Gunicorn
# (Igual que antes, usando el puerto definido)
CMD ["gunicorn", "-k", "uvicorn.workers.UvicornWorker", "-w", "4", "-b", "0.0.0.0:${PORT}", "app.main:app"]

# Comando alternativo para correr con Uvicorn directamente
# CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "${PORT}"]