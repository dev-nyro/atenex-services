# Use a specific Python version slim image
FROM python:3.10-slim

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE 1
ENV PYTHONUNBUFFERED 1
ENV POETRY_VERSION=1.7.1
ENV POETRY_HOME="/opt/poetry"
ENV POETRY_VENV="/opt/poetry/venv"
ENV PATH="$POETRY_HOME/bin:$POETRY_VENV/bin:$PATH"
ENV POETRY_VIRTUALENVS_CREATE=false

# Install system dependencies including curl
RUN apt-get update && \
    apt-get install -y --no-install-recommends curl build-essential && \
    rm -rf /var/lib/apt/lists/*

# Install Poetry
RUN curl -sSL https://install.python-poetry.org | python3 -

# Set working directory
WORKDIR /app

# Copy dependency definition files
# poetry.lock* will be ignored if it doesn't exist in the repo/context
COPY pyproject.toml poetry.lock* ./

# --- CORRECTION: Separate Install and Check ---

# Install project dependencies using Poetry
# This will generate a poetry.lock inside the container if one wasn't copied
RUN poetry install --no-root --no-dev --no-interaction --no-ansi

# Verify ONNX Runtime providers in a separate layer AFTER installation is complete
RUN python -c "import onnxruntime; print('ONNX Runtime available providers:', onnxruntime.get_available_providers())"

# --- END CORRECTION ---

# Copy the application code into the container
COPY ./app /app/app

# Expose the port the app runs on (relevant for API, not worker, but good practice)
EXPOSE 8000

# Set the command to run the application using Gunicorn (for API deployment)
# For the worker deployment, this CMD will be overridden by command/args in Kubernetes YAML
CMD ["gunicorn", "-k", "uvicorn.workers.UvicornWorker", "-w", "4", "-b", "0.0.0.0:8000", "app.main:app"]