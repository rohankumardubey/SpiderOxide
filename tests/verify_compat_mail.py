from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import threading
import warnings
from importlib.util import find_spec
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


async def verify_mail() -> None:
    from twisted.internet.defer import Deferred

    from spideroxide.mail import MailSender as NativeMailSender
    from spideroxide.settings import Settings

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        from scrapy.mail import MailSender

    loop = asyncio.get_running_loop()
    assert MailSender is not NativeMailSender
    assert issubclass(MailSender, NativeMailSender)
    crawler = type("Crawler", (), {"settings": Settings()})()
    assert isinstance(MailSender.from_crawler(crawler), MailSender)

    callbacks: list[dict[str, object]] = []
    assert (
        MailSender(debug=True).send(
            "to@example.test",
            "debug",
            "body",
            _callback=lambda **values: callbacks.append(values),
        )
        is None
    )
    assert callbacks[0]["to"] == ["to@example.test"]
    assert callbacks[0]["msg"]["Subject"] == "debug"

    with patch.dict(sys.modules, {"twisted.internet.defer": None}):
        assert MailSender(debug=True).send("to@example.test", "debug", "body") is None
        try:
            MailSender().send("to@example.test", "live", "body")
        except ModuleNotFoundError:
            pass
        else:
            raise AssertionError("live shim mail silently ran without Twisted")

    sent: list[tuple[list[str], bytes]] = []

    def record_send(recipients: list[str], payload: bytes) -> None:
        sent.append((recipients, payload))

    with patch.object(MailSender, "_sendmail", side_effect=record_send):
        delivery = MailSender().send("to@example.test", "live", "body", cc="cc@example.test")
        assert isinstance(delivery, Deferred)
        assert await asyncio.wait_for(delivery.asFuture(loop), 5) is None
    assert sent[0][0] == ["to@example.test", "cc@example.test"]
    assert b"Subject: live" in sent[0][1]

    with patch.object(MailSender, "_sendmail", side_effect=OSError("SMTP unavailable")):
        failure = MailSender().send("to@example.test", "failure", "body")
        assert isinstance(failure, Deferred)
        try:
            await asyncio.wait_for(failure.asFuture(loop), 5)
        except OSError as error:
            assert str(error) == "SMTP unavailable"
        else:
            raise AssertionError("mail failure did not reach the Deferred errback")

    started = threading.Event()
    release = threading.Event()

    def blocked_send(recipients: list[str], payload: bytes) -> None:
        started.set()
        if not release.wait(5):
            raise TimeoutError("SMTP mock was not released")

    with patch.object(MailSender, "_sendmail", side_effect=blocked_send):
        pending = MailSender().send("to@example.test", "cancel", "body")
        assert isinstance(pending, Deferred)
        try:
            assert await asyncio.to_thread(started.wait, 5)
            pending.cancel()
            try:
                await asyncio.wait_for(pending.asFuture(loop), 5)
            except asyncio.CancelledError:
                pass
            else:
                raise AssertionError("Deferred cancellation did not cancel the mail task")
        finally:
            release.set()

    with patch.object(NativeMailSender, "_sendmail", return_value=None):
        native_delivery = NativeMailSender().send("to@example.test", "native", "body")
        assert isinstance(native_delivery, asyncio.Task)
        assert await asyncio.wait_for(native_delivery, 5) is None


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--shim":
        asyncio.run(verify_mail())
    elif find_spec("twisted") is None:
        print("Scrapy mail Deferred parity skipped (Twisted not installed)")
    else:
        env = {
            **os.environ,
            "PYTHONPATH": os.pathsep.join(
                filter(
                    None,
                    (
                        str(ROOT / "compat" / "scrapy" / "src"),
                        str(ROOT / "src"),
                        os.environ.get("PYTHONPATH"),
                    ),
                )
            ),
            "SPIDEROXIDE_SCRAPY_COMPAT_ALLOW_CONFLICT": "1",
        }
        subprocess.run(
            [sys.executable, __file__, "--shim"],
            env=env,
            cwd=ROOT,
            check=True,
        )
        print("Scrapy mail Deferred parity passed")
