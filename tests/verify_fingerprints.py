from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from spideroxide._native import _canonicalize_url
from w3lib.url import canonicalize_url

from spideroxide import (
    DupeFilter,
    Headers,
    Request,
    Scheduler,
    fingerprint_request,
    fingerprint_requests,
)

CANONICAL_URLS = {
    "http://www.example.com": "http://www.example.com/",
    "HTTP://Example.COM:80": "http://example.com:80/",
    "http://www.example.com:/do?a=1": "http://www.example.com/do?a=1",
    "http://www.example.com/do?c=3&b=5&b=2&a=50": ("http://www.example.com/do?a=50&b=2&b=5&c=3"),
    "http://www.example.com/do?b=&c&a=2": "http://www.example.com/do?a=2&b=&c=",
    "http://www.example.com/do?1750,4": "http://www.example.com/do?1750%2C4=",
    "http://www.example.com/do?q=a%20space&a=1": ("http://www.example.com/do?a=1&q=a+space"),
    "http://www.example.com/résumé?q=résumé": (
        "http://www.example.com/r%C3%A9sum%C3%A9?q=r%C3%A9sum%C3%A9"
    ),
    "http://www.example.com/a%a3do?q=r%e9sum%e9": ("http://www.example.com/a%A3do?q=r%E9sum%E9"),
    "https://example.com/a%23b%2cc#bash": "https://example.com/a%23b,c",
    "http://www.example.com/a %20do?a=1": "http://www.example.com/a%20%20do?a=1",
    "http://www.bücher.de?q=bücher": "http://www.xn--bcher-kva.de/?q=b%C3%BCcher",
    "http://faß.de/path": "http://fass.de/path",
    "http://ς.gr/path": "http://xn--4xa.gr/path",
    "http://example.com:00080/path": "http://example.com:00080/path",
    "http://example.com:opaque/path": "http://example.com:opaque/path",
    "http://USER:P%40SS@example.com/path": "http://USER:P%40SS@example.com/path",
    "http://user:p ass@example.com/path": "http://user:p ass@example.com/path",
    "http://foo@bar@example.com/path": "http://foo@bar@example.com/path",
    "http://はじめよう.みんな/?query=サ&maxResults=5": (
        "http://xn--p8j9a0d9c9a.xn--q9jyb4c/?maxResults=5&query=%E3%82%B5"
    ),
    "http://foo.com/AC%2FDC+rocks%3f/?yeah=1": ("http://foo.com/AC%2FDC+rocks%3F/?yeah=1"),
    " https://example.com ": "https://example.com/",
    "data:text/plain,a b?z=2&a=1#f": "data:text/plain,a%20b?a=1&z=2",
    "file:///tmp/a b?z=2&a=1#f": "file:///tmp/a%20b?a=1&z=2",
}


def _verify_canonicalization() -> None:
    for url, expected in CANONICAL_URLS.items():
        assert canonicalize_url(url) == expected
        assert _canonicalize_url(url) == expected
        expected_with_fragment = canonicalize_url(url, keep_fragments=True)
        assert _canonicalize_url(url, True) == expected_with_fragment


def _fingerprint_cases() -> list[tuple[Request, list[str] | None, bool, str]]:
    return [
        (
            Request("http://Example.com:80/a?b=2&a=1#frag"),
            None,
            False,
            "f5676cfd272c58f8cac12be0705d2ee8fd659e1f",
        ),
        (
            Request("http://Example.com:80/a?b=2&a=1#frag"),
            None,
            True,
            "1dcd57bfcd5d0a09c7e95e2126e7c8d16d0c3545",
        ),
        (
            Request(
                "http://example.com/a%2fb?q=b%a3",
                method="post",
                body=b"\x00\xff",
            ),
            None,
            False,
            "1306d635576d1f6783dc729acf51b1b8ac359663",
        ),
        (
            Request(
                "https://example.com/path",
                headers=Headers(
                    [
                        (b"X-A", b"1"),
                        (b"X-A", b"2"),
                        (b"X-Z", b"last"),
                    ]
                ),
            ),
            ["x-z", "X-A"],
            False,
            "995e1b56e8988ba3e2bad8fe6f49f4caaf4bbfee",
        ),
        (
            Request("https://example.com/path", headers={"X-A": "1"}),
            ["missing"],
            False,
            "4924be7d6554dc9acfd69298c9d4b32f6bc4ffdf",
        ),
        (
            Request(
                "https://example.com/page",
                headers=Headers(
                    [
                        (b"X-Test", b"one"),
                        (b"X-Test", b"two"),
                    ]
                ),
            ),
            ["X-Test", "x-test"],
            False,
            "d3cef5be5bb053d554514504755ad12c72c52292",
        ),
        (
            Request("https://example.com/", method=" get "),
            None,
            False,
            "eb37c054d6096f9c3824d78f056564736ecc7062",
        ),
        (
            Request(
                "https://example.com/path?b=2&a=1#frag",
                meta={"verbatim_url": True},
            ),
            None,
            False,
            "9fd138d3683e58c3a35e97bae1ef218d313f19f0",
        ),
    ]


def _verify_scrapy_vectors() -> None:
    for request, include_headers, keep_fragments, expected in _fingerprint_cases():
        values = {
            fingerprint_request(
                request,
                include_headers=include_headers,
                keep_fragments=keep_fragments,
                backend=backend,
            ).hex()
            for backend in ("python", "rust")
        }
        assert values == {expected}
        assert len(bytes.fromhex(expected)) == 20

    try:
        from scrapy.http import Request as ScrapyRequest
        from scrapy.utils.request import fingerprint as scrapy_fingerprint
    except ImportError:
        return

    for request, include_headers, keep_fragments, _ in _fingerprint_cases():
        scrapy_request = ScrapyRequest(
            request.url,
            method=request.method,
            body=request.body,
            headers=request.headers.to_scrapy_dict(),
            meta=request.meta,
        )
        expected = scrapy_fingerprint(
            scrapy_request,
            include_headers=include_headers,
            keep_fragments=keep_fragments,
        )
        assert (
            fingerprint_request(
                request,
                include_headers=include_headers,
                keep_fragments=keep_fragments,
                backend="rust",
            )
            == expected
        )


def _verify_duplicate_contract() -> None:
    equivalent = [
        Request("https://example.com/path?b=2&a=1#first"),
        Request("https://EXAMPLE.com/path?a=1&b=2#second"),
    ]
    distinct_default_port = Request("https://example.com:443/path?a=1&b=2")
    verbatim = [
        Request("https://example.com/path?b=2&a=1", meta={"verbatim_url": True}),
        Request("https://example.com/path?a=1&b=2", meta={"verbatim_url": True}),
    ]

    for backend in ("python", "rust"):
        duplicate_filter = DupeFilter(backend)
        assert [duplicate_filter.seen_request(request) for request in equivalent] == [False, True]
        assert duplicate_filter.seen_request(distinct_default_port) is False

        verbatim_filter = DupeFilter(backend)
        assert [verbatim_filter.seen_request(request) for request in verbatim] == [False, False]

        scheduler = Scheduler(backend)
        assert [scheduler.push_request(request) for request in verbatim] == [True, True]


def _verify_batch_header_iterable() -> None:
    requests = [
        Request("https://example.com/one", headers={"X-Test": "one"}),
        Request("https://example.com/two", headers={"X-Test": "two"}),
    ]
    for backend in ("python", "rust"):
        expected = [
            fingerprint_request(
                request,
                include_headers=["X-Test"],
                backend=backend,
            )
            for request in requests
        ]
        actual = fingerprint_requests(
            requests,
            include_headers=(name for name in ["X-Test"]),
            backend=backend,
        )
        assert actual == expected


def main() -> None:
    _verify_canonicalization()
    _verify_scrapy_vectors()
    _verify_duplicate_contract()
    _verify_batch_header_iterable()
    print(
        "Fingerprints passed: Scrapy SHA-1 framing, headers, fragments, verbatim URLs, "
        "canonicalization, duplicates, and Python/Rust parity"
    )


if __name__ == "__main__":
    main()
