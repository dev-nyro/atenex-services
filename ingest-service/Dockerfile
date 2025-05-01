# ---- Stage 1: Builder ----
# Use a specific Python version slim image as builder base
# LLM_COMMENT: Builder stage to install dependencies including build tools.
FROM python:3.10-slim as builder

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    POETRY_VERSION=1.7.1 \
    POETRY_HOME="/opt/poetry" \
    PATH="$POETRY_HOME/bin:$PATH" \
    POETRY_NO_INTERACTION=1 \
    # LLM_COMMENT: Install into a standard location within the builder stage.
    POETRY_VIRTUALENVS_CREATE=false \
    # LLM_COMMENT: Define standard paths for Python packages.
    PYTHONPATH="/usr/local/lib/python3.10/site-packages" \
    PIP_TARGET="/usr/local/lib/python3.10/site-packages"

# Install system build dependencies and curl
# LLM_COMMENT: Install build tools needed for some Python packages + curl for Poetry.
RUN apt-get update && \
    apt-get install -y --no-install-recommends curl build-essential && \
    rm -rf /var/lib/apt/lists/*

# Install Poetry
# LLM_COMMENT: Install Poetry itself.
RUN curl -sSL https://install.python-poetry.org | python3 - --version ${POETRY_VERSION}

# Set working directory for builder
WORKDIR /build

# Copy ONLY the dependency definition files
# LLM_COMMENT: Copy dependency files to leverage cache.
COPY pyproject.toml poetry.lock* ./


# Debug: mostrar binario poetry y versión
RUN ls -l /opt/poetry/bin && /opt/poetry/bin/poetry --version
# Forzar PATH correcto antes de instalar dependencias
ENV PATH="/opt/poetry/bin:$PATH"
RUN poetry install --no-root --only main
# RUN poetry install --no-root # Use this if any dev dependency IS required for building a main dependency


# ---- Stage 2: Final Image ----
# Use the same slim base image for the final stage
# LLM_COMMENT: Final stage using the same slim base.
FROM python:3.10-slim

# Set environment variables for the final image (no POETRY needed here)
# LLM_COMMENT: Set runtime environment variables.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    # LLM_COMMENT: Set path for application execution.
    WORKDIR="/app"

# Install ONLY runtime system dependencies (if any are needed, e.g., libpq for psycopg2 if not binary)
# RUN apt-get update && apt-get install -y --no-install-recommends some-runtime-lib && rm -rf /var/lib/apt/lists/*
# LLM_COMMENT: Currently, no runtime system dependencies seem explicitly required beyond what python-slim provides. Add if necessary.

# Set working directory
WORKDIR ${WORKDIR}

# Copy installed dependencies from the builder stage
# LLM_COMMENT: Copy only the installed packages from the builder stage. This is the key step.
COPY --from=builder /usr/local/lib/python3.10/site-packages /usr/local/lib/python3.10/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

# Copy the application code
# LLM_COMMENT: Copy application code.
COPY ./app /app/app

# Expose the port the API runs on
# LLM_COMMENT: Expose the API port.
EXPOSE 8000

# Set the default command to run the API (can be overridden)
# LLM_COMMENT: Default command for API. Workers will override this in Kubernetes YAML.
CMD ["gunicorn", "-k", "uvicorn.workers.UvicornWorker", "-w", "4", "-b", "0.0.0.0:8000", "app.main:app"]