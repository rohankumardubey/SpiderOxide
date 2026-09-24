# SpiderOxide Scrapy compatibility

`spideroxide-scrapy-compat` exposes SpiderOxide through documented Scrapy 2.19 import paths.
Install it instead of the upstream `Scrapy` distribution when running a project on SpiderOxide:

```bash
python -m pip uninstall Scrapy
python -m pip install spideroxide spideroxide-scrapy-compat
```

The compatibility distribution and upstream Scrapy both own the top-level `scrapy` package and
must not be installed into the same environment. Use separate environments when running
differential tests. The shim detects an installed upstream `Scrapy` distribution and raises an
explicit error rather than loading a mixed namespace. Repository differential tests may set
`SPIDEROXIDE_SCRAPY_COMPAT_ALLOW_CONFLICT=1` because they control `sys.path` and process isolation.

The shim covers APIs already implemented by SpiderOxide, including requests and responses, items,
selectors, loaders, spiders and crawl rules, settings, signals, middleware, download handlers,
pipelines, exporters, feed storage, cache policies, extensions, request fingerprints, and common
project utilities. Missing APIs remain explicit and are tracked in the main compatibility roadmap;
the shim does not silently substitute incomplete behavior.

Build the compatibility wheel independently from the repository root:

```bash
python -m pip wheel --no-deps ./compat/scrapy
```
