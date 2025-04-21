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
COPY pyproject.toml poetry.lock* ./

# >>>>>>>>>>> CORRECTION: Verify ONNX Runtime providers after install <<<<<<<<<<<<<<
# Install project dependencies using Poetry
# Add check for ONNX Runtime providers after installation
RUN poetry install --no-root --no-dev --no-interaction --no-ansi \
    && python -c "import onnxruntime; print('ONNX Runtime available providers:', onnxruntime.get_available_providers())"
# >>>>>>>>>>> END CORRECTION <<<<<<<<<<<<<<

# Copy the application code into the container
COPY ./app /app/app

# Expose the port the app runs on (relevant for API, not worker, but good practice)
EXPOSE 8000

# Set the command to run the application using Gunicorn (for API deployment)
# For the worker deployment, this CMD will be overridden by command/args in Kubernetes YAML
CMD ["gunicorn", "-k", "uvicorn.workers.UvicornWorker", "-w", "4", "-b", "0.0.0.0:8000", "app.main:app"]