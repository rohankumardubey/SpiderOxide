from __future__ import annotations

import asyncio
import json
import logging
import os
import pickle
import subprocess
import sys
import tempfile
import time
import warnings
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from spideroxide import (  # noqa: E402
    AutoThrottle,
    Crawler,
    PeriodicLog,
    Request,
    Response,
    Spider,
    SpiderState,
    StatsMailer,
)
from spideroxide.exceptions import NotConfigured, ScrapyDeprecationWarning  # noqa: E402
from spideroxide.python_slots import PythonDownloadSlots  # noqa: E402
from spideroxide.stats import StatsCollector  # noqa: E402

SIGNATURE_SNAPSHOT = """
import importlib
import inspect
import json
import warnings
with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    methods = {
        "throttle.AutoThrottle": ("__init__", "from_crawler", "_min_delay",
            "_max_delay", "_start_delay", "_spider_opened", "_response_downloaded",
            "_get_slot", "_adjust_delay"),
        "spiderstate.SpiderState": ("__init__", "from_crawler", "spider_opened", "spider_closed"),
        "logcount.LogCount": ("__init__", "from_crawler", "spider_opened", "spider_closed"),
        "periodic_log.PeriodicLog": ("__init__", "from_crawler", "spider_opened",
            "spider_closed", "log", "log_delta", "log_timing", "log_crawler_stats",
            "param_allowed"),
        "statsmailer.StatsMailer": ("__init__", "from_crawler", "spider_closed"),
        "remote_control.RemoteControl": ("__init__", "from_crawler", "start", "stop"),
        "telnet.TelnetConsole": ("__init__", "from_crawler", "start_listening",
            "stop_listening", "protocol", "_get_telnet_vars"),
    }
    result = {}
    for qualified, names in methods.items():
        module_name, class_name = qualified.split(".")
        cls = getattr(importlib.import_module("scrapy.extensions." + module_name), class_name)
        for name in names:
            result[qualified + "." + name] = [
                (param.name, param.kind.name, param.default is inspect.Parameter.empty)
                for param in inspect.signature(getattr(cls, name)).parameters.values()
            ]
print(json.dumps(result, sort_keys=True))
"""


def verify_signatures() -> None:
    try:
        upstream = version("Scrapy")
    except PackageNotFoundError:
        return
    if upstream != "2.19.0":
        return
    baseline = json.loads(
        subprocess.run(
            [sys.executable, "-c", SIGNATURE_SNAPSHOT],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    )
    env = {
        **os.environ,
        "PYTHONPATH": f"{ROOT / 'compat' / 'scrapy' / 'src'}:{ROOT / 'src'}",
        "SPIDEROXIDE_SCRAPY_COMPAT_ALLOW_CONFLICT": "1",
    }
    candidate = json.loads(
        subprocess.run(
            [sys.executable, "-c", SIGNATURE_SNAPSHOT],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    )
    assert candidate == baseline, [
        (name, signature, candidate.get(name))
        for name, signature in baseline.items()
        if candidate.get(name) != signature
    ]


class SlotSpider(Spider):
    name = "slot-parity"

    def start_requests(self):
        for index in range(3):
            yield Request(f"slow://example.test/{index}", dont_filter=True)

    def parse(self, response: Response) -> None:
        return None


class TimedHandler:
    lazy = True
    calls: list[float] = []

    async def download_request(self, request: Request) -> Response:
        self.calls.append(time.monotonic())
        request.meta["download_latency"] = 0.04
        return Response(request.url, request=request)


class MailSpider(Spider):
    name = "stats-parity"
    start_urls = ["data:text/plain,ok"]

    def parse(self, response: Response) -> None:
        return None


class FakeMail:
    def __init__(self) -> None:
        self.sent: list[tuple[list[str], str, str]] = []

    def send(self, to: list[str], subject: str, body: str) -> asyncio.Task[None]:
        async def deliver() -> None:
            self.sent.append((to, subject, body))

        return asyncio.create_task(deliver())


class Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


def verify_public_hooks() -> None:
    with tempfile.TemporaryDirectory() as directory:
        spider = MailSpider()
        state = SpiderState(directory)
        assert state.jobdir == directory
        assert state.statefn == str(Path(directory, "spider.state"))
        state.spider_opened(spider)
        assert spider.state == {}
        spider.state["sequence"] = 1
        state.spider_closed(spider)
        assert pickle.loads(Path(state.statefn).read_bytes()) == {"sequence": 1}
        state.spider_opened(spider)
        assert spider.state == {"sequence": 1}

    stats = StatsCollector()
    stats.set_value("start_time", datetime.now(timezone.utc))
    stats.set_value("response_received_count", 4)
    stats.set_value("item_scraped_count", 3)
    periodic = PeriodicLog(
        stats,
        interval=60,
        ext_stats={"include": ["response"]},
        ext_delta={"include": ["response", "item"], "exclude": ["item"]},
        ext_timing_enabled=True,
    )
    periodic.time_prev = datetime.now(timezone.utc)
    periodic.delta_prev = {}
    assert periodic.log_delta() == {"delta": {"response_received_count": 4}}
    assert periodic.log_delta() == {"delta": {"response_received_count": 0}}
    assert periodic.log_crawler_stats() == {"stats": {"response_received_count": 4}}
    assert set(periodic.log_timing()["time"]) == {
        "log_interval",
        "start_time",
        "utcnow",
        "log_interval_real",
        "elapsed",
    }
    capture = Capture()
    log = logging.getLogger("spideroxide.operational")
    original_level = log.level
    log.setLevel(logging.INFO)
    log.addHandler(capture)
    try:
        periodic.log()
    finally:
        log.removeHandler(capture)
        log.setLevel(original_level)
    assert set(json.loads(capture.messages[0])) == {"time", "delta", "stats"}
    with warnings.catch_warnings(record=True) as recorded:
        warnings.simplefilter("always")
        periodic.multiplier = 3
        assert periodic.multiplier == 3
    assert [entry.category for entry in recorded] == [
        ScrapyDeprecationWarning,
        ScrapyDeprecationWarning,
    ]

    settings = {"AUTOTHROTTLE_ENABLED": True, "AUTOTHROTTLE_TARGET_CONCURRENCY": 1}
    crawler = Crawler(SlotSpider, settings)
    extension = AutoThrottle.from_crawler(crawler)
    extension.mindelay, extension.maxdelay = 0.01, 0.05
    slot = type("Slot", (), {"delay": 0.01})()
    extension._adjust_delay(slot, 0.04, Response("https://example.test"))
    assert slot.delay == 0.04
    extension._adjust_delay(slot, 0.001, Response("https://example.test", status=503))
    assert slot.delay == 0.04
    configured = Crawler(
        SlotSpider,
        {
            "AUTOTHROTTLE_ENABLED": True,
            "DOWNLOAD_SLOTS": {
                "example.test": {"delay": 0.02, "concurrency": 1, "randomize_delay": "false"}
            },
        },
    )
    slots = PythonDownloadSlots(configured, SlotSpider())
    _, configured_slot = slots._get_slot(Request("slow://example.test"))
    assert configured_slot.delay == 0.02
    assert configured_slot.concurrency == 1
    assert configured_slot.randomize is False
    try:
        AutoThrottle.from_crawler(
            Crawler(
                SlotSpider, {"AUTOTHROTTLE_ENABLED": True, "AUTOTHROTTLE_TARGET_CONCURRENCY": 0}
            )
        )
    except NotConfigured:
        pass
    else:
        raise AssertionError("AutoThrottle requires positive target concurrency")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", ScrapyDeprecationWarning)
        try:
            StatsMailer.from_crawler(Crawler(MailSpider, {}))
        except NotConfigured:
            pass
        else:
            raise AssertionError("StatsMailer must be disabled without recipients")


async def verify_crawl(engine: str) -> None:
    TimedHandler.calls = []
    crawler = Crawler(
        SlotSpider,
        {
            "ENGINE_BACKEND": engine,
            "AUTOTHROTTLE_ENABLED": True,
            "AUTOTHROTTLE_START_DELAY": 0.01,
            "AUTOTHROTTLE_MAX_DELAY": 0.05,
            "AUTOTHROTTLE_TARGET_CONCURRENCY": 1,
            "RANDOMIZE_DOWNLOAD_DELAY": False,
            "CONCURRENT_REQUESTS": 1,
            "DOWNLOAD_HANDLERS": {"slow": TimedHandler},
            "LOGSTATS_INTERVAL": 0,
        },
    )
    received: list[str] = []

    def downloaded(request: Request) -> None:
        received.append(request.url)

    from spideroxide import signals

    crawler.signals.connect(downloaded, signals.response_downloaded)
    result = await crawler.crawl()
    extension = crawler.get_extension(AutoThrottle)
    assert isinstance(extension, AutoThrottle)
    assert result.reason == "finished"
    assert result.stats["downloader/slot/example.test/delay"] >= 0.035
    assert len(received) == 3
    assert len(TimedHandler.calls) == 3
    assert all(
        later - earlier >= 0.032
        for earlier, later in zip(TimedHandler.calls, TimedHandler.calls[1:], strict=False)
    ), (engine, TimedHandler.calls)

    mail = FakeMail()
    with patch("spideroxide.statsmailer.MailSender.from_crawler", return_value=mail):
        with warnings.catch_warnings(record=True) as recorded:
            warnings.simplefilter("always")
            mailed = Crawler(
                MailSpider,
                {
                    "ENGINE_BACKEND": engine,
                    "STATSMAILER_RCPTS": ["admin@example.test"],
                    "LOGSTATS_INTERVAL": 0,
                },
            )
            mail_result = await mailed.crawl()
    assert isinstance(mailed.get_extension(StatsMailer), StatsMailer)
    assert mail_result.reason == "finished"
    assert len(mail.sent) == 1
    recipients, subject, body = mail.sent[0]
    assert recipients == ["admin@example.test"]
    assert subject == "Scrapy stats for: stats-parity"
    assert "response_received_count" in body
    assert any(entry.category is ScrapyDeprecationWarning for entry in recorded)


if __name__ == "__main__":
    verify_signatures()
    verify_public_hooks()
    for backend in ("python", "rust"):
        asyncio.run(verify_crawl(backend))
    print("Extension parity passed: state, logging hooks, throttle slots, and stats mail")
