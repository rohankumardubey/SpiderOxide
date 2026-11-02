from __future__ import annotations

import warnings
from collections.abc import Callable, Sequence
from typing import IO, TYPE_CHECKING, Any

from spideroxide.exceptions import ScrapyDeprecationWarning
from spideroxide.mail import MailSender as _NativeMailSender

if TYPE_CHECKING:
    from twisted.internet.defer import Deferred

warnings.warn(
    "The scrapy.mail module is deprecated and will be removed in a future release. "
    "Please use a dedicated Python mail library instead.",
    ScrapyDeprecationWarning,
    stacklevel=2,
)


class MailSender(_NativeMailSender):
    def send(
        self,
        to: str | list[str],
        subject: str,
        body: str,
        cc: str | list[str] | None = None,
        attachs: Sequence[tuple[str, str, IO[Any]]] = (),
        mimetype: str = "text/plain",
        charset: str | None = None,
        _callback: Callable[..., None] | None = None,
    ) -> Deferred[None] | None:  # type: ignore[override]
        if not self.debug:
            from twisted.internet.defer import Deferred

        delivery = super().send(to, subject, body, cc, attachs, mimetype, charset, _callback)
        if delivery is None:
            return None
        return Deferred.fromFuture(delivery)


__all__ = ["MailSender"]
