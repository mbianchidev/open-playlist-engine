"""Logging filters for bearer-like share tokens embedded in public URLs."""

from __future__ import annotations

import logging
import re
from typing import Any

_SHARE_TOKEN_PATH = re.compile(
    r"(/(?:api/public/shares|share|shared)/)[A-Za-z0-9_-]{32,}"
)


def _redact(value: Any) -> Any:
    if isinstance(value, str):
        return _SHARE_TOKEN_PATH.sub(r"\1[redacted]", value)
    if isinstance(value, tuple):
        return tuple(_redact(item) for item in value)
    if isinstance(value, dict):
        return {key: _redact(item) for key, item in value.items()}
    return value


class ShareTokenRedactionFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = _redact(record.msg)
        record.args = _redact(record.args)
        return True


def configure_share_token_redaction() -> None:
    for name in ("uvicorn.access", "uvicorn.error", "app"):
        logger = logging.getLogger(name)
        if any(isinstance(filter_, ShareTokenRedactionFilter) for filter_ in logger.filters):
            continue
        logger.addFilter(ShareTokenRedactionFilter())

