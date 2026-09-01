from __future__ import annotations

import functools
import hashlib
from collections.abc import Iterable
from contextlib import suppress
from io import BytesIO
from typing import TYPE_CHECKING, Any

from itemadapter import ItemAdapter

from ..exceptions import NotConfigured
from ..http import Request, Response
from ..utils import maybe_await
from .files import FileException, FilesPipeline
from .media import FileInfo, MediaPipeline

if TYPE_CHECKING:
    from os import PathLike

    from PIL.Image import Image

    from ..crawler import Crawler


class ImageException(FileException):
    """General image processing error."""


class ImagesPipeline(FilesPipeline):
    MEDIA_NAME = "image"
    MIN_WIDTH = 0
    MIN_HEIGHT = 0
    EXPIRES = 90
    THUMBS: dict[str, tuple[int, int]] = {}
    DEFAULT_IMAGES_URLS_FIELD = "image_urls"
    DEFAULT_IMAGES_RESULT_FIELD = "images"

    def __init__(
        self,
        store_uri: str | PathLike[str],
        download_func: object = None,
        *,
        crawler: Crawler,
    ) -> None:
        try:
            from PIL import Image, ImageOps
        except ImportError:
            raise NotConfigured(
                "ImagesPipeline requires Pillow; install spideroxide[images]"
            ) from None
        self._Image = Image
        self._ImageOps = ImageOps
        super().__init__(store_uri, download_func, crawler=crawler)
        resolve = functools.partial(self._key_for_pipe, base_class_name="ImagesPipeline")
        self.expires = crawler.settings.getint(resolve("IMAGES_EXPIRES"), self.EXPIRES)
        urls_field = getattr(self, "IMAGES_URLS_FIELD", self.DEFAULT_IMAGES_URLS_FIELD)
        result_field = getattr(self, "IMAGES_RESULT_FIELD", self.DEFAULT_IMAGES_RESULT_FIELD)
        self.images_urls_field = str(crawler.settings.get(resolve("IMAGES_URLS_FIELD"), urls_field))
        self.images_result_field = str(
            crawler.settings.get(resolve("IMAGES_RESULT_FIELD"), result_field)
        )
        self.min_width = crawler.settings.getint(resolve("IMAGES_MIN_WIDTH"), self.MIN_WIDTH)
        self.min_height = crawler.settings.getint(resolve("IMAGES_MIN_HEIGHT"), self.MIN_HEIGHT)
        self.thumbs = dict(crawler.settings.get(resolve("IMAGES_THUMBS"), self.THUMBS))

    @classmethod
    def from_crawler(cls, crawler: Crawler) -> ImagesPipeline:
        return cls(crawler.settings.get("IMAGES_STORE"), crawler=crawler)

    async def file_downloaded(
        self,
        response: Response,
        request: Request,
        info: MediaPipeline.SpiderInfo,
        *,
        item: Any = None,
    ) -> str:
        return await self.image_downloaded(response, request, info, item=item)

    async def image_downloaded(
        self,
        response: Response,
        request: Request,
        info: MediaPipeline.SpiderInfo,
        *,
        item: Any = None,
    ) -> str:
        checksum: str | None = None
        for path, image, content in self.get_images(
            response,
            request,
            info,
            item=item,
        ):
            persisted = await maybe_await(
                self.store.persist_file(
                    path,
                    content,
                    info,
                    meta={"width": image.width, "height": image.height},
                    headers={"Content-Type": "image/jpeg"},
                )
            )
            if checksum is None:
                checksum = (
                    persisted
                    if isinstance(persisted, str)
                    else hashlib.md5(content.getvalue()).hexdigest()  # noqa: S324
                )
        if checksum is None:
            raise ImageException("image conversion produced no output")
        return checksum

    def get_images(
        self,
        response: Response,
        request: Request,
        info: MediaPipeline.SpiderInfo,
        *,
        item: Any = None,
    ) -> Iterable[tuple[str, Image, BytesIO]]:
        path = self.file_path(request, response=response, info=info, item=item)
        original_body = BytesIO(response.body)
        original = self._Image.open(original_body)
        image = self._ImageOps.exif_transpose(original)
        width, height = image.size
        if width < self.min_width or height < self.min_height:
            raise ImageException(
                f"Image too small ({width}x{height} < {self.min_width}x{self.min_height})"
            )

        converted, content = self.convert_image(
            image,
            response_body=BytesIO(response.body),
        )
        yield path, converted, content
        for thumb_id, size in self.thumbs.items():
            thumb_path = self.thumb_path(
                request,
                thumb_id,
                response=response,
                info=info,
                item=item,
            )
            thumb, thumb_content = self.convert_image(
                converted,
                tuple(size),
                response_body=content,
            )
            yield thumb_path, thumb, thumb_content

    def convert_image(
        self,
        image: Image,
        size: tuple[int, int] | None = None,
        *,
        response_body: BytesIO,
    ) -> tuple[Image, BytesIO]:
        if image.format in {"PNG", "WEBP"} and image.mode == "RGBA":
            background = self._Image.new("RGBA", image.size, (255, 255, 255))
            background.paste(image, image)
            image = background.convert("RGB")
        elif image.mode == "P":
            image = image.convert("RGBA")
            background = self._Image.new("RGBA", image.size, (255, 255, 255))
            background.paste(image, image)
            image = background.convert("RGB")
        elif image.mode != "RGB":
            image = image.convert("RGB")

        if size:
            image = image.copy()
            image.thumbnail(size, self._Image.Resampling.LANCZOS)
        elif image.format == "JPEG":
            return image, response_body

        output = BytesIO()
        image.save(output, "JPEG")
        return image, output

    def get_media_requests(
        self,
        item: Any,
        info: MediaPipeline.SpiderInfo,
    ) -> list[Request]:
        del info
        urls = ItemAdapter(item).get(self.images_urls_field, [])
        if not isinstance(urls, list):
            raise TypeError(
                f"{self.images_urls_field} must be a list of URLs, got {type(urls).__name__}."
            )
        return [Request(url) for url in urls]

    def item_completed(
        self,
        results: list[tuple[bool, FileInfo | BaseException]],
        item: Any,
        info: MediaPipeline.SpiderInfo,
    ) -> Any:
        MediaPipeline.item_completed(self, results, item, info)
        with suppress(KeyError):
            ItemAdapter(item)[self.images_result_field] = [
                value for success, value in results if success
            ]
        return item

    def file_path(
        self,
        request: Request,
        response: Response | None = None,
        info: MediaPipeline.SpiderInfo | None = None,
        *,
        item: Any = None,
    ) -> str:
        del response, info, item
        image_guid = hashlib.sha1(request.url.encode()).hexdigest()  # noqa: S324
        return f"full/{image_guid}.jpg"

    def thumb_path(
        self,
        request: Request,
        thumb_id: str,
        response: Response | None = None,
        info: MediaPipeline.SpiderInfo | None = None,
        *,
        item: Any = None,
    ) -> str:
        del response, info, item
        image_guid = hashlib.sha1(request.url.encode()).hexdigest()  # noqa: S324
        return f"thumbs/{thumb_id}/{image_guid}.jpg"
