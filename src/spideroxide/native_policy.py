from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .crawler import Crawler


def runtime_for(crawler: Crawler | None) -> object | None:
    if crawler is None:
        return None
    return crawler.native_policy_runtime


def sync_stats(crawler: Crawler) -> None:
    runtime = runtime_for(crawler)
    if runtime is None:
        return
    for key, delta in runtime.drain_stats().items():
        crawler.stats.inc_value(key, delta)
