# Performance evidence

[Back to overview](../README.md)

## Benchmarks

### SpiderOxide compared with Scrapy

SpiderOxide was compared with Scrapy 2.17.0 using real Scrapy requests,
`RFPDupeFilter`, and Scrapy's `Scheduler` configured with its standard priority queue and FIFO
in-memory queues. The SpiderOxide Rust backend and Scrapy processed the same deterministic workload
with 70,000 unique requests and 30,000 duplicates.

<table>
  <thead>
    <tr>
      <th>100,000 requests</th>
      <th>SpiderOxide</th>
      <th>Scrapy</th>
      <th>Speedup</th>
    </tr>
  </thead>
  <tbody>
    <tr>
      <td>Fingerprinting</td>
      <td>191.075 ms</td>
      <td>1,800.374 ms</td>
      <td><strong>9.42x</strong></td>
    </tr>
    <tr>
      <td>Duplicate filtering</td>
      <td>121.251 ms</td>
      <td>1,870.802 ms</td>
      <td><strong>15.43x</strong></td>
    </tr>
    <tr>
      <td>Scheduler insertion</td>
      <td>149.358 ms</td>
      <td>2,319.392 ms</td>
      <td><strong>15.53x</strong></td>
    </tr>
    <tr>
      <td>Scheduler removal</td>
      <td>69.920 ms</td>
      <td>393.972 ms</td>
      <td><strong>5.63x</strong></td>
    </tr>
    <tr>
      <td>Combined scheduler flow</td>
      <td>170.683 ms</td>
      <td>2,697.717 ms</td>
      <td><strong>15.81x</strong></td>
    </tr>
  </tbody>
</table>

At 10,000 requests, the combined scheduler flow completed in 14.188 ms for SpiderOxide and
257.629 ms for Scrapy, an 18.16x speedup.

The comparison used SpiderOxide 0.1.0, Scrapy 2.17.0, CPython 3.14.6, and Rust 1.98.1 on macOS
arm64 with 12 logical CPUs. Each value is the median of 10 measured runs after 3 warm-up runs.
Request construction was outside the timed sections, run order alternated, and garbage collection
ran before each timing.

These are component benchmarks. They exclude networking, response parsing, selectors, middleware,
pipelines, reactor overhead, and complete crawl behavior. They must not be interpreted as a 15.81x
complete-crawl speedup.

The complete 10,000 and 100,000 request results are available in the
[Scrapy comparison report](../benchmarks/results/scrapy_comparison.md),
[JSON data](../benchmarks/results/scrapy_comparison.json), and
[CSV data](../benchmarks/results/scrapy_comparison.csv).

Use the interactive benchmark launcher to install dependencies, build the native extension, and
choose a benchmark or verification suite:

```bash
./run.sh
```

For repeatable non-interactive runs:

```bash
./run.sh --task scrapy --size 100000
./run.sh --task benchmarks --size 100000
./run.sh --task all --size 100000
```

`scrapy` compares SpiderOxide with Scrapy. `benchmarks` runs the Scrapy comparison and the Python
reference comparison. `all` runs every benchmark followed by the full verification suite. Run
`./run.sh --help` for the complete task list.

### Python reference benchmark

SpiderOxide also includes the original Python reference implementation used to verify the native
algorithm in isolation. Its full results are available in the
[reference report](../benchmarks/results/report.md),
[JSON data](../benchmarks/results/results.json), and
[CSV data](../benchmarks/results/results.csv).

Run the Python reference benchmark with:

```bash
python benchmarks/run_all.py
```
