from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Sequence

from w3lib.url import canonicalize_url

RequestData = tuple[str, str, bytes, int]
FingerprintHeaders = Sequence[tuple[bytes, Sequence[bytes]]]


def fingerprint(
    url: str,
    method: str = "GET",
    body: bytes = b"",
    headers: FingerprintHeaders = (),
    keep_fragments: bool = False,
    verbatim_url: bool = False,
) -> bytes:
    normalized_headers = {
        name.hex(): [value.hex() for value in values] for name, values in sorted(headers) if values
    }
    fingerprint_data = {
        "method": method.upper(),
        "url": url if verbatim_url else canonicalize_url(url, keep_fragments=keep_fragments),
        "body": body.hex(),
        "headers": normalized_headers,
    }
    fingerprint_json = json.dumps(fingerprint_data, sort_keys=True)
    return hashlib.sha1(fingerprint_json.encode()).digest()  # noqa: S324


def fingerprint_batch(requests: Iterable[Sequence[object]]) -> list[bytes]:
    return [
        fingerprint(str(request[0]), str(request[1]), bytes(request[2])) for request in requests
    ]
