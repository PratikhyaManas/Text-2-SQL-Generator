#!/usr/bin/env bash
#
# setup_env.sh — installs dependencies, seeds the sample database, and
# creates a local .env file.
#
# Usage: ./scripts/setup_env.sh

set -euo pipefail
cd "$(dirname "$0")/.."

echo "=============================================="
echo " Secure Text-to-SQL: environment setup"
echo "=============================================="

if [ ! -f ".env" ]; then
    [ -f ".env.example" ] && cp .env.example .env && echo "Created .env from .env.example"
    if [ ! -f ".env.example" ]; then
        cat > .env <<'EOF'
API_HOST=0.0.0.0
API_PORT=8000
DEBUG=false
ENVIRONMENT=production
LLM_PROVIDER=mock
DB_PATH=data/sample.db
AUDIT_LOG_PATH=logs/audit.jsonl
LOG_LEVEL=INFO
EOF
        echo "Created a production-style .env file"
    fi
    [ -f ".env.example" ] && cp .env.example .env && echo "Created .env from .env.example"
else
    echo ".env already exists, leaving it untouched"
fi

install_and_report() {
    echo ""
    echo "Setup complete! Next steps:"
    echo "  $1"
    echo "  python scripts/seed_db.py   # (already run below)"
    echo "  $2"
}

if command -v poetry >/dev/null 2>&1; then
    echo "Detected Poetry. Installing dependencies..."
    poetry install
    poetry run python scripts/seed_db.py
    install_and_report "poetry shell" "poetry run python src/main.py"

elif command -v uv >/dev/null 2>&1; then
    echo "Detected UV. Creating virtual environment and installing dependencies..."
    uv venv
    # shellcheck disable=SC1091
    source .venv/bin/activate
    uv pip install -e ".[dev]"
    python scripts/seed_db.py
    install_and_report "source .venv/bin/activate" "python src/main.py"

else
    echo "No Poetry/UV found. Falling back to pip + venv..."
    python3 -m venv .venv
    # shellcheck disable=SC1091
    source .venv/bin/activate
    pip install --upgrade pip
    pip install -e ".[dev]"
    python scripts/seed_db.py
    install_and_report "source .venv/bin/activate" "python src/main.py"
fi

echo "=============================================="
echo " Done. Try: curl -X POST http://localhost:8000/query \\"
echo "   -H 'Content-Type: application/json' \\"
echo "   -d '{\"question\": \"how many customers do we have\"}'"
echo ""
echo " By default the app runs in offline 'mock' LLM mode (no Ollama"
echo " needed). To use a real local model: install Ollama, run"
echo " 'ollama pull llama3.2', then set llm_provider: ollama in"
echo " configs/config.yaml."
echo "=============================================="
