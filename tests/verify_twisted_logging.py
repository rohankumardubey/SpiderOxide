from __future__ import annotations

import logging
import os
import subprocess
import sys
import warnings
from contextlib import redirect_stderr
from importlib.util import find_spec
from io import StringIO
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


class CaptureHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


def verify_native_logging() -> None:
    from spideroxide.logutils import configure_logging

    if find_spec("twisted") is None:
        configure_logging({"LOG_INSTALL_ROOT_HANDLER": False})
        return

    from twisted.python import log as twisted_log

    before = tuple(twisted_log.theLogPublisher.observers)
    configure_logging({"LOG_INSTALL_ROOT_HANDLER": False})
    assert tuple(twisted_log.theLogPublisher.observers) == before


def verify_scrapy_logging() -> None:
    from twisted.python import log as twisted_log

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        from scrapy.utils.log import configure_logging

    before = tuple(twisted_log.theLogPublisher.observers)
    settings = {"LOG_INSTALL_ROOT_HANDLER": False}
    with patch("scrapy._compat.find_spec", return_value=None):
        configure_logging(settings)
    assert tuple(twisted_log.theLogPublisher.observers) == before

    configure_logging(settings)
    assert len(twisted_log.theLogPublisher.observers) == len(before) + 1
    observer = twisted_log.theLogPublisher.observers[-1]
    configure_logging(settings)
    assert twisted_log.theLogPublisher.observers[-1] is observer
    assert len(twisted_log.theLogPublisher.observers) == len(before) + 1

    logger = logging.getLogger("twisted")
    handler = CaptureHandler()
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    try:
        twisted_log.msg("bridged legacy event", logLevel=logging.WARNING)
        with redirect_stderr(StringIO()):
            twisted_log.err(ValueError("bridged failure"))
        assert len(handler.records) == 2, handler.records
        assert handler.records[0].name == "twisted"
        assert handler.records[0].levelno == logging.WARNING
        assert "bridged legacy event" in handler.records[0].getMessage()
        assert handler.records[1].levelno == logging.CRITICAL
        assert "bridged failure" in handler.records[1].getMessage()
    finally:
        logger.removeHandler(handler)
        twisted_log.removeObserver(observer)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--shim":
        verify_scrapy_logging()
    else:
        verify_native_logging()
        if find_spec("twisted") is not None:
            environment = {
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
                cwd=ROOT,
                env=environment,
                check=True,
            )
        print("Twisted logging bridge passed")
