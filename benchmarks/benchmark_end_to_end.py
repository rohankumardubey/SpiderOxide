from common import run_cli

if __name__ == "__main__":
    run_cli(
        ("end_to_end_single", "end_to_end_batch"),
        "Benchmark complete duplicate-filter and scheduling flow",
    )
