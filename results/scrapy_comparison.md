# SpiderOxide versus Scrapy component benchmark

This report compares SpiderOxide's Rust request-processing backend with Scrapy 2.17.0.

## Environment

* Python 3.14.6
* SpiderOxide 0.1.0
* Scrapy 2.17.0
* rustc 1.97.1 (8bab26f4f 2026-07-14)
* macOS-26.6.1-arm64-arm-64bit-Mach-O
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
| fingerprint | 10,000 | 20.819 ms | 180.422 ms | 8.67x |
| dupefilter | 10,000 | 13.067 ms | 181.628 ms | 13.90x |
| scheduler_insert | 10,000 | 24.614 ms | 238.118 ms | 9.67x |
| scheduler_remove | 10,000 | 16.958 ms | 38.338 ms | 2.26x |
| end_to_end | 10,000 | 42.266 ms | 271.284 ms | 6.42x |
| fingerprint | 100,000 | 211.912 ms | 1873.443 ms | 8.84x |
| dupefilter | 100,000 | 141.861 ms | 1911.053 ms | 13.47x |
| scheduler_insert | 100,000 | 272.694 ms | 2443.718 ms | 8.96x |
| scheduler_remove | 100,000 | 220.222 ms | 408.325 ms | 1.85x |
| end_to_end | 100,000 | 504.474 ms | 2743.858 ms | 5.44x |

A speedup above 1.0 means SpiderOxide was faster.

## Scope

This is a component benchmark. It excludes networking, response parsing, selectors, middleware, pipelines, Twisted and asyncio reactor overhead, and full crawl behavior. It must not be interpreted as an end-to-end Scrapy crawl speedup.
