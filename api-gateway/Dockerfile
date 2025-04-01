# api-gateway/Dockerfile
ARG PYTHON_VERSION=3.10
FROM python:${PYTHON_VERSION}-slim as base

# --- Python Build Stage ---
# Installs dependencies into a virtual environment based on pyproject.toml
FROM base as python-build-stage
ARG PYTHON_VERSION
ARG POETRY_VERSION=1.7.1

# System dependencies needed for poetry installation and sed
RUN apt-get update && apt-get install --no-install-recommends -y \
    curl \
    sed \
    && apt-get autoremove -y && apt-get clean -y && rm -rf /var/lib/apt/lists/*

# Install poetry itself
ENV POETRY_HOME="/opt/poetry" \
    POETRY_VIRTUALENVS_IN_PROJECT=true \
    POETRY_NO_INTERACTION=1
ENV PATH="/opt/poetry/bin:$PATH"
RUN curl -sSL https://install.python-poetry.org | python - --version ${POETRY_VERSION}

# Set working directory for the build stage
WORKDIR /app

# --- Dependency Installation ---
# Copy ONLY pyproject.toml.
COPY pyproject.toml ./

# *** NUEVO PASO: Eliminar temporalmente la dependencia de desarrollo conflictiva ***
# Esto evita que pytest-httpx interfiera con la resolución de httpx para 'main'.
# Usamos sed para eliminar la línea que empieza con 'pytest-httpx ='.
# El delimitador | se usa en sed para evitar conflictos con / en paths si los hubiera.
RUN sed -i '/^pytest-httpx =/d' pyproject.toml && \
    echo "pyproject.toml modified for build:" && \
    cat pyproject.toml # Opcional: mostrar el archivo modificado en los logs

# Install runtime dependencies into the project's .venv directory.
# Poetry ahora resolverá solo desde el pyproject.toml modificado.
# --no-root: Don't install the project package itself.
# --only main: Install only dependencies from the main group (runtime).
# --compile: Pre-compile pyc files.
RUN poetry install --no-root --only main --compile

# --- Runtime Stage ---
# Creates the final image by copying the virtual environment and app code
FROM base
ARG PYTHON_VERSION

# Set environment variables for the runtime container
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH" \
    PORT=8080

# Set the working directory for the runtime container
WORKDIR /app

# Copy the virtual environment created in the build stage
COPY --from=python-build-stage /app/.venv /app/.venv

# Copy the application code
COPY ./app ./app

# Expose the port the app runs on
EXPOSE ${PORT}

# Optional: Run as non-root user
# RUN groupadd -r appuser && useradd --no-log-init -r -g appuser appuser
# RUN chown -R appuser:appuser /app
# USER appuser

# Command to run the application using Gunicorn
CMD ["gunicorn", "-k", "uvicorn.workers.UvicornWorker", "-w", "4", "-b", "0.0.0.0:8080", "app.main:app"]