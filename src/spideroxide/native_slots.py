from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from typing import Any
from urllib.parse import urlsplit

from .http import Request, Response
from .settings import Settings
from .stats import StatsCollector

Download = Callable[[Request], Awaitable[Response]]


class NativeDownloadSlots:
    def __init__(self, settings: Settings, spider: object, stats: StatsCollector) -> None:
        try:
            from ._native import NativeDownloadSlotManager
        except ImportError as error:
            raise RuntimeError("native download slots require the Rust extension") from error

        concurrency = settings.getint("CONCURRENT_REQUESTS_PER_DOMAIN", 8)
        delay = float(getattr(spider, "download_delay", settings.getfloat("DOWNLOAD_DELAY", 0.0)))
        self._slot_settings = self._read_slot_settings(settings)
        self._stats = stats
        self._debug = settings.getbool("AUTOTHROTTLE_DEBUG", False)
        self._autothrottle_enabled = settings.getbool("AUTOTHROTTLE_ENABLED", False)
        self._logger = spider.logger
        self._manager = NativeDownloadSlotManager(
            concurrency,
            delay,
            settings.getbool("RANDOMIZE_DOWNLOAD_DELAY", True),
            settings.getbool("AUTOTHROTTLE_ENABLED", False),
            settings.getfloat("AUTOTHROTTLE_START_DELAY", 5.0),
            settings.getfloat("AUTOTHROTTLE_MAX_DELAY", 60.0),
            settings.getfloat("AUTOTHROTTLE_TARGET_CONCURRENCY", 1.0),
        )

    @staticmethod
    def _read_slot_settings(settings: Settings) -> dict[str, dict[str, object]]:
        configured = settings.getdict("DOWNLOAD_SLOTS", {})
        slots: dict[str, dict[str, object]] = {}
        for key, value in configured.items():
            if not isinstance(key, str) or not key:
                raise ValueError("DOWNLOAD_SLOTS keys must be non-empty strings")
            if not isinstance(value, Mapping):
                raise TypeError(f"DOWNLOAD_SLOTS[{key!r}] must be a mapping")
            options = dict(value)
            unknown = options.keys() - {"concurrency", "delay", "randomize_delay"}
            if unknown:
                names = ", ".join(sorted(unknown))
                raise ValueError(f"unknown DOWNLOAD_SLOTS[{key!r}] options: {names}")
            slots[key] = options
        return slots

    @staticmethod
    def slot_key(request: Request) -> str:
        configured = request.meta.get("download_slot")
        if configured is not None:
            if not isinstance(configured, str) or not configured:
                raise ValueError("request meta download_slot must be a non-empty string")
            return configured
        hostname = urlsplit(request.url).hostname
        if not hostname:
            raise ValueError(f"request URL has no hostname: {request.url!r}")
        return hostname

    async def download(self, request: Request, download: Download) -> Response:
        key = self.slot_key(request)
        options = self._slot_settings.get(key, {})
        lease = await self._manager.acquire(
            key,
            self._optional_int(options, "concurrency"),
            self._optional_float(options, "delay"),
            self._optional_bool(options, "randomize_delay"),
        )
        request.meta["download_slot"] = key
        request.meta.pop("download_latency", None)
        started = asyncio.get_running_loop().time()
        latency: float | None = None
        status: int | None = None
        try:
            response = await download(request)
            reported_latency = request.meta.get("download_latency")
            if reported_latency is not None:
                latency = float(reported_latency)
            elif not self._autothrottle_enabled:
                latency = asyncio.get_running_loop().time() - started
                request.meta["download_latency"] = latency
            status = response.status
            return response
        finally:
            self._manager.release(
                lease,
                latency,
                status,
                request.meta.get("autothrottle_dont_adjust_delay") is not True,
            )
            self._sync_stats(key)

    def _sync_stats(self, key: str) -> None:
        for name, value in self._manager.drain_stats().items():
            self._stats.inc_value(name, value)
        concurrency, active, delay, latency = self._manager.slot_state(key)
        prefix = f"downloader/slot/{key}"
        self._stats.set_value(f"{prefix}/concurrency", concurrency)
        self._stats.set_value(f"{prefix}/active", active)
        self._stats.set_value(f"{prefix}/delay", delay)
        if latency is not None:
            self._stats.set_value(f"{prefix}/latency", latency)
        if self._debug:
            self._logger.info(
                "slot: %s | conc:%2d | active:%2d | delay:%5d ms | latency:%5d ms",
                key,
                concurrency,
                active,
                delay * 1000,
                (latency or 0.0) * 1000,
            )

    def close(self) -> None:
        self._manager.close()
        for name, value in self._manager.drain_stats().items():
            self._stats.inc_value(name, value)

    @staticmethod
    def _optional_int(options: Mapping[str, object], name: str) -> int | None:
        value = options.get(name)
        return None if value is None else int(value)

    @staticmethod
    def _optional_float(options: Mapping[str, object], name: str) -> float | None:
        value = options.get(name)
        return None if value is None else float(value)

    @staticmethod
    def _optional_bool(options: Mapping[str, object], name: str) -> bool | None:
        value: Any = options.get(name)
        if value is None:
            return None
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {"1", "true", "yes", "on"}:
                return True
            if normalized in {"0", "false", "no", "off", ""}:
                return False
            raise ValueError(f"DOWNLOAD_SLOTS {name} is not a boolean: {value!r}")
        return bool(value)
