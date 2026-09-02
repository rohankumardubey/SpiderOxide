from __future__ import annotations

import random
from collections.abc import Sequence
from typing import Protocol

RequestData = tuple[str, str, bytes, int]
DEFAULT_SEED = 20250308
DATASET_SIZES = (10_000, 100_000, 1_000_000)


class RequestLike(Protocol):
    url: str
    method: str
    body: bytes
    priority: int


def _reorder_query(url: str, rng: random.Random) -> str:
    without_fragment, fragment_separator, fragment = url.partition("#")
    prefix, separator, query = without_fragment.partition("?")
    if not separator:
        return url
    pairs = query.split("&")
    rng.shuffle(pairs)
    suffix = f"#{fragment}" if fragment_separator else ""
    return f"{prefix}?{'&'.join(pairs)}{suffix}"


def generate_requests(size: int, seed: int = DEFAULT_SEED) -> list[RequestData]:
    """Create a deterministic workload with 70% unique fingerprints and 30% duplicates."""
    if size <= 0:
        raise ValueError("size must be positive")

    rng = random.Random(seed + size)
    unique_count = max(1, round(size * 0.7))
    hosts = ("Example.COM", "api.example.org", "xn--r8jz45g.xn--zckzah")
    unique: list[RequestData] = []
    for index in range(unique_count):
        scheme = "HTTP" if index % 3 == 0 else "https"
        host = hosts[index % len(hosts)]
        default_port = ":80" if scheme.lower() == "http" and index % 4 == 0 else ""
        if scheme.lower() == "https" and index % 4 == 0:
            default_port = ":443"
        query = f"z={index % 17}&id={index}&tag={index % 5}&tag={index % 3}"
        if index % 2:
            query = "&".join(reversed(query.split("&")))
        path = "" if index % 19 == 0 else f"/items/{index % 1000}/detail"
        fragment = f"#section-{index % 7}" if index % 11 == 0 else ""
        method = "post" if index % 4 == 0 else "GET"
        body = f'{{"request":{index}}}'.encode() if method.lower() == "post" else b""
        unique.append(
            (
                f"{scheme}://{host}{default_port}{path}?{query}{fragment}",
                method,
                body,
                rng.randint(-100, 100),
            )
        )

    requests = list(unique)
    for _ in range(size - unique_count):
        url, method, body, _ = rng.choice(unique)
        requests.append((_reorder_query(url, rng), method.swapcase(), body, rng.randint(-100, 100)))
    rng.shuffle(requests)
    return requests


def request_fields(request: RequestLike) -> tuple[str, str, bytes, int]:
    return (
        str(request.url),
        str(request.method),
        bytes(request.body),
        int(request.priority),
    )


def request_tuple(request: Sequence[object]) -> RequestData:
    return (str(request[0]), str(request[1]), bytes(request[2]), int(request[3]))
