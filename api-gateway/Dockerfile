# api-gateway/Dockerfile
ARG PYTHON_VERSION=3.10
FROM python:${PYTHON_VERSION}-slim as base

# Python build stage
FROM base as python-build-stage
ARG PYTHON_VERSION
ARG POETRY_VERSION=1.7.1 # Usa una versión específica de Poetry

# System dependencies
RUN apt-get update && apt-get install --no-install-recommends -y \
    # poetry dependencies
    curl \
    # clean up
    && apt-get autoremove -y && apt-get clean -y && rm -rf /var/lib/apt/lists/*

# Install poetry
ENV POETRY_HOME="/opt/poetry" \
    POETRY_VIRTUALENVS_IN_PROJECT=true \
    POETRY_NO_INTERACTION=1 \
    PATH="$POETRY_HOME/bin:$PATH"
RUN curl -sSL https://install.python-poetry.org | python - --version ${POETRY_VERSION}

# Set working directory
WORKDIR /app

# Copy only requirements to cache them
COPY poetry.lock pyproject.toml ./

# Install runtime dependencies
# --no-root avoids installing the project itself, only dependencies
# --no-dev excludes development dependencies
# --compile might speed up startup slightly (optional)
RUN poetry install --no-root --no-dev --compile


# Runtime stage
FROM base
ARG PYTHON_VERSION
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    # Set paths to find installed packages
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

# Copy virtual env from build stage
COPY --from=python-build-stage /app/.venv /app/.venv
# Copy application code
COPY ./app ./app

# Expose the port the app runs on (default 8080, configurable)
# La variable PORT es leída por Gunicorn si se usa $(PORT) en el comando
ENV PORT=8080
EXPOSE ${PORT}

# Set user (optional but recommended for security)
# RUN useradd --create-home --shell /bin/bash appuser
# USER appuser

# Command to run the application using Gunicorn
# El puerto aquí debe coincidir con el EXPOSE y la configuración de K8s Service/Deployment
# Usar $(PORT) permite que el puerto sea configurable a través de la variable de entorno.
CMD ["gunicorn", "-k", "uvicorn.workers.UvicornWorker", "-w", "4", "-b", "0.0.0.0:$(PORT)", "app.main:app"]