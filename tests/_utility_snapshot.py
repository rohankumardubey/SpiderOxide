from __future__ import annotations

import gc
import json
import logging
import os
import re
import sys
import warnings
from contextlib import redirect_stdout
from io import BytesIO, StringIO
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
if os.environ.get("SPIDEROXIDE_SHIM") == "1":
    sys.path.insert(0, str(ROOT / "compat" / "scrapy" / "src"))
    sys.path.insert(1, str(ROOT / "src"))
    os.environ["SPIDEROXIDE_SCRAPY_COMPAT_ALLOW_CONFLICT"] = "1"

from scrapy import Request, Spider
from scrapy.exceptions import CloseSpider, DropItem, StopDownload
from scrapy.http import Response
from scrapy.logformatter import LogFormatter
from scrapy.settings import Settings
from scrapy.utils.curl import curl_to_request_kwargs
from scrapy.utils.job import job_dir
from scrapy.utils.log import (
    LogCounterHandler,
    SpiderLoggerAdapter,
    StreamLogger,
    TopLevelFormatter,
    configure_logging,
    get_scrapy_root_handler,
    logformatter_adapter,
)
from scrapy.utils.serialize import ScrapyJSONEncoder
from scrapy.utils.trackref import (
    format_live_refs,
    get_oldest,
    iter_all,
    live_refs,
    object_ref,
    print_live_refs,
)
from scrapy.utils.url import (
    add_http_if_no_scheme,
    guess_scheme,
    strip_url,
    url_has_any_extension,
    url_is_from_spider,
)


class ExampleSpider(Spider):
    name = "example.test"
    allowed_domains = ["example.test"]


class _Crawler:
    settings = Settings({"DEFAULT_DROPITEM_LOG_LEVEL": "WARNING"})


def main() -> None:
    spider = ExampleSpider()
    spider.crawler = _Crawler()
    request = Request(
        "https://example.test/path",
        headers={"Referer": "https://example.test/origin"},
        flags=["request-flag"],
    )
    response = Response(request.url, status=201, flags=["response-flag"])
    formatter = LogFormatter()
    dropped = DropItem("no thanks", log_level="INFO")
    output = {}
    for name, args in {
        "crawled": (request, response, spider),
        "scraped": ({"result": "ok"}, response, spider),
        "scraped_start": ({"result": "ok"}, None, spider),
        "dropped": ({"result": "ok"}, dropped, response, spider),
        "item_error": ({"result": "ok"}, ValueError("oops"), response, spider),
        "spider_error": (ValueError("oops"), request, response, spider),
        "download_error": (ValueError("oops"), request, spider, "oops"),
    }.items():
        formatter_method = getattr(formatter, name.removesuffix("_start"))
        formatted = formatter_method(*args)
        output[name] = {
            "level": formatted["level"],
            "message": formatted["msg"] % formatted["args"],
            "keys": sorted(formatted["args"]),
            "adapter": str(logformatter_adapter(formatted)),
        }
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        legacy = logformatter_adapter({"msg": "legacy %(value)s", "value": "hello"})
    output["legacy_adapter"] = (str(legacy), [w.category.__name__ for w in caught])
    record = logging.LogRecord("scrapy.foo.bar", logging.WARNING, __file__, 1, "hi", (), None)
    TopLevelFormatter(["scrapy"]).filter(record)
    adapter = SpiderLoggerAdapter(logging.getLogger("fixture"), {"spider": spider})
    _, extra = adapter.process("message", {"extra": {"source": "fixture"}})
    logs: list[str] = []

    class _Capture(logging.Handler):
        def emit(self, entry: logging.LogRecord) -> None:
            logs.append(entry.getMessage())

    fixture_logger = logging.getLogger("utility-snapshot")
    capture = _Capture()
    fixture_logger.addHandler(capture)
    fixture_logger.setLevel(logging.INFO)
    StreamLogger(fixture_logger).write("one\n two\n")
    fixture_logger.removeHandler(capture)

    class _Stats:
        values: dict[str, int] = {}

        def inc_value(self, key: str) -> None:
            self.values[key] = self.values.get(key, 0) + 1

    class _StatsCrawler:
        stats = _Stats()

    LogCounterHandler(_StatsCrawler()).emit(record)
    with warnings.catch_warnings(record=True) as log_warnings:
        warnings.simplefilter("always")
        configure_logging({"LOG_INSTALL_ROOT_HANDLER": False}, install_root_handler=False)
    output["logging"] = {
        "name": record.name,
        "adapter_keys": sorted(extra["extra"]),
        "stream": logs,
        "stats": _Stats.values,
        "root_handler": get_scrapy_root_handler() is None,
        "warnings": [w.category.__name__ for w in log_warnings],
    }
    output["exceptions"] = {
        "close_args": CloseSpider("finish").args,
        "close_reason": CloseSpider("finish").reason,
        "drop_args": dropped.args,
        "drop_level": dropped.log_level,
        "stop_fail": StopDownload(fail=False).fail,
    }
    output["url"] = {
        "http": [
            add_http_if_no_scheme(value)
            for value in (
                "example.test",
                "//example.test/path",
                "https://example.test",
                "file:///tmp/x",
                "example.test:80/path",
            )
        ],
        "guess": [
            guess_scheme(value)
            for value in ("example.test", "//example.test", "/tmp/file", "./file", "../file")
        ],
        "strip": [
            strip_url("https://user:pass@example.test:443/path?q=1#fragment"),
            strip_url("https://user:pass@example.test:443/path?q=1#fragment", origin_only=True),
            strip_url(
                "https://user:pass@example.test:443/path?q=1#fragment",
                strip_credentials=False,
                strip_default_port=False,
                strip_fragment=False,
            ),
        ],
        "extensions": url_has_any_extension("https://example.test/X.PNG?x=1", [".png"]),
        "spider": url_is_from_spider("https://sub.example.test/a", ExampleSpider),
    }
    output["curl"] = [
        curl_to_request_kwargs(command)
        for command in (
            "curl example.test/path",
            "curl -X PATCH https://example.test -H 'X-Test: one' -d '$name=one' -d 'age=2'",
            "curl https://example.test -b 'session=abc' -u user:password --compressed",
            "curl https://example.test -d ''",
        )
    ]
    with warnings.catch_warnings(record=True) as curl_warnings:
        warnings.simplefilter("always")
        curl_to_request_kwargs("curl https://example.test --unknown")
    output["curl_warning"] = [(str(w.message), w.category.__name__) for w in curl_warnings]
    output["serialized"] = json.loads(
        json.dumps(
            {"number": {1}, "request": request, "response": response},
            cls=ScrapyJSONEncoder,
        )
    )
    with TemporaryDirectory() as directory:
        target = str(Path(directory) / "nested" / "job")
        output["job_dir"] = (
            job_dir(Settings({"JOBDIR": target})) == target,
            Path(target).is_dir(),
        )
    with warnings.catch_warnings(record=True) as mail_warnings:
        warnings.simplefilter("always")
        from scrapy.mail import MailSender

    output["mail_import_warning"] = [w.category.__name__ for w in mail_warnings]
    if os.environ.get("SPIDEROXIDE_SHIM") != "1":
        from twisted.internet import reactor

        assert reactor is not None
    mail = MailSender(debug=True)
    captured = {}

    def callback(**kwargs: object) -> None:
        captured.update(kwargs)

    result = mail.send(
        "to@example.test",
        "subject",
        "body",
        cc="cc@example.test",
        attachs=[("file.txt", "text/plain", BytesIO(b"attachment"))],
        _callback=callback,
    )
    output["mail"] = {
        "return": result,
        "to": captured["to"],
        "cc": captured["cc"],
        "mime": captured["msg"]["Content-Type"],
        "attachment": captured["msg"].get_payload()[1]["Content-Disposition"],
    }
    live_refs.clear()

    class Tracked(object_ref):
        pass

    class Ignored(object_ref):
        pass

    first, second, ignored = Tracked(), Tracked(), Ignored()
    report = format_live_refs()
    captured_report = StringIO()
    with redirect_stdout(captured_report):
        print_live_refs(ignore=Ignored)
    output["trackref"] = {
        "report": re.sub(r"oldest: \d+s ago", "oldest: Ns ago", report),
        "printed": re.sub(r"oldest: \d+s ago", "oldest: Ns ago", captured_report.getvalue()),
        "oldest": get_oldest("Tracked") is first,
        "members": len(tuple(iter_all("Tracked"))),
        "missing": get_oldest("Missing") is None and not tuple(iter_all("Missing")),
    }
    del second, ignored
    gc.collect()
    output["trackref"]["remaining"] = len(tuple(iter_all("Tracked")))
    print(json.dumps(output, sort_keys=True, default=lambda value: value.decode("latin-1")))


if __name__ == "__main__":
    main()
