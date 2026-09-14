from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from spideroxide._native import NativeCrawlCoordinator, _shutdown_async_runtime


async def _exercise_runtime() -> None:
    coordinator = NativeCrawlCoordinator(1, 2, None, "fifo", "fifo", None, None)
    coordinator.close_input()
    assert await coordinator.next_request() is None
    coordinator.close()


def _verify_subprocess_shutdowns() -> None:
    source = """
import asyncio
from spideroxide._native import NativeCrawlCoordinator

async def main():
    coordinator = NativeCrawlCoordinator(1, 2, None, "fifo", "fifo", None, None)
    waiter = asyncio.ensure_future(coordinator.next_request())
    await asyncio.sleep(0)
    waiter.cancel()
    await asyncio.gather(waiter, return_exceptions=True)
    coordinator.abort()
    coordinator.close()

asyncio.run(main())
"""
    for _ in range(25):
        subprocess.run([sys.executable, "-c", source], cwd=ROOT, check=True, timeout=10)


def main() -> None:
    _verify_subprocess_shutdowns()
    asyncio.run(_exercise_runtime())
    assert _shutdown_async_runtime() is True
    assert _shutdown_async_runtime() is False
    print("Native runtime passed: owned Tokio lifecycle, cancellation, and clean shutdown")


if __name__ == "__main__":
    main()
