from __future__ import annotations

import hashlib
import io
import shutil
import tarfile
import tempfile
from pathlib import Path
from zipfile import ZipFile

from release_artifacts import PLATFORMS, ROOT, VERSIONS, _wheel_tag, assemble, tomllib

VERSION = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
EXTRAS = (
    "twisted",
    "remote-control",
    "images",
    "s3",
    "gcs",
    "remote-feeds",
    "remote-media",
    "dev",
    "benchmark",
)


def _sdist(path: Path) -> None:
    path.parent.mkdir(parents=True)
    with tarfile.open(path, "w:gz") as archive:
        for name in (
            "LICENSE",
            "Cargo.lock",
            "Cargo.toml",
            "crates/spideroxide-native/Cargo.toml",
            "src/spideroxide/__init__.py",
            "compat/scrapy/LICENSE",
            "compat/scrapy/pyproject.toml",
        ):
            content = b"fixture"
            entry = tarfile.TarInfo(f"spideroxide-{VERSION}/{name}")
            entry.size = len(content)
            archive.addfile(entry, io.BytesIO(content))


def _wheel(directory: Path, python: str, tag: str, requires_python: str = ">=3.10, <3.15") -> Path:
    path = directory / f"spideroxide-{VERSION}-cp{python}-cp{python}-{tag}.whl"
    with ZipFile(path, "w") as archive:
        archive.writestr("spideroxide/__init__.py", "")
        archive.writestr("spideroxide/py.typed", "")
        archive.writestr("spideroxide/_native.so", "")
        archive.writestr(f"spideroxide-{VERSION}.dist-info/licenses/LICENSE", "MIT")
        archive.writestr(
            f"spideroxide-{VERSION}.dist-info/METADATA",
            f"Name: spideroxide\nVersion: {VERSION}\nRequires-Python: {requires_python}\n"
            + "".join(f"Provides-Extra: {extra}\n" for extra in EXTRAS),
        )
        archive.writestr(
            f"spideroxide-{VERSION}.dist-info/WHEEL",
            f"Wheel-Version: 1.0\nTag: cp{python}-cp{python}-{tag}\n",
        )
    return path


def _compat(directory: Path) -> None:
    with ZipFile(
        directory / f"spideroxide_scrapy_compat-{VERSION}-py3-none-any.whl", "w"
    ) as archive:
        archive.writestr("scrapy/__init__.py", "")
        archive.writestr("scrapy/cmdline.py", "")
        archive.writestr(f"spideroxide_scrapy_compat-{VERSION}.dist-info/licenses/LICENSE", "MIT")
        archive.writestr(
            f"spideroxide_scrapy_compat-{VERSION}.dist-info/METADATA",
            f"Name: spideroxide-scrapy-compat\nVersion: {VERSION}\n"
            f"Requires-Python: >=3.10, <3.15\nRequires-Dist: spideroxide=={VERSION}\n",
        )
        archive.writestr(
            f"spideroxide_scrapy_compat-{VERSION}.dist-info/entry_points.txt",
            "[console_scripts]\nscrapy = scrapy.cmdline:execute\n",
        )


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="spideroxide-release-inventory-") as root:
        base = Path(root)
        artifacts = base / "artifacts"
        _sdist(artifacts / "source-distribution" / f"spideroxide-{VERSION}.tar.gz")
        for os_name, (prefix, architecture) in PLATFORMS.items():
            for version in VERSIONS:
                directory = artifacts / f"wheels-{os_name}-py{version}"
                directory.mkdir()
                tag = (
                    f"{prefix}11_0_{architecture}"
                    if prefix == "macosx_"
                    else f"{prefix}{architecture}"
                )
                _wheel(directory, version.replace(".", ""), tag)
                _compat(directory)
        output = base / "dist"
        assemble(artifacts, output)
        assert len(list(output.glob("spideroxide-*.whl"))) == 25
        assert len(list(output.iterdir())) == 28
        checksum_lines = (output / "SHA256SUMS").read_text(encoding="utf-8").splitlines()
        assert len(checksum_lines) == 27
        for line in checksum_lines:
            digest, name = line.split("  ", 1)
            assert hashlib.sha256((output / name).read_bytes()).hexdigest() == digest

        missing = artifacts / "wheels-windows-2025-py3.10"
        next(missing.glob("spideroxide-*.whl")).unlink()
        try:
            assemble(artifacts, base / "incomplete")
        except ValueError:
            assert not (base / "incomplete").exists()
        else:
            raise AssertionError("Incomplete matrix was accepted")

        _wheel(missing, "310", "win_amd64", requires_python=">=3.11")
        try:
            assemble(artifacts, base / "unsupported-python")
        except ValueError as exc:
            assert "Unexpected Python support" in str(exc)
        else:
            raise AssertionError("Incorrect Python support was accepted")

        mac = next((artifacts / "wheels-macos-15-py3.10").glob("spideroxide-*.whl"))
        newer = base / mac.name.replace("macosx_11_0_", "macosx_12_0_")
        shutil.copyfile(mac, newer)
        try:
            _wheel_tag(newer, VERSION, "310", PLATFORMS["macos-15"])
        except ValueError as exc:
            assert "Wheel requires newer than macOS 11" in str(exc)
        else:
            raise AssertionError("Unsupported macOS tag was accepted")
    print("Release inventory passed: 25 wheels, sdist, checksums, and invalid-target rejection")


if __name__ == "__main__":
    main()
