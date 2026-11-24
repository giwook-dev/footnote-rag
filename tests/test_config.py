"""Settings: fake mode is forced in tests and secrets never show up in repr or logs."""

import io
import json
import logging

from pydantic import SecretStr

from footnote_rag.config import Settings, get_settings
from footnote_rag.observability import _mask_processor


def test_tests_run_in_fake_mode_without_keys():
    s = get_settings()
    assert (s.llm_mode, s.embedder_mode, s.dart_mode) == ("fake", "fake", "fake")
    assert s.anthropic_api_key is None
    assert s.voyage_api_key is None
    assert s.opendart_api_key is None


def test_auto_mode_resolves_from_keys():
    s = Settings(_env_file=None, llm_provider="auto", anthropic_api_key=SecretStr("sk-x"))
    assert s.llm_mode == "real"
    s = Settings(_env_file=None, llm_provider="auto", anthropic_api_key=None)
    assert s.llm_mode == "fake"
    s = Settings(_env_file=None, llm_provider="fake", anthropic_api_key=SecretStr("sk-x"))
    assert s.llm_mode == "fake"


def test_empty_env_values_mean_unset(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    monkeypatch.setenv("JWT_SECRET", "")
    s = Settings(_env_file=None, llm_provider="auto")
    assert s.anthropic_api_key is None and s.llm_mode == "fake"
    assert len(s.jwt_secret.get_secret_value()) >= 32


def test_secrets_are_masked_in_repr_and_dump():
    secret = "sk-ant-THIS-SHOULD-NEVER-LEAK"
    s = Settings(
        _env_file=None,
        anthropic_api_key=SecretStr(secret),
        voyage_api_key=SecretStr(secret),
        opendart_api_key=SecretStr(secret),
        jwt_secret=SecretStr(secret),
    )
    assert secret not in repr(s)
    assert secret not in str(s)
    assert secret not in json.dumps(s.model_dump(mode="json"))
    assert s.anthropic_api_key.get_secret_value() == secret


def test_settings_in_log_output_are_masked():
    secret = "voyage-SECRET-VALUE"
    s = Settings(_env_file=None, voyage_api_key=SecretStr(secret))
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    logger = logging.getLogger("test.settings")
    logger.addHandler(handler)
    try:
        logger.warning("settings=%r key=%s", s, s.voyage_api_key)
    finally:
        logger.removeHandler(handler)
    assert secret not in stream.getvalue()


def test_structlog_processor_masks_sensitive_fields():
    event = {
        "event": "x",
        "url": "https://opendart.fss.or.kr/api/list.json?crtfc_key=abc123&corp_code=1",
        "password": "hunter2",
        "nested": {"api_key": "k", "ok": "fine"},
    }
    out = _mask_processor(None, "info", event)
    assert "abc123" not in out["url"]
    assert out["password"] == "***"
    assert out["nested"] == {"api_key": "***", "ok": "fine"}
