from __future__ import annotations

import argparse
import json
import platform
import resource
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from benchmarks.generate_data import generate_requests
from spideroxide import _rust as rust_impl
from spideroxide._python import PythonScheduler


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--implementation", choices=("python", "rust"), required=True)
    parser.add_argument("--mode", choices=("single", "batch"), required=True)
    parser.add_argument("--size", type=int, required=True)
    arguments = parser.parse_args()

    requests = generate_requests(arguments.size)
    scheduler_type = (
        PythonScheduler if arguments.implementation == "python" else rust_impl.RustScheduler
    )
    scheduler = scheduler_type()
    if arguments.mode == "single":
        for request in requests:
            scheduler.push(*request)
        while scheduler.pop() is not None:
            pass
    else:
        scheduler.push_batch(requests)
        scheduler.pop_batch(arguments.size)

    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    peak_bytes = int(peak if platform.system() == "Darwin" else peak * 1024)
    print(
        json.dumps(
            {
                "implementation": arguments.implementation,
                "mode": arguments.mode,
                "size": arguments.size,
                "peak_process_bytes": peak_bytes,
                "measurement": "resource.getrusage(RUSAGE_SELF).ru_maxrss",
            }
        )
    )


if __name__ == "__main__":
    main()
