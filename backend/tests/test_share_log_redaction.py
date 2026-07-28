from __future__ import annotations

import logging

from app.core.logging import ShareTokenRedactionFilter


def test_share_tokens_are_redacted_from_log_messages_and_arguments() -> None:
    token = "a" * 48
    filter_ = ShareTokenRedactionFilter()
    record = logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname=__file__,
        lineno=10,
        msg='%s - "%s %s HTTP/1.1" %s',
        args=(
            "127.0.0.1",
            "GET",
            f"/api/public/shares/{token}/download?format=json",
            200,
        ),
        exc_info=None,
    )

    assert filter_.filter(record)
    rendered = record.getMessage()
    assert token not in rendered
    assert "/api/public/shares/[redacted]/download" in rendered
