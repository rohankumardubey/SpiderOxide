"""Opt-in localhost HTTP control for an asyncio crawler."""

from __future__ import annotations

import ast
import asyncio
import contextlib
import hmac
import inspect
import io
import json
import logging
import math
import os
import secrets
import stat
import time
import traceback
import uuid
from datetime import datetime
from pathlib import Path
from types import CodeType
from typing import TYPE_CHECKING, Any

from . import signals
from .exceptions import NotConfigured

if TYPE_CHECKING:
    from .crawler import Crawler

logger = logging.getLogger(__name__)


def _job_files_dir(setting: object) -> Path:
    if setting:
        return Path(setting)  # type: ignore[arg-type]
    base = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
    return base / "spideroxide" / "job_files"


def _write_job_file(path: Path, data: dict[str, object]) -> None:
    directory = path.parent
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    temporary = f".{path.name}.{uuid.uuid4().hex}.tmp"
    try:
        info = os.fstat(directory_fd)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise PermissionError(
                f"Remote control jobs directory must be owned and private: {directory}"
            )
        descriptor = os.open(
            temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=directory_fd
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(data, stream)
            os.replace(temporary, path.name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(temporary, dir_fd=directory_fd)
            raise
    finally:
        os.close(directory_fd)


def _cap(value: str, limit: int) -> tuple[str, bool]:
    raw = value.encode("utf-8")
    if len(raw) <= limit:
        return value, False
    extra_kb = (len(raw) - limit) // 1024 + 1
    head = raw[:limit].decode("utf-8", "ignore")
    return f"{head}…[truncated, +{extra_kb}KB]", True


class RemoteControl:
    """Authenticated, loopback-only /status and explicitly opted-in /execute."""

    def __init__(self, crawler: Crawler):
        if not crawler.settings.getbool("REMOTE_CONTROL_ENABLED"):
            raise NotConfigured
        if os.name != "posix":
            raise NotConfigured("RemoteControl requires POSIX private job-file permissions")
        try:
            from aiohttp import web
        except ImportError as error:
            raise NotConfigured("RemoteControl requires spideroxide[remote-control]") from error
        self._web = web
        self._crawler = crawler
        self._default_timeout = crawler.settings.getfloat("REMOTE_CONTROL_TIMEOUT_DEFAULT")
        self._max_timeout = crawler.settings.getfloat("REMOTE_CONTROL_TIMEOUT_MAX")
        self._output_max_bytes = crawler.settings.getint("REMOTE_CONTROL_OUTPUT_MAX_BYTES")
        self._traceback_max_bytes = crawler.settings.getint("REMOTE_CONTROL_TRACEBACK_MAX_BYTES")
        self._code_max_bytes = crawler.settings.getint("REMOTE_CONTROL_CODE_MAX_BYTES")
        if (
            not 0 < self._default_timeout <= self._max_timeout
            or not math.isfinite(self._max_timeout)
            or min(self._output_max_bytes, self._traceback_max_bytes, self._code_max_bytes) < 1
        ):
            raise NotConfigured(
                "REMOTE_CONTROL_* timeout and byte limits must be finite and positive"
            )
        self._execute_enabled = crawler.settings.getbool("REMOTE_CONTROL_EXECUTE_ENABLED")
        self._jobs_dir = _job_files_dir(crawler.settings.get("REMOTE_CONTROL_JOBS_DIR"))
        self._stash: dict[str, Any] = {}
        self._auth_token: str | None = None
        self._runner: Any = None
        self._job_file_path: Path | None = None
        self.port: int | None = None
        crawler.signals.connect(self.start, signals.engine_started)
        crawler.signals.connect(self.stop, signals.engine_stopped)

    @classmethod
    def from_crawler(cls, crawler: Crawler) -> RemoteControl:
        return cls(crawler)

    async def start(self) -> None:
        if self._runner is not None:
            return
        try:
            self._auth_token = secrets.token_urlsafe(32)
            app = self._web.Application(client_max_size=self._code_max_bytes + 4096)
            app.router.add_get("/status", self._handle_status, allow_head=False)
            if self._execute_enabled:
                app.router.add_post("/execute", self._handle_execute)
            self._runner = self._web.AppRunner(
                app, access_log=None, shutdown_timeout=2.0, handler_cancellation=True
            )
            await self._runner.setup()
            site = self._web.TCPSite(self._runner, "127.0.0.1", 0)
            await site.start()
            self.port = self._runner.addresses[0][1]
            spider = self._crawler.spider
            if spider is None:
                raise RuntimeError("RemoteControl requires a running spider")
            job_path = self._jobs_dir / f"{os.getpid()}-{uuid.uuid4().hex}.json"
            _write_job_file(
                job_path,
                {
                    "version": 1,
                    "pid": os.getpid(),
                    "port": self.port,
                    "token": self._auth_token,
                    "spider": spider.name,
                    "project": self._crawler.settings.get("BOT_NAME"),
                    "scrapy_version": "2.19.0",
                    "start_time": time.time(),
                },
            )
            self._job_file_path = job_path
            logger.info("Remote control listening on 127.0.0.1:%s", self.port)
        except asyncio.CancelledError:
            await self.stop()
            raise
        except Exception:
            logger.exception("Remote control HTTP server failed to start")
            await self.stop()
            raise

    async def stop(self) -> None:
        if self._job_file_path is not None:
            with contextlib.suppress(OSError):
                self._job_file_path.unlink(missing_ok=True)
            self._job_file_path = None
        runner = self._runner
        self._runner = None
        self.port = None
        self._auth_token = None
        self._stash.clear()
        if runner is not None:
            try:
                await runner.cleanup()
            except Exception:
                logger.exception("Error stopping remote control HTTP server")

    def _is_authenticated(self, request: Any) -> bool:
        authorization = request.headers.get("Authorization", "")
        return bool(
            self._auth_token
            and authorization.isascii()
            and authorization.startswith("Bearer ")
            and hmac.compare_digest(authorization[7:], self._auth_token)
        )

    async def _handle_status(self, request: Any) -> Any:
        if not self._is_authenticated(request):
            return self._web.json_response({"error": "unauthorized"}, status=401)
        return self._web.json_response(self._get_status())

    async def _handle_execute(self, request: Any) -> Any:
        if not self._is_authenticated(request):
            return self._web.json_response({"error": "unauthorized"}, status=401)
        if not self._execute_enabled:
            return self._web.json_response({"error": "execution disabled"}, status=403)
        try:
            body = await request.json()
        except self._web.HTTPRequestEntityTooLarge:
            return self._web.json_response({"error": "code too large"}, status=413)
        except Exception:
            return self._web.json_response({"error": "invalid JSON body"}, status=400)
        if not isinstance(body, dict) or not isinstance(body.get("code"), str):
            return self._web.json_response({"error": "Missing or invalid 'code' value"}, status=400)
        source = body["code"]
        if len(source.encode("utf-8")) > self._code_max_bytes:
            return self._web.json_response({"error": "code too large"}, status=413)
        requested = body.get("timeout_sec")
        if requested is not None and (
            isinstance(requested, bool)
            or not isinstance(requested, (int, float))
            or not math.isfinite(requested)
        ):
            return self._web.json_response({"error": "Invalid 'timeout_sec' value"}, status=400)
        timeout = self._default_timeout if requested is None or requested <= 0 else requested
        timeout = min(timeout, self._max_timeout)
        try:
            compiled = compile(source, "<execute>", "exec", flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT)
        except (SyntaxError, ValueError):
            error, truncated = _cap(traceback.format_exc(), self._traceback_max_bytes)
            result: dict[str, Any] = {
                "status": "compile_error",
                "output": "",
                "traceback": error,
                "elapsed_sec": 0.0,
            }
            if truncated:
                result["traceback_truncated"] = True
            return self._web.json_response(result)
        return self._web.json_response(await self._run_code(compiled, timeout))

    def _make_namespace(self, buf: io.StringIO) -> dict[str, Any]:
        def _print(*args: Any, **kwargs: Any) -> None:
            kwargs.setdefault("file", buf)
            print(*args, **kwargs)

        return {"crawler": self._crawler, "stash": self._stash, "print": _print}

    async def _run_code(self, code: CodeType, timeout: float) -> dict[str, Any]:
        output = io.StringIO()
        namespace = self._make_namespace(output)
        status = "ok"
        error: str | None = None
        start = time.perf_counter()
        try:
            result = eval(code, namespace)  # noqa: S307 - requires explicit opt-in and bearer token
            if inspect.isawaitable(result):
                try:
                    await asyncio.wait_for(result, timeout)
                except asyncio.TimeoutError:
                    status = "timeout"
        except Exception:
            status = "error"
            error = traceback.format_exc()
        elapsed = round(time.perf_counter() - start, 3)
        rendered, output_truncated = _cap(output.getvalue(), self._output_max_bytes)
        error_truncated = False
        if error is not None:
            error, error_truncated = _cap(error, self._traceback_max_bytes)
        response: dict[str, Any] = {
            "status": status,
            "output": rendered,
            "traceback": error,
            "elapsed_sec": elapsed,
        }
        if output_truncated:
            response["output_truncated"] = True
        if error_truncated:
            response["traceback_truncated"] = True
        return response

    def _get_status(self) -> dict[str, object]:
        spider = self._crawler.spider
        assert spider is not None
        start_time = self._crawler.stats.get_value("start_time")
        return {
            "pid": os.getpid(),
            "spider": spider.name,
            "project": self._crawler.settings.get("BOT_NAME"),
            "scrapy_version": "2.19.0",
            "start_time": start_time.timestamp() if isinstance(start_time, datetime) else None,
        }
