from __future__ import annotations

from collections.abc import Iterator

from .components import build_components


class ExtensionManager:
    def __init__(self, crawler: object, extensions: object, *, base: object = None) -> None:
        self.crawler = crawler
        self.middlewares = tuple(build_components(extensions, crawler, base=base))

    @classmethod
    def from_crawler(cls, crawler: object) -> ExtensionManager:
        settings = crawler.settings  # type: ignore[attr-defined]
        return cls(
            crawler,
            settings.get("EXTENSIONS", {}),
            base=settings.get("EXTENSIONS_BASE", {}),
        )

    def __iter__(self) -> Iterator[object]:
        return iter(self.middlewares)

    def __len__(self) -> int:
        return len(self.middlewares)

    def get_by_type(self, extension_type: type[object]) -> object | None:
        return next(
            (extension for extension in self.middlewares if isinstance(extension, extension_type)),
            None,
        )
