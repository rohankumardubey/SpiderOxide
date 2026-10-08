import warnings

from spideroxide.exceptions import ScrapyDeprecationWarning
from spideroxide.mail import MailSender

warnings.warn(
    "The scrapy.mail module is deprecated and will be removed in a future release. "
    "Please use a dedicated Python mail library instead.",
    ScrapyDeprecationWarning,
    stacklevel=2,
)

__all__ = ["MailSender"]
