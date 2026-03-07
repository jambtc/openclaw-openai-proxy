FROM python:3.10-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Copy project files
COPY pyproject.toml .
COPY openclaw_openai_proxy ./openclaw_openai_proxy
COPY config.yaml .
COPY .env .

# Install python dependencies
RUN pip install --no-cache-dir .

# Expose proxy port
EXPOSE 4010

# Run the proxy
CMD ["openclaw-openai-proxy"]
