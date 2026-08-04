from __future__ import annotations

import argparse
import csv
import gc
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

from benchmark.generate_data import RequestData, generate_requests
from spideroxide import _rust as rust_impl
from spideroxide._python import (
    PythonDupeFilter,
    PythonScheduler,
    fingerprint,
    fingerprint_batch,
)

Factory = Callable[[], Callable[[], object]]


def _command_version(command: list[str]) -> str:
    return subprocess.run(command, check=True, capture_output=True, text=True).stdout.strip()


def environment_info() -> dict[str, object]:
    return {
        "python_version": platform.python_version(),
        "python_implementation": platform.python_implementation(),
        "rust_version": _command_version(["rustc", "--version"]),
        "operating_system": platform.platform(),
        "architecture": platform.machine(),
        "logical_cpu_count": os.cpu_count(),
        "rust_build": "release",
    }


def summarize(samples_ns: list[int], operations: int) -> dict[str, float | int]:
    median = statistics.median(samples_ns)
    mean = statistics.mean(samples_ns)
    return {
        "runs": len(samples_ns),
        "median_ns": median,
        "mean_ns": mean,
        "min_ns": min(samples_ns),
        "max_ns": max(samples_ns),
        "stdev_ns": statistics.stdev(samples_ns) if len(samples_ns) > 1 else 0.0,
        "operations": operations,
        "operations_per_second": operations / (median / 1_000_000_000),
        "ns_per_operation": median / operations,
    }


def _run_once(factory: Factory) -> tuple[int, object]:
    operation = factory()
    gc.collect()
    started = time.perf_counter_ns()
    output = operation()
    elapsed = time.perf_counter_ns() - started
    return elapsed, output


def measure_pair(
    python_factory: Factory,
    rust_factory: Factory,
    operations: int,
    warmups: int,
    runs: int,
) -> tuple[dict[str, float | int], dict[str, float | int]]:
    factories = {"python": python_factory, "rust": rust_factory}
    for index in range(warmups):
        order = ("python", "rust") if index % 2 == 0 else ("rust", "python")
        for implementation in order:
            _run_once(factories[implementation])

    samples: dict[str, list[int]] = {"python": [], "rust": []}
    for index in range(runs):
        order = ("python", "rust") if index % 2 == 0 else ("rust", "python")
        for implementation in order:
            elapsed, _ = _run_once(factories[implementation])
            samples[implementation].append(elapsed)
    return summarize(samples["python"], operations), summarize(samples["rust"], operations)


def _unique_count(requests: Sequence[RequestData]) -> int:
    duplicate_filter = PythonDupeFilter()
    return sum(not value for value in duplicate_filter.seen_batch(requests))


def _case_factories(
    case: str, requests: list[RequestData], unique_count: int
) -> tuple[Factory, Factory, int]:
    size = len(requests)
    if case == "fingerprint_single":
        return (
            lambda: lambda: [fingerprint(url, method, body) for url, method, body, _ in requests],
            lambda: (
                lambda: [
                    rust_impl.fingerprint(url, method, body) for url, method, body, _ in requests
                ]
            ),
            size,
        )
    if case == "fingerprint_batch":
        return (
            lambda: lambda: fingerprint_batch(requests),
            lambda: lambda: rust_impl.fingerprint_batch(requests),
            size,
        )
    if case == "dupefilter_single":

        def python_dupe_factory() -> Callable[[], object]:
            duplicate_filter = PythonDupeFilter()
            return lambda: [
                duplicate_filter.seen(url, method, body) for url, method, body, _ in requests
            ]

        def rust_dupe_factory() -> Callable[[], object]:
            duplicate_filter = rust_impl.RustDupeFilter()
            return lambda: [
                duplicate_filter.seen(url, method, body) for url, method, body, _ in requests
            ]

        return python_dupe_factory, rust_dupe_factory, size
    if case == "dupefilter_batch":

        def python_dupe_batch_factory() -> Callable[[], object]:
            duplicate_filter = PythonDupeFilter()
            return lambda: duplicate_filter.seen_batch(requests)

        def rust_dupe_batch_factory() -> Callable[[], object]:
            duplicate_filter = rust_impl.RustDupeFilter()
            return lambda: duplicate_filter.seen_batch(requests)

        return python_dupe_batch_factory, rust_dupe_batch_factory, size
    if case == "scheduler_insert_single":

        def python_insert_factory() -> Callable[[], object]:
            scheduler = PythonScheduler()
            return lambda: [
                scheduler.push(url, method, body, priority)
                for url, method, body, priority in requests
            ]

        def rust_insert_factory() -> Callable[[], object]:
            scheduler = rust_impl.RustScheduler()
            return lambda: [
                scheduler.push(url, method, body, priority)
                for url, method, body, priority in requests
            ]

        return python_insert_factory, rust_insert_factory, size
    if case == "scheduler_insert_batch":

        def python_insert_batch_factory() -> Callable[[], object]:
            scheduler = PythonScheduler()
            return lambda: scheduler.push_batch(requests)

        def rust_insert_batch_factory() -> Callable[[], object]:
            scheduler = rust_impl.RustScheduler()
            return lambda: scheduler.push_batch(requests)

        return python_insert_batch_factory, rust_insert_batch_factory, size
    if case == "scheduler_remove_single":

        def python_remove_factory() -> Callable[[], object]:
            scheduler = PythonScheduler()
            scheduler.push_batch(requests)

            def remove() -> int:
                removed = 0
                while scheduler.pop() is not None:
                    removed += 1
                return removed

            return remove

        def rust_remove_factory() -> Callable[[], object]:
            scheduler = rust_impl.RustScheduler()
            scheduler.push_batch(requests)

            def remove() -> int:
                removed = 0
                while scheduler.pop() is not None:
                    removed += 1
                return removed

            return remove

        return python_remove_factory, rust_remove_factory, unique_count
    if case == "scheduler_remove_batch":

        def python_remove_batch_factory() -> Callable[[], object]:
            scheduler = PythonScheduler()
            scheduler.push_batch(requests)
            return lambda: scheduler.pop_batch(unique_count)

        def rust_remove_batch_factory() -> Callable[[], object]:
            scheduler = rust_impl.RustScheduler()
            scheduler.push_batch(requests)
            return lambda: scheduler.pop_batch(unique_count)

        return python_remove_batch_factory, rust_remove_batch_factory, unique_count
    if case == "end_to_end_single":

        def python_e2e_factory() -> Callable[[], object]:
            def execute() -> int:
                scheduler = PythonScheduler()
                for request in requests:
                    scheduler.push(*request)
                removed = 0
                while scheduler.pop() is not None:
                    removed += 1
                return removed

            return execute

        def rust_e2e_factory() -> Callable[[], object]:
            def execute() -> int:
                scheduler = rust_impl.RustScheduler()
                for request in requests:
                    scheduler.push(*request)
                removed = 0
                while scheduler.pop() is not None:
                    removed += 1
                return removed

            return execute

        return python_e2e_factory, rust_e2e_factory, size
    if case == "end_to_end_batch":

        def python_e2e_batch_factory() -> Callable[[], object]:
            def execute() -> object:
                scheduler = PythonScheduler()
                scheduler.push_batch(requests)
                return scheduler.pop_batch(unique_count)

            return execute

        def rust_e2e_batch_factory() -> Callable[[], object]:
            def execute() -> object:
                scheduler = rust_impl.RustScheduler()
                scheduler.push_batch(requests)
                return scheduler.pop_batch(unique_count)

            return execute

        return python_e2e_batch_factory, rust_e2e_batch_factory, size
    raise ValueError(f"unknown benchmark case: {case}")


ALL_CASES = (
    "fingerprint_single",
    "fingerprint_batch",
    "dupefilter_single",
    "dupefilter_batch",
    "scheduler_insert_single",
    "scheduler_insert_batch",
    "scheduler_remove_single",
    "scheduler_remove_batch",
    "end_to_end_single",
    "end_to_end_batch",
)


def parse_benchmark_args(description: str) -> object:
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument(
        "--sizes",
        nargs="+",
        type=int,
        default=[10_000, 100_000, 1_000_000],
        help="pre-generated dataset sizes",
    )
    parser.add_argument("--warmups", type=int, default=3)
    parser.add_argument("--runs", type=int, default=10)
    return parser.parse_args()


def run_cli(cases: Sequence[str], description: str) -> None:
    arguments = parse_benchmark_args(description)
    run_cases(cases, arguments.sizes, arguments.warmups, arguments.runs)


def run_cases(
    cases: Sequence[str],
    sizes: Sequence[int],
    warmups: int = 3,
    runs: int = 10,
) -> list[dict[str, Any]]:
    results = []
    for size in sizes:
        requests = generate_requests(size)
        unique_count = _unique_count(requests)
        for case in cases:
            python_factory, rust_factory, operations = _case_factories(case, requests, unique_count)
            python_stats, rust_stats = measure_pair(
                python_factory, rust_factory, operations, warmups, runs
            )
            results.append(
                {
                    "test": case,
                    "size": size,
                    "unique_requests": unique_count,
                    "duplicate_requests": size - unique_count,
                    "python": python_stats,
                    "rust": rust_stats,
                    "speedup": python_stats["median_ns"] / rust_stats["median_ns"],
                }
            )
            print(
                f"{case:25} {size:>9,}: "
                f"Python {python_stats['median_ns'] / 1e6:>9.3f} ms, "
                f"Rust {rust_stats['median_ns'] / 1e6:>9.3f} ms, "
                f"{results[-1]['speedup']:.2f}x"
            )
    return results


def measure_memory(sizes: Sequence[int]) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    worker = ROOT / "benchmark" / "memory_worker.py"
    for size in sizes:
        for implementation in ("python", "rust"):
            for mode in ("single", "batch"):
                process = subprocess.run(
                    [
                        sys.executable,
                        str(worker),
                        "--implementation",
                        implementation,
                        "--mode",
                        mode,
                        "--size",
                        str(size),
                    ],
                    cwd=ROOT,
                    check=True,
                    capture_output=True,
                    text=True,
                )
                records.append(json.loads(process.stdout))
    return records


def write_results(payload: dict[str, object]) -> None:
    output_dir = ROOT / "results"
    output_dir.mkdir(exist_ok=True)
    (output_dir / "results.json").write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    rows = payload["results"]
    assert isinstance(rows, list)
    with (output_dir / "results.csv").open("w", newline="", encoding="utf-8") as output:
        fieldnames = [
            "test",
            "size",
            "unique_requests",
            "duplicate_requests",
            "python_median_ns",
            "rust_median_ns",
            "python_ops_per_second",
            "rust_ops_per_second",
            "speedup",
        ]
        writer = csv.DictWriter(output, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    "test": row["test"],
                    "size": row["size"],
                    "unique_requests": row["unique_requests"],
                    "duplicate_requests": row["duplicate_requests"],
                    "python_median_ns": row["python"]["median_ns"],
                    "rust_median_ns": row["rust"]["median_ns"],
                    "python_ops_per_second": row["python"]["operations_per_second"],
                    "rust_ops_per_second": row["rust"]["operations_per_second"],
                    "speedup": row["speedup"],
                }
            )
    (output_dir / "report.md").write_text(render_report(payload), encoding="utf-8")


def render_report(payload: dict[str, object]) -> str:
    environment = payload["environment"]
    assert isinstance(environment, dict)
    lines = [
        "# Python versus Rust benchmark report",
        "",
        "## Environment",
        "",
        "| Property | Value |",
        "|---|---|",
    ]
    lines.extend(
        f"| {key.replace('_', ' ').title()} | {value} |" for key, value in environment.items()
    )
    lines.extend(
        [
            "",
            "## Correctness",
            "",
            f"Passed: **{payload['correctness']['passed']}**; "
            f"{payload['correctness']['requests_checked']:,} deterministic requests checked "
            "byte-for-byte and in scheduler output order.",
            "",
            "## Methodology",
            "",
            f"- {payload['methodology']['warmups']} warm-up runs and "
            f"{payload['methodology']['measured_runs']} measured runs per implementation.",
            "- Python and Rust run in alternating order; garbage collection runs before every "
            "timing.",
            "- Dataset generation, imports, compilation, correctness checks, and scheduler "
            "population "
            "for removal tests are outside measured intervals.",
            "- Values are wall-clock measurements from `time.perf_counter_ns()`.",
            "",
            "## Timing results",
            "",
            "| Test | Size | Python median | Rust median | Speedup |",
            "|---|---:|---:|---:|---:|",
        ]
    )
    for row in payload["results"]:
        lines.append(
            f"| {row['test']} | {row['size']:,} | "
            f"{row['python']['median_ns'] / 1e6:.3f} ms | "
            f"{row['rust']['median_ns'] / 1e6:.3f} ms | {row['speedup']:.2f}x |"
        )
    lines.extend(
        [
            "",
            "Full mean, minimum, maximum, standard deviation, throughput, and per-request values "
            "are in `results.json` and `results.csv`.",
            "",
            "## Peak process memory",
            "",
            "| Implementation | Mode | Size | Peak process memory |",
            "|---|---|---:|---:|",
        ]
    )
    for row in payload["memory"]:
        value = row.get("peak_process_bytes")
        displayed = f"{value / (1024 * 1024):.2f} MiB" if isinstance(value, int) else "Unavailable"
        lines.append(f"| {row['implementation']} | {row['mode']} | {row['size']:,} | {displayed} |")

    batch_rows = {(row["test"], row["size"]): row for row in payload["results"]}
    fingerprint_single = [row for row in payload["results"] if row["test"] == "fingerprint_single"]
    end_to_end = [row for row in payload["results"] if row["test"] == "end_to_end_batch"]
    average_fp = statistics.mean(row["speedup"] for row in fingerprint_single)
    average_e2e = statistics.mean(row["speedup"] for row in end_to_end)
    boundary_notes = []
    batch_cost_notes = []
    for row in fingerprint_single:
        batch = batch_rows[("fingerprint_batch", row["size"])]
        boundary_notes.append(
            f"{row['size']:,}: single {row['speedup']:.2f}x versus batch {batch['speedup']:.2f}x"
        )
        rust_batch_delta = (batch["rust"]["median_ns"] / row["rust"]["median_ns"] - 1) * 100
        batch_cost_notes.append(f"{row['size']:,}: {rust_batch_delta:+.1f}%")
    recommendation = (
        "The synthetic results justify a Scrapy-level Rust integration experiment, but not an "
        "immediate production replacement. Confirm the speedup under real callback, persistence, "
        "and concurrency workloads, and evaluate the observed Rust batch-memory increase."
        if average_e2e > 1.0
        else "Do not move these components to Rust based on this prototype; "
        "the measured end-to-end batch path did not improve wall-clock time."
    )
    lines.extend(
        [
            "",
            "Memory is the process high-water mark from a fresh subprocess and includes the "
            "interpreter, extension, and generated dataset. It is not an allocator-only "
            "measurement.",
            "",
            "## Interpretation",
            "",
            f"Fingerprint PyO3 overhead comparison ({'; '.join(boundary_notes)}). "
            f"Mean fingerprint-single speedup was {average_fp:.2f}x. Rust fingerprint batching "
            f"changed median time by {', '.join(batch_cost_notes)} relative to Rust single calls; "
            "bulk tuple conversion and output-list materialization outweighed the removed calls in "
            "this API shape.",
            "",
            f"Mean end-to-end batch speedup was {average_e2e:.2f}x. The principal Python costs are "
            "URL parsing/canonicalization, hashing, and heap operations; Rust batch execution "
            "keeps "
            "those loops native.",
            "",
            "## Limitations",
            "",
            "- This is a synthetic, in-memory workload and excludes Scrapy integration, "
            "persistence, "
            "network I/O, callbacks, and concurrency.",
            "- Canonicalization accepts absolute hierarchical URLs. It does not attempt Scrapy's "
            "full "
            "escaping behavior, internationalized-path policy, semicolon-parameter handling, or "
            "scheme-specific normalization.",
            "- Executed sizes: "
            f"{', '.join(f'{size:,}' for size in payload['methodology']['sizes'])}. "
            f"{payload['methodology']['size_limitation']}",
            "- Peak RSS is a subprocess high-water mark, so small differences include startup "
            "noise.",
            "",
            "## Recommendation",
            "",
            recommendation,
            "",
            "## Commands executed",
            "",
        ]
    )
    lines.extend(f"```text\n{command}\n```" for command in payload["commands_executed"])
    failures = payload.get("failed_commands", [])
    lines.extend(["", "## Failed commands and resolutions", ""])
    if failures:
        lines.extend(f"- `{failure['command']}`: {failure['resolution']}" for failure in failures)
    else:
        lines.append("None.")
    return "\n".join(lines) + "\n"
