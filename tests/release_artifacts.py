"""Verify and assemble the tested wheel matrix into one release candidate."""

from __future__ import annotations

import argparse
import hashlib
import shutil
from email.parser import Parser
from pathlib import Path
from zipfile import ZipFile

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib

from distribution_smoke import _verify_archives

ROOT = Path(__file__).resolve().parents[1]
VERSIONS = ("3.10", "3.11", "3.12", "3.13", "3.14")
PLATFORMS = {
    "ubuntu-24.04": ("manylinux_2_28_", "x86_64"),
    "ubuntu-24.04-arm": ("manylinux_2_28_", "aarch64"),
    "macos-15-intel": ("macosx_", "x86_64"),
    "macos-15": ("macosx_", "arm64"),
    "windows-2025": ("win_", "amd64"),
}


def _one(directory: Path, pattern: str) -> Path:
    matches = list(directory.glob(pattern))
    if len(matches) != 1:
        raise ValueError(f"Expected exactly one {pattern} in {directory}, found {matches}")
    return matches[0]


def _wheel_tag(wheel: Path, version: str, python: str, platform: tuple[str, str]) -> str:
    prefix = f"spideroxide-{version}-cp{python}-cp{python}-"
    if not wheel.name.startswith(prefix) or not wheel.name.endswith(".whl"):
        raise ValueError(f"Unexpected wheel name for CPython {python}: {wheel.name}")
    tag = wheel.name[len(prefix) : -4]
    platform_prefix, architecture = platform
    if not tag.startswith(platform_prefix) or not tag.endswith("_" + architecture):
        raise ValueError(f"Unexpected platform tag: {wheel.name}")
    if platform_prefix == "macosx_":
        macos_version = tag.split("_")[1:3]
        if len(macos_version) != 2 or tuple(map(int, macos_version)) > (11, 0):
            raise ValueError(f"Wheel requires newer than macOS 11: {wheel.name}")
    with ZipFile(wheel) as archive:
        wheel_metadata = archive.read(f"spideroxide-{version}.dist-info/WHEEL").decode()
        if f"Tag: cp{python}-cp{python}-{tag}\n" not in wheel_metadata:
            raise ValueError(f"Wheel metadata tag differs from filename: {wheel.name}")
    return tag


def _python_support(wheel: Path) -> None:
    with ZipFile(wheel) as archive:
        metadata = Parser().parsestr(
            archive.read(
                next(name for name in archive.namelist() if name.endswith("/METADATA"))
            ).decode()
        )
    supported = {part.strip() for part in metadata["Requires-Python"].split(",")}
    if supported != {">=3.10", "<3.15"}:
        raise ValueError(f"Unexpected Python support in {wheel.name}: {supported}")


def assemble(artifacts: Path, output: Path) -> None:
    version = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"][
        "version"
    ]
    sdist_dir = artifacts / "source-distribution"
    sdist = _one(sdist_dir, f"spideroxide-{version}.tar.gz")
    if set(sdist_dir.iterdir()) != {sdist}:
        raise ValueError(f"Unexpected source artifacts in {sdist_dir}")
    expected = {"source-distribution"}
    selected: list[Path] = [sdist]
    tags: set[tuple[str, str]] = set()
    for os_name, platform in PLATFORMS.items():
        for python_version in VERSIONS:
            name = f"wheels-{os_name}-py{python_version}"
            expected.add(name)
            directory = artifacts / name
            python = python_version.replace(".", "")
            wheel = _one(directory, f"spideroxide-{version}-cp{python}-*.whl")
            compat = _one(directory, f"spideroxide_scrapy_compat-{version}-py3-none-any.whl")
            if set(directory.iterdir()) != {wheel, compat}:
                raise ValueError(f"Unexpected wheel artifacts in {directory}")
            tag = _wheel_tag(wheel, version, python, platform)
            wheel_target = (python, tag)
            if wheel_target in tags:
                raise ValueError(f"Duplicate wheel target: {wheel_target}")
            tags.add(wheel_target)
            _verify_archives(wheel, compat, sdist)
            _python_support(wheel)
            _python_support(compat)
            selected.append(wheel)
            if os_name == "ubuntu-24.04" and python_version == "3.10":
                selected.append(compat)
    if {entry.name for entry in artifacts.iterdir()} != expected:
        raise ValueError(f"Missing or unexpected artifacts in {artifacts}")
    output.mkdir(parents=True, exist_ok=True)
    if any(output.iterdir()):
        raise ValueError(f"Output directory is not empty: {output}")
    for source in selected:
        shutil.copyfile(source, output / source.name)
    checksums = "\n".join(
        f"{hashlib.sha256(file.read_bytes()).hexdigest()}  {file.name}"
        for file in sorted(output.iterdir())
    )
    (output / "SHA256SUMS").write_text(checksums + "\n", encoding="utf-8")
    print(f"Release candidate: {len(tags)} native wheels, one shim wheel, sdist, SHA256SUMS")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifacts", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    assemble(args.artifacts, args.out)
