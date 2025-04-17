# Usar imagen base Python
FROM python:3.10-slim

# Establecer variables de entorno como en ingest-service
# (POETRY_HOME es importante para que el script de instalación sepa dónde poner poetry)
ENV PYTHONUNBUFFERED=1 \
    PYTHONFAULTHANDLER=1 \
    POETRY_VERSION=1.7.1 \
    POETRY_HOME="/opt/poetry" \
    # Ya no necesitamos POETRY_VENV en el PATH si llamamos a poetry directamente
    # POETRY_VENV="/opt/poetry/venv" \
    # Importante: Asegurar que poetry no cree un venv en el proyecto
    POETRY_VIRTUALENVS_CREATE=false \
    # Añadir SOLO el binario de poetry al PATH para uso general si es necesario
    # Pero para el RUN específico, usaremos la ruta completa.
    PATH="$POETRY_HOME/bin:$PATH" \
    # Puerto por defecto para query-service
    PORT=8001

# Instalar dependencias del sistema (curl para instalar poetry)
# Combinar comandos para reducir capas
RUN apt-get update && \
    apt-get install -y --no-install-recommends curl build-essential && \
    rm -rf /var/lib/apt/lists/*

# Instalar Poetry usando el script oficial
# El script usará POETRY_HOME para decidir dónde instalarse (/opt/poetry)
RUN curl -sSL https://install.python-poetry.org | python3 -

# Establecer directorio de trabajo
WORKDIR /app

# Copiar solo pyproject.toml y poetry.lock primero
COPY pyproject.toml poetry.lock ./

# Instalar dependencias del proyecto usando Poetry
# *** CORRECCIÓN: Llamar a poetry usando la ruta absoluta ***
RUN /opt/poetry/bin/poetry install --no-root --no-dev --no-interaction --no-ansi

# Copiar el código de la aplicación al contenedor
COPY app ./app

# Exponer el puerto en el que correrá la aplicación
EXPOSE ${PORT}

# Comando para correr la aplicación con Gunicorn
# (Igual que antes, usando el puerto definido)
CMD ["gunicorn", "-k", "uvicorn.workers.UvicornWorker", "-w", "4", "-b", "0.0.0.0:${PORT}", "app.main:app"]

# Comando alternativo para correr con Uvicorn directamente
# CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "${PORT}"]