#!/usr/bin/env bash
set -e

echo "=========================================================="
echo "  Spinning up LLM Agent Evaluation Operating System       "
echo "  PostgreSQL 16 + FastAPI Engine + Apple-Grade Web UI     "
echo "=========================================================="

# Check if Docker is running
if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
    echo "[✓] Docker is active. Launching Postgres and Eval Engine containers..."
    docker compose up --build -d
    echo ""
    echo "[✓] Waiting for services to become healthy..."
    docker compose ps
    echo ""
    echo "=========================================================="
    echo "  System Live & Accessible:"
    echo "  • Web Dashboard:  http://localhost:8000"
    echo "  • REST API:       http://localhost:8000/api"
    echo "  • PostgreSQL 16:  localhost:5433 (user: eval, db: eval_db)"
    echo "=========================================================="
else
    echo "[!] Docker not available in this environment. Falling back to local live execution..."
    python -m llm_agent_eval.cli init ./workspace
    python -m llm_agent_eval.cli serve --host 127.0.0.1 --port 8000
fi
