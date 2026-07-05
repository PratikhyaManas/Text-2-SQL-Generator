"""
Layered configuration for the secure Text-to-SQL system.

Resolution order (later wins): code defaults -> configs/config.yaml ->
.env -> real environment variables. This lets the same code run against
a local Ollama model in dev and a different model/host in another
environment, purely through config, with no code changes.
"""

import os
from typing import Any, Dict

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # --- API server ---
    api_host: str = "127.0.0.1"
    api_port: int = 8000
    debug: bool = False
    environment: str = "development"
    log_level: str = "INFO"

    # --- LLM ---
    # "ollama" talks to a real local model server; "mock" uses a small
    # rule-based generator so the whole pipeline can be demoed/tested
    # without any local model installed.
    llm_provider: str = "mock"
    ollama_host: str = "http://localhost:11434"
    ollama_model: str = "llama3.2"
    llm_timeout_seconds: int = 30

    # --- Database ---
    db_path: str = "data/sample.db"
    database_paths: Dict[str, str] = {}

    # --- Security / query guardrails ---
    max_result_rows: int = 100
    default_row_limit: int = 50
    query_timeout_seconds: int = 5
    max_retries: int = 1
    row_filters: Dict[str, str] = {}
    redaction_enabled: bool = True
    cache_enabled: bool = True
    cache_ttl_seconds: int = 300
    rate_limit_per_minute: int = 60
    conversation_history_limit: int = 3
    include_examples_in_prompt: bool = True

    # --- Audit ---
    audit_log_path: str = "logs/audit.jsonl"

    # --- In-memory limits ---
    max_cache_entries: int = 512
    max_history_entries: int = 1000
    max_rate_limit_users: int = 1000

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")


def _load_yaml_config(path: str) -> Dict[str, Any]:
    import yaml

    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
            return data or {}
    except Exception:
        return {}


def _load_dotenv_values(path: str) -> Dict[str, str]:
    if not os.path.exists(path):
        return {}

    values: Dict[str, str] = {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            for raw_line in f:
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip().strip("\"'")
    except Exception:
        return {}
    return values


def load_config(yaml_path: str = "configs/config.yaml", env_path: str = ".env") -> Settings:
    settings = Settings(_env_file=env_path)

    # Precedence target: defaults -> yaml -> .env -> environment.
    # `Settings()` already resolved defaults + .env + environment, so we only
    # apply YAML values for fields that were not set by .env/environment.
    yaml_config = _load_yaml_config(yaml_path)
    dotenv_values = _load_dotenv_values(env_path)

    for key, value in yaml_config.items():
        env_key = key.upper()
        if hasattr(settings, key) and env_key not in os.environ and env_key not in dotenv_values:
            setattr(settings, key, value)

    return settings


settings = load_config()
