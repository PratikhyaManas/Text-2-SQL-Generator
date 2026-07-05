from src.core.config import load_config


def test_environment_overrides_dotenv_and_yaml(tmp_path, monkeypatch):
    yaml_path = tmp_path / "config.yaml"
    yaml_path.write_text("api_port: 8200\n", encoding="utf-8")

    env_path = tmp_path / ".env"
    env_path.write_text("API_PORT=8100\n", encoding="utf-8")

    monkeypatch.setenv("API_PORT", "8300")

    settings = load_config(yaml_path=str(yaml_path), env_path=str(env_path))

    assert settings.api_port == 8300


def test_dotenv_overrides_yaml_when_environment_absent(tmp_path, monkeypatch):
    yaml_path = tmp_path / "config.yaml"
    yaml_path.write_text("api_port: 8200\n", encoding="utf-8")

    env_path = tmp_path / ".env"
    env_path.write_text("API_PORT=8100\n", encoding="utf-8")

    monkeypatch.delenv("API_PORT", raising=False)

    settings = load_config(yaml_path=str(yaml_path), env_path=str(env_path))

    assert settings.api_port == 8100


def test_yaml_applies_when_no_env_sources_set(tmp_path, monkeypatch):
    yaml_path = tmp_path / "config.yaml"
    yaml_path.write_text("api_port: 8200\n", encoding="utf-8")

    env_path = tmp_path / ".env"
    env_path.write_text("\n", encoding="utf-8")

    monkeypatch.delenv("API_PORT", raising=False)

    settings = load_config(yaml_path=str(yaml_path), env_path=str(env_path))

    assert settings.api_port == 8200
