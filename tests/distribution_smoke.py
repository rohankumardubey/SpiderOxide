"""Verify built distributions in a fresh environment outside the source checkout."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import tarfile
import tempfile
import venv
from email.parser import Parser
from pathlib import Path
from zipfile import ZipFile


def _one(directory: Path, pattern: str) -> Path:
    matches = list(directory.glob(pattern))
    if len(matches) != 1:
        raise AssertionError(f"Expected one {pattern} in {directory}, found {matches}")
    return matches[0].resolve()


def _verify_archives(wheel: Path, compat: Path, sdist: Path | None) -> None:
    with ZipFile(wheel) as archive:
        names = archive.namelist()
        assert "spideroxide/__init__.py" in names
        assert "spideroxide/py.typed" in names
        assert any(name.startswith("spideroxide/_native.") for name in names)
        assert any(name.endswith("/licenses/LICENSE") for name in names)
        assert not any(
            name.startswith(("crates/", "tests/", "target/"))
            or "__pycache__" in name
            or name.endswith((".pyc", ".pyo"))
            for name in names
        )
        metadata = Parser().parsestr(
            archive.read(next(name for name in names if name.endswith("/METADATA"))).decode()
        )
        native_version = metadata["Version"]
        for extra in (
            "twisted",
            "remote-control",
            "images",
            "s3",
            "gcs",
            "remote-feeds",
            "remote-media",
            "dev",
            "benchmark",
        ):
            assert extra in metadata.get_all("Provides-Extra", []), extra
    with ZipFile(compat) as archive:
        names = archive.namelist()
        assert "scrapy/__init__.py" in names and "scrapy/cmdline.py" in names
        assert any(name.endswith("/licenses/LICENSE") for name in names)
        entry = archive.read(next(name for name in names if name.endswith("/entry_points.txt")))
        assert b"scrapy = scrapy.cmdline:execute" in entry
        compat_metadata = Parser().parsestr(
            archive.read(next(name for name in names if name.endswith("/METADATA"))).decode()
        )
        assert compat_metadata["Version"] == native_version
        assert f"spideroxide=={native_version}" in compat_metadata.get_all("Requires-Dist", [])
    if sdist is not None:
        with tarfile.open(sdist, "r:gz") as archive:
            names = archive.getnames()
            for name in (
                "LICENSE",
                "Cargo.lock",
                "Cargo.toml",
                "crates/spideroxide-native/Cargo.toml",
                "src/spideroxide/__init__.py",
                "compat/scrapy/LICENSE",
                "compat/scrapy/pyproject.toml",
            ):
                assert any(member.endswith("/" + name) for member in names), name
            assert not any(
                "/target/" in name
                or "/.venv/" in name
                or "/__pycache__/" in name
                or ".egg-info/" in name
                or name.endswith((".pyc", ".pyo"))
                for name in names
            )


def _run(*args: str, cwd: Path, env: dict[str, str]) -> str:
    result = subprocess.run(args, cwd=cwd, env=env, text=True, capture_output=True, timeout=300)
    if result.returncode:
        raise RuntimeError(
            f"{args!r} failed (exit {result.returncode}):\n{result.stdout}\n{result.stderr}"
        )
    return result.stdout


def _smoke(python: Path, command: Path, directory: Path, env: dict[str, str]) -> None:
    code = """
import asyncio
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
import scrapy
import spideroxide
from scrapy import Spider
from scrapy.crawler import Crawler

assert scrapy.__version__ == "2.19.0"
assert version("spideroxide-scrapy-compat") == version("spideroxide")
assert Path(spideroxide.__file__).resolve().is_relative_to(Path(__import__("sys").prefix))
try:
    version("Scrapy")
except PackageNotFoundError:
    pass
else:
    raise AssertionError("Installed shim conflicts with upstream Scrapy")

class FixtureSpider(Spider):
    name = "installed"
    start_urls = ("data:text/plain,installed",)
    def parse(self, response):
        return {"body": response.text}

async def main():
    for backend in ("python", "rust"):
        result = await Crawler(FixtureSpider, {
            "ENGINE_BACKEND": backend, "ROBOTSTXT_OBEY": False
        }).crawl()
        assert result.items == ({"body": "installed"},), result.items

asyncio.run(main())
"""
    _run(os.fspath(python), "-c", code, cwd=directory, env=env)
    assert "2.19.0" in _run(os.fspath(command), "version", cwd=directory, env=env)
    spider = directory / "fixture.py"
    spider.write_text(
        "from scrapy import Spider\n"
        "class Fixture(Spider):\n"
        "    name = 'installed-cli'\n"
        "    start_urls = ['data:text/plain,cli']\n"
        "    def parse(self, response):\n"
        "        return {'body': response.text}\n",
        encoding="utf-8",
    )
    _run(
        os.fspath(command),
        "runspider",
        os.fspath(spider),
        "-o",
        "output.jsonl:jsonlines",
        cwd=directory,
        env=env,
    )
    assert [json.loads(row) for row in (directory / "output.jsonl").read_text().splitlines()] == [
        {"body": "cli"}
    ]


def verify(
    wheels: Path,
    sdist: Path | None,
    extras: list[str],
    third_party: bool,
    previous_wheels: Path | None,
    migrate_scrapy: bool,
) -> None:
    if previous_wheels and migrate_scrapy:
        raise ValueError(
            "A previous-release upgrade and an upstream Scrapy migration need separate runs"
        )
    wheel = _one(wheels, "spideroxide-*.whl")
    compat = _one(wheels, "spideroxide_scrapy_compat-*.whl")
    _verify_archives(wheel, compat, sdist)
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env.pop("SPIDEROXIDE_SCRAPY_COMPAT_ALLOW_CONFLICT", None)
    with tempfile.TemporaryDirectory(prefix="spideroxide-installed-") as root:
        directory = Path(root)
        environment = directory / "environment"
        venv.EnvBuilder(with_pip=True).create(environment)
        bin_path = environment / ("Scripts" if os.name == "nt" else "bin")
        python = bin_path / ("python.exe" if os.name == "nt" else "python")
        command = bin_path / ("scrapy.exe" if os.name == "nt" else "scrapy")
        if migrate_scrapy:
            _run(
                os.fspath(python),
                "-m",
                "pip",
                "install",
                "Scrapy==2.19.0",
                cwd=directory,
                env=env,
            )
            _run(os.fspath(command), "version", cwd=directory, env=env)
            _run(
                os.fspath(python), "-m", "pip", "uninstall", "-y", "Scrapy", cwd=directory, env=env
            )
            _run(
                os.fspath(python),
                "-c",
                "from importlib.util import find_spec; assert find_spec('scrapy') is None",
                cwd=directory,
                env=env,
            )
        _run(
            os.fspath(python),
            "-m",
            "pip",
            "install",
            os.fspath(_one(previous_wheels, "spideroxide-*.whl") if previous_wheels else wheel),
            os.fspath(
                _one(previous_wheels, "spideroxide_scrapy_compat-*.whl")
                if previous_wheels
                else compat
            ),
            cwd=directory,
            env=env,
        )
        if previous_wheels:
            previous_version = _run(
                os.fspath(python),
                "-c",
                "from importlib.metadata import version; print(version('spideroxide'))",
                cwd=directory,
                env=env,
            ).strip()
            _run(
                os.fspath(python),
                "-m",
                "pip",
                "install",
                "--upgrade",
                os.fspath(wheel),
                os.fspath(compat),
                cwd=directory,
                env=env,
            )
            current_version = _run(
                os.fspath(python),
                "-c",
                "from importlib.metadata import version; print(version('spideroxide'))",
                cwd=directory,
                env=env,
            ).strip()
            _run(
                os.fspath(python),
                "-c",
                "from pip._vendor.packaging.version import Version; "
                "import sys; assert Version(sys.argv[2]) > Version(sys.argv[1])",
                previous_version,
                current_version,
                cwd=directory,
                env=env,
            )
        _run(os.fspath(python), "-m", "pip", "check", cwd=directory, env=env)
        _smoke(python, command, directory, env)
        _run(
            os.fspath(python),
            "-m",
            "pip",
            "uninstall",
            "-y",
            "spideroxide-scrapy-compat",
            cwd=directory,
            env=env,
        )
        _run(
            os.fspath(python),
            "-c",
            "from importlib.util import find_spec; assert find_spec('scrapy') is None",
            cwd=directory,
            env=env,
        )
        _run(
            os.fspath(python),
            "-m",
            "pip",
            "uninstall",
            "-y",
            "spideroxide",
            cwd=directory,
            env=env,
        )
        _run(
            os.fspath(python),
            "-c",
            "from importlib.util import find_spec; assert find_spec('spideroxide') is None",
            cwd=directory,
            env=env,
        )
        for extra in extras:
            extra_env = directory / f"extra-{extra}"
            venv.EnvBuilder(with_pip=True).create(extra_env)
            extra_python = extra_env / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
            _run(
                os.fspath(extra_python),
                "-m",
                "pip",
                "install",
                f"{wheel}[{extra}]",
                cwd=directory,
                env=env,
            )
            modules = {
                "twisted": "twisted.conch.manhole",
                "remote-control": "aiohttp",
                "images": "PIL",
                "s3": "boto3",
                "gcs": "google.cloud.storage",
                "remote-feeds": "boto3,google.cloud.storage",
                "remote-media": "boto3,google.cloud.storage",
                "dev": "aiohttp",
                "benchmark": "scrapy",
            }[extra]
            _run(
                os.fspath(extra_python),
                "-c",
                f"import importlib; [importlib.import_module(m) for m in {modules!r}.split(',')]",
                cwd=directory,
                env=env,
            )
            _run(os.fspath(extra_python), "-m", "pip", "check", cwd=directory, env=env)
            if extra == "benchmark":
                _run(
                    os.fspath(extra_python),
                    "-c",
                    "import scrapy, spideroxide; "
                    "from importlib.metadata import version; "
                    "assert scrapy.__version__ == version('Scrapy')",
                    cwd=directory,
                    env=env,
                )
            elif extra == "dev":
                extra_bin = extra_env / ("Scripts" if os.name == "nt" else "bin")
                for tool in ("ruff", "maturin"):
                    _run(
                        os.fspath(extra_bin / (f"{tool}.exe" if os.name == "nt" else tool)),
                        "--version",
                        cwd=directory,
                        env=env,
                    )
        if third_party:
            third_party_env = directory / "third-party"
            venv.EnvBuilder(with_pip=True).create(third_party_env)
            third_party_python = third_party_env / (
                "Scripts/python.exe" if os.name == "nt" else "bin/python"
            )
            _run(
                os.fspath(third_party_python),
                "-m",
                "pip",
                "install",
                os.fspath(wheel),
                "-r",
                os.fspath(Path(__file__).resolve().parents[1] / "requirements-certification.txt"),
                cwd=directory,
                env=env,
            )
            _run(
                os.fspath(third_party_python),
                "-c",
                "import scrapy, spideroxide, scrapy_redis, scrapy_playwright; "
                "from importlib.metadata import version; "
                "assert scrapy.__version__ == version('Scrapy'); "
                "assert version('scrapy-deltafetch') and version('scrapy-xlsx'); "
                "assert version('scrapy-splash') and version('scrapy-zyte-smartproxy'); "
                "assert version('spidermon') and version('fakeredis') and version('openpyxl')",
                cwd=directory,
                env=env,
            )
            _run(os.fspath(third_party_python), "-m", "pip", "check", cwd=directory, env=env)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--wheels", type=Path, required=True)
    parser.add_argument("--sdist", type=Path)
    parser.add_argument("--extras", nargs="*", default=[])
    parser.add_argument("--third-party", action="store_true")
    parser.add_argument("--previous-wheels", type=Path)
    parser.add_argument("--migrate-scrapy", action="store_true")
    arguments = parser.parse_args()
    verify(
        arguments.wheels,
        arguments.sdist,
        arguments.extras,
        arguments.third_party,
        arguments.previous_wheels,
        arguments.migrate_scrapy,
    )
    print("Distributions passed: archives, isolated crawls and CLI, uninstall, optional checks")
