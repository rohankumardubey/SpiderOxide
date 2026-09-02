from __future__ import annotations

import hashlib
from collections.abc import Iterable, Sequence
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

RequestData = tuple[str, str, bytes, int]


def canonicalize_url(url: str) -> str:
    """Canonicalize a URL using the benchmark's deliberately small specification."""
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    hostname = parts.hostname
    if not scheme or hostname is None:
        raise ValueError(f"URL must include a scheme and hostname: {url!r}")

    host = hostname.encode("idna").decode("ascii").lower()
    if ":" in host:
        host = f"[{host}]"

    try:
        port = parts.port
    except ValueError as exc:
        raise ValueError(f"invalid port in URL: {url!r}") from exc

    if port is not None and not (
        (scheme == "http" and port == 80) or (scheme == "https" and port == 443)
    ):
        host = f"{host}:{port}"

    userinfo = ""
    if parts.username is not None:
        userinfo = parts.username
        if parts.password is not None:
            userinfo += f":{parts.password}"
        userinfo += "@"

    query_pairs = parse_qsl(parts.query, keep_blank_values=True)
    query_pairs.sort()
    query = urlencode(query_pairs)
    path = quote(parts.path or "/", safe="/:@-._~!$&'()*+,;=%")
    return urlunsplit((scheme, userinfo + host, path, query, ""))


def fingerprint(
    url: str,
    method: str = "GET",
    body: bytes = b"",
) -> bytes:
    normalized_method = method.strip().upper().encode("utf-8")
    canonical_url = canonicalize_url(url).encode("utf-8")
    digest = hashlib.sha256()
    digest.update(normalized_method)
    digest.update(b"\0")
    digest.update(canonical_url)
    digest.update(b"\0")
    digest.update(body)
    return digest.digest()


def fingerprint_batch(requests: Iterable[Sequence[object]]) -> list[bytes]:
    return [
        fingerprint(str(request[0]), str(request[1]), bytes(request[2])) for request in requests
    ]
