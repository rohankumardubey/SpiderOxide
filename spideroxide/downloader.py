from __future__ import annotations

from typing import Protocol

import httpx

from .exceptions import DownloadError
from .headers import Headers
from .http import Request, Response, TextResponse
from .settings import Settings


class Downloader(Protocol):
    async def fetch(self, request: Request) -> Response: ...


def _textual_response(headers: Headers) -> bool:
    content_type = headers.get("Content-Type", b"").decode("latin-1").lower()
    return content_type.startswith("text/") or any(
        marker in content_type for marker in ("json", "xml", "javascript")
    )


class HttpxDownloader:
    def __init__(
        self,
        settings: Settings | None = None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.settings = settings or Settings()
        timeout = self.settings.getfloat("DOWNLOAD_TIMEOUT", 180.0)
        user_agent = str(self.settings.get("USER_AGENT", "SpiderOxide/0.1"))
        self.max_size = self.settings.getint("DOWNLOAD_MAXSIZE", 0)
        self.client = httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=True,
            headers={"User-Agent": user_agent} if user_agent else None,
            transport=transport,
        )

    async def fetch(self, request: Request) -> Response:
        try:
            async with self.client.stream(
                request.method,
                request.url,
                headers=request.headers.to_http_pairs(),
                content=request.body or None,
                cookies=request.cookies,
            ) as raw_response:
                declared_size = int(raw_response.headers.get("Content-Length", 0))
                if self.max_size and declared_size > self.max_size:
                    raise DownloadError(
                        f"response exceeded DOWNLOAD_MAXSIZE ({self.max_size} bytes)"
                    )
                body = bytearray()
                async for chunk in raw_response.aiter_bytes():
                    body.extend(chunk)
                    if self.max_size and len(body) > self.max_size:
                        raise DownloadError(
                            f"response exceeded DOWNLOAD_MAXSIZE ({self.max_size} bytes)"
                        )

                headers = Headers()
                for name, value in raw_response.headers.multi_items():
                    headers.appendlist(name, value)
                response_type = TextResponse if _textual_response(headers) else Response
                return response_type(
                    url=str(raw_response.url),
                    status=raw_response.status_code,
                    headers=headers,
                    body=bytes(body),
                    request=request,
                    protocol=raw_response.http_version,
                )
        except httpx.HTTPError as error:
            raise DownloadError(f"unable to download {request.url}: {error}") from error

    async def close(self) -> None:
        await self.client.aclose()


UrllibDownloader = HttpxDownloader
