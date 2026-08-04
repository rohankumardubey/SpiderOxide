from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from benchmark.generate_data import DEFAULT_SEED, generate_requests, request_fields
from benchmark.verify_integration import run_integration_checks
from spideroxide import _rust as rust_impl
from spideroxide._python import PythonDupeFilter, PythonScheduler, fingerprint


def _verify_examples() -> None:
    equivalent_groups = [
        [
            ("http://Example.COM/path?b=2&a=1#fragment", "GET", b""),
            ("HTTP://example.com:80/path?a=1&b=2", "get", b""),
        ],
        [
            ("https://EXAMPLE.com?tag=2&tag=1", "GET", b""),
            ("https://example.com:443/?tag=1&tag=2", "GET", b""),
        ],
        [
            ("https://例え.テスト/商品?q=値", "POST", "本文".encode()),
            (
                "https://xn--r8jz45g.xn--zckzah/商品?q=%E5%80%A4#ignored",
                "post",
                "本文".encode(),
            ),
        ],
    ]
    for group in equivalent_groups:
        fingerprints = []
        for url, method, body in group:
            python_value = fingerprint(url, method, body)
            rust_value = rust_impl.fingerprint(url, method, body)
            assert python_value == rust_value, (url, python_value.hex(), rust_value.hex())
            fingerprints.append(python_value)
        assert len(set(fingerprints)) == 1, group

    changed_requests = [
        ("https://example.com/path", "GET", b""),
        ("https://example.com/path", "POST", b""),
        ("https://example.com/path", "POST", b"payload"),
    ]
    assert len({fingerprint(*request) for request in changed_requests}) == len(changed_requests)


def _verify_generated(size: int) -> None:
    requests = generate_requests(size, DEFAULT_SEED)
    python_fingerprints = [fingerprint(url, method, body) for url, method, body, _ in requests]
    rust_fingerprints = rust_impl.fingerprint_batch(requests)
    assert python_fingerprints == rust_fingerprints

    python_filter = PythonDupeFilter()
    rust_filter = rust_impl.RustDupeFilter()
    python_decisions = python_filter.seen_batch(requests)
    rust_decisions = rust_filter.seen_batch(requests)
    assert python_decisions == rust_decisions
    assert python_decisions.count(False) == round(size * 0.7)

    python_scheduler = PythonScheduler()
    rust_scheduler = rust_impl.RustScheduler()
    assert python_scheduler.push_batch(requests) == rust_scheduler.push_batch(requests)
    python_output = python_scheduler.pop_batch(size)
    rust_output = rust_scheduler.pop_batch(size)
    assert [request_fields(value) for value in python_output] == [
        request_fields(value) for value in rust_output
    ]
    assert python_scheduler.pop() is None
    assert rust_scheduler.pop() is None


def run_correctness(size: int = 10_000) -> dict[str, object]:
    _verify_examples()
    _verify_generated(size)
    run_integration_checks()

    fifo_requests = [
        ("https://example.com/first", "GET", b"", 5),
        ("https://example.com/high", "GET", b"", 10),
        ("https://example.com/second", "GET", b"", 5),
        ("https://example.com/low", "GET", b"", -1),
    ]
    for scheduler_type in (PythonScheduler, rust_impl.RustScheduler):
        scheduler = scheduler_type()
        assert scheduler.push_batch(fifo_requests) == [True, True, True, True]
        output = scheduler.pop_batch(10)
        assert [request_fields(value)[0] for value in output] == [
            "https://example.com/high",
            "https://example.com/first",
            "https://example.com/second",
            "https://example.com/low",
        ]
        assert scheduler.pop() is None
    return {"passed": True, "requests_checked": size, "seed": DEFAULT_SEED}


if __name__ == "__main__":
    result = run_correctness()
    print(
        f"Correctness passed: {result['requests_checked']:,} deterministic requests "
        "plus URL and scheduler edge cases"
    )
