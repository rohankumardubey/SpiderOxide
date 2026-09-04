class SpiderOxideError(Exception):
    """Base exception for SpiderOxide."""


class NotConfigured(SpiderOxideError):
    """Raised when a component is intentionally disabled by configuration."""


class IgnoreRequest(SpiderOxideError):
    """Raised by middleware to discard a request."""


class DropItem(SpiderOxideError):
    """Raised by an item pipeline to discard an item."""


class DownloadError(SpiderOxideError):
    """Raised when a request cannot be downloaded."""


class NotSupported(SpiderOxideError):
    """Raised when no download handler supports a request URL scheme."""


class CloseSpider(SpiderOxideError):
    """Request an orderly spider shutdown."""

    def __init__(self, reason: str = "cancelled") -> None:
        self.reason = reason
        super().__init__(reason)
