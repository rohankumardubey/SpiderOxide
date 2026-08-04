from __future__ import annotations

from common import (
    ALL_CASES,
    environment_info,
    measure_memory,
    parse_benchmark_args,
    run_cases,
    write_results,
)
from verify_correctness import run_correctness


def main() -> None:
    arguments = parse_benchmark_args("Run correctness checks and every benchmark")
    correctness = run_correctness()
    results = run_cases(ALL_CASES, arguments.sizes, arguments.warmups, arguments.runs)
    memory = measure_memory(arguments.sizes)
    full_default_sizes = arguments.sizes == [10_000, 100_000, 1_000_000]
    limitation = (
        "All specified dataset sizes were executed."
        if full_default_sizes
        else "The 1,000,000-request suite was not executed because its 10 measured runs across "
        "all scenarios were impractical for this interactive environment."
    )
    command = (
        "python benchmark/run_all.py "
        f"--sizes {' '.join(str(size) for size in arguments.sizes)} "
        f"--warmups {arguments.warmups} --runs {arguments.runs}"
    )
    payload = {
        "environment": environment_info(),
        "correctness": correctness,
        "methodology": {
            "timer": "time.perf_counter_ns",
            "warmups": arguments.warmups,
            "measured_runs": arguments.runs,
            "alternating_order": True,
            "garbage_collection_before_each_run": True,
            "sizes": arguments.sizes,
            "seed": correctness["seed"],
            "size_limitation": limitation,
        },
        "results": results,
        "memory": memory,
        "commands_executed": [
            "python3 -m venv .venv",
            ".venv/bin/python -m pip install --upgrade pip",
            ".venv/bin/python -m pip install -r requirements.txt",
            ".venv/bin/python -m ruff format .",
            ".venv/bin/python -m ruff check .",
            "cargo fmt --manifest-path rust_impl/Cargo.toml -- --check",
            "cargo clippy --manifest-path rust_impl/Cargo.toml --release -- -D warnings",
            ".venv/bin/maturin develop --release",
            ".venv/bin/python benchmark/verify_correctness.py",
            command,
        ],
        "failed_commands": [],
    }
    write_results(payload)
    print("Wrote results/results.json, results/results.csv, and results/report.md")


if __name__ == "__main__":
    main()
