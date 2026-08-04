from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from importlib import import_module
from typing import Literal

from ._python import PythonDupeFilter, PythonScheduler
from ._python import fingerprint as python_fingerprint
from ._python import fingerprint_batch as python_fingerprint_batch

BackendChoice = Literal["python", "rust", "auto"]
BACKEND_ENV_VAR = "SCRAPY_RUST_BACKEND"


class BackendUnavailableError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class BackendImplementation:
    name: Literal["python", "rust"]
    fingerprint: Callable[[str, str, bytes], bytes]
    fingerprint_batch: Callable[[object], list[bytes]]
    dupe_filter_type: type
    scheduler_type: type


PYTHON_BACKEND = BackendImplementation(
    name="python",
    fingerprint=python_fingerprint,
    fingerprint_batch=python_fingerprint_batch,
    dupe_filter_type=PythonDupeFilter,
    scheduler_type=PythonScheduler,
)


def _load_rust_backend() -> BackendImplementation:
    module = import_module("spideroxide._rust")
    return BackendImplementation(
        name="rust",
        fingerprint=module.fingerprint,
        fingerprint_batch=module.fingerprint_batch,
        dupe_filter_type=module.RustDupeFilter,
        scheduler_type=module.RustScheduler,
    )


def resolve_backend(choice: BackendChoice | str | None = None) -> BackendImplementation:
    selected = (choice or os.environ.get(BACKEND_ENV_VAR, "python")).strip().lower()
    if selected == "python":
        return PYTHON_BACKEND
    if selected not in {"rust", "auto"}:
        raise ValueError(f"invalid backend {selected!r}; expected 'python', 'rust', or 'auto'")

    try:
        return _load_rust_backend()
    except ImportError as exc:
        if selected == "auto":
            return PYTHON_BACKEND
        raise BackendUnavailableError(
            "Rust backend requested but the extension is unavailable; "
            "run `maturin develop --release` or select the Python backend"
        ) from exc
