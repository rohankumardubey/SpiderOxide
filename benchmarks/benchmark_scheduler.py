from common import run_cli

if __name__ == "__main__":
    run_cli(
        (
            "scheduler_insert_single",
            "scheduler_insert_batch",
            "scheduler_remove_single",
            "scheduler_remove_batch",
        ),
        "Benchmark priority scheduling",
    )
