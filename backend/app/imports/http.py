from __future__ import annotations

import asyncio
import ipaddress
import socket
import ssl
import urllib.parse
from collections.abc import Awaitable, Callable, Collection, Mapping, Sequence
from dataclasses import dataclass

from app.imports.urls import validate_https_url

_REDIRECT_STATUSES = {301, 302, 303, 307, 308}
_MAX_HEADER_BYTES = 64 * 1024


class SafeHttpError(RuntimeError):
    pass


class UnsafeRemoteAddress(SafeHttpError):
    pass


class TooManyRedirects(SafeHttpError):
    pass


class ResponseTooLarge(SafeHttpError):
    pass


@dataclass(frozen=True)
class SafeHttpResponse:
    status_code: int
    headers: Mapping[str, str]
    body: bytes
    url: str


Resolver = Callable[[str, int], Awaitable[Sequence[str]]]
Requester = Callable[
    [str, str],
    Awaitable[SafeHttpResponse],
]


class SafeHttpFetcher:
    def __init__(
        self,
        *,
        allowed_hosts: Collection[str],
        max_redirects: int,
        max_response_bytes: int,
        timeout_s: float,
        resolver: Resolver | None = None,
        requester: Callable[..., Awaitable[SafeHttpResponse]] | None = None,
    ) -> None:
        self._allowed_hosts = {host.strip().lower().rstrip(".") for host in allowed_hosts}
        self._max_redirects = max(0, max_redirects)
        self._max_response_bytes = max_response_bytes
        self._timeout_s = timeout_s
        self._resolver = resolver or _resolve_addresses
        self._requester = requester or _https_get_pinned

    async def fetch(self, url: str) -> SafeHttpResponse:
        current = url
        redirects = 0
        while True:
            parsed, host = validate_https_url(
                current,
                allowed_hosts=self._allowed_hosts,
            )
            addresses = list(await self._resolver(host, parsed.port or 443))
            address = _validated_address(addresses, host)
            response = await self._requester(
                current,
                address,
                max_response_bytes=self._max_response_bytes,
                timeout_s=self._timeout_s,
            )
            if len(response.body) > self._max_response_bytes:
                raise ResponseTooLarge(
                    f"response exceeds the {self._max_response_bytes} byte limit"
                )
            if response.status_code not in _REDIRECT_STATUSES:
                return response
            location = response.headers.get("location") or response.headers.get("Location")
            if not location:
                raise SafeHttpError("redirect response did not include a Location header")
            if redirects >= self._max_redirects:
                raise TooManyRedirects(
                    f"playlist URL exceeded the {self._max_redirects} redirect limit"
                )
            redirects += 1
            current = urllib.parse.urljoin(response.url, location)


async def _resolve_addresses(host: str, port: int) -> Sequence[str]:
    try:
        rows = await asyncio.to_thread(
            socket.getaddrinfo,
            host,
            port,
            socket.AF_UNSPEC,
            socket.SOCK_STREAM,
        )
    except socket.gaierror as exc:
        raise SafeHttpError(f"could not resolve playlist host '{host}'") from exc
    return sorted({str(row[4][0]) for row in rows})


def _validated_address(addresses: Sequence[str], host: str) -> str:
    if not addresses:
        raise SafeHttpError(f"playlist host '{host}' did not resolve to an address")
    parsed: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    for value in addresses:
        try:
            address = ipaddress.ip_address(value)
        except ValueError as exc:
            raise SafeHttpError(f"playlist host '{host}' resolved to an invalid address") from exc
        if not address.is_global:
            raise UnsafeRemoteAddress(
                f"playlist host '{host}' resolved to a private or local network address"
            )
        parsed.append(address)
    return str(sorted(parsed, key=lambda item: (item.version, int(item)))[0])


async def _https_get_pinned(
    url: str,
    address: str,
    *,
    max_response_bytes: int,
    timeout_s: float,
) -> SafeHttpResponse:
    parsed = urllib.parse.urlsplit(url)
    host = parsed.hostname or ""
    target = parsed.path or "/"
    if parsed.query:
        target += "?" + parsed.query
    context = ssl.create_default_context()
    writer: asyncio.StreamWriter | None = None
    try:
        async with asyncio.timeout(timeout_s):
            reader, writer = await asyncio.open_connection(
                host=address,
                port=443,
                ssl=context,
                server_hostname=host,
                limit=_MAX_HEADER_BYTES,
            )
            request = (
                f"GET {target} HTTP/1.1\r\n"
                f"Host: {host}\r\n"
                "Accept: application/json\r\n"
                "Accept-Encoding: identity\r\n"
                "Connection: close\r\n"
                "User-Agent: Open-Playlist-Engine/1\r\n"
                "\r\n"
            ).encode("ascii")
            writer.write(request)
            await writer.drain()
            status_code, headers = await _read_response_head(reader)
            body = await _read_body(reader, headers, max_response_bytes)
            return SafeHttpResponse(
                status_code=status_code,
                headers=headers,
                body=body,
                url=url,
            )
    except TimeoutError as exc:
        raise SafeHttpError("playlist URL request timed out") from exc
    except (OSError, ssl.SSLError, asyncio.IncompleteReadError) as exc:
        raise SafeHttpError(f"playlist URL request failed: {exc}") from exc
    finally:
        if writer is not None:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass


async def _read_response_head(
    reader: asyncio.StreamReader,
) -> tuple[int, dict[str, str]]:
    try:
        raw = await reader.readuntil(b"\r\n\r\n")
    except asyncio.LimitOverrunError as exc:
        raise SafeHttpError("playlist response headers are too large") from exc
    if len(raw) > _MAX_HEADER_BYTES:
        raise SafeHttpError("playlist response headers are too large")
    lines = raw.decode("iso-8859-1").split("\r\n")
    status_parts = lines[0].split(" ", 2)
    if len(status_parts) < 2 or not status_parts[1].isdigit():
        raise SafeHttpError("playlist response had an invalid HTTP status")
    headers: dict[str, str] = {}
    for line in lines[1:]:
        if not line:
            continue
        if ":" not in line:
            raise SafeHttpError("playlist response had a malformed header")
        name, value = line.split(":", 1)
        normalized = name.strip().lower()
        if not normalized:
            raise SafeHttpError("playlist response had a malformed header")
        headers[normalized] = value.strip()
    encoding = headers.get("content-encoding", "").lower()
    if encoding not in {"", "identity"}:
        raise SafeHttpError("compressed playlist responses are not accepted")
    return int(status_parts[1]), headers


async def _read_body(
    reader: asyncio.StreamReader,
    headers: Mapping[str, str],
    max_response_bytes: int,
) -> bytes:
    transfer_encoding = headers.get("transfer-encoding", "").lower()
    if "chunked" in transfer_encoding:
        return await _read_chunked_body(reader, max_response_bytes)
    content_length = headers.get("content-length")
    if content_length is not None:
        try:
            length = int(content_length)
        except ValueError as exc:
            raise SafeHttpError("playlist response has an invalid Content-Length") from exc
        if length < 0:
            raise SafeHttpError("playlist response has an invalid Content-Length")
        if length > max_response_bytes:
            raise ResponseTooLarge(
                f"response exceeds the {max_response_bytes} byte limit"
            )
        return await reader.readexactly(length)
    chunks: list[bytes] = []
    size = 0
    while True:
        chunk = await reader.read(min(64 * 1024, max_response_bytes - size + 1))
        if not chunk:
            return b"".join(chunks)
        size += len(chunk)
        if size > max_response_bytes:
            raise ResponseTooLarge(
                f"response exceeds the {max_response_bytes} byte limit"
            )
        chunks.append(chunk)


async def _read_chunked_body(
    reader: asyncio.StreamReader,
    max_response_bytes: int,
) -> bytes:
    chunks: list[bytes] = []
    size = 0
    while True:
        line = await _read_limited_line(reader)
        try:
            chunk_size = int(line.split(b";", 1)[0].strip(), 16)
        except ValueError as exc:
            raise SafeHttpError("playlist response has invalid chunk framing") from exc
        if chunk_size == 0:
            trailer_size = 0
            while True:
                trailer = await _read_limited_line(reader)
                trailer_size += len(trailer)
                if trailer_size > _MAX_HEADER_BYTES:
                    raise SafeHttpError("playlist response trailers are too large")
                if trailer in {b"\r\n", b"\n", b""}:
                    break
            return b"".join(chunks)
        size += chunk_size
        if size > max_response_bytes:
            raise ResponseTooLarge(
                f"response exceeds the {max_response_bytes} byte limit"
            )
        chunks.append(await reader.readexactly(chunk_size))
        if await reader.readexactly(2) != b"\r\n":
            raise SafeHttpError("playlist response has invalid chunk framing")


async def _read_limited_line(reader: asyncio.StreamReader) -> bytes:
    try:
        line = await reader.readline()
    except (ValueError, asyncio.LimitOverrunError) as exc:
        raise SafeHttpError("playlist response chunk framing is too large") from exc
    if len(line) > _MAX_HEADER_BYTES:
        raise SafeHttpError("playlist response chunk framing is too large")
    return line
