# embedding-service/Dockerfile
FROM python:3.10-slim

# Set working directory
WORKDIR /app

# Set environment variables to prevent Python from writing pyc files to disc and for unbuffered output
ENV PYTHONDONTWRITEBYTECODE 1
ENV PYTHONUNBUFFERED 1

# Install system dependencies that might be needed by onnxruntime or other libs
# For onnxruntime, libgomp1 is common. Add others if build fails.
# RUN apt-get update && apt-get install -y --no-install-recommends \
#     libgomp1 \
#  && rm -rf /var/lib/apt/lists/*

# Install Poetry
# Pinning Poetry version for reproducible builds
RUN pip install --no-cache-dir poetry==1.7.1

# Copy only pyproject.toml and poetry.lock to leverage Docker cache for dependency installation
COPY pyproject.toml poetry.lock ./

# Configure Poetry to not create virtual environments within the project directory
# Install project dependencies using Poetry.
# --no-interaction: Do not ask any interactive questions
# --no-ansi: Disable ANSI output
# --no-dev: Do not install development dependencies
RUN poetry config virtualenvs.create false && \
    poetry install --no-interaction --no-ansi --no-dev

# Copy the rest of the application code into the /app directory
COPY ./app /app/app

# Expose the port the app runs on (defined by EMBEDDING_PORT, default 8003)
# This should match the port Gunicorn binds to.
EXPOSE 8003

# Command to run the application using Gunicorn with Uvicorn workers
# -w: Number of Gunicorn worker processes. A common starting point is (2 * CPU_CORES) + 1.
#     Adjust based on your specific needs and available resources.
# -k: The type of worker class to use.
# -b: The address and port to bind to. 0.0.0.0 makes it accessible from outside the container.
# -t: Worker timeout in seconds.
# The port here (8003) must match the EXPOSE instruction and settings.PORT if overridden.
CMD ["gunicorn", "-w", "2", "-k", "uvicorn.workers.UvicornWorker", "app.main:app", "--bind", "0.0.0.0:8003", "--timeout", "120"]