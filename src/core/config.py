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
    api_host: str = "0.0.0.0"
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
    log_level: str = "INFO"

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


def load_config() -> Settings:
    settings = Settings()

    yaml_config = _load_yaml_config("configs/config.yaml")
    for key, value in yaml_config.items():
        if hasattr(settings, key):
            setattr(settings, key, value)

    return settings


settings = load_config()
