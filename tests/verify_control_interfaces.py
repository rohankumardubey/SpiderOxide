from __future__ import annotations

import asyncio
import json
import logging
import os
import stat
import subprocess
import sys
import uuid
from pathlib import Path
from unittest.mock import patch

import httpx

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from spideroxide import Crawler, NotConfigured, Spider  # noqa: E402
from spideroxide.remote_control import RemoteControl  # noqa: E402
from spideroxide.settings import Settings  # noqa: E402
from spideroxide.telnet import TelnetConsole  # noqa: E402


class ControlSpider(Spider):
    name = "control-check"
    start_urls = ["data:text/plain,control"]
    ready: asyncio.Event
    release: asyncio.Event

    async def parse(self, response: object) -> None:
        self.ready.set()
        await self.release.wait()


def _settings(engine: str, job_dir: Path, *, execute: bool = False) -> Settings:
    return Settings(
        {
            "ENGINE_BACKEND": engine,
            "EXTENSIONS_BASE": {
                "spideroxide.operational.CoreStats": 0,
                "spideroxide.telnet.TelnetConsole": 0,
                "spideroxide.remote_control.RemoteControl": 0,
            },
            "REMOTE_CONTROL_ENABLED": True,
            "REMOTE_CONTROL_EXECUTE_ENABLED": execute,
            "REMOTE_CONTROL_JOBS_DIR": str(job_dir),
            "REMOTE_CONTROL_OUTPUT_MAX_BYTES": 8,
            "REMOTE_CONTROL_TRACEBACK_MAX_BYTES": 70,
            "REMOTE_CONTROL_TIMEOUT_DEFAULT": 0.1,
            "REMOTE_CONTROL_TIMEOUT_MAX": 0.2,
        }
    )


async def _verify_server(engine: str, *, execute: bool) -> None:
    job_dir = ROOT / "tests" / f".control-jobs-{uuid.uuid4().hex}"
    ControlSpider.ready = asyncio.Event()
    ControlSpider.release = asyncio.Event()
    crawler = Crawler(ControlSpider, _settings(engine, job_dir, execute=execute))
    crawl = asyncio.create_task(crawler.crawl_async())
    try:
        await asyncio.wait_for(ControlSpider.ready.wait(), 15)
        extension = crawler.get_extension(RemoteControl)
        assert extension is not None and extension.port is not None
        assert crawler.get_extension(TelnetConsole) is None
        path = extension._job_file_path
        assert path is not None and path.parent == job_dir
        assert stat.S_IMODE(job_dir.stat().st_mode) == 0o700
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        job = json.loads(path.read_text(encoding="utf-8"))
        assert job["version"] == 1 and job["port"] == extension.port
        assert job["spider"] == ControlSpider.name and job["pid"] == os.getpid()
        assert len(job["token"]) >= 32
        assert extension._auth_token == job["token"]
        url = f"http://127.0.0.1:{extension.port}"
        async with httpx.AsyncClient(trust_env=False, timeout=2) as client:

            async def get_status(headers: dict[str, str] | None = None) -> httpx.Response:
                return await client.get(f"{url}/status", headers=headers)

            assert (await get_status()).status_code == 401
            assert (await get_status({"Authorization": "Bearer wrong"})).status_code == 401
            assert (await get_status({"Authorization": f"Basic {job['token']}"})).status_code == 401
            auth = {"Authorization": f"Bearer {job['token']}"}
            status = await get_status(auth)
            assert status.status_code == 200
            assert status.json()["spider"] == ControlSpider.name
            assert status.json()["pid"] == os.getpid()
            assert isinstance(status.json()["start_time"], float)
            assert (await client.head(f"{url}/status", headers=auth)).status_code == 405
            assert (
                await client.post(f"{url}/execute", json={"code": "print('unauthorized')"})
            ).status_code in (401, 404)
            if execute:
                assert (
                    await client.post(
                        f"{url}/execute",
                        headers={"Authorization": "Bearer invalid"},
                        json={"code": "print('unauthorized')"},
                    )
                ).status_code == 401

                async def run(code: str, **params: object) -> httpx.Response:
                    return await client.post(
                        f"{url}/execute", headers=auth, json={"code": code, **params}
                    )

                assert (await run("stash['count'] = 2\nprint(stash['count'])")).json()[
                    "output"
                ] == "2\n"
                assert (await run("print(stash['count'])")).json()["output"] == "2\n"
                assert (await run("await __import__('asyncio').sleep(1)", timeout_sec=0.01)).json()[
                    "status"
                ] == "timeout"
                assert (await run("raise ValueError('x')")).json()["status"] == "error"
                assert (await run("print('abcdefghijklmnop')")).json()["output_truncated"]
                assert (await run("if :")).json()["status"] == "compile_error"
                for invalid in (True, "bad"):
                    assert (await run("print(1)", timeout_sec=invalid)).status_code == 400
                assert (
                    await client.post(
                        f"{url}/execute",
                        headers=auth,
                        content=b'{"code":"print(1)","timeout_sec":Infinity}',
                    )
                ).status_code == 400
                assert (
                    await client.post(f"{url}/execute", headers=auth, content=b"{")
                ).status_code == 400
                assert (
                    await client.post(f"{url}/execute", headers=auth, json={"code": None})
                ).status_code == 400
                assert (
                    await client.post(f"{url}/execute", headers=auth, json={"code": "a" * 70000})
                ).status_code == 413
            else:
                assert (await client.post(f"{url}/execute", headers=auth)).status_code == 404
        ControlSpider.release.set()
        await asyncio.wait_for(crawl, 15)
        assert not path.exists()
        assert extension._auth_token is None and extension.port is None
        assert extension._runner is None and extension._stash == {}
        await extension.stop()
        try:
            async with httpx.AsyncClient(trust_env=False, timeout=1) as client:
                await client.get(url, headers=auth)
        except httpx.ConnectError:
            pass
        else:
            raise AssertionError("HTTP endpoint still listening after crawler shutdown")
    finally:
        ControlSpider.release.set()
        if not crawl.done():
            await asyncio.wait_for(crawl, 15)
        for child in job_dir.glob("*"):
            child.unlink()
        if job_dir.exists():
            job_dir.rmdir()


async def _verify_failure(engine: str) -> None:
    job_dir = ROOT / "tests" / f".control-insecure-{uuid.uuid4().hex}"
    link_dir = ROOT / "tests" / f".control-symlink-{uuid.uuid4().hex}"
    target_dir = ROOT / "tests" / f".control-private-{uuid.uuid4().hex}"
    job_dir.mkdir(mode=0o755)
    job_dir.chmod(0o755)
    crawler = Crawler(ControlSpider, _settings(engine, job_dir))
    crawler.spider = ControlSpider()
    try:
        extension = RemoteControl.from_crawler(crawler)
        with patch.object(logging.getLogger("spideroxide.remote_control"), "exception") as logged:
            try:
                await extension.start()
            except PermissionError:
                pass
            else:
                raise AssertionError("Insecure job directory must reject remote control startup")
            logged.assert_called_once()
        assert extension.port is None and extension._runner is None
        assert extension._auth_token is None and extension._job_file_path is None
        assert list(job_dir.iterdir()) == []
        await extension.stop()
        target_dir.mkdir(mode=0o700)
        link_dir.symlink_to(target_dir, target_is_directory=True)
        extension._jobs_dir = link_dir
        with patch.object(logging.getLogger("spideroxide.remote_control"), "exception") as logged:
            try:
                await extension.start()
            except OSError:
                pass
            else:
                raise AssertionError("Symlinked job directory must reject remote control startup")
            logged.assert_called_once()
        assert extension.port is None and extension._auth_token is None
        assert not list(target_dir.iterdir())
    finally:
        job_dir.rmdir()
        if link_dir.is_symlink():
            link_dir.unlink()
        if target_dir.exists():
            target_dir.rmdir()


async def _verify_disabled(engine: str) -> None:
    ControlSpider.ready = asyncio.Event()
    ControlSpider.release = asyncio.Event()
    crawler = Crawler(
        ControlSpider,
        {
            "ENGINE_BACKEND": engine,
            "EXTENSIONS_BASE": {
                "spideroxide.telnet.TelnetConsole": 0,
                "spideroxide.remote_control.RemoteControl": 0,
            },
        },
    )
    task = asyncio.create_task(crawler.crawl_async())
    try:
        await asyncio.wait_for(ControlSpider.ready.wait(), 15)
        assert crawler.get_extension(RemoteControl) is None
        assert crawler.get_extension(TelnetConsole) is None
    finally:
        ControlSpider.release.set()
        await asyncio.wait_for(task, 15)


async def main() -> None:
    settings = Settings()
    assert settings.getbool("REMOTE_CONTROL_ENABLED") is False
    assert settings.getbool("REMOTE_CONTROL_EXECUTE_ENABLED") is False
    assert settings.getbool("TELNETCONSOLE_ENABLED") is False
    crawler = Crawler(ControlSpider)
    for cls in (RemoteControl, TelnetConsole):
        try:
            cls.from_crawler(crawler)
        except NotConfigured:
            pass
        else:
            raise AssertionError(f"{cls.__name__} unexpectedly enabled")
    crawler = Crawler(ControlSpider, {"TELNETCONSOLE_ENABLED": True})
    try:
        TelnetConsole.from_crawler(crawler)
    except NotConfigured as error:
        assert "Twisted reactor" in str(error)
    else:
        raise AssertionError("TelnetConsole must not bind unauthenticated")
    for field, value in (
        ("REMOTE_CONTROL_TIMEOUT_DEFAULT", 0),
        ("REMOTE_CONTROL_TIMEOUT_MAX", float("inf")),
        ("REMOTE_CONTROL_OUTPUT_MAX_BYTES", 0),
        ("REMOTE_CONTROL_CODE_MAX_BYTES", -1),
    ):
        try:
            RemoteControl.from_crawler(
                Crawler(
                    ControlSpider,
                    {
                        "REMOTE_CONTROL_ENABLED": True,
                        field: value,
                    },
                )
            )
        except NotConfigured:
            pass
        else:
            raise AssertionError(f"Invalid {field} accepted")
    for engine in ("python", "rust"):
        await _verify_disabled(engine)
        await _verify_server(engine, execute=False)
        await _verify_server(engine, execute=True)
        await _verify_failure(engine)
    env = os.environ.copy()
    env["SPIDEROXIDE_SCRAPY_COMPAT_ALLOW_CONFLICT"] = "1"
    env["PYTHONPATH"] = f"{ROOT / 'compat' / 'scrapy' / 'src'}:{ROOT / 'src'}"
    subprocess.run(
        [
            sys.executable,
            "-c",
            """
from scrapy.extensions.periodic_log import PeriodicLog
from scrapy.extensions.remote_control import RemoteControl
from scrapy.extensions.statsmailer import StatsMailer
from scrapy.extensions.telnet import TelnetConsole, update_telnet_vars
from scrapy.extensions.throttle import AutoThrottle
from scrapy.settings import Settings
from spideroxide.operational import AutoThrottle as NativeAutoThrottle
from spideroxide.operational import PeriodicLog as NativePeriodicLog
from spideroxide.remote_control import RemoteControl as NativeRemoteControl
from spideroxide.statsmailer import StatsMailer as NativeStatsMailer
from spideroxide.telnet import TelnetConsole as NativeTelnetConsole
assert RemoteControl is NativeRemoteControl and TelnetConsole is NativeTelnetConsole
assert AutoThrottle is NativeAutoThrottle and PeriodicLog is NativePeriodicLog
assert StatsMailer is NativeStatsMailer and update_telnet_vars is not None
assert not Settings().getbool("REMOTE_CONTROL_ENABLED")
""",
        ],
        cwd=ROOT,
        env=env,
        check=True,
    )


if __name__ == "__main__":
    asyncio.run(main())
    print("Control interfaces passed: secure defaults, auth, execution, lifecycle and cleanup")
