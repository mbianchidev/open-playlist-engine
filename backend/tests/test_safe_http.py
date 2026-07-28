from __future__ import annotations

import asyncio
from collections.abc import Sequence

import pytest

from app.imports.http import (
    ResponseTooLarge,
    SafeHttpError,
    SafeHttpFetcher,
    SafeHttpResponse,
    TooManyRedirects,
    UnsafeRemoteAddress,
    _read_chunked_body,
)
from app.imports.urls import UnsafePlaylistUrl


async def _public_resolver(host: str, port: int) -> Sequence[str]:
    assert port == 443
    return ["93.184.216.34"]


async def test_safe_fetch_follows_only_allowlisted_public_redirects() -> None:
    calls: list[tuple[str, str]] = []

    async def requester(
        url: str, address: str, *, max_response_bytes: int, timeout_s: float
    ) -> SafeHttpResponse:
        calls.append((url, address))
        if url == "https://share.example/open-playlists/list":
            return SafeHttpResponse(
                status_code=302,
                headers={"location": "https://cdn.example/list.json"},
                body=b"",
                url=url,
            )
        return SafeHttpResponse(
            status_code=200,
            headers={"content-type": "application/json"},
            body=b'{"name":"List"}',
            url=url,
        )

    fetcher = SafeHttpFetcher(
        allowed_hosts={"share.example", "cdn.example"},
        max_redirects=2,
        max_response_bytes=100,
        timeout_s=1,
        resolver=_public_resolver,
        requester=requester,
    )

    response = await fetcher.fetch("https://share.example/open-playlists/list")

    assert response.body == b'{"name":"List"}'
    assert calls == [
        ("https://share.example/open-playlists/list", "93.184.216.34"),
        ("https://cdn.example/list.json", "93.184.216.34"),
    ]


async def test_safe_fetch_rejects_private_dns_results_before_connecting() -> None:
    called = False

    async def resolver(host: str, port: int) -> Sequence[str]:
        return ["10.0.0.5"]

    async def requester(
        url: str, address: str, *, max_response_bytes: int, timeout_s: float
    ) -> SafeHttpResponse:
        nonlocal called
        called = True
        raise AssertionError("requester must not be called")

    fetcher = SafeHttpFetcher(
        allowed_hosts={"share.example"},
        max_redirects=1,
        max_response_bytes=100,
        timeout_s=1,
        resolver=resolver,
        requester=requester,
    )

    with pytest.raises(UnsafeRemoteAddress, match="private or local"):
        await fetcher.fetch("https://share.example/open-playlists/list")
    assert called is False


async def test_safe_fetch_rejects_redirects_to_unallowlisted_hosts() -> None:
    async def requester(
        url: str, address: str, *, max_response_bytes: int, timeout_s: float
    ) -> SafeHttpResponse:
        return SafeHttpResponse(
            status_code=302,
            headers={"location": "https://localhost/admin"},
            body=b"",
            url=url,
        )

    fetcher = SafeHttpFetcher(
        allowed_hosts={"share.example"},
        max_redirects=2,
        max_response_bytes=100,
        timeout_s=1,
        resolver=_public_resolver,
        requester=requester,
    )

    with pytest.raises(UnsafePlaylistUrl):
        await fetcher.fetch("https://share.example/open-playlists/list")


async def test_safe_fetch_caps_redirects() -> None:
    async def requester(
        url: str, address: str, *, max_response_bytes: int, timeout_s: float
    ) -> SafeHttpResponse:
        return SafeHttpResponse(
            status_code=302,
            headers={"location": "/again"},
            body=b"",
            url=url,
        )

    fetcher = SafeHttpFetcher(
        allowed_hosts={"share.example"},
        max_redirects=1,
        max_response_bytes=100,
        timeout_s=1,
        resolver=_public_resolver,
        requester=requester,
    )

    with pytest.raises(TooManyRedirects, match="1"):
        await fetcher.fetch("https://share.example/open-playlists/list")


async def test_safe_fetch_caps_response_size_even_for_injected_transports() -> None:
    async def requester(
        url: str, address: str, *, max_response_bytes: int, timeout_s: float
    ) -> SafeHttpResponse:
        return SafeHttpResponse(
            status_code=200,
            headers={"content-type": "application/json"},
            body=b"x" * 101,
            url=url,
        )

    fetcher = SafeHttpFetcher(
        allowed_hosts={"share.example"},
        max_redirects=1,
        max_response_bytes=100,
        timeout_s=1,
        resolver=_public_resolver,
        requester=requester,
    )

    with pytest.raises(ResponseTooLarge, match="100"):
        await fetcher.fetch("https://share.example/open-playlists/list")


async def test_chunked_response_rejects_overlong_framing_as_safe_error() -> None:
    reader = asyncio.StreamReader(limit=10)
    reader.feed_data(b"f" * 11)
    reader.feed_eof()

    with pytest.raises(SafeHttpError, match="chunk framing"):
        await _read_chunked_body(reader, 100)
