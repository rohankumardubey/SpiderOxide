from __future__ import annotations

import asyncio
import logging
import smtplib
import ssl
from collections.abc import Callable, Sequence
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.nonmultipart import MIMENonMultipart
from email.mime.text import MIMEText
from email.utils import formatdate
from typing import IO, Any

logger = logging.getLogger(__name__)


def _addresses(value: str | list[str] | None) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    return list(value)


class MailSender:
    def __init__(
        self,
        smtphost: str = "localhost",
        mailfrom: str = "scrapy@localhost",
        smtpuser: str | None = None,
        smtppass: str | None = None,
        smtpport: int = 25,
        smtptls: bool = False,
        smtpssl: bool = False,
        debug: bool = False,
    ) -> None:
        self.smtphost = smtphost
        self.mailfrom = mailfrom
        self.smtpuser = None if smtpuser is None else smtpuser.encode()
        self.smtppass = None if smtppass is None else smtppass.encode()
        self.smtpport = smtpport
        self.smtptls = smtptls
        self.smtpssl = smtpssl
        self.debug = debug

    @classmethod
    def from_crawler(cls, crawler: object) -> MailSender:
        settings = crawler.settings
        return cls(
            smtphost=settings["MAIL_HOST"],
            mailfrom=settings["MAIL_FROM"],
            smtpuser=settings["MAIL_USER"],
            smtppass=settings["MAIL_PASS"],
            smtpport=settings.getint("MAIL_PORT"),
            smtptls=settings.getbool("MAIL_TLS"),
            smtpssl=settings.getbool("MAIL_SSL"),
        )

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
    ) -> asyncio.Task[None] | None:
        msg: MIMEBase = MIMEMultipart() if attachs else MIMENonMultipart(*mimetype.split("/", 1))
        recipients = _addresses(to)
        copies = _addresses(cc)
        msg["From"] = self.mailfrom
        msg["To"] = ", ".join(recipients)
        msg["Date"] = formatdate(localtime=True)
        msg["Subject"] = subject
        if copies:
            msg["Cc"] = ", ".join(copies)
        if attachs:
            if charset:
                msg.set_charset(charset)
            msg.attach(MIMEText(body, "plain", charset or "us-ascii"))
            for name, content_type, stream in attachs:
                part = MIMEBase(*content_type.split("/", 1))
                part.set_payload(stream.read())
                encoders.encode_base64(part)
                part.add_header("Content-Disposition", "attachment", filename=name)
                msg.attach(part)
        else:
            msg.set_payload(body, charset)

        if _callback is not None:
            _callback(
                to=recipients,
                subject=subject,
                body=body,
                cc=copies,
                attach=attachs,
                msg=msg,
            )
        details = {
            "mailto": recipients,
            "mailcc": copies,
            "mailsubject": subject,
            "mailattachs": len(attachs),
        }
        if self.debug:
            logger.debug(
                'Debug mail sent OK: To=%(mailto)s Cc=%(mailcc)s Subject="%(mailsubject)s" '
                "Attachs=%(mailattachs)d",
                details,
            )
            return None

        async def deliver() -> None:
            try:
                await asyncio.to_thread(
                    self._sendmail,
                    [*recipients, *copies],
                    msg.as_string().encode(charset or "utf-8"),
                )
            except Exception as error:
                logger.error(
                    'Unable to send mail: To=%(mailto)s Cc=%(mailcc)s Subject="%(mailsubject)s" '
                    "Attachs=%(mailattachs)d- %(mailerr)s",
                    {**details, "mailerr": str(error)},
                )
                raise
            logger.info(
                'Mail sent OK: To=%(mailto)s Cc=%(mailcc)s Subject="%(mailsubject)s" '
                "Attachs=%(mailattachs)d",
                details,
            )

        return asyncio.get_running_loop().create_task(deliver())

    def _sendmail(self, recipients: list[str], message: bytes) -> None:
        smtp_class = smtplib.SMTP_SSL if self.smtpssl else smtplib.SMTP
        options = {"context": ssl.create_default_context()} if self.smtpssl else {}
        with smtp_class(self.smtphost, self.smtpport, **options) as connection:
            if self.smtptls and not self.smtpssl:
                connection.starttls(context=ssl.create_default_context())
            if self.smtpuser is not None:
                connection.login(self.smtpuser.decode(), (self.smtppass or b"").decode())
            connection.sendmail(self.mailfrom, recipients, message)
