# Contributing to SpiderOxide

Thank you for helping build a fast, reliable web crawling framework.

## Ground rules

- Preserve observable behavior before optimizing it.
- Do not add benchmark claims without checked-in raw results and environment details.
- Keep the Rust implementation free of `unsafe` code.
- Keep Python and Rust component behavior aligned while both implementations are supported.
- Do not make a native component the default until correctness, failure handling, and real-workload
  performance have been demonstrated.

## Development setup

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
maturin develop --release
```

On Windows PowerShell, activate with `.\.venv\Scripts\Activate.ps1`.

## Required checks

Run these before opening a pull request:

```bash
python -m ruff format .
python -m ruff check .
cargo fmt --manifest-path rust_impl/Cargo.toml -- --check
cargo clippy --manifest-path rust_impl/Cargo.toml --release -- -D warnings
maturin develop --release
python benchmark/verify_integration.py
python benchmark/verify_correctness.py
python benchmark/verify_crawler.py
python benchmark/verify_spider_middleware.py
python benchmark/verify_http_models.py
python benchmark/verify_extensions.py
python benchmark/verify_native_downloader.py
python benchmark/verify_native_engine.py
python benchmark/verify_scheduler_queues.py
python benchmark/verify_job_state.py
python benchmark/verify_selectors.py
python benchmark/verify_retry.py
python benchmark/verify_native_policy.py
python benchmark/verify_depth.py
python benchmark/verify_native_slots.py
python benchmark/verify_redirect.py
python benchmark/verify_proxy.py
python benchmark/verify_native_robots.py
```

The release wheel must also contain the public `spideroxide` package without Rust source or build
artifacts. CI builds the wheel and imports it from outside the repository checkout.

## Changing behavior

Canonicalization, fingerprint framing, duplicate decisions, and queue ordering form one shared
contract. A behavior change must:

1. be implemented in both backends;
2. include focused edge-case validation;
3. pass the deterministic 10,000-request parity suite;
4. be documented in the README;
5. explain compatibility impact.

## Benchmark changes

Generate data before timed sections and use equivalent immutable inputs for both implementations.
Keep alternating run order, warm-ups, garbage collection, and summary statistics intact. Never
replace measured values with estimates.

For expensive experiments, start with:

```bash
python benchmark/run_all.py --sizes 1000 --warmups 1 --runs 2
```

Use the required three warm-ups and ten measured runs for publishable results.
