# api-gateway/Dockerfile
ARG PYTHON_VERSION=3.10
FROM python:${PYTHON_VERSION}-slim as base

# Python build stage
FROM base as python-build-stage
ARG PYTHON_VERSION
ARG POETRY_VERSION=1.7.1

# System dependencies
RUN apt-get update && apt-get install --no-install-recommends -y \
    curl \
    && apt-get autoremove -y && apt-get clean -y && rm -rf /var/lib/apt/lists/*

# Install poetry
ENV POETRY_HOME="/opt/poetry" \
    POETRY_VIRTUALENVS_IN_PROJECT=true \
    POETRY_NO_INTERACTION=1
# Corrección: Usar valor explícito en PATH
ENV PATH="/opt/poetry/bin:$PATH"
RUN curl -sSL https://install.python-poetry.org | python - --version ${POETRY_VERSION}

# Set working directory
WORKDIR /app

# Copy only requirements to cache them
# **ASEGÚRATE QUE ESTOS ARCHIVOS EXISTEN EN api-gateway/**
COPY poetry.lock pyproject.toml ./

# Install runtime dependencies
RUN poetry install --no-root --no-dev --compile

# Runtime stage
FROM base
ARG PYTHON_VERSION
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    # Set paths to find installed packages
    PATH="/app/.venv/bin:$PATH" \
    # Define el puerto por defecto que usará Gunicorn si no se sobrescribe
    PORT=8080

WORKDIR /app

# Copy virtual env from build stage
COPY --from=python-build-stage /app/.venv /app/.venv
# Copy application code
COPY ./app ./app

# Expose the port the app runs on
EXPOSE ${PORT}

# Set user (optional but recommended for security - requires directory permissions adjustment)
# RUN groupadd -r appuser && useradd --no-log-init -r -g appuser appuser
# RUN chown -R appuser:appuser /app
# USER appuser

# Command to run the application using Gunicorn
# Corrección: Usar puerto explícito o ${PORT} en la forma shell de CMD si es necesario.
# Aquí usamos el puerto 8080 directamente, que coincide con EXPOSE y la variable PORT.
CMD ["gunicorn", "-k", "uvicorn.workers.UvicornWorker", "-w", "4", "-b", "0.0.0.0:8080", "app.main:app"]