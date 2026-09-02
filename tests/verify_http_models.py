from __future__ import annotations

import asyncio
import sys
import warnings
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from spideroxide import (
    FormRequest,
    Headers,
    HtmlResponse,
    HttpxDownloader,
    JsonRequest,
    Request,
    Response,
    TextResponse,
    XmlResponse,
)
from spideroxide.downloader import _response


def _callback(response: Response) -> None:
    return None


def _verify_headers() -> None:
    headers = Headers(
        [
            (b"X-Test", "one"),
            ("X-Test", b"two"),
            ("Content-Length", 12),
        ],
        encoding="utf-8",
    )
    assert headers.getlist("x-test") == [b"one", b"two"]
    assert headers[b"CONTENT-LENGTH"] == b"12"
    assert headers.to_scrapy_dict() == {
        b"X-Test": [b"one", b"two"],
        b"Content-Length": [b"12"],
    }
    copied = headers.copy()
    copied.appendlist("X-Test", "three")
    assert headers.getlist("X-Test") == [b"one", b"two"]
    assert copied.getlist("X-Test") == [b"one", b"two", b"three"]
    copied["X-Assigned"] = ["one", "two"]
    copied.appendlist("X-Assigned", ["three", "four"])
    assert copied.getlist("X-Assigned") == [b"one", b"two", b"three", b"four"]
    copied.appendlist("X-Empty", [])
    assert "X-Empty" not in copied


def _verify_request() -> None:
    request = Request(
        "https://example.test/café?q=a b",
        callback=_callback,
        method="post",
        headers=[("X-Test", "one"), ("X-Test", "two")],
        body="café",
        encoding="latin-1",
        cookies={"session": "one"},
        meta={"depth": 2},
        priority=-10,
        flags=("seed",),
        cb_kwargs={"name": "value"},
    )
    assert request.url == "https://example.test/caf%C3%A9?q=a%20b"
    assert request.method == "POST"
    assert request.body == b"caf\xe9"
    assert request.headers.getlist("X-Test") == [b"one", b"two"]
    assert request.copy() is not request
    replaced = request.replace(url="https://example.test/next", priority=20)
    assert replaced.url == "https://example.test/next"
    assert replaced.priority == 20
    assert replaced.meta == request.meta
    assert replaced.meta is not request.meta
    assert request.follow("../other").url == "https://example.test/other"

    serialized = Request(
        "https://example.test",
        headers={"X-Test": ["one", "two"]},
    ).to_dict()
    assert serialized["headers"] == {b"X-Test": [b"one", b"two"]}
    assert serialized["body"] == b""
    assert serialized["method"] == "GET"
    try:
        request.to_dict()
    except ValueError:
        pass
    else:
        raise AssertionError("request callback was serialized without a spider")

    curl = Request.from_curl(
        "curl 'https://example.test/api' -X POST "
        "-H 'X-Test: one' -H 'X-Test: two' "
        "-b 'first=1; second=2' -d 'name=value'"
    )
    assert curl.method == "POST"
    assert curl.headers.getlist("X-Test") == [b"one", b"two"]
    assert curl.cookies == {"first": "1", "second": "2"}
    assert curl.body == b"name=value"
    authenticated = Request.from_curl("curl -u 'user:secret' https://example.test/private")
    assert authenticated.headers["Authorization"] == b"Basic dXNlcjpzZWNyZXQ="
    compact = Request.from_curl(
        "curl --user=user:secret --data=name=value "
        "--header='X-Test: compact' https://example.test/private"
    )
    assert compact.headers["Authorization"] == b"Basic dXNlcjpzZWNyZXQ="
    assert compact.headers["X-Test"] == b"compact"
    assert compact.body == b"name=value"
    assert Request.from_curl("curl example.test/path").url == "http://example.test/path"

    for field, value in (("callback", "parse"), ("errback", "failed")):
        try:
            Request("https://example.test", **{field: value})
        except TypeError:
            pass
        else:
            raise AssertionError(f"non-callable {field} was accepted")
    try:
        Request("https://example.test", priority=1.5)  # type: ignore[arg-type]
    except TypeError:
        pass
    else:
        raise AssertionError("non-integer priority was accepted")
    try:
        Request.from_curl(
            "curl --unsupported https://example.test",
            ignore_unknown_options=False,
        )
    except ValueError:
        pass
    else:
        raise AssertionError("unknown curl option was accepted")


def _verify_specialized_requests() -> None:
    form = FormRequest(
        "https://example.test/submit",
        formdata={"tag": ["one", "two"], "query": "café"},
    )
    assert form.method == "POST"
    assert form.body == b"tag=one&tag=two&query=caf%C3%A9"
    assert form.headers["Content-Type"] == b"application/x-www-form-urlencoded"

    query = FormRequest(
        "https://example.test/search?old=value",
        method="GET",
        formdata={"q": "spider oxide"},
    )
    assert query.url == "https://example.test/search?q=spider+oxide"
    assert query.body == b""
    assert (
        FormRequest(
            "https://example.test/bytes",
            formdata={b"name": b"value"},
        ).body
        == b"name=value"
    )
    generated = (value for value in ("one", "two"))
    assert (
        FormRequest(
            "https://example.test/generated",
            formdata={"tag": generated},
        ).body
        == b"tag=one&tag=two"
    )

    json_request = JsonRequest(
        "https://example.test/api",
        data={"second": 2, "first": "café"},
    )
    assert json_request.method == "POST"
    assert json_request.body == b'{"first": "caf\\u00e9", "second": 2}'
    assert json_request.headers["Content-Type"] == b"application/json"
    assert json_request.headers["Accept"].startswith(b"application/json")
    assert json_request.replace(data={"updated": True}).body == b'{"updated": true}'
    assert json_request.to_dict()["_class"] == "spideroxide.http.JsonRequest"


def _verify_responses() -> None:
    request = Request(
        "https://example.test/root/page",
        meta={"source": "test"},
        cb_kwargs={"identifier": 7},
    )
    response = Response(
        "https://example.test/café?q=a b",
        status=201,
        headers={"X-Test": "one"},
        body=b"payload",
        request=request,
        flags=("cached",),
        protocol="HTTP/2",
    )
    assert response.meta == {"source": "test"}
    assert response.cb_kwargs == {"identifier": 7}
    assert response.copy() is not response
    assert response.replace(status=202).status == 202
    assert response.follow("../next").url == "https://example.test/next"
    assert response.follow("../next").priority == 0
    assert [item.url for item in response.follow_all(["one", "two"])] == [
        "https://example.test/one",
        "https://example.test/two",
    ]
    assert Response("https://example.test/café?q=a b").url == ("https://example.test/café?q=a b")
    positional = Response(
        "https://example.test",
        200,
        {},
        b"",
        request,
        (),
        "HTTP/2",
    )
    assert positional.protocol == "HTTP/2"
    assert positional.certificate is None
    try:
        _ = Response("https://example.test").text
    except AttributeError:
        pass
    else:
        raise AssertionError("binary Response exposed text")
    try:
        _ = Response("https://example.test").meta
    except AttributeError:
        pass
    else:
        raise AssertionError("detached Response exposed request metadata")


def _verify_text_responses() -> None:
    html = HtmlResponse(
        "https://example.test/root/page",
        headers={"Content-Type": "text/html; charset=utf-8"},
        body=(
            '<base href="https://cdn.example.test/assets/">'
            '<a href="one">One</a><a>No href</a><a href="two">Two</a>'
        ),
        encoding="utf-8",
    )
    assert repr(html) == "<200 https://example.test/root/page>"
    assert html.selector.type == "html"
    assert html.urljoin("image.png") == "https://cdn.example.test/assets/image.png"
    assert [request.url for request in html.follow_all(css="a")] == [
        "https://cdn.example.test/assets/one",
        "https://cdn.example.test/assets/two",
    ]
    assert [request.url for request in html.follow_all(xpath="//a/@href")] == [
        "https://cdn.example.test/assets/one",
        "https://cdn.example.test/assets/two",
    ]

    xml = XmlResponse(
        "https://example.test/feed",
        body=b'<?xml version="1.0"?><items><item>one</item></items>',
    )
    assert xml.selector.type == "xml"
    assert xml.xpath("//item/text()").get() == "one"

    payload = TextResponse(
        "https://example.test/api",
        headers={"Content-Type": "application/json"},
        body=b'{"ok": true}',
    )
    assert payload.json() is payload.json()
    assert payload.jmespath("ok").get() is True
    assert payload.body_as_unicode() == '{"ok": true}'


def _verify_form_response() -> None:
    html = HtmlResponse(
        "https://example.test/page",
        body=b"""
            <base href="https://base.example.test/forms/">
            <form id="login" action="submit" method="post">
              <input name="user" value="old">
              <input name="remove" value="yes">
              <input type="checkbox" name="remember" checked>
              <select name="role">
                <option value="user">User</option>
                <option value="admin" selected>Admin</option>
              </select>
              <select name="unused" multiple>
                <option value="one">One</option>
              </select>
              <textarea name="note">hello</textarea>
              <button type="button" name="go" value="preview">Preview</button>
              <button name="go" value="yes">Go</button>
            </form>
        """,
    )
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        request = FormRequest.from_response(
            html,
            formid="login",
            formdata={"user": "new", "remove": None},
        )
    assert request.url == "https://base.example.test/forms/submit"
    assert request.method == "POST"
    assert request.body == b"remember=on&role=admin&note=hello&go=yes&user=new"


def _verify_response_factory() -> None:
    request = Request("https://example.test")
    cases = [
        ("text/html", HtmlResponse),
        ("application/xhtml+xml", HtmlResponse),
        ("application/xml", XmlResponse),
        ("application/rss+xml", XmlResponse),
        ("application/json", TextResponse),
        ("application/x-json", TextResponse),
        ("application/json-seq", TextResponse),
        ("application/octet-stream", Response),
    ]
    for content_type, expected in cases:
        response = _response(
            request,
            url=request.url,
            status=200,
            header_pairs=[("Content-Type", content_type)],
            body=b"<content/>",
            protocol="HTTP/1.1",
        )
        assert type(response) is expected

    inferred = [
        ("https://example.test/page.html", b"", HtmlResponse),
        ("https://example.test/feed.xml", b"", XmlResponse),
        ("https://example.test/content", b"<html></html>", HtmlResponse),
        ("https://example.test/content", b'<?xml version="1.0"?><x/>', XmlResponse),
        ("https://example.test/content", b"plain text", TextResponse),
        ("https://example.test/content", b"\x00\x01\xff", Response),
        ("https://example.test/feed.xml.gz", b"\x1f\x8b\x08\x00", Response),
        (
            "https://example.test/content",
            '<?xml version="1.0"?><x/>'.encode("utf-16"),
            XmlResponse,
        ),
    ]
    for url, body, expected in inferred:
        response = _response(
            request,
            url=url,
            status=200,
            header_pairs=[],
            body=body,
            protocol="HTTP/1.1",
        )
        assert type(response) is expected


async def _verify_async_contract() -> None:
    requests = Response("https://example.test").follow_all(["one", "two"])
    assert [request.url for request in requests] == [
        "https://example.test/one",
        "https://example.test/two",
    ]

    async def handler(request: httpx.Request) -> httpx.Response:
        assert (b"X-Unicode", "☃".encode()) in request.headers.raw
        return httpx.Response(200, content=b"ok")

    downloader = HttpxDownloader(transport=httpx.MockTransport(handler))
    response = await downloader.fetch(Request("https://example.test", headers={"X-Unicode": "☃"}))
    assert isinstance(response, TextResponse)
    assert response.text == "ok"
    await downloader.close()


def _verify() -> None:
    _verify_headers()
    _verify_request()
    _verify_specialized_requests()
    _verify_responses()
    _verify_text_responses()
    _verify_form_response()
    _verify_response_factory()
    asyncio.run(_verify_async_contract())


if __name__ == "__main__":
    _verify()
    print(
        "HTTP models passed: headers, requests, curl, forms, JSON, response subclasses, "
        "selectors, following, serialization, and downloader typing"
    )
