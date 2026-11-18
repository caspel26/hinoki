"""Metrics helpers used by the generated CRUD orientation benchmark."""

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
