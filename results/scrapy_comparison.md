# SpiderOxide versus Scrapy component benchmark

This report compares SpiderOxide's Rust request-processing backend with Scrapy 2.17.0.

## Environment

* Python 3.14.6
* SpiderOxide 0.1.0
* Scrapy 2.17.0
* rustc 1.97.1 (8bab26f4f 2026-07-14)
* macOS-26.6.2-arm64-arm-64bit-Mach-O
* arm64, 12 logical CPUs
* SpiderOxide backend: Rust release

## Methodology

* 2 warm-up runs
* 3 measured runs
* alternating execution order
* garbage collection before each timing
* request construction outside timed sections
* Scrapy FIFO in-memory queues configured to match SpiderOxide ordering

| Test | Requests | SpiderOxide median | Scrapy median | Speedup |
|---|---:|---:|---:|---:|
| fingerprint | 100,000 | 199.235 ms | 1711.598 ms | 8.59x |
| dupefilter | 100,000 | 125.842 ms | 1796.282 ms | 14.27x |
| scheduler_insert | 100,000 | 158.372 ms | 2280.390 ms | 14.40x |
| scheduler_remove | 100,000 | 67.064 ms | 369.942 ms | 5.52x |
| end_to_end | 100,000 | 173.051 ms | 2591.497 ms | 14.98x |

A speedup above 1.0 means SpiderOxide was faster.

## Scope

This is a component benchmark. It excludes networking, response parsing, selectors, middleware, pipelines, Twisted and asyncio reactor overhead, and full crawl behavior. It must not be interpreted as an end-to-end Scrapy crawl speedup.
