from common import run_cli

if __name__ == "__main__":
    run_cli(
        ("fingerprint_single", "fingerprint_batch"),
        "Benchmark request fingerprinting",
    )
