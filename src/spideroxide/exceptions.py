class SpiderOxideError(Exception):
    """Base exception for SpiderOxide."""


class ScrapyDeprecationWarning(Warning):
    """Warning category for Scrapy-compatible deprecated APIs."""


class NotConfigured(SpiderOxideError):
    """Raised when a component is intentionally disabled by configuration."""


class IgnoreRequest(SpiderOxideError):
    """Raised by middleware to discard a request."""


class DropItem(SpiderOxideError):
    """Raised by an item pipeline to discard an item."""

    def __init__(self, message: str, log_level: str | None = None) -> None:
        super().__init__(message)
        self.log_level = log_level


class DownloadError(SpiderOxideError):
    """Raised when a request cannot be downloaded."""


class DownloadFailedError(DownloadError):
    """Raised when a network request fails."""


class DownloadTimeoutError(DownloadFailedError):
    """Raised when a request exceeds its download timeout."""


class CannotResolveHostError(DownloadFailedError):
    """Raised when a target hostname cannot be resolved."""


class DownloadConnectionRefusedError(DownloadFailedError):
    """Raised when a target or proxy refuses a connection."""


class UnsupportedURLSchemeError(DownloadFailedError):
    """Raised when the transport cannot handle a URL scheme."""


class ResponseDataLossError(DownloadFailedError):
    """Raised when a response body ends before its declared length."""


class DownloadCancelledError(DownloadError):
    """Raised when downloader limits cancel a response."""


class NotSupported(SpiderOxideError):
    """Raised when no download handler supports a request URL scheme."""


class CloseSpider(SpiderOxideError):
    """Request an orderly spider shutdown."""

    def __init__(self, reason: str = "cancelled") -> None:
        self.reason = reason
        super().__init__()


class DontCloseSpider(SpiderOxideError):
    """Prevent a spider from closing while handling the idle signal."""


class StopDownload(SpiderOxideError):
    """Stop receiving a response body from a downloader signal handler."""

    def __init__(self, *, fail: bool = True) -> None:
        self.fail = fail
        self.response: object | None = None
        super().__init__()
