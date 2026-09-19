FROM postgres:16-bookworm AS postgres16-client

FROM python:3.12-bookworm

WORKDIR /app

# Install curl for healthchecks and libpq for the PostgreSQL 16 client tools
# copied below. The application never uses these tools to execute evaluated
# agent code.
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    libpq5 \
    && rm -rf /var/lib/apt/lists/*

# Keep dump/restore major-version compatible with the Compose PostgreSQL 16
# service. Debian's default client package can advance to a newer major and
# emit restore SQL that the deployed server rejects.
COPY --from=postgres16-client /usr/lib/postgresql/16/bin/pg_dump /usr/local/bin/pg_dump
COPY --from=postgres16-client /usr/lib/postgresql/16/bin/pg_restore /usr/local/bin/pg_restore
RUN pg_dump --version && pg_restore --version

# Install the resolved runtime dependencies. The lockfile intentionally holds
# package versions only; deployment credentials come from the environment.
COPY pyproject.toml README.md requirements.lock /app/
RUN pip install --no-cache-dir --requirement requirements.lock

# Copy source code and assets
COPY src/ /app/src/
COPY web/ /app/web/
COPY fixtures/ /app/fixtures/
COPY migrations/ /app/migrations/
COPY skills/ /app/skills/

# Install the engine CLI
RUN pip install --no-cache-dir --no-deps -e .

EXPOSE 8000

ENV PYTHONUNBUFFERED=1

# The API image is not an agent runtime. Evaluated code must execute in the
# separately constrained rootless runtime, never as this service account.
RUN useradd --system --create-home --uid 10001 evalengine \
    && mkdir -p /var/lib/llm-agent-eval \
    && chown -R evalengine:evalengine /app /var/lib/llm-agent-eval
USER evalengine

CMD ["eval-engine", "serve", "--host", "0.0.0.0", "--port", "8000"]
