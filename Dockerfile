FROM python:3.12-slim

WORKDIR /app

# Install build essentials if needed and curl for healthchecks
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install the resolved runtime dependencies. The lockfile intentionally holds
# package versions only; deployment credentials come from the environment.
COPY pyproject.toml README.md requirements.lock /app/
RUN pip install --no-cache-dir --requirement requirements.lock

# Copy source code and assets
COPY src/ /app/src/
COPY web/ /app/web/
COPY fixtures/ /app/fixtures/

# Install the engine CLI
RUN pip install --no-cache-dir --no-deps -e .

EXPOSE 8000

ENV PYTHONUNBUFFERED=1

CMD ["eval-engine", "serve", "--host", "0.0.0.0", "--port", "8000"]
