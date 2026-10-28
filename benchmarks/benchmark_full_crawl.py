"""Compare complete crawls against the same local HTTP workload."""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import os
import platform
import statistics
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

ENGINES = ("scrapy", "python", "rust")
ROOT = Path(__file__).resolve().parents[1]


def _crawl(engine: str, base_url: str, pages: int, concurrency: int) -> None:
    if engine == "scrapy":
        from scrapy import Spider
        from scrapy.crawler import CrawlerProcess
    else:
        from spideroxide import CrawlerProcess, Spider

    items: list[tuple[int, int, str | None]] = []

    class BenchmarkSpider(Spider):
        name = "full-crawl-benchmark"
        start_urls = tuple(f"{base_url}/{index}" for index in range(pages))

        def parse(self, response):
            index = int(urlsplit(response.url).path.removeprefix("/"))
            title = response.css("title::text").get()
            items.append((index, response.status, title))
            return {"id": index, "status": response.status, "title": title}

    settings: dict[str, object] = {
        "CONCURRENT_REQUESTS": concurrency,
        "CONCURRENT_REQUESTS_PER_DOMAIN": concurrency,
        "ROBOTSTXT_OBEY": False,
        "COOKIES_ENABLED": False,
        "RETRY_ENABLED": False,
        "LOG_ENABLED": False,
        "DOWNLOAD_TIMEOUT": 10,
    }
    if engine != "scrapy":
        settings.update(
            ENGINE_BACKEND=engine,
            DOWNLOADER_BACKEND=engine,
            DOWNLOAD_VERIFY_CERTIFICATES=True,
        )
    started = time.perf_counter_ns()
    process = CrawlerProcess(settings)
    crawler = process.create_crawler(BenchmarkSpider)
    process.crawl(crawler)
    process.start(install_signal_handlers=False)
    elapsed_ns = time.perf_counter_ns() - started

    expected = [(index, 200, f"page-{index}") for index in range(pages)]
    if sorted(items, key=lambda item: item[0]) != expected:
        raise AssertionError(f"{engine} crawl returned incorrect items: {items[:5]}")
    stats = crawler.stats.get_stats()
    if stats.get("item_scraped_count") != pages or stats.get("finish_reason") != "finished":
        raise AssertionError(f"{engine} crawl did not finish cleanly: {stats}")
    print(json.dumps({"engine": engine, "pages": pages, "crawl_ns": elapsed_ns}))


class FixtureServer(ThreadingHTTPServer):
    def __init__(self, pages: int) -> None:
        super().__init__(("127.0.0.1", 0), FixtureHandler)
        self.pages = pages
        self.request_count = 0
        self.request_lock = threading.Lock()


class FixtureHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:
        server = self.server
        assert isinstance(server, FixtureServer)
        path = urlsplit(self.path).path
        if not path.startswith("/") or not path[1:].isdigit():
            self.send_error(404)
            return
        index = int(path[1:])
        if index >= server.pages:
            self.send_error(404)
            return
        with server.request_lock:
            server.request_count += 1
        body = (
            f"<!doctype html><html><title>page-{index}</title><body>item-{index}</body></html>"
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        pass


def _measure(engine: str, server: FixtureServer, pages: int, concurrency: int) -> int:
    before = server.request_count
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env.pop("SPIDEROXIDE_SCRAPY_COMPAT_ALLOW_CONFLICT", None)
    for name in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
    ):
        env.pop(name, None)
    env["NO_PROXY"] = env["no_proxy"] = "127.0.0.1,localhost"
    result = subprocess.run(
        [
            sys.executable,
            os.fspath(Path(__file__).resolve()),
            "--worker",
            engine,
            "--url",
            f"http://127.0.0.1:{server.server_port}",
            "--pages",
            str(pages),
            "--concurrency",
            str(concurrency),
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode:
        raise RuntimeError(f"{engine} crawl failed:\n{result.stdout}\n{result.stderr}")
    payload = json.loads(result.stdout)
    if payload["engine"] != engine or payload["pages"] != pages:
        raise ValueError(f"{engine} worker returned unexpected result: {payload}")
    if server.request_count - before != pages:
        raise AssertionError(
            f"{engine} fetched {server.request_count - before} pages, expected {pages}"
        )
    return int(payload["crawl_ns"])


def run(pages: int, concurrency: int, warmups: int, runs: int) -> dict[str, object]:
    if pages < 1 or concurrency < 1 or warmups < 0 or runs < 1:
        raise ValueError(
            "pages, concurrency, and runs must be positive; warmups cannot be negative"
        )
    if importlib.metadata.version("Scrapy") != "2.19.0":
        raise ValueError("Full-crawl comparison requires upstream Scrapy 2.19.0")
    try:
        importlib.metadata.version("spideroxide-scrapy-compat")
    except importlib.metadata.PackageNotFoundError:
        pass
    else:
        raise ValueError(
            "Install upstream Scrapy and the SpiderOxide shim in separate environments"
        )
    server = FixtureServer(pages)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    samples: dict[str, list[int]] = {engine: [] for engine in ENGINES}
    try:
        for iteration in range(warmups + runs):
            order = ENGINES[iteration % len(ENGINES) :] + ENGINES[: iteration % len(ENGINES)]
            for engine in order:
                elapsed = _measure(engine, server, pages, concurrency)
                if iteration >= warmups:
                    samples[engine].append(elapsed)
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
    return {
        "environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "architecture": platform.machine(),
            "logical_cpus": os.cpu_count(),
            "scrapy": importlib.metadata.version("Scrapy"),
            "spideroxide": importlib.metadata.version("spideroxide"),
        },
        "workload": {
            "pages": pages,
            "concurrency": concurrency,
            "warmups": warmups,
            "runs": runs,
            "server": "loopback HTTP/1.1, one small HTML page per URL",
            "timing": "crawler construction through completed crawl, excluding process imports",
        },
        "median_ms": {
            engine: round(statistics.median(values) / 1_000_000, 3)
            for engine, values in samples.items()
        },
        "samples_ns": samples,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pages", type=int, default=100)
    parser.add_argument("--concurrency", type=int, default=16)
    parser.add_argument("--warmups", type=int, default=1)
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--worker", choices=ENGINES)
    parser.add_argument("--url")
    args = parser.parse_args()
    if args.worker:
        if not args.url:
            parser.error("--worker requires --url")
        _crawl(args.worker, args.url, args.pages, args.concurrency)
        return
    if args.url:
        parser.error("--url is only valid with --worker")
    payload = run(args.pages, args.concurrency, args.warmups, args.runs)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(payload, indent=2))


if __name__ == "__main__":
    main()
