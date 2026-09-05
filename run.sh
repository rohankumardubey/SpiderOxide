#!/usr/bin/env bash

set -euo pipefail

cd "$(dirname "$0")"

usage() {
    cat <<'EOF'
Usage: ./run.sh [--task TASK] [--size REQUESTS]

Tasks:
  scrapy       SpiderOxide versus Scrapy component benchmark
  fingerprint  Request fingerprint benchmark
  dupefilter   Duplicate-filter benchmark
  scheduler    Scheduler benchmark
  pipeline     Request pipeline component benchmark
  benchmarks   Run all benchmark suites
  verify       Run the full verification suite
  all          Run all benchmarks and verification
EOF
}

task=""
size=""
while (($#)); do
    case "$1" in
        --task)
            shift
            if (($# == 0)); then
                usage >&2
                exit 2
            fi
            task="$1"
            ;;
        --size)
            shift
            if (($# == 0)); then
                usage >&2
                exit 2
            fi
            size="$1"
            ;;
        -h | --help)
            usage
            exit 0
            ;;
        *)
            echo "Unknown argument: $1" >&2
            usage >&2
            exit 2
            ;;
    esac
    shift
done

if [[ -n "$size" && ! "$size" =~ ^[1-9][0-9]*$ ]]; then
    echo "--size must be a positive integer" >&2
    exit 2
fi

if [[ -z "$task" && -n "$size" ]]; then
    task="scrapy"
fi

if [[ -z "$task" ]]; then
    cat <<'EOF'
Select what to run:
  1) SpiderOxide versus Scrapy component benchmark
  2) Request fingerprint benchmark
  3) Duplicate-filter benchmark
  4) Scheduler benchmark
  5) Request pipeline component benchmark
  6) All benchmark suites
  7) Full verification suite
  8) Everything
EOF
    read -r -p "Enter option [1-8]: " selection
    case "$selection" in
        1) task="scrapy" ;;
        2) task="fingerprint" ;;
        3) task="dupefilter" ;;
        4) task="scheduler" ;;
        5) task="pipeline" ;;
        6) task="benchmarks" ;;
        7) task="verify" ;;
        8) task="all" ;;
        *)
            echo "Invalid option: $selection" >&2
            exit 2
            ;;
    esac
fi

case "$task" in
    scrapy | fingerprint | dupefilter | scheduler | pipeline | benchmarks | verify | all) ;;
    *)
        echo "Unknown task: $task" >&2
        usage >&2
        exit 2
        ;;
esac

python3 -m venv .venv
source .venv/bin/activate
export PATH="$HOME/.cargo/bin:$PATH"

python -m pip install -r requirements-benchmark.txt
python -m maturin develop --release

benchmark_args=()
if [[ -n "$size" ]]; then
    benchmark_args=(--sizes "$size")
fi

run_benchmarks() {
    python benchmarks/benchmark_scrapy.py "${benchmark_args[@]}"
    python benchmarks/run_all.py "${benchmark_args[@]}"
}

case "$task" in
    scrapy)
        python benchmarks/benchmark_scrapy.py "${benchmark_args[@]}"
        ;;
    fingerprint)
        python benchmarks/benchmark_fingerprint.py "${benchmark_args[@]}"
        ;;
    dupefilter)
        python benchmarks/benchmark_dupefilter.py "${benchmark_args[@]}"
        ;;
    scheduler)
        python benchmarks/benchmark_scheduler.py "${benchmark_args[@]}"
        ;;
    pipeline)
        python benchmarks/benchmark_end_to_end.py "${benchmark_args[@]}"
        ;;
    benchmarks)
        run_benchmarks
        ;;
    verify)
        python tests/run_all.py
        ;;
    all)
        run_benchmarks
        python tests/run_all.py
        ;;
esac
