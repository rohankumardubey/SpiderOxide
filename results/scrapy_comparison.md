# SpiderOxide versus Scrapy component benchmark

This report compares SpiderOxide's Rust request-processing backend with Scrapy 2.17.0.

## Environment

* Python 3.14.6
* SpiderOxide 0.1.0
* Scrapy 2.17.0
* rustc 1.97.1 (8bab26f4f 2026-07-14)
* macOS-26.5.2-arm64-arm-64bit-Mach-O
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
| fingerprint | 10,000 | 20.423 ms | 172.717 ms | 8.46x |
| dupefilter | 10,000 | 12.935 ms | 176.490 ms | 13.64x |
| scheduler_insert | 10,000 | 24.228 ms | 233.778 ms | 9.65x |
| scheduler_remove | 10,000 | 15.710 ms | 35.689 ms | 2.27x |
| end_to_end | 10,000 | 41.700 ms | 264.216 ms | 6.34x |
| fingerprint | 100,000 | 208.984 ms | 1837.775 ms | 8.79x |
| dupefilter | 100,000 | 140.853 ms | 1876.409 ms | 13.32x |
| scheduler_insert | 100,000 | 272.259 ms | 2424.178 ms | 8.90x |
| scheduler_remove | 100,000 | 214.439 ms | 389.061 ms | 1.81x |
| end_to_end | 100,000 | 505.125 ms | 2716.902 ms | 5.38x |

A speedup above 1.0 means SpiderOxide was faster.

## Scope

This is a component benchmark. It excludes networking, response parsing, selectors, middleware, pipelines, Twisted and asyncio reactor overhead, and full crawl behavior. It must not be interpreted as an end-to-end Scrapy crawl speedup.
