from __future__ import annotations

import inspect
import logging
from collections.abc import Iterable, Iterator

from .components import build_component, component_references, load_object
from .exceptions import NotConfigured
from .utils import maybe_await

logger = logging.getLogger(__name__)


def _loaded_reference(reference: object) -> object:
    return load_object(reference) if isinstance(reference, str) else reference


def _required_services(reference: object) -> tuple[object, ...]:
    required = getattr(_loaded_reference(reference), "requires", ())
    if required is None:
        return ()
    if isinstance(required, (str, bytes)) or not isinstance(required, Iterable):
        required = (required,)
    return tuple(_loaded_reference(dependency) for dependency in required)


def _matches_service(candidate: object, dependency: object) -> bool:
    if inspect.isclass(dependency):
        if inspect.isclass(candidate):
            return issubclass(candidate, dependency)
        return isinstance(candidate, dependency)
    return candidate is dependency


def _ordered_services(value: object, *, base: object = None) -> list[object]:
    if base is None:
        references = component_references(value)
    else:
        from .components import merged_component_references

        references = merged_component_references(base, value)
    loaded = [_loaded_reference(reference) for reference in references]
    dependencies: list[set[int]] = []
    for index, reference in enumerate(references):
        indexes = set()
        for dependency in _required_services(reference):
            dependency_index = next(
                (
                    candidate_index
                    for candidate_index, candidate in enumerate(loaded)
                    if _matches_service(candidate, dependency)
                ),
                None,
            )
            if dependency_index is None:
                raise ValueError(
                    f"Service {loaded[index]!r} requires {dependency!r}, but it is not configured"
                )
            indexes.add(dependency_index)
        dependencies.append(indexes)

    pending = set(range(len(references)))
    ordered = []
    while pending:
        ready = [index for index in sorted(pending) if not dependencies[index] & pending]
        if not ready:
            cycle = [loaded[index] for index in sorted(pending)]
            raise ValueError(f"Service dependency cycle detected: {cycle!r}")
        for index in ready:
            pending.remove(index)
            ordered.append(references[index])
    return ordered


class ServiceManager:
    """Construct and run crawler-scoped SpiderOxide services."""

    def __init__(self, crawler: object) -> None:
        self.crawler = crawler
        self.services: list[object] = []
        self._started: list[object] = []
        self._loaded = False
        self._state = "created"

    def load_settings(self, services: object, *, base: object = None) -> None:
        if self._loaded:
            raise RuntimeError("Services have already been loaded")
        self._loaded = True
        for reference in _ordered_services(services, base=base):
            dependencies = _required_services(reference)
            missing = [
                dependency
                for dependency in dependencies
                if not any(_matches_service(service, dependency) for service in self.services)
            ]
            if missing:
                raise ValueError(
                    f"Service {_loaded_reference(reference)!r} requires enabled "
                    f"services {missing!r}"
                )
            try:
                self.services.append(build_component(reference, self.crawler))
            except NotConfigured:
                continue

    @classmethod
    def from_crawler(cls, crawler: object) -> ServiceManager:
        manager = cls(crawler)
        previous = getattr(crawler, "services", None)
        crawler.services = manager  # type: ignore[attr-defined]
        settings = crawler.settings  # type: ignore[attr-defined]
        try:
            manager.load_settings(
                settings.get("SERVICES", {}),
                base=settings.get("SERVICES_BASE", {}),
            )
        except BaseException:
            crawler.services = previous  # type: ignore[attr-defined]
            raise
        return manager

    async def start(self) -> None:
        if self._state != "created":
            raise RuntimeError("Services have already been started")
        self._state = "starting"
        try:
            for service in self.services:
                start = getattr(service, "start", None)
                if start is not None:
                    await maybe_await(start())
                self._started.append(service)
        except BaseException:
            await self._stop_started(suppress_errors=True)
            self._state = "stopped"
            raise
        self._state = "running"

    async def stop(self) -> None:
        if self._state == "stopped":
            return
        try:
            await self._stop_started(suppress_errors=False)
        finally:
            self._state = "stopped"

    async def _stop_started(self, *, suppress_errors: bool) -> None:
        first_error: BaseException | None = None
        while self._started:
            service = self._started.pop()
            stop = getattr(service, "stop", None)
            if stop is None:
                continue
            try:
                await maybe_await(stop())
            except BaseException as error:
                logger.exception("Error stopping service %r", service)
                if first_error is None:
                    first_error = error
        if first_error is not None and not suppress_errors:
            raise first_error

    def __iter__(self) -> Iterator[object]:
        return iter(self.services)

    def __len__(self) -> int:
        return len(self.services)

    def get_by_type(self, service_type: type[object]) -> object | None:
        return next(
            (service for service in self.services if isinstance(service, service_type)),
            None,
        )
