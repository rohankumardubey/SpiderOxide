from __future__ import annotations

from collections.abc import Iterator, Mapping, MutableMapping
from dataclasses import dataclass
from typing import Any

PRIORITIES = {
    "default": 0,
    "project": 20,
    "spider": 30,
    "command": 40,
}

DEFAULT_SETTINGS: dict[str, object] = {
    "CONCURRENT_REQUESTS": 16,
    "CONCURRENT_REQUESTS_PER_DOMAIN": 8,
    "ENGINE_BACKEND": "python",
    "ENGINE_MAX_PENDING": 0,
    "DOWNLOAD_DELAY": 0.0,
    "RANDOMIZE_DOWNLOAD_DELAY": True,
    "DOWNLOAD_SLOTS": {},
    "DOWNLOAD_TIMEOUT": 180.0,
    "DOWNLOAD_MAXSIZE": 0,
    "DOWNLOADER_BACKEND": "python",
    "USER_AGENT": "SpiderOxide/0.1",
    "DOWNLOADER_MIDDLEWARES_BASE": {
        "spideroxide.robots.RobotsTxtMiddleware": 100,
        "spideroxide.retry.RetryMiddleware": 550,
        "spideroxide.redirect.RedirectMiddleware": 600,
        "spideroxide.downloadermiddlewares.DownloaderStatsMiddleware": 850,
    },
    "DOWNLOADER_MIDDLEWARES": {},
    "DOWNLOADER_STATS": True,
    "SPIDER_MIDDLEWARES_BASE": {
        "spideroxide.depth.DepthMiddleware": 900,
    },
    "SPIDER_MIDDLEWARES": [],
    "ITEM_PIPELINES": [],
    "DEPTH_LIMIT": 0,
    "DEPTH_PRIORITY": 0,
    "DEPTH_STATS_VERBOSE": False,
    "RETRY_ENABLED": True,
    "RETRY_TIMES": 2,
    "RETRY_HTTP_CODES": [500, 502, 503, 504, 522, 524, 408, 429],
    "RETRY_PRIORITY_ADJUST": -1,
    "RETRY_EXCEPTIONS": ["spideroxide.exceptions.DownloadError"],
    "RETRY_GIVE_UP_LOG_LEVEL": "ERROR",
    "ROBOTSTXT_OBEY": False,
    "ROBOTSTXT_USER_AGENT": None,
    "REDIRECT_ENABLED": True,
    "REDIRECT_MAX_TIMES": 20,
    "REDIRECT_PRIORITY_ADJUST": 2,
    "AUTOTHROTTLE_ENABLED": False,
    "AUTOTHROTTLE_START_DELAY": 5.0,
    "AUTOTHROTTLE_MAX_DELAY": 60.0,
    "AUTOTHROTTLE_TARGET_CONCURRENCY": 1.0,
    "AUTOTHROTTLE_DEBUG": False,
}


@dataclass(slots=True)
class Setting:
    value: object
    priority: int


class Settings(MutableMapping[str, object]):
    def __init__(
        self,
        values: Mapping[str, object] | None = None,
        *,
        include_defaults: bool = True,
    ) -> None:
        self._values: dict[str, Setting] = {}
        self._frozen = False
        if include_defaults:
            self.update_values(DEFAULT_SETTINGS, priority="default")
        if values:
            self.update_values(values, priority="project")

    @staticmethod
    def _priority(value: int | str) -> int:
        if isinstance(value, int):
            return value
        try:
            return PRIORITIES[value]
        except KeyError as exc:
            raise ValueError(f"unknown settings priority: {value!r}") from exc

    def set(self, name: str, value: object, priority: int | str = "project") -> None:
        if self._frozen:
            raise TypeError("settings are frozen")
        numeric_priority = self._priority(priority)
        current = self._values.get(name)
        if current is None or numeric_priority >= current.priority:
            self._values[name] = Setting(value, numeric_priority)

    def update_values(
        self,
        values: Mapping[str, object],
        priority: int | str = "project",
    ) -> None:
        for name, value in values.items():
            self.set(name, value, priority)

    def freeze(self) -> None:
        self._frozen = True

    @property
    def frozen(self) -> bool:
        return self._frozen

    def getbool(self, name: str, default: bool = False) -> bool:
        value = self.get(name, default)
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {"1", "true", "yes", "on"}:
                return True
            if normalized in {"0", "false", "no", "off", ""}:
                return False
            raise ValueError(f"setting {name} is not a boolean: {value!r}")
        return bool(value)

    def getint(self, name: str, default: int = 0) -> int:
        return int(self.get(name, default))

    def getfloat(self, name: str, default: float = 0.0) -> float:
        return float(self.get(name, default))

    def getlist(self, name: str, default: list[object] | None = None) -> list[object]:
        value = self.get(name, default or [])
        if isinstance(value, str):
            return [part.strip() for part in value.split(",") if part.strip()]
        return list(value)  # type: ignore[arg-type]

    def getdict(self, name: str, default: Mapping[str, Any] | None = None) -> dict[str, Any]:
        value = self.get(name, default or {})
        if not isinstance(value, Mapping):
            raise TypeError(f"setting {name} is not a mapping")
        return dict(value)

    def __getitem__(self, name: str) -> object:
        return self._values[name].value

    def __setitem__(self, name: str, value: object) -> None:
        self.set(name, value)

    def __delitem__(self, name: str) -> None:
        if self._frozen:
            raise TypeError("settings are frozen")
        del self._values[name]

    def __iter__(self) -> Iterator[str]:
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)

    def copy(self) -> Settings:
        copied = Settings(include_defaults=False)
        copied._values = {
            name: Setting(attribute.value, attribute.priority)
            for name, attribute in self._values.items()
        }
        return copied
