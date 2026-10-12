"""Asyncio download slots used by the Python engine when AutoThrottle is enabled."""

from __future__ import annotations

import asyncio
import math
import random
from collections.abc import Awaitable, Callable
from urllib.parse import urlsplit

from .http import Request, Response
from .native_slots import NativeDownloadSlots
from .settings import Settings
from .stats import StatsCollector


class PythonDownloadSlot:
    def __init__(self, concurrency: int, delay: float, randomize: bool) -> None:
        if concurrency < 1 or not math.isfinite(delay) or delay < 0:
            raise ValueError(
                "Download slot concurrency must be positive and delay finite/nonnegative"
            )
        self.concurrency = concurrency
        self.delay = delay
        self.randomize = randomize
        self.transferring: set[Request] = set()
        self.semaphore = asyncio.Semaphore(concurrency)
        self.lock = asyncio.Lock()
        self.next_start = 0.0
        self.last_start = 0.0


class PythonDownloadSlots:
    def __init__(self, crawler: object, spider: object) -> None:
        settings: Settings = crawler.settings
        self._stats: StatsCollector = crawler.stats
        self.concurrency = settings.getint("CONCURRENT_REQUESTS_PER_DOMAIN", 8)
        minimum_delay = float(
            getattr(spider, "download_delay", settings.getfloat("DOWNLOAD_DELAY"))
        )
        start_delay = settings.getfloat("AUTOTHROTTLE_START_DELAY")
        maximum_delay = settings.getfloat("AUTOTHROTTLE_MAX_DELAY")
        if (
            not all(
                math.isfinite(value) and value >= 0
                for value in (minimum_delay, start_delay, maximum_delay)
            )
            or maximum_delay < minimum_delay
        ):
            raise ValueError(
                "AutoThrottle delays must be finite/nonnegative and max >= download delay"
            )
        self._delay = max(minimum_delay, start_delay)
        self.randomize = settings.getbool("RANDOMIZE_DOWNLOAD_DELAY")
        self.configured = NativeDownloadSlots._read_slot_settings(settings)
        self.slots: dict[str, PythonDownloadSlot] = {}

    def _get_slot(self, request: Request) -> tuple[str, PythonDownloadSlot]:
        key = request.meta.get("download_slot")
        if key is None:
            key = urlsplit(request.url).hostname
        if not isinstance(key, str) or not key:
            raise ValueError("download_slot must be a non-empty hostname or slot name")
        slot = self.slots.get(key)
        if slot is None:
            options = self.configured.get(key, {})
            concurrency = NativeDownloadSlots._optional_int(options, "concurrency")
            delay = NativeDownloadSlots._optional_float(options, "delay")
            randomize = NativeDownloadSlots._optional_bool(options, "randomize_delay")
            slot = PythonDownloadSlot(
                self.concurrency if concurrency is None else concurrency,
                self._delay if delay is None else delay,
                self.randomize if randomize is None else randomize,
            )
            self.slots[key] = slot
        request.meta["download_slot"] = key
        request.meta.pop("download_latency", None)
        return key, slot

    async def download(
        self, request: Request, download: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        key, slot = self._get_slot(request)
        async with slot.semaphore:
            async with slot.lock:
                loop = asyncio.get_running_loop()
                await asyncio.sleep(max(0.0, slot.next_start - loop.time()))
                delay = random.uniform(0.5, 1.5) * slot.delay if slot.randomize else slot.delay
                slot.last_start = loop.time()
                slot.next_start = slot.last_start + delay
            slot.transferring.add(request)
            started = asyncio.get_running_loop().time()
            try:
                response = await download(request)
                latency = request.meta.get("download_latency")
                if latency is None:
                    latency = asyncio.get_running_loop().time() - started
                    request.meta["download_latency"] = latency
                self._stats.set_value(f"downloader/slot/{key}/latency", latency)
                return response
            finally:
                slot.transferring.remove(request)
                prefix = f"downloader/slot/{key}"
                self._stats.set_value(f"{prefix}/concurrency", slot.concurrency)
                self._stats.set_value(f"{prefix}/active", len(slot.transferring))
                self._stats.set_value(f"{prefix}/delay", slot.delay)
