from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMPAT_SRC = ROOT / "compat" / "scrapy" / "src"
SOURCE = ROOT / "src"
SNAPSHOT = ROOT / "tests" / "_contract_snapshot.py"
COMMAND = [sys.executable, "-c", "from scrapy.cmdline import execute; execute()"]
COMPAT_ENVIRONMENT = {
    **os.environ,
    "PYTHONPATH": os.pathsep.join((str(COMPAT_SRC), str(SOURCE), str(ROOT))),
    "SPIDEROXIDE_SCRAPY_COMPAT_ALLOW_CONFLICT": "1",
}


def _snapshot(*, compatibility: bool) -> dict[str, object]:
    environment = dict(os.environ)
    if compatibility:
        environment.update(COMPAT_ENVIRONMENT)
    else:
        environment.pop("PYTHONPATH", None)
        environment.pop("SPIDEROXIDE_SCRAPY_COMPAT_ALLOW_CONFLICT", None)
    result = subprocess.run(
        [sys.executable, str(SNAPSHOT)],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _run(
    *args: str,
    cwd: Path,
    expected: int = 0,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        [*COMMAND, *args],
        cwd=cwd,
        env=COMPAT_ENVIRONMENT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == expected, (
        f"scrapy {' '.join(args)} returned {result.returncode}, expected {expected}\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    return result


def _verify_differential_snapshot() -> None:
    try:
        version("Scrapy")
    except PackageNotFoundError:
        return
    assert _snapshot(compatibility=True) == _snapshot(compatibility=False)


def _verify_check_command() -> None:
    with tempfile.TemporaryDirectory(prefix="spideroxide-contracts-") as temporary:
        project = Path(temporary)
        _run("startproject", "contract_project", ".", cwd=project)
        package = project / "contract_project"
        (package / "contracts.py").write_text(
            """from scrapy.contracts import Contract


class StatusContract(Contract):
    name = "status"

    def pre_process(self, response):
        assert response.status == int(self.args[0])
""",
            encoding="utf-8",
        )
        with (package / "settings.py").open("a", encoding="utf-8") as settings:
            settings.write(
                '\nSPIDER_CONTRACTS = {"contract_project.contracts.StatusContract": 10}\n'
            )
        (package / "pipelines.py").write_text(
            """from pathlib import Path


class MarkerPipeline:
    def open_spider(self, spider):
        Path("pipeline.txt").touch()

    def process_item(self, item, spider):
        return item
""",
            encoding="utf-8",
        )
        spider = package / "spiders" / "contract_spider.py"
        spider.write_text(
            """import asyncio

import scrapy


class ContractSpider(scrapy.Spider):
    name = "contracts"
    custom_settings = {
        "FEEDS": {"contract-items.jsonl": {"format": "jsonlines"}},
        "ITEM_PIPELINES": {"contract_project.pipelines.MarkerPipeline": 100},
    }

    async def parse(self, response, expected):
        \"\"\"
        @url data:text/plain,contract
        @status 200
        @cb_kwargs {"expected": "contract"}
        @returns items 1 1
        @scrapes value
        \"\"\"
        await asyncio.sleep(0)
        assert response.text == expected
        yield {"value": response.text}
""",
            encoding="utf-8",
        )

        listed = _run("check", "--list", cwd=project)
        assert listed.stdout == "contracts\n  * parse\n"

        passed = _run("check", "-a", "source=cli", cwd=project)
        assert "Ran 3 contracts" in passed.stderr
        assert passed.stderr.rstrip().endswith("OK")
        assert not (project / "contract-items.jsonl").exists()
        assert not (project / "pipeline.txt").exists()

        native = _run(
            "check",
            "-s",
            "ENGINE_BACKEND=rust",
            cwd=project,
        )
        assert "Ran 3 contracts" in native.stderr
        assert native.stderr.rstrip().endswith("OK")

        override = _run(
            "check",
            "-s",
            'ITEM_PIPELINES={"contract_project.pipelines.MarkerPipeline": 100}',
            "-s",
            'FEEDS={"contract-items.jsonl": {"format": "jsonlines"}}',
            cwd=project,
        )
        assert override.returncode == 0
        assert (project / "contract-items.jsonl").exists()
        assert (project / "pipeline.txt").exists()
        (project / "contract-items.jsonl").unlink()
        (project / "pipeline.txt").unlink()

        invalid = _run("check", "-a", "invalid", cwd=project, expected=2)
        assert "Invalid -a value, use -a NAME=VALUE" in invalid.stderr

        spider.write_text(
            spider.read_text(encoding="utf-8").replace(
                "@scrapes value",
                "@scrapes missing",
            ),
            encoding="utf-8",
        )
        failed = _run("check", cwd=project, expected=1)
        assert "Missing fields: missing" in failed.stderr
        assert "FAILED (failures=1)" in failed.stderr


def main() -> None:
    _verify_differential_snapshot()
    _verify_check_command()
    print(
        "Contracts passed: Scrapy differential APIs, built-ins, custom contracts, "
        "sync/async hooks, request configuration, discovery, and check reporting"
    )


if __name__ == "__main__":
    main()
