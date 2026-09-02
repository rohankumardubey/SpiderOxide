from __future__ import annotations

from bz2 import BZ2File
from gzip import GzipFile
from io import IOBase
from lzma import LZMAFile
from typing import Any, BinaryIO

from .components import load_object


class GzipPlugin:
    def __init__(self, file: BinaryIO, feed_options: dict[str, Any]) -> None:
        self.file = file
        self.feed_options = feed_options
        self.gzipfile = GzipFile(
            fileobj=file,
            mode="wb",
            compresslevel=feed_options.get("gzip_compresslevel", 9),
            mtime=feed_options.get("gzip_mtime"),
            filename=feed_options.get("gzip_filename"),
        )

    def write(self, data: bytes) -> int:
        return self.gzipfile.write(data)

    def close(self) -> None:
        self.gzipfile.close()


class Bz2Plugin:
    def __init__(self, file: BinaryIO, feed_options: dict[str, Any]) -> None:
        self.file = file
        self.feed_options = feed_options
        self.bz2file = BZ2File(
            filename=file,
            mode="wb",
            compresslevel=feed_options.get("bz2_compresslevel", 9),
        )

    def write(self, data: bytes) -> int:
        return self.bz2file.write(data)

    def close(self) -> None:
        self.bz2file.close()


class LZMAPlugin:
    def __init__(self, file: BinaryIO, feed_options: dict[str, Any]) -> None:
        self.file = file
        self.feed_options = feed_options
        self.lzmafile = LZMAFile(
            filename=file,
            mode="wb",
            format=feed_options.get("lzma_format"),
            check=feed_options.get("lzma_check", -1),
            preset=feed_options.get("lzma_preset"),
            filters=feed_options.get("lzma_filters"),
        )

    def write(self, data: bytes) -> int:
        return self.lzmafile.write(data)

    def close(self) -> None:
        self.lzmafile.close()


class PostProcessingManager(IOBase):
    def __init__(
        self,
        plugins: list[object],
        file: BinaryIO,
        feed_options: dict[str, Any],
    ) -> None:
        self.plugins = [
            load_object(plugin) if isinstance(plugin, str) else plugin for plugin in plugins
        ]
        self.file = file
        self.feed_options = feed_options
        previous: object = file
        instances = []
        for plugin in reversed(self.plugins):
            if not callable(plugin):
                raise TypeError(f"feed postprocessing plugin is not callable: {plugin!r}")
            instance = plugin(previous, feed_options)
            instances.append(instance)
            previous = instance
        self._plugin_instances = list(reversed(instances))
        self.head_plugin = previous

    def write(self, data: bytes) -> int:
        return self.head_plugin.write(data)  # type: ignore[no-any-return, union-attr]

    def tell(self) -> int:
        return self.file.tell()

    def close(self) -> None:
        if self.closed:
            return
        failure: Exception | None = None
        try:
            for plugin in self._plugin_instances:
                try:
                    plugin.close()
                except Exception as error:
                    if failure is None:
                        failure = error
        finally:
            super().close()
        if failure is not None:
            raise failure

    def writable(self) -> bool:
        return True
