from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from spideroxide import (
    BACKEND_ENV_VAR,
    BackendUnavailableError,
    DupeFilter,
    ScheduledRequest,
    Scheduler,
    fingerprint_batch,
    fingerprint_request,
    fingerprint_requests,
    resolve_backend,
)


@dataclass(frozen=True, slots=True)
class Request:
    url: str
    method: str = "GET"
    body: bytes = b""
    priority: int = 0


def _verify_selection() -> None:
    with patch.dict(os.environ, {}, clear=True):
        assert resolve_backend().name == "python"
    with patch.dict(os.environ, {BACKEND_ENV_VAR: "rust"}):
        assert resolve_backend().name == "rust"
    assert resolve_backend("python").name == "python"
    assert resolve_backend("rust").name == "rust"

    with patch(
        "spideroxide.backend.import_module",
        side_effect=ModuleNotFoundError("native extension unavailable"),
    ):
        assert resolve_backend("auto").name == "python"
        try:
            resolve_backend("rust")
        except BackendUnavailableError:
            pass
        else:
            raise AssertionError("explicit Rust selection must not silently fall back")

    try:
        resolve_backend("unknown")
    except ValueError:
        pass
    else:
        raise AssertionError("invalid backend selection must fail")


def _request_fields(request: ScheduledRequest) -> tuple[str, str, bytes, int]:
    return (
        str(request.url),
        str(request.method),
        bytes(request.body),
        int(request.priority),
    )


def _verify_facade() -> None:
    requests = [
        Request("https://example.com/first?b=2&a=1", priority=5),
        Request("https://example.com/high", priority=10),
        Request("https://example.com/first?a=1&b=2", priority=99),
        Request("https://example.com/second", method="POST", body=b"value", priority=5),
    ]
    assert fingerprint_requests(requests, backend="python") == fingerprint_requests(
        requests, backend="rust"
    )
    assert fingerprint_request(requests[0], backend="python") == fingerprint_request(
        requests[0], backend="rust"
    )
    triples = [(request.url, request.method, request.body) for request in requests]
    assert fingerprint_batch(triples, backend="python") == fingerprint_batch(
        triples,
        backend="rust",
    )
    for backend in ("python", "rust"):
        assert DupeFilter(backend).seen_batch(triples) == [False, False, True, False]

    outputs: dict[str, list[tuple[str, str, bytes, int]]] = {}
    for backend in ("python", "rust"):
        duplicate_filter = DupeFilter(backend)
        assert duplicate_filter.backend_name == backend
        assert duplicate_filter.seen_request(requests[0]) is False
        assert duplicate_filter.seen_requests(requests[1:]) == [False, True, False]
        assert len(duplicate_filter) == 3

        scheduler = Scheduler(backend)
        assert scheduler.backend_name == backend
        assert scheduler.push_request(requests[0]) is True
        assert scheduler.push_requests(requests[1:]) == [True, False, True]
        assert len(scheduler) == 3
        popped = scheduler.pop_batch(10)
        outputs[backend] = [_request_fields(request) for request in popped]
        assert popped == [requests[1], requests[0], requests[3]]
        assert popped[0] is requests[1]
        assert scheduler.pop() is None
        try:
            scheduler.pop_batch(-1)
        except ValueError:
            pass
        else:
            raise AssertionError("negative batch sizes must fail consistently")
    assert outputs["python"] == outputs["rust"]
    assert [request[0] for request in outputs["python"]] == [
        "https://example.com/high",
        "https://example.com/first?b=2&a=1",
        "https://example.com/second",
    ]


def run_integration_checks() -> dict[str, object]:
    _verify_selection()
    _verify_facade()
    return {"passed": True, "backends": ["python", "rust"], "default": "python"}


if __name__ == "__main__":
    result = run_integration_checks()
    print(
        f"Integration checks passed: {', '.join(result['backends'])} backends; "
        f"default={result['default']}"
    )
