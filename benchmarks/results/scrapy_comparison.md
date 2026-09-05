# SpiderOxide versus Scrapy component benchmark

This report compares SpiderOxide's Rust request-processing backend with Scrapy 2.17.0.

## Environment

* Python 3.14.6
* SpiderOxide 0.1.0
* Scrapy 2.17.0
* rustc 1.98.1 (48a229cea 2026-09-01)
* macOS-26.6.2-arm64-arm-64bit-Mach-O
* arm64, 12 logical CPUs
* SpiderOxide backend: Rust release

## Methodology

* 3 warm-up runs
* 10 measured runs
* alternating execution order
* garbage collection before each timing
* request construction outside timed sections
* Scrapy FIFO in-memory queues configured to match SpiderOxide ordering

| Test | Requests | SpiderOxide median | Scrapy median | Speedup |
|---|---:|---:|---:|---:|
| fingerprint | 10,000 | 19.130 ms | 173.531 ms | 9.07x |
| dupefilter | 10,000 | 11.417 ms | 175.327 ms | 15.36x |
| scheduler_insert | 10,000 | 14.449 ms | 237.953 ms | 16.47x |
| scheduler_remove | 10,000 | 3.496 ms | 36.467 ms | 10.43x |
| end_to_end | 10,000 | 14.188 ms | 257.629 ms | 18.16x |
| fingerprint | 100,000 | 191.075 ms | 1800.374 ms | 9.42x |
| dupefilter | 100,000 | 121.251 ms | 1870.802 ms | 15.43x |
| scheduler_insert | 100,000 | 149.358 ms | 2319.392 ms | 15.53x |
| scheduler_remove | 100,000 | 69.920 ms | 393.972 ms | 5.63x |
| end_to_end | 100,000 | 170.683 ms | 2697.717 ms | 15.81x |

A speedup above 1.0 means SpiderOxide was faster.

## Scope

This is a component benchmark. It excludes networking, response parsing, selectors, middleware, pipelines, Twisted and asyncio reactor overhead, and full crawl behavior. It must not be interpreted as an end-to-end Scrapy crawl speedup.
