# Base image
FROM python:3.11-slim

# Environment
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Work directory
WORKDIR /app

# Install system dependencies (including vim)
RUN apt-get update && \
    apt-get install -y --no-install-recommends gcc libpq-dev vim && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy project files
COPY . .

# Default to interactive shell
CMD ["/bin/bash"]
