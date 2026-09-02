from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from spideroxide import Headers, Request, Response, Settings  # noqa: E402
from spideroxide.exceptions import IgnoreRequest, NotConfigured  # noqa: E402
from spideroxide.redirect import RedirectMiddleware  # noqa: E402
from spideroxide.stats import StatsCollector  # noqa: E402


def _middleware(**settings: object) -> RedirectMiddleware:
    crawler = SimpleNamespace(settings=Settings(settings), stats=StatsCollector())
    return RedirectMiddleware.from_crawler(crawler)


def _verify_method_and_metadata() -> None:
    middleware = _middleware(REDIRECT_PRIORITY_ADJUST=3)
    request = Request(
        "https://example.test/source",
        method="POST",
        headers=Headers(
            {
                "Content-Type": "application/json",
                "Content-Length": "2",
                "Authorization": "Bearer token",
                "Cookie": "explicit=yes",
                "Referer": "https://example.test/",
            }
        ),
        cookies={"request": "yes"},
        body=b"{}",
        meta={"download_slot": "old", "download_latency": 2.0},
        priority=10,
    )
    response = Response(
        request.url,
        status=302,
        headers=Headers({"Location": "/target"}),
        request=request,
    )
    redirected = middleware.process_response(request, response, SimpleNamespace())
    assert isinstance(redirected, Request)
    assert redirected.url == "https://example.test/target"
    assert redirected.method == "GET"
    assert redirected.body == b""
    assert "Content-Type" not in redirected.headers
    assert redirected.headers["Authorization"] == b"Bearer token"
    assert redirected.headers["Cookie"] == b"explicit=yes"
    assert "Referer" not in redirected.headers
    assert redirected.cookies == {}
    assert redirected.priority == 13
    assert redirected.meta["redirect_times"] == 1
    assert redirected.meta["redirect_ttl"] == 19
    assert redirected.meta["redirect_urls"] == [request.url]
    assert redirected.meta["redirect_reasons"] == [302]
    assert "download_slot" not in redirected.meta
    assert "download_latency" not in redirected.meta
    assert middleware.stats.get_value("redirect/count") == 1

    preserved = middleware.process_response(
        request,
        Response(
            request.url,
            status=307,
            headers=Headers({"Location": "/preserved"}),
            request=request,
        ),
        SimpleNamespace(),
    )
    assert isinstance(preserved, Request)
    assert preserved.method == "POST"
    assert preserved.body == b"{}"


def _verify_security_and_limits() -> None:
    middleware = _middleware(REDIRECT_MAX_TIMES=1)
    request = Request(
        "https://example.test/source",
        headers=Headers(
            {
                "Authorization": "Bearer token",
                "Cookie": "explicit=yes",
            }
        ),
        cookies={"request": "yes"},
    )
    response = Response(
        request.url,
        status=301,
        headers=Headers({"Location": "http://other.test/target"}),
        request=request,
    )
    redirected = middleware.process_response(request, response, SimpleNamespace())
    assert isinstance(redirected, Request)
    assert "Authorization" not in redirected.headers
    assert "Cookie" not in redirected.headers
    assert redirected.cookies == {}

    exhausted = redirected.replace(
        meta={**redirected.meta, "redirect_ttl": 0},
    )
    try:
        middleware.process_response(
            exhausted,
            Response(
                exhausted.url,
                status=301,
                headers=Headers({"Location": "/again"}),
                request=exhausted,
            ),
            SimpleNamespace(),
        )
    except IgnoreRequest as error:
        assert "max redirections" in str(error)
    else:
        raise AssertionError("redirect limit was not enforced")


def _verify_bypasses() -> None:
    middleware = _middleware()
    request = Request(
        "https://example.test/source",
        meta={"dont_redirect": True},
    )
    response = Response(
        request.url,
        status=302,
        headers=Headers({"Location": "/target"}),
        request=request,
    )
    assert middleware.process_response(request, response, SimpleNamespace()) is response

    unsupported = Response(
        request.url,
        status=302,
        headers=Headers({"Location": "mailto:test@example.test"}),
        request=request.replace(meta={}),
    )
    assert (
        middleware.process_response(
            unsupported.request,
            unsupported,
            SimpleNamespace(),
        )
        is unsupported
    )

    try:
        RedirectMiddleware(Settings({"REDIRECT_ENABLED": False}))
    except NotConfigured:
        pass
    else:
        raise AssertionError("disabled redirect middleware was configured")


if __name__ == "__main__":
    _verify_method_and_metadata()
    _verify_security_and_limits()
    _verify_bypasses()
    print("Redirect middleware verification passed.")
