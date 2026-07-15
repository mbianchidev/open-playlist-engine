from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.core.rate_limit import RateLimiter
from app.core.session_tokens import SessionTokenError, sign_session, verify_session


def test_signed_sessions_are_purpose_bound_tamper_evident_and_expiring() -> None:
    secret = "a" * 64
    now = datetime(2026, 7, 14, tzinfo=UTC)
    token = sign_session("recipient-session", purpose="share-recipient", secret=secret, now=now)

    assert (
        verify_session(
            token,
            purpose="share-recipient",
            secret=secret,
            max_age_s=3600,
            now=now + timedelta(minutes=5),
        )
        == "recipient-session"
    )
    with pytest.raises(SessionTokenError, match="purpose"):
        verify_session(
            token,
            purpose="owner",
            secret=secret,
            max_age_s=3600,
            now=now + timedelta(minutes=5),
        )
    with pytest.raises(SessionTokenError, match="signature"):
        verify_session(
            f"{token[:-1]}x",
            purpose="share-recipient",
            secret=secret,
            max_age_s=3600,
            now=now + timedelta(minutes=5),
        )
    with pytest.raises(SessionTokenError, match="expired"):
        verify_session(
            token,
            purpose="share-recipient",
            secret=secret,
            max_age_s=60,
            now=now + timedelta(minutes=5),
        )


@pytest.mark.asyncio
async def test_rate_limiter_rejects_without_blocking_and_reports_retry_after() -> None:
    limiter = RateLimiter()

    assert (
        await limiter.try_consume("share:token:view", capacity=2, refill_per_s=1, cost=1)
        is None
    )
    assert (
        await limiter.try_consume("share:token:view", capacity=2, refill_per_s=1, cost=1)
        is None
    )
    retry_after = await limiter.try_consume(
        "share:token:view", capacity=2, refill_per_s=1, cost=1
    )

    assert retry_after is not None
    assert 0 < retry_after <= 1


