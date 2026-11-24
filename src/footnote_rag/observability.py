"""구조화 로그(structlog JSON), request_id 전파, 비밀값 마스킹.

역할: 로그 이벤트에 ``request_id``를 붙이고 비밀번호·토큰·``crtfc_key`` 값을 ``***``로 가린다.
주요 요소: ``configure_logging``, ``get_logger``, ``mask_url``, ``request_id_var``(ContextVar).
호출 관계: ``main``의 미들웨어가 ``request_id_var``를 설정하고, ``audit.service.record``가 그 값을 읽는다.
학습 문서: docs/study/09-testing-and-ops.md (관측성), docs/study/07-dart-integration.md (키 마스킹)

Structured JSON logging, request IDs and secret masking.
"""

from __future__ import annotations

import logging
import re
import sys
from contextvars import ContextVar
from typing import Any

import structlog

request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)

_CRTFC_RE = re.compile(r"(crtfc_key=)[^&\s\"']+", re.IGNORECASE)
_SENSITIVE_KEYS = {
    "password",
    "password_hash",
    "token",
    "access_token",
    "authorization",
    "api_key",
    "crtfc_key",
    "anthropic_api_key",
    "voyage_api_key",
    "opendart_api_key",
    "jwt_secret",
}


def mask_url(url: str) -> str:
    """Hide the OpenDART ``crtfc_key`` query value in a URL or message."""
    return _CRTFC_RE.sub(r"\1***", url)


def _mask_value(value: Any) -> Any:
    if isinstance(value, str):
        return mask_url(value)
    if isinstance(value, dict):
        return {
            k: ("***" if str(k).lower() in _SENSITIVE_KEYS else _mask_value(v))
            for k, v in value.items()
        }
    if isinstance(value, list | tuple):
        return type(value)(_mask_value(v) for v in value)
    return value


def _mask_processor(_logger: Any, _name: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    return _mask_value(event_dict)


def _add_request_id(_logger: Any, _name: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    rid = request_id_var.get()
    if rid and "request_id" not in event_dict:
        event_dict["request_id"] = rid
    return event_dict


class _StdlibMaskFilter(logging.Filter):
    """Mask crtfc_key in plain stdlib log records (third-party libraries)."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - never break logging
            return True
        masked = mask_url(message)
        if masked != message:
            record.msg, record.args = masked, None
        return True


_configured = False


def configure_logging(level: str = "INFO", json_logs: bool = True) -> None:
    global _configured
    if _configured:
        return
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=level.upper())
    # httpx logs full request URLs at INFO, which would include OpenDART's crtfc_key.
    for noisy in ("httpx", "httpcore", "anthropic", "voyageai", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    for handler in logging.getLogger().handlers:
        handler.addFilter(_StdlibMaskFilter())
    renderer: Any = (
        structlog.processors.JSONRenderer(ensure_ascii=False)
        if json_logs
        else structlog.dev.ConsoleRenderer()
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            _add_request_id,
            structlog.processors.format_exc_info,
            _mask_processor,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.getLevelName(level.upper()) if isinstance(level, str) else level
        ),
        cache_logger_on_first_use=False,
    )
    _configured = True


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)
