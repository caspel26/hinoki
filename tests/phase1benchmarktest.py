"""Metrics helpers used by the generated CRUD orientation benchmark."""

import sys
from importlib import import_module
from pathlib import Path

import pytest


def test_phase1_request_count_uses_status_counts_not_sample_duration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = Path(__file__).parent.parent
    monkeypatch.syspath_prepend(str(repository / "benchmarks/comparative"))
    request_count = import_module("phase1_metrics").request_count
    result = {
        "summary": {"total": 20.0},
        "statusCodeDistribution": {"200": 86_765},
    }

    assert request_count(result) == 86_765


def test_archived_phase1_report_links_to_the_public_raw_data() -> None:
    repository = Path(__file__).parent.parent
    sys.path.insert(0, str(repository / "benchmarks/comparative"))
    try:
        orientation = import_module("phase1_orientation")
    finally:
        sys.path.pop(0)

    note = orientation.raw_results_reference(
        Path("benchmarks/comparative/results/2026-11-18/crud-phase1")
    )
    archive_release = "https://github.com/caspel26/hinoki/releases/download/bench-2026-11-18-crud-phase1/"
    assert archive_release in note
    assert "c32ea9a982b5bc5d30cce8d79f10bc93890603cebb123d4f927f41436049a3b2" in note
