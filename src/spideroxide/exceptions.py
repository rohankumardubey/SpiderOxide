from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .http import Response


class SpiderOxideError(Exception):
    """Base exception for SpiderOxide."""


class ScrapyDeprecationWarning(Warning):
    """Warning category for Scrapy-compatible deprecated APIs."""


class NotConfigured(Exception):
    """Raised when a component is intentionally disabled by configuration."""


class _InvalidOutput(TypeError):
    """Raised when middleware returns invalid output."""


class IgnoreRequest(Exception):
    """Raised by middleware to discard a request."""


class DontCloseSpider(Exception):
    """Prevent a spider from closing while handling the idle signal."""


class CloseSpider(Exception):
    """Request an orderly spider shutdown."""

    def __init__(self, reason: str = "cancelled"):
        super().__init__()
        self.reason = reason


class StopDownload(Exception):
    """Stop receiving a response body from a downloader signal handler."""

    response: Response | None

    def __init__(self, *, fail: bool = True):
        super().__init__()
        self.fail = fail


class DownloadConnectionRefusedError(Exception):
    """Raised when a target or proxy refuses a connection."""


class CannotResolveHostError(Exception):
    """Raised when a target hostname cannot be resolved."""


class DownloadTimeoutError(Exception):
    """Raised when a request exceeds its download timeout."""


class DownloadCancelledError(Exception):
    """Raised when downloader limits cancel a response."""


class DownloadFailedError(Exception):
    """Raised when a network request fails."""


class ResponseDataLossError(Exception):
    """Raised when a response body ends before its declared length."""


class UnsupportedURLSchemeError(Exception):
    """Raised when the transport cannot handle a URL scheme."""


class DropItem(Exception):
    """Raised by an item pipeline to discard an item."""

    def __init__(self, message: str, log_level: str | None = None):
        super().__init__(message)
        self.log_level = log_level


class DownloadError(SpiderOxideError):
    """Raised when a request cannot be downloaded."""


class NotSupported(Exception):
    """Raised when no download handler supports a request URL scheme."""


class UsageError(Exception):
    """Raised for command-line usage errors."""

    def __init__(self, *a: Any, **kw: Any):
        self.print_help = kw.pop("print_help", True)
        super().__init__(*a, **kw)


class ContractFail(AssertionError):
    """Raised for a failing spider contract."""


# Scrapy's downloader exceptions are direct Exception subclasses; the extra
# SpiderOxide DownloadError remains supported through this internal catch group.
_DOWNLOAD_ERRORS: tuple[type[Exception], ...] = (
    DownloadError,
    DownloadConnectionRefusedError,
    CannotResolveHostError,
    DownloadTimeoutError,
    DownloadCancelledError,
    DownloadFailedError,
    ResponseDataLossError,
    UnsupportedURLSchemeError,
)
