"""Stable links for benchmark data kept in GitHub release assets."""

import re
from pathlib import Path

ARCHIVED_RESULTS = {
    "2026-11-27": (
        "[the archived raw results](https://github.com/caspel26/hinoki/releases/download/bench-2026-11-27/bench-2026-11-27.tar.gz) "
        "(SHA-256: `a70e7a9be78f32de2af74bc117928c184f70f5d55878a4d7bfc4296df618048d`; "
        "[checksum file](https://github.com/caspel26/hinoki/releases/download/bench-2026-11-27/bench-2026-11-27.tar.gz.sha256))"
    ),
    "2026-11-25": (
        "[the archived raw results](https://github.com/caspel26/hinoki/releases/download/bench-2026-11-25/bench-2026-11-25.tar.gz) "
        "(SHA-256: `0db26f6e288a42f9532f9aad3e927e28690386ce0f262ee3ed1b456a4f32d7d5`; "
        "[checksum file](https://github.com/caspel26/hinoki/releases/download/bench-2026-11-25/bench-2026-11-25.tar.gz.sha256))"
    ),
}


def result_date(directory: Path) -> str:
    return next(
        (
            parent.name
            for parent in (directory, *directory.parents)
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", parent.name)
        ),
        directory.parent.name,
    )


def comparative_results_reference(run_date: str) -> str:
    return ARCHIVED_RESULTS.get(
        run_date, f"`benchmarks/comparative/results/{run_date}/`"
    )


def phase1_results_reference(raw_path: Path) -> str:
    if raw_path.as_posix() == "benchmarks/comparative/results/2026-11-18/crud-phase1":
        return (
            "Raw `oha` JSON, per-sample start-load metadata and SQL stats, server logs, "
            "environment metadata, verifier output, request bodies, and the machine-readable "
            "summary are available in the [2026-11-18 benchmark archive](https://github.com/"
            "caspel26/hinoki/releases/download/bench-2026-11-18-crud-phase1/"
            "bench-2026-11-18-crud-phase1.tar.gz) (SHA-256: "
            "`c32ea9a982b5bc5d30cce8d79f10bc93890603cebb123d4f927f41436049a3b2`; "
            "[checksum file](https://github.com/caspel26/hinoki/releases/download/"
            "bench-2026-11-18-crud-phase1/bench-2026-11-18-crud-phase1.tar.gz.sha256)). "
            "The archive manifest records per-file hashes and measurement provenance."
        )
    return (
        "Raw `oha` JSON, per-sample start-load metadata and SQL stats, server logs, "
        "environment metadata, verifier output, request bodies, and the machine-readable "
        f"summary are retained in `{raw_path}/`."
    )
