# SpiderOxide benchmark report

## Environment

| Property | Value |
|---|---|
| Python Version | 3.14.6 |
| Python Implementation | CPython |
| Rust Version | rustc 1.97.1 (8bab26f4f 2026-07-14) |
| Operating System | macOS-26.5.2-arm64-arm-64bit-Mach-O |
| Architecture | arm64 |
| Logical Cpu Count | 12 |
| Rust Build | release |

## Correctness

Passed: **True**; 10,000 deterministic requests checked byte-for-byte and in scheduler output order.

## Methodology

- 3 warm-up runs and 10 measured runs per implementation.
- Python and Rust run in alternating order; garbage collection runs before every timing.
- Dataset generation, imports, compilation, correctness checks, and scheduler population for removal tests are outside measured intervals.
- Values are wall-clock measurements from `time.perf_counter_ns()`.

## Timing results

| Test | Size | Python median | Rust median | Speedup |
|---|---:|---:|---:|---:|
| fingerprint_single | 10,000 | 81.420 ms | 10.985 ms | 7.41x |
| fingerprint_batch | 10,000 | 82.748 ms | 11.341 ms | 7.30x |
| dupefilter_single | 10,000 | 82.875 ms | 11.224 ms | 7.38x |
| dupefilter_batch | 10,000 | 83.944 ms | 11.670 ms | 7.19x |
| scheduler_insert_single | 10,000 | 87.388 ms | 12.522 ms | 6.98x |
| scheduler_insert_batch | 10,000 | 89.143 ms | 11.815 ms | 7.54x |
| scheduler_remove_single | 10,000 | 2.762 ms | 0.919 ms | 3.01x |
| scheduler_remove_batch | 10,000 | 2.417 ms | 0.503 ms | 4.81x |
| end_to_end_single | 10,000 | 93.570 ms | 13.691 ms | 6.83x |
| end_to_end_batch | 10,000 | 92.376 ms | 12.022 ms | 7.68x |
| fingerprint_single | 100,000 | 848.131 ms | 126.551 ms | 6.70x |
| fingerprint_batch | 100,000 | 858.518 ms | 129.497 ms | 6.63x |
| dupefilter_single | 100,000 | 865.680 ms | 131.508 ms | 6.58x |
| dupefilter_batch | 100,000 | 875.077 ms | 134.390 ms | 6.51x |
| scheduler_insert_single | 100,000 | 918.117 ms | 148.586 ms | 6.18x |
| scheduler_insert_batch | 100,000 | 924.922 ms | 136.651 ms | 6.77x |
| scheduler_remove_single | 100,000 | 57.142 ms | 17.560 ms | 3.25x |
| scheduler_remove_batch | 100,000 | 46.376 ms | 8.911 ms | 5.20x |
| end_to_end_single | 100,000 | 987.814 ms | 166.552 ms | 5.93x |
| end_to_end_batch | 100,000 | 979.383 ms | 147.051 ms | 6.66x |

Full mean, minimum, maximum, standard deviation, throughput, and per-request values are in `results.json` and `results.csv`.

## Peak process memory

| Implementation | Mode | Size | Peak process memory |
|---|---|---:|---:|
| python | single | 10,000 | 29.62 MiB |
| python | batch | 10,000 | 29.75 MiB |
| rust | single | 10,000 | 29.48 MiB |
| rust | batch | 10,000 | 31.12 MiB |
| python | single | 100,000 | 69.94 MiB |
| python | batch | 100,000 | 71.84 MiB |
| rust | single | 100,000 | 81.89 MiB |
| rust | batch | 100,000 | 99.88 MiB |

Memory is the process high-water mark from a fresh subprocess and includes the interpreter, extension, and generated dataset. It is not an allocator-only measurement.

## Interpretation

Fingerprint PyO3 overhead comparison (10,000: single 7.41x versus batch 7.30x; 100,000: single 6.70x versus batch 6.63x). Mean fingerprint-single speedup was 7.06x. Rust fingerprint batching was 3.2% slower at 10,000 and 2.3% slower at 100,000 than Rust single calls; bulk tuple conversion and output-list materialization outweighed the removed calls in this API shape.

Mean end-to-end batch speedup was 7.17x. The principal Python costs are URL parsing/canonicalization, hashing, and heap operations; Rust batch execution keeps those loops native.

## Limitations

- This is a synthetic, in-memory workload and excludes Scrapy integration, persistence, network I/O, callbacks, and concurrency.
- Canonicalization accepts absolute hierarchical URLs. It does not attempt Scrapy's full escaping behavior, internationalized-path policy, semicolon-parameter handling, or scheme-specific normalization.
- Executed sizes: 10,000, 100,000. The 1,000,000-request suite was not executed because its 10 measured runs across all scenarios were impractical for this interactive environment.
- Peak RSS is a subprocess high-water mark, so small differences include startup noise.

## Recommendation

The synthetic results justify a Scrapy-level Rust integration experiment, but not an immediate production replacement. Confirm the speedup under real callback, persistence, and concurrency workloads, and evaluate the observed Rust batch-memory increase.

## Commands executed

```text
python3 -m venv .venv
```
```text
.venv/bin/python -m pip install --quiet --upgrade pip
```
```text
.venv/bin/python -m pip install --quiet -r requirements.txt
```
```text
.venv/bin/python -m ruff format .
```
```text
.venv/bin/python -m ruff check .
```
```text
cargo fmt --manifest-path rust_impl/Cargo.toml -- --check
```
```text
cargo clippy --manifest-path rust_impl/Cargo.toml --release -- -D warnings
```
```text
.venv/bin/maturin develop --release
```
```text
.venv/bin/python benchmark/verify_correctness.py
```
```text
.venv/bin/python benchmark/run_all.py --sizes 100 --warmups 1 --runs 2
```
```text
.venv/bin/python benchmark/run_all.py --sizes 10000 100000 --warmups 3 --runs 10
```

## Failed commands and resolutions

- `cargo fmt --manifest-path rust_impl/Cargo.toml -- --check`: Applied `cargo fmt`, then the check passed.
- `cargo clippy --manifest-path rust_impl/Cargo.toml --release -- -D warnings`: Changed PyO3 method defaults to explicitly extracted optional values; Clippy then passed.
- `.venv/bin/maturin develop --release && .venv/bin/python benchmark/verify_correctness.py`: The build succeeded; aligned Python Unicode-path percent encoding with Rust and reran validation.
- `.venv/bin/python benchmark/verify_correctness.py`: Preserved URL fragments while shuffling duplicate query parameters; all checks then passed.
