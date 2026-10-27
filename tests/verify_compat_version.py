from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib

ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT = ROOT / "tests" / "_version_deprecation_snapshot.py"
COMPAT_SRC = ROOT / "compat" / "scrapy" / "src"
SOURCE = ROOT / "src"


def _snapshot(*, shim: bool) -> dict[str, object]:
    result = subprocess.run(
        [sys.executable, str(SNAPSHOT)],
        cwd=ROOT,
        env={**os.environ, "SPIDEROXIDE_SHIM": "1" if shim else "0"},
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        raise RuntimeError(
            f"Snapshot exited {result.returncode}:\n{result.stdout}\n{result.stderr}"
        )
    return json.loads(result.stdout)


def _verify_declared_version() -> str:
    native = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    compat = tomllib.loads((ROOT / "compat/scrapy/pyproject.toml").read_text(encoding="utf-8"))[
        "project"
    ]
    assert native["version"] == compat["version"]
    assert compat["dependencies"] == [f"spideroxide=={native['version']}"]
    target = json.loads((ROOT / "conformance/manifest.json").read_text(encoding="utf-8"))[
        "scrapy_version"
    ]
    assert target == "2.19.0"
    assert (ROOT / "requirements-conformance.txt").read_text(encoding="utf-8").strip() == (
        f"Scrapy=={target}"
    )
    return compat["version"]


def _command(directory: Path, env: dict[str, str], *args: str) -> str:
    result = subprocess.run(
        [sys.executable, "-c", "from scrapy.cmdline import execute; execute()", *args],
        cwd=directory,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    if result.returncode:
        raise RuntimeError(
            f"scrapy {' '.join(args)} exited {result.returncode}:\n{result.stdout}\n{result.stderr}"
        )
    return result.stdout


def _verify_existing_project() -> None:
    upstream_env = os.environ.copy()
    upstream_env.pop("PYTHONPATH", None)
    upstream_env.pop("SPIDEROXIDE_SCRAPY_COMPAT_ALLOW_CONFLICT", None)
    shim_env = {
        **upstream_env,
        "PYTHONPATH": os.pathsep.join((str(COMPAT_SRC), str(SOURCE))),
        "SPIDEROXIDE_SCRAPY_COMPAT_ALLOW_CONFLICT": "1",
    }
    with tempfile.TemporaryDirectory(prefix="spideroxide-project-upgrade-") as root:
        directory = Path(root)
        _command(directory, upstream_env, "startproject", "demo")
        project = directory / "demo"
        settings = project / "demo" / "settings.py"
        with settings.open("a", encoding="utf-8") as file:
            file.write('\nUSER_AGENT = "version-migration-test"\nROBOTSTXT_OBEY = False\n')
        spider = project / "demo" / "spiders" / "fixture.py"
        spider.write_text(
            "import scrapy\n\n"
            "class Fixture(scrapy.Spider):\n"
            "    name = 'fixture'\n"
            "    start_urls = ['data:text/plain,upgraded']\n"
            "    def parse(self, response):\n"
            "        yield {'body': response.text}\n",
            encoding="utf-8",
        )
        assert _command(project, shim_env, "settings", "--get", "USER_AGENT").strip() == (
            "version-migration-test"
        )
        for backend in ("python", "rust"):
            output = f"{backend}.jsonl"
            _command(
                project,
                shim_env,
                "crawl",
                "fixture",
                "-s",
                f"ENGINE_BACKEND={backend}",
                "-O",
                f"{output}:jsonlines",
            )
            assert [
                json.loads(line)
                for line in (project / output).read_text(encoding="utf-8").splitlines()
            ] == [{"body": "upgraded"}]


if __name__ == "__main__":
    declared_version = _verify_declared_version()
    candidate = _snapshot(shim=True)
    assert candidate.pop("shim_version") == declared_version
    try:
        installed = version("Scrapy")
    except PackageNotFoundError:
        print("Scrapy 2.19.0 not installed; checked shim warnings and version metadata only")
    else:
        assert installed == "2.19.0", f"Unsupported upstream baseline: Scrapy {installed}"
        baseline = _snapshot(shim=False)
        assert baseline.pop("shim_version") is None
        assert candidate == baseline, (candidate, baseline)
        _verify_existing_project()
        print(
            "Scrapy 2.19.0 version, deprecation messages, categories, and call sites match; "
            "upstream project crawls with both backends"
        )
