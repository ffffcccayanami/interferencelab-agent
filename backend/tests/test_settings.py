"""配置校验：占位符拒绝、密钥掩码、目录创建。"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.settings import ConfigError, Settings

# 运行时拼接的假 key：只用于校验格式与掩码，仓库里不出现"看起来像真 key"的字面量。
FAKE_KEY = "sk-" + "abcdef1234567890"


def build(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "_env_file": None,
        "llm_enabled": True,
        "llm_model_id": "example-model",
        "llm_base_url": "http://127.0.0.1:8000/v1",
        "llm_api_key": "sk-" + "a" * 32,
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def test_valid_key_passes() -> None:
    build().validate_for_runtime()


def test_placeholder_key_is_rejected() -> None:
    with pytest.raises(ConfigError) as excinfo:
        build(llm_api_key="YOUR_LLM_API_KEY").validate_for_runtime()
    assert "占位符" in str(excinfo.value)


def test_empty_key_is_rejected() -> None:
    with pytest.raises(ConfigError):
        build(llm_api_key="").validate_for_runtime()


def test_missing_base_url_is_rejected() -> None:
    with pytest.raises(ConfigError):
        build(llm_base_url="").validate_for_runtime()


def test_non_http_base_url_is_rejected() -> None:
    with pytest.raises(ConfigError) as excinfo:
        build(llm_base_url="127.0.0.1:8000/v1").validate_for_runtime()
    assert "http" in str(excinfo.value)


def test_placeholder_model_id_is_rejected() -> None:
    with pytest.raises(ConfigError) as excinfo:
        build(llm_model_id="YOUR_LLM_MODEL_ID").validate_for_runtime()
    assert "LLM_MODEL_ID" in str(excinfo.value)


def test_auth_mode_none_allows_empty_key() -> None:
    settings = build(llm_auth_mode="none", llm_api_key="")
    settings.validate_for_runtime()
    assert settings.llm_configured is True
    assert settings.llm_key_fingerprint() == "unset"


def test_endpoint_and_health_url_are_normalized() -> None:
    settings = build(llm_base_url="https://gateway.example.com/v1/")
    assert settings.llm_base_url_normalized == "https://gateway.example.com/v1"
    assert settings.llm_endpoint == "gateway.example.com"
    assert settings.llm_health_url == "https://gateway.example.com/v1/models"


def test_disabled_llm_skips_key_validation() -> None:
    settings = build(llm_enabled=False, llm_api_key="")
    settings.validate_for_runtime()
    assert settings.llm_key_fingerprint() == "unset"


def test_fingerprint_is_masked() -> None:
    settings = build(llm_api_key=FAKE_KEY)
    fingerprint = settings.llm_key_fingerprint()
    assert fingerprint.startswith("sk-a")
    assert "def1234567890" not in fingerprint
    assert "..." in fingerprint


def test_secret_is_not_in_repr() -> None:
    settings = build(llm_api_key=FAKE_KEY)
    assert "abcdef1234567890" not in repr(settings)


def test_ensure_dirs_creates_layout(workdir: Path) -> None:
    settings = Settings(_env_file=None, llm_enabled=False, infl_data_dir=workdir / "var")
    settings.ensure_dirs()
    assert settings.db_path.parent.is_dir()
    assert settings.uploads_dir.is_dir()
    assert settings.runs_dir.is_dir()
