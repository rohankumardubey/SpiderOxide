from __future__ import annotations

import argparse
import csv
import gc
import importlib.metadata
import json
import os
import platform
import statistics
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import scrapy
from scrapy.core.scheduler import Scheduler as ScrapyScheduler
from scrapy.crawler import Crawler as ScrapyCrawler
from scrapy.dupefilters import RFPDupeFilter
from scrapy.http import Request as ScrapyRequest
from scrapy.spiders import Spider as ScrapySpider
from scrapy.statscollectors import MemoryStatsCollector
from scrapy.utils.request import RequestFingerprinter
from scrapy.utils.request import fingerprint as scrapy_fingerprint

from benchmark.generate_data import RequestData, generate_requests
from spideroxide import DupeFilter, Request, Scheduler, fingerprint_request

Factory = Callable[[], Callable[[], object]]
CASES = (
    "fingerprint",
    "dupefilter",
    "scheduler_insert",
    "scheduler_remove",
    "end_to_end",
)
SCRAPY_SETTINGS = {
    "SCHEDULER_PRIORITY_QUEUE": "scrapy.pqueues.ScrapyPriorityQueue",
    "SCHEDULER_MEMORY_QUEUE": "scrapy.squeues.FifoMemoryQueue",
    "SCHEDULER_START_MEMORY_QUEUE": None,
}


def _spideroxide_requests(data: Sequence[RequestData]) -> list[Request]:
    return [
        Request(
            url,
            method=method,
            body=body,
            priority=priority,
            meta={"benchmark_index": index},
        )
        for index, (url, method, body, priority) in enumerate(data)
    ]


def _scrapy_requests(data: Sequence[RequestData]) -> list[ScrapyRequest]:
    return [
        ScrapyRequest(
            url,
            method=method,
            body=body,
            priority=priority,
            meta={"benchmark_index": index},
        )
        for index, (url, method, body, priority) in enumerate(data)
    ]


def _scrapy_scheduler() -> ScrapyScheduler:
    crawler = ScrapyCrawler(ScrapySpider, SCRAPY_SETTINGS)
    crawler.stats = MemoryStatsCollector(crawler)
    crawler.request_fingerprinter = RequestFingerprinter.from_crawler(crawler)
    scheduler = ScrapyScheduler.from_crawler(crawler)
    spider = ScrapySpider.from_crawler(crawler, name="spideroxide-benchmark")
    scheduler.open(spider)
    return scheduler


def _summary(samples_ns: list[int], operations: int) -> dict[str, float | int]:
    median = statistics.median(samples_ns)
    return {
        "runs": len(samples_ns),
        "median_ns": median,
        "mean_ns": statistics.mean(samples_ns),
        "min_ns": min(samples_ns),
        "max_ns": max(samples_ns),
        "stdev_ns": statistics.stdev(samples_ns) if len(samples_ns) > 1 else 0.0,
        "operations": operations,
        "operations_per_second": operations / (median / 1_000_000_000),
        "ns_per_operation": median / operations,
    }


def _run_once(factory: Factory) -> int:
    operation = factory()
    gc.collect()
    started = time.perf_counter_ns()
    operation()
    return time.perf_counter_ns() - started


def _measure(
    spideroxide_factory: Factory,
    scrapy_factory: Factory,
    operations: int,
    warmups: int,
    runs: int,
) -> tuple[dict[str, float | int], dict[str, float | int]]:
    factories = {
        "spideroxide": spideroxide_factory,
        "scrapy": scrapy_factory,
    }
    for index in range(warmups):
        order = ("spideroxide", "scrapy") if index % 2 == 0 else ("scrapy", "spideroxide")
        for implementation in order:
            _run_once(factories[implementation])

    samples: dict[str, list[int]] = {"spideroxide": [], "scrapy": []}
    for index in range(runs):
        order = ("spideroxide", "scrapy") if index % 2 == 0 else ("scrapy", "spideroxide")
        for implementation in order:
            samples[implementation].append(_run_once(factories[implementation]))
    return (
        _summary(samples["spideroxide"], operations),
        _summary(samples["scrapy"], operations),
    )


def _case_factories(
    case: str,
    data: list[RequestData],
    unique_count: int,
) -> tuple[Factory, Factory, int]:
    size = len(data)
    if case == "fingerprint":

        def spideroxide_factory() -> Callable[[], object]:
            requests = _spideroxide_requests(data)
            return lambda: [fingerprint_request(request, backend="rust") for request in requests]

        def scrapy_factory() -> Callable[[], object]:
            requests = _scrapy_requests(data)
            return lambda: [scrapy_fingerprint(request) for request in requests]

        return spideroxide_factory, scrapy_factory, size

    if case == "dupefilter":

        def spideroxide_dupe_factory() -> Callable[[], object]:
            requests = _spideroxide_requests(data)
            duplicate_filter = DupeFilter("rust")
            return lambda: [duplicate_filter.seen_request(request) for request in requests]

        def scrapy_dupe_factory() -> Callable[[], object]:
            requests = _scrapy_requests(data)
            duplicate_filter = RFPDupeFilter()
            return lambda: [duplicate_filter.request_seen(request) for request in requests]

        return spideroxide_dupe_factory, scrapy_dupe_factory, size

    if case == "scheduler_insert":

        def spideroxide_insert_factory() -> Callable[[], object]:
            requests = _spideroxide_requests(data)
            scheduler = Scheduler("rust")
            return lambda: [scheduler.push_request(request) for request in requests]

        def scrapy_insert_factory() -> Callable[[], object]:
            requests = _scrapy_requests(data)
            scheduler = _scrapy_scheduler()
            return lambda: [scheduler.enqueue_request(request) for request in requests]

        return spideroxide_insert_factory, scrapy_insert_factory, size

    if case == "scheduler_remove":

        def spideroxide_remove_factory() -> Callable[[], object]:
            requests = _spideroxide_requests(data)
            scheduler = Scheduler("rust")
            scheduler.push_requests(requests)
            return lambda: scheduler.pop_batch(unique_count)

        def scrapy_remove_factory() -> Callable[[], object]:
            requests = _scrapy_requests(data)
            scheduler = _scrapy_scheduler()
            for request in requests:
                scheduler.enqueue_request(request)
            return lambda: [scheduler.next_request() for _ in range(unique_count)]

        return spideroxide_remove_factory, scrapy_remove_factory, unique_count

    if case == "end_to_end":

        def spideroxide_end_to_end_factory() -> Callable[[], object]:
            requests = _spideroxide_requests(data)

            def execute() -> object:
                scheduler = Scheduler("rust")
                scheduler.push_requests(requests)
                return scheduler.pop_batch(unique_count)

            return execute

        def scrapy_end_to_end_factory() -> Callable[[], object]:
            requests = _scrapy_requests(data)

            def execute() -> object:
                scheduler = _scrapy_scheduler()
                for request in requests:
                    scheduler.enqueue_request(request)
                return [scheduler.next_request() for _ in range(unique_count)]

            return execute

        return spideroxide_end_to_end_factory, scrapy_end_to_end_factory, size

    raise ValueError(f"unknown benchmark case: {case}")


def verify_comparable(data: list[RequestData]) -> dict[str, int]:
    spideroxide_requests = _spideroxide_requests(data)
    scrapy_requests = _scrapy_requests(data)

    spideroxide_filter = DupeFilter("rust")
    scrapy_filter = RFPDupeFilter()
    spideroxide_decisions = [
        spideroxide_filter.seen_request(request) for request in spideroxide_requests
    ]
    scrapy_decisions = [scrapy_filter.request_seen(request) for request in scrapy_requests]
    if spideroxide_decisions != scrapy_decisions:
        raise AssertionError("duplicate decisions differ between SpiderOxide and Scrapy")

    spideroxide_scheduler = Scheduler("rust")
    scrapy_scheduler = _scrapy_scheduler()
    spideroxide_inserted = spideroxide_scheduler.push_requests(spideroxide_requests)
    scrapy_inserted = [scrapy_scheduler.enqueue_request(request) for request in scrapy_requests]
    if spideroxide_inserted != scrapy_inserted:
        raise AssertionError("scheduler insertion decisions differ")

    unique_count = spideroxide_inserted.count(True)
    spideroxide_output = spideroxide_scheduler.pop_batch(unique_count)
    scrapy_output = [scrapy_scheduler.next_request() for _ in range(unique_count)]
    spideroxide_order = [request.meta["benchmark_index"] for request in spideroxide_output]
    scrapy_order = [
        request.meta["benchmark_index"] for request in scrapy_output if request is not None
    ]
    if spideroxide_order != scrapy_order:
        raise AssertionError("scheduler output order differs")
    return {
        "requests": len(data),
        "unique": unique_count,
        "duplicates": len(data) - unique_count,
    }


def run_benchmarks(
    sizes: Sequence[int],
    warmups: int,
    runs: int,
) -> tuple[list[dict[str, Any]], list[dict[str, int]]]:
    results = []
    sanity = []
    for size in sizes:
        data = generate_requests(size)
        comparable = verify_comparable(data)
        sanity.append(comparable)
        for case in CASES:
            spideroxide_factory, scrapy_factory, operations = _case_factories(
                case,
                data,
                comparable["unique"],
            )
            spideroxide_stats, scrapy_stats = _measure(
                spideroxide_factory,
                scrapy_factory,
                operations,
                warmups,
                runs,
            )
            speedup = scrapy_stats["median_ns"] / spideroxide_stats["median_ns"]
            record = {
                "test": case,
                "size": size,
                "unique_requests": comparable["unique"],
                "duplicate_requests": comparable["duplicates"],
                "spideroxide": spideroxide_stats,
                "scrapy": scrapy_stats,
                "speedup": speedup,
            }
            results.append(record)
            print(
                f"{case:20} {size:>9,}: "
                f"SpiderOxide {spideroxide_stats['median_ns'] / 1e6:>9.3f} ms, "
                f"Scrapy {scrapy_stats['median_ns'] / 1e6:>9.3f} ms, "
                f"{speedup:.2f}x"
            )
    return results, sanity


def _environment() -> dict[str, object]:
    return {
        "python_version": platform.python_version(),
        "spideroxide_version": importlib.metadata.version("spideroxide"),
        "scrapy_version": scrapy.__version__,
        "rust_version": subprocess.run(
            ["rustc", "--version"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip(),
        "operating_system": platform.platform(),
        "architecture": platform.machine(),
        "logical_cpu_count": os.cpu_count(),
        "spideroxide_backend": "Rust release",
    }


def _report(payload: dict[str, object]) -> str:
    environment = payload["environment"]
    lines = [
        "# SpiderOxide versus Scrapy component benchmark",
        "",
        "This report compares SpiderOxide's Rust request-processing backend with "
        f"Scrapy {environment['scrapy_version']}.",
        "",
        "## Environment",
        "",
        f"* Python {environment['python_version']}",
        f"* SpiderOxide {environment['spideroxide_version']}",
        f"* Scrapy {environment['scrapy_version']}",
        f"* {environment['rust_version']}",
        f"* {environment['operating_system']}",
        f"* {environment['architecture']}, {environment['logical_cpu_count']} logical CPUs",
        f"* SpiderOxide backend: {environment['spideroxide_backend']}",
        "",
        "## Methodology",
        "",
        f"* {payload['methodology']['warmups']} warm-up runs",
        f"* {payload['methodology']['runs']} measured runs",
        "* alternating execution order",
        "* garbage collection before each timing",
        "* request construction outside timed sections",
        "* Scrapy FIFO in-memory queues configured to match SpiderOxide ordering",
        "",
        "| Test | Requests | SpiderOxide median | Scrapy median | Speedup |",
        "|---|---:|---:|---:|---:|",
    ]
    for row in payload["results"]:
        lines.append(
            f"| {row['test']} | {row['size']:,} | "
            f"{row['spideroxide']['median_ns'] / 1e6:.3f} ms | "
            f"{row['scrapy']['median_ns'] / 1e6:.3f} ms | {row['speedup']:.2f}x |"
        )
    lines.extend(
        [
            "",
            "A speedup above 1.0 means SpiderOxide was faster.",
            "",
            "## Scope",
            "",
            "This is a component benchmark. It excludes networking, response parsing, selectors, "
            "middleware, pipelines, Twisted and asyncio reactor overhead, and full crawl behavior. "
            "It must not be interpreted as an end-to-end Scrapy crawl speedup.",
            "",
        ]
    )
    return "\n".join(lines)


def write_results(
    output_dir: Path,
    results: list[dict[str, Any]],
    sanity: list[dict[str, int]],
    sizes: Sequence[int],
    warmups: int,
    runs: int,
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "environment": _environment(),
        "methodology": {
            "sizes": list(sizes),
            "warmups": warmups,
            "runs": runs,
            "timer": "time.perf_counter_ns",
            "alternating_order": True,
            "request_construction_timed": False,
            "scrapy_queue": "ScrapyPriorityQueue with FifoMemoryQueue",
        },
        "sanity": sanity,
        "results": results,
    }
    (output_dir / "scrapy_comparison.json").write_text(
        json.dumps(payload, indent=2) + "\n",
        encoding="utf-8",
    )
    with (output_dir / "scrapy_comparison.csv").open(
        "w",
        newline="",
        encoding="utf-8",
    ) as output:
        writer = csv.DictWriter(
            output,
            fieldnames=[
                "test",
                "size",
                "spideroxide_median_ns",
                "scrapy_median_ns",
                "speedup",
            ],
            lineterminator="\n",
        )
        writer.writeheader()
        for row in results:
            writer.writerow(
                {
                    "test": row["test"],
                    "size": row["size"],
                    "spideroxide_median_ns": row["spideroxide"]["median_ns"],
                    "scrapy_median_ns": row["scrapy"]["median_ns"],
                    "speedup": row["speedup"],
                }
            )
    (output_dir / "scrapy_comparison.md").write_text(
        _report(payload),
        encoding="utf-8",
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Compare SpiderOxide components with Scrapy")
    parser.add_argument("--sizes", nargs="+", type=int, default=[10_000, 100_000])
    parser.add_argument("--warmups", type=int, default=3)
    parser.add_argument("--runs", type=int, default=10)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "results")
    arguments = parser.parse_args()
    results, sanity = run_benchmarks(arguments.sizes, arguments.warmups, arguments.runs)
    write_results(
        arguments.output_dir,
        results,
        sanity,
        arguments.sizes,
        arguments.warmups,
        arguments.runs,
    )


if __name__ == "__main__":
    main()
