# Python versus Rust benchmark report

## Environment

| Property | Value |
|---|---|
| Python Version | 3.14.6 |
| Python Implementation | CPython |
| Rust Version | rustc 1.98.1 (48a229cea 2026-09-01) |
| Operating System | macOS-26.6.2-arm64-arm-64bit-Mach-O |
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
| fingerprint_single | 10,000 | 88.861 ms | 11.422 ms | 7.78x |
| fingerprint_batch | 10,000 | 91.086 ms | 12.992 ms | 7.01x |
| dupefilter_single | 10,000 | 88.729 ms | 11.817 ms | 7.51x |
| dupefilter_batch | 10,000 | 90.006 ms | 13.065 ms | 6.89x |
| scheduler_insert_single | 10,000 | 95.302 ms | 13.111 ms | 7.27x |
| scheduler_insert_batch | 10,000 | 95.328 ms | 11.752 ms | 8.11x |
| scheduler_remove_single | 10,000 | 3.265 ms | 1.032 ms | 3.16x |
| scheduler_remove_batch | 10,000 | 2.461 ms | 0.606 ms | 4.06x |
| end_to_end_single | 10,000 | 99.457 ms | 14.188 ms | 7.01x |
| end_to_end_batch | 10,000 | 98.977 ms | 12.350 ms | 8.01x |
| fingerprint_single | 100,000 | 878.954 ms | 129.256 ms | 6.80x |
| fingerprint_batch | 100,000 | 926.323 ms | 155.298 ms | 5.96x |
| dupefilter_single | 100,000 | 929.473 ms | 136.749 ms | 6.80x |
| dupefilter_batch | 100,000 | 921.467 ms | 153.586 ms | 6.00x |
| scheduler_insert_single | 100,000 | 950.682 ms | 146.310 ms | 6.50x |
| scheduler_insert_batch | 100,000 | 965.086 ms | 130.538 ms | 7.39x |
| scheduler_remove_single | 100,000 | 52.171 ms | 17.144 ms | 3.04x |
| scheduler_remove_batch | 100,000 | 41.105 ms | 10.202 ms | 4.03x |
| end_to_end_single | 100,000 | 1028.733 ms | 163.368 ms | 6.30x |
| end_to_end_batch | 100,000 | 1007.856 ms | 149.713 ms | 6.73x |
| fingerprint_single | 1,000,000 | 8839.833 ms | 1345.579 ms | 6.57x |
| fingerprint_batch | 1,000,000 | 8863.580 ms | 1630.382 ms | 5.44x |
| dupefilter_single | 1,000,000 | 9150.947 ms | 1428.898 ms | 6.40x |
| dupefilter_batch | 1,000,000 | 8890.739 ms | 1653.340 ms | 5.38x |
| scheduler_insert_single | 1,000,000 | 10351.096 ms | 1552.534 ms | 6.67x |
| scheduler_insert_batch | 1,000,000 | 10097.284 ms | 1412.311 ms | 7.15x |
| scheduler_remove_single | 1,000,000 | 1411.942 ms | 462.049 ms | 3.06x |
| scheduler_remove_batch | 1,000,000 | 1232.215 ms | 293.359 ms | 4.20x |
| end_to_end_single | 1,000,000 | 11877.413 ms | 2016.847 ms | 5.89x |
| end_to_end_batch | 1,000,000 | 12396.837 ms | 1746.734 ms | 7.10x |

Full mean, minimum, maximum, standard deviation, throughput, and per-request values are in `results.json` and `results.csv`.

## Peak process memory

| Implementation | Mode | Size | Peak process memory |
|---|---|---:|---:|
| python | single | 10,000 | 66.56 MiB |
| python | batch | 10,000 | 66.73 MiB |
| rust | single | 10,000 | 66.28 MiB |
| rust | batch | 10,000 | 68.92 MiB |
| python | single | 100,000 | 107.83 MiB |
| python | batch | 100,000 | 109.25 MiB |
| rust | single | 100,000 | 118.95 MiB |
| rust | batch | 100,000 | 137.95 MiB |
| python | single | 1,000,000 | 553.55 MiB |
| python | batch | 1,000,000 | 556.36 MiB |
| rust | single | 1,000,000 | 599.47 MiB |
| rust | batch | 1,000,000 | 783.47 MiB |

Memory is the process high-water mark from a fresh subprocess and includes the interpreter, extension, and generated dataset. It is not an allocator-only measurement.

## Interpretation

Fingerprint PyO3 overhead comparison (10,000: single 7.78x versus batch 7.01x; 100,000: single 6.80x versus batch 5.96x; 1,000,000: single 6.57x versus batch 5.44x). Mean fingerprint-single speedup was 7.05x. Rust fingerprint batching changed median time by 10,000: +13.7%, 100,000: +20.1%, 1,000,000: +21.2% relative to Rust single calls; bulk tuple conversion and output-list materialization outweighed the removed calls in this API shape.

Mean end-to-end batch speedup was 7.28x. The principal Python costs are URL parsing/canonicalization, hashing, and heap operations; Rust batch execution keeps those loops native.

## Limitations

- This is a synthetic, in-memory workload and excludes Scrapy integration, persistence, network I/O, callbacks, and concurrency.
- Canonicalization accepts absolute hierarchical URLs. It does not attempt Scrapy's full escaping behavior, internationalized-path policy, semicolon-parameter handling, or scheme-specific normalization.
- Executed sizes: 10,000, 100,000, 1,000,000. All specified dataset sizes were executed.
- Peak RSS is a subprocess high-water mark, so small differences include startup noise.

## Recommendation

The synthetic results justify a Scrapy-level Rust integration experiment, but not an immediate production replacement. Confirm the speedup under real callback, persistence, and concurrency workloads, and evaluate the observed Rust batch-memory increase.

## Commands executed

```text
python3 -m venv .venv
```
```text
.venv/bin/python -m pip install --upgrade pip
```
```text
.venv/bin/python -m pip install -r requirements.txt
```
```text
.venv/bin/python -m ruff format .
```
```text
.venv/bin/python -m ruff check .
```
```text
cargo fmt --manifest-path crates/spideroxide-native/Cargo.toml -- --check
```
```text
cargo clippy --manifest-path crates/spideroxide-native/Cargo.toml --release -- -D warnings
```
```text
.venv/bin/maturin develop --release
```
```text
.venv/bin/python tests/verify_correctness.py
```
```text
python benchmarks/run_all.py --sizes 10000 100000 1000000 --warmups 3 --runs 10
```

## Failed commands and resolutions

None.
