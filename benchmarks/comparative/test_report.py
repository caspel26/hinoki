import contextlib
import io
import json
import re
import runpy
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import report  # type: ignore[import-not-found]
import report_html  # type: ignore[import-not-found]

JSONData = dict[str, Any]


def sample(rate: float) -> JSONData:
    """An `oha` document, the legacy format the report still reads."""
    return {
        "summary": {"requestsPerSec": rate},
        "statusCodeDistribution": {"200": 500},
        "errorDistribution": {"aborted due to deadline": 64},
        "latencyPercentiles": {"p50": 0.001, "p95": 0.003, "p99": 0.005},
    }


def bombardier_sample(rate: float, p50_us: int = 800, p99_us: int = 2500) -> JSONData:
    return {
        "spec": {"numberOfConnections": 100, "numberOfRequests": 100000},
        "result": {
            "timeTakenSeconds": 100000 / rate,
            "req1xx": 0,
            "req2xx": 100000,
            "req3xx": 0,
            "req4xx": 0,
            "req5xx": 0,
            "others": 0,
            "latency": {
                "mean": 900.0,
                "percentiles": {
                    "50": p50_us,
                    "75": 900,
                    "90": 1200,
                    "95": 1500,
                    "99": p99_us,
                },
            },
            "rps": {"mean": 1.0},
        },
    }


def write_environment(
    directory: Path,
    mode: str,
    stacks: list[str],
    date: str = "2026-11-21 06:00:00 +0100",
    restarts: int = 1,
    runs: int = 3,
    server_cpus: str | None = "0-3",
) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "environment.json").write_text(
        json.dumps(
            {
                "date": date,
                "cpu": "Test CPU",
                "cores": 8,
                "machine": "Linux",
                "loadavg_at_start": [0.1, 0.2, 0.3],
                "postgres": "PostgreSQL 17",
                "load_tool": "bombardier",
                "bombardier": "v1.2.6",
                "host": {"memory_bytes": 17179869184},
                "settings": {
                    "server_mode": mode,
                    "stacks": stacks,
                    "workers": 4,
                    "server_cpus": server_cpus,
                    "load_cpus": "6-7" if server_cpus else None,
                    "concurrency": 100,
                    "requests": 100000,
                    "restarts": restarts,
                    "warmup_s": 10,
                    "runs": runs,
                },
            }
        )
    )


def write_results(
    directory: Path,
    mode: str,
    rates: dict[str, tuple[float, ...]],
    overrides: dict[tuple[str, str], tuple[float, ...]] | None = None,
    scenarios: tuple[str, ...] = tuple(report.SCENARIOS),
    **environment: Any,
) -> None:
    """Bombardier samples, one server start, ``rates`` per stack and ``overrides`` per scenario."""
    write_environment(directory, mode, list(rates), **environment)
    for stack, stack_rates in rates.items():
        (directory / f"{stack}-verify.txt").write_text("OK")
        for scenario in scenarios:
            values = (overrides or {}).get((stack, scenario), stack_rates)
            for number, rate in enumerate(values, start=1):
                (directory / f"{stack}-{scenario}-r1-{number}.json").write_text(
                    json.dumps(bombardier_sample(rate))
                )


def write_legacy_results(directory: Path, stack: str = "hinoki") -> None:
    write_environment(directory, "documented", [stack], runs=2)
    env_path = directory / "environment.json"
    env = json.loads(env_path.read_text())
    env["load_tool"] = "oha"
    env["oha"] = "oha 1.17"
    env["settings"] = {
        **env["settings"],
        "concurrency": 64,
        "runs": 2,
        "duration_s": 20,
    }
    del env["settings"]["requests"], env["settings"]["restarts"], env["host"]
    env_path.write_text(json.dumps(env))
    (directory / f"{stack}-verify.txt").write_text("OK")
    for scenario in report.SCENARIOS:
        for index, rate in enumerate((10.0, 12.0), start=1):
            (directory / f"{stack}-{scenario}-{index}.json").write_text(
                json.dumps(sample(rate))
            )


def write_workers(directory: Path, stack: str, cpu_times: list[str]) -> None:
    rows = "\n".join(f"{n} 1 {t} worker" for n, t in enumerate(cpu_times, start=2))
    (directory / f"{stack}-workers.txt").write_text(
        f"pid pgid cputime command\n{rows}\n"
    )


def page_of(*directories: Path) -> str:
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        report.main(list(directories))
    return buffer.getvalue()


@contextlib.contextmanager
def results_root() -> Iterator[Path]:
    with tempfile.TemporaryDirectory() as raw:
        yield Path(raw) / "results" / "2026-11-21"


def full_session(root: Path, kiso_overrides: Any = None) -> list[Path]:
    """Documented, same-Granian and Kiso runs, the Kiso one taken at another time."""
    documented, granian, kiso = (
        root / "documented",
        root / "same-granian",
        root / "kiso",
    )
    write_results(
        documented,
        "documented",
        {"hinoki": (100.0, 101.0, 99.0), "fastapi": (60.0, 61.0, 59.0)},
    )
    write_results(
        granian,
        "same-granian",
        {"hinoki-granian": (100.0, 101.0, 99.0), "fastapi-granian": (70.0, 71.0, 69.0)},
    )
    write_results(
        kiso,
        "kiso",
        {"hinoki-kiso": (150.0, 151.0, 149.0)},
        overrides=kiso_overrides,
        date="2026-11-21 19:56:02 +0100",
    )
    return [documented, granian, kiso]


def test_summarizes_median_range_and_unexpected_responses() -> None:
    summary = report.run_summary([sample(10), sample(20), sample(40)], "200")
    assert summary["latency_ms"]["p99"] == {"median": 5.0, "low": 5.0, "high": 5.0}

    assert summary["median"] == 20
    assert (summary["low"], summary["high"]) == (10, 40)
    assert summary["wrong"] == 0
    assert summary["runs"] == 3
    assert summary["cv_pct"] > 15
    assert summary["samples"] == [10, 20, 40]


def test_latency_spread_is_median_of_per_run_percentiles_with_run_range() -> None:
    runs = [sample(10), sample(10), sample(10)]
    for run, p95 in zip(runs, (0.002, 0.004, 0.010), strict=True):
        run["latencyPercentiles"]["p95"] = p95

    p95 = report.run_summary(runs, "200")["latency_ms"]["p95"]

    assert p95 == {"median": 4.0, "low": 2.0, "high": 10.0}


def test_value_axis_labels_zero_midpoint_and_maximum() -> None:
    axis = report_html.value_axis(12000.0)

    assert axis.count("<span>") == 4
    assert "<span>0</span><span>6,000</span><span>12,000</span>" in axis
    assert 'aria-hidden="true"' in axis


def test_stack_labels_name_the_framework_and_the_server_the_same_way_everywhere() -> (
    None
):
    assert report.stack_label("hinoki") == "Hinoki (Granian)"
    assert report.stack_label("hinoki-granian") == "Hinoki (Granian)"
    assert report.stack_label("hinoki-kiso") == "Hinoki (Kiso, native server)"
    assert report.stack_label("fastapi") == "FastAPI (Uvicorn)"
    assert report.stack_label("fastapi-granian") == "FastAPI (Granian)"
    assert report.stack_label("ninja") == "Django Ninja (Uvicorn)"
    assert report.stack_label("bolt") == "Django Bolt (Actix, native server)"
    assert report.stack_family("fastapi-granian") == "fastapi"
    assert report.stack_family("hinoki") == "hinoki"
    assert report.stack_family("hinoki-kiso") == "hinoki-kiso"


def row(median: float, low: float, high: float, cv: float = 1.0) -> JSONData:
    return {
        "median": median,
        "low": low,
        "high": high,
        "cv_pct": cv,
        "start_medians": [median, median],
        "samples": [low, median, high],
    }


def test_an_unstable_stack_is_explained_instead_of_hidden() -> None:
    steady = row(100.0, 99.0, 101.0, cv=2.0)
    shaky = {**row(100.0, 60.0, 140.0, cv=24.5), "start_medians": [140.0, 60.0]}

    assert report.instability(steady) is None
    reason = report.instability(shaky)
    assert reason is not None
    assert "24.5%" in reason
    assert "above 15%" in reason
    assert "60" in reason
    assert "140" in reason
    assert "two server starts" in reason


def test_a_gap_under_ten_percent_is_a_tie_and_overlapping_ranges_are_not_settled() -> (
    None
):
    base = row(100.0, 95.0, 105.0)

    assert report.compare(row(200.0, 190.0, 210.0), base) == "higher"
    assert report.compare(base, row(200.0, 190.0, 210.0)) == "lower"
    assert report.compare(row(95.0, 92.0, 99.0), base) == "tie"
    assert report.compare(row(150.0, 90.0, 160.0), base) == "unsettled"
    assert report.compare(row(200.0, 190.0, 210.0, cv=30.0), base) == "unsettled"


def test_the_best_stable_cell_is_marked_and_a_close_second_ties_with_it() -> None:
    rows = {
        "hinoki": row(100.0, 99.0, 101.0),
        "hinoki-kiso": row(95.0, 94.0, 96.0),
        "fastapi": row(50.0, 49.0, 51.0),
        "ninja": row(400.0, 100.0, 700.0, cv=40.0),
    }

    best, tied = report.best_stacks(rows)

    assert best == "hinoki"
    assert tied == {"hinoki-kiso"}
    assert report.best_stacks({"ninja": rows["ninja"]}) == (None, set())


def test_one_table_shows_every_stack_together_with_hinoki_on_granian_next_to_kiso() -> (
    None
):
    with results_root() as root:
        page = page_of(*full_session(root))

    matrix = page[page.index('<table class="bench-matrix"') :]
    matrix = matrix[: matrix.index("</table>")]
    columns = re.findall(r'<th scope="col"[^>]*>(?:<[^>]+>)*([^<]+)', matrix)
    assert columns[:4] == [
        "Request type",
        "Hinoki (Granian)",
        "Hinoki (Kiso, native server)",
        "FastAPI (Uvicorn)",
    ]
    assert "<caption>" in matrix
    assert page.count('<table class="bench-matrix"') == 3
    assert "Django Bolt" not in matrix


def test_the_best_cell_of_each_row_is_highlighted_for_text_readers_too() -> None:
    with results_root() as root:
        page = page_of(*full_session(root))

    overview = page[page.index('<table class="bench-matrix"') :]
    overview = overview[: overview.index("</table>")]
    assert overview.count("is-best") == len(report.SCENARIOS)
    assert re.search(r'is-best[^>]*>.*?<span class="bench-sr">Highest median', overview)
    assert "150" in overview  # Kiso, the highest median of this fixture


def test_the_first_lines_say_what_was_measured_and_how_kiso_compares_with_granian() -> (
    None
):
    overrides = {
        ("hinoki-kiso", "health"): (200.0, 201.0, 199.0),
        ("hinoki-kiso", "list"): (200.0, 201.0, 199.0),
        ("hinoki-kiso", "detail"): (101.0, 102.0, 100.0),
        ("hinoki-kiso", "create"): (50.0, 51.0, 49.0),
    }
    with results_root() as root:
        page = page_of(*full_session(root, overrides))

    lead = page[page.index('<div class="bench-lead"') :]
    lead = lead[: lead.index("</div>")]
    assert "Test CPU" in lead
    assert "8 logical CPUs" in lead
    assert "2026-11-21" in lead
    assert "100 concurrent connections" in lead
    assert (
        "Hinoki (Kiso, native server) against Hinoki (Granian): ahead in 2 of 4 "
        "request types, tied (within 10%) in 1, behind in 1." in lead
    )
    assert "not a ranking" in lead.lower()
    assert page.index("bench-lead") < page.index('<table class="bench-matrix"')


def test_without_a_kiso_run_the_page_makes_no_kiso_claim() -> None:
    with results_root() as root:
        sessions = full_session(root)
        page = page_of(*sessions[:2])

    assert "Kiso" not in page
    assert "against Hinoki" not in page


def test_an_unstable_result_stays_visible_with_its_reason_and_every_sample() -> None:
    overrides = {("hinoki-kiso", "create"): (50.0, 100.0, 150.0, 60.0, 140.0)}
    with results_root() as root:
        page = page_of(*full_session(root, overrides))

    assert "withheld" not in page.lower()
    assert 'class="bench-badge is-unstable"' in page
    assert "Unstable" in page
    assert "coefficient of variation" in page.lower()
    panel = page[page.index('id="bench-request-create"') :]
    panel = panel[: panel.index("</section>")]
    assert "Hinoki (Kiso, native server)" in panel
    assert "above 15%" in panel
    assert "All 5 samples" in panel
    assert "50, 60, 100, 140, 150" in panel


def test_an_unstable_stack_never_wins_the_highest_median_mark() -> None:
    overrides = {("hinoki-kiso", "create"): (10.0, 400.0, 900.0)}
    with results_root() as root:
        page = page_of(*full_session(root, overrides))

    panel = page[page.index('id="bench-request-create"') :]
    panel = panel[: panel.index("</section>")]
    best_row = re.search(r'<tr data-stack="[^"]*" class="is-best">.*?</tr>', panel)
    assert best_row is not None
    assert "Hinoki (Granian)" in best_row[0]


def test_each_request_type_has_a_chart_a_real_table_and_a_labelled_tab() -> None:
    with results_root() as root:
        page = page_of(*full_session(root))

    explorer = page[page.index("data-bench-explorer") :]
    explorer = explorer[: explorer.index("## Server setups in detail")]
    assert explorer.count('role="tab"') == len(report.SCENARIOS)
    assert explorer.count('role="tabpanel"') == len(report.SCENARIOS)
    for scenario in report.SCENARIOS:
        assert f'aria-controls="bench-request-{scenario}"' in explorer
        assert f'id="bench-request-{scenario}"' in explorer
    assert explorer.count('class="bench-chart"') == len(report.SCENARIOS)
    assert explorer.count('class="bench-chart" aria-hidden="true"') == len(
        report.SCENARIOS
    )
    assert explorer.count("<caption>") == len(report.SCENARIOS)
    for heading in ("Median req/s", "Lowest&ndash;highest", "p50 ms", "p99 ms", "CV"):
        assert f'<th scope="col">{heading}</th>' in explorer
    assert page.count('aria-pressed="true"') == 3  # the one legend, a toggle per stack


def test_the_legend_names_each_stack_once_with_a_stable_family_key() -> None:
    with results_root() as root:
        page = page_of(*full_session(root))

    legend = page[page.index('<ul class="bench-legend"') :]
    legend = legend[: legend.index("</ul>")]
    assert legend.count("<li") == 3
    assert 'data-stack="hinoki"' in legend
    assert 'data-stack="hinoki-kiso"' in legend
    assert 'data-stack="fastapi"' in legend


def test_the_expert_views_keep_documented_and_same_granian_below_the_overview() -> None:
    with results_root() as root:
        page = page_of(*full_session(root))

    assert page.index("## Results at a glance") < page.index(
        "## Server setups in detail"
    )
    assert '=== "Each stack on its own server"' in page
    assert '=== "All on the same server (Granian)"' in page
    detail = page[page.index("## Server setups in detail") :]
    assert "FastAPI (Granian)" in detail
    assert "Kiso" not in detail.split("## Method and limits")[0]


def test_the_page_has_the_sections_a_reader_needs_in_order() -> None:
    with results_root() as root:
        page = page_of(*full_session(root))

    headings = [
        "# Benchmarks",
        "## Results at a glance",
        "## One request type at a time",
        "## Server setups in detail",
        "## Method and limits",
    ]
    positions = [page.index(heading) for heading in headings]
    assert positions == sorted(positions)
    assert page.index("FIELD NOTE") < page.index("# Benchmarks")
    assert not page.startswith("---")
    for title in ("Conditions", "How the numbers are computed", "Reproduce"):
        assert f'??? info "{title}"' in page


def test_the_measurement_sessions_are_listed_when_they_differ() -> None:
    with results_root() as root:
        page = page_of(*full_session(root))

    assert "separate run" in page
    assert "19:56" in page


def test_the_conditions_state_where_servers_and_load_generator_ran() -> None:
    with results_root() as root:
        page = page_of(*full_session(root))
        directory = root / "unpinned"
        write_results(
            directory, "documented", {"hinoki": (1.0, 1.1, 0.9)}, server_cpus=None
        )
        unpinned = page_of(directory)

    assert (
        "| CPU placement | server processes on CPUs 0-3; load generator on CPUs 6-7 |"
        in page
    )
    assert "no CPU pinning" in unpinned
    assert "CPUs None" not in unpinned


def test_no_other_platform_run_is_mentioned() -> None:
    with results_root() as root:
        page = page_of(*full_session(root))

    for word in ("macOS", "Apple", "Docker Desktop", "oha"):
        assert word not in page


def test_missing_raw_scenario_fails_instead_of_partial_report() -> None:
    with (
        tempfile.TemporaryDirectory() as temporary,
        pytest.raises(ValueError, match="missing raw results"),
    ):
        report.load_runs(Path(temporary), "hinoki", "list")


def test_missing_metadata_and_invalid_stack_configuration_are_rejected() -> None:
    with tempfile.TemporaryDirectory() as raw:
        directory = Path(raw)
        with pytest.raises(ValueError, match="missing environment metadata"):
            report.scenario_rows(directory)
        with pytest.raises(ValueError, match="no benchmark stacks"):
            report.stack_names(directory, {})
        with pytest.raises(ValueError, match="unknown stack"):
            report.stack_names(directory, {"stacks": ["unknown"]})
        with pytest.raises(ValueError, match="missing verification output"):
            report.stack_names(directory, {"stacks": ["hinoki"]})


def test_report_cli_generates_a_page_from_retained_sample_files(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    with results_root() as directory:
        write_results(directory, "documented", {"hinoki": (10.0, 12.0, 11.0)})

        monkeypatch.setattr(sys, "argv", [str(report.__file__), str(directory)])
        runpy.run_path(str(report.__file__), run_name="__main__")
        output = capsys.readouterr().out

    assert "# Benchmarks" in output
    assert "Hinoki (Granian)" in output


def test_report_cli_requires_at_least_one_results_directory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "argv", [str(report.__file__)])

    with pytest.raises(SystemExit, match=r"usage: report\.py RESULTS_DIR"):
        runpy.run_path(str(report.__file__), run_name="__main__")


def test_stack_with_one_busy_process_is_flagged_but_balanced_stacks_are_not() -> None:
    with tempfile.TemporaryDirectory() as raw:
        directory = Path(raw)
        write_workers(
            directory, "bolt", ["0:00.30", "0:00.02", "0:00.02", "0:00.02", "8:40.38"]
        )
        write_workers(
            directory,
            "hinoki",
            ["0:00.08", "0:00.02", "6:40.31", "10:27.76", "9:55.71", "10:25.25"],
        )
        write_workers(
            directory, "ninja", ["0:00.30", "8:42.06", "8:42.37", "8:42.29", "8:42.83"]
        )

        flagged = report.single_process_stacks(
            directory, ["bolt", "hinoki", "ninja", "fastapi"], 4
        )

    assert flagged == ["bolt"]


def test_report_explains_when_a_stack_is_excluded_for_worker_imbalance() -> None:
    with results_root() as root:
        write_results(
            root / "documented",
            "documented",
            {"hinoki": (10.0, 11.0, 12.0), "bolt": (20.0, 21.0, 22.0)},
        )
        write_workers(
            root / "documented", "bolt", ["10:00.00", "0:05.00", "0:05.00", "0:05.00"]
        )

        page = page_of(root / "documented")

    assert "Django Bolt (Actix, native server) is not reported" in page
    assert "one process did nearly all the work" in page
    assert '<th scope="col" data-stack="bolt"' not in page


def test_cpu_time_accepts_macos_and_linux_ps_formats() -> None:
    assert report.cpu_seconds("1:02.50") == 62.5
    assert report.cpu_seconds("00:05:59") == 359
    assert report.cpu_seconds("01:02:03") == 3723
    assert report.cpu_seconds("2-00:00:01") == 172801


def test_run_py_offers_a_kiso_mode_that_serves_with_the_rust_server() -> None:
    import run  # type: ignore[import-not-found]

    assert run.SERVER_MODES["kiso"] is run.KISO_SERVERS
    command, _ = run.KISO_SERVERS["hinoki-kiso"]
    assert command[:2] == ["hinoki", "serve"]
    assert command[command.index("--server") + 1] == "kiso"
    assert not set(run.KISO_SERVERS) & (
        set(run.DOCUMENTED_SERVERS) | set(run.GRANIAN_SERVERS)
    )


def test_a_sample_waits_for_the_load_to_fall_and_records_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import run  # type: ignore[import-not-found]

    loads = iter([2.5, 1.8, 0.9])
    sleeps: list[float] = []
    monkeypatch.setattr(run.os, "getloadavg", lambda: (next(loads), 0.0, 0.0))
    monkeypatch.setattr(run.time, "sleep", sleeps.append)
    log = tmp_path / "loadavg.txt"

    run.wait_for_quiet(1.0, log, "hinoki-kiso-list-1")

    assert sleeps == [run.QUIET_POLL, run.QUIET_POLL]
    assert log.read_text() == "hinoki-kiso-list-1 load1=0.90\n"


def test_a_host_that_stays_busy_stops_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import run  # type: ignore[import-not-found]

    clock = iter(range(0, 10_000, 600))
    monkeypatch.setattr(run.os, "getloadavg", lambda: (3.0, 0.0, 0.0))
    monkeypatch.setattr(run.time, "sleep", lambda _: None)
    monkeypatch.setattr(run.time, "monotonic", lambda: next(clock))

    with pytest.raises(SystemExit, match="load average stayed above"):
        run.wait_for_quiet(1.0, tmp_path / "loadavg.txt", "x")
    assert not (tmp_path / "loadavg.txt").exists()


def test_without_a_limit_the_load_is_only_recorded(tmp_path: Path) -> None:
    import run  # type: ignore[import-not-found]

    run.wait_for_quiet(None, tmp_path / "loadavg.txt", "x")

    assert (tmp_path / "loadavg.txt").read_text().startswith("x load1=")


def test_bombardier_samples_are_pooled_across_starts_without_discarding_any() -> None:
    with results_root() as directory:
        write_environment(directory, "documented", ["hinoki"], restarts=2)
        (directory / "hinoki-verify.txt").write_text("OK")
        for restart, values in {
            1: (100.0, 102.0, 98.0),
            2: (200.0, 202.0, 1.0),
        }.items():
            for number, rate in enumerate(values, start=1):
                path = directory / f"hinoki-list-r{restart}-{number}.json"
                path.write_text(json.dumps(bombardier_sample(rate)))

        summary = report.run_summary(
            report.load_runs(directory, "hinoki", "list"), "200"
        )

    assert summary["runs"] == 6
    assert summary["starts"] == 2
    assert summary["median"] == pytest.approx(101.0)
    assert (summary["low"], summary["high"]) == (
        pytest.approx(1.0),
        pytest.approx(202.0),
    )
    assert summary["start_medians"] == [pytest.approx(100.0), pytest.approx(200.0)]
    assert summary["latency_ms"]["p50"]["median"] == pytest.approx(0.8)
    assert summary["latency_ms"]["p99"]["median"] == pytest.approx(2.5)
    assert summary["wrong"] == 0


def test_bombardier_report_states_conditions_and_the_aggregation_rule() -> None:
    with results_root() as directory:
        write_results(directory, "documented", {"hinoki": (100.0, 101.0)}, restarts=2)
        page = page_of(directory)

    assert "Samples are pooled" in page
    assert "none is discarded" in page
    assert "| Load generator | bombardier v1.2.6 |" in page
    assert "| Connections | 100 |" in page
    assert "| Requests per sample | 100,000 |" in page
    assert "| Memory | 16 GiB |" in page
    assert "| Date | 2026-11-21 06:00:00 +0100 |" in page
    assert "run.py --server-mode documented" in page
    assert "2 server starts" in page


def test_json_payload_scenarios_are_reported_only_when_their_files_exist() -> None:
    with results_root() as root:
        write_results(
            root / "current",
            "documented",
            {"hinoki": (100.0, 101.0)},
            scenarios=(*report.SCENARIOS, "json-1k", "json-10k"),
        )
        write_legacy_results(root / "legacy")

        reported = report.scenario_rows(root / "current")[0]
        legacy_reported = report.scenario_rows(root / "legacy")[0]

    assert {"json-1k", "json-10k"} <= set(reported)
    assert not {"json-1k", "json-10k"} & set(legacy_reported)


def test_scenarios_follow_the_order_without_a_database_first() -> None:
    with results_root() as root:
        write_results(
            root / "documented",
            "documented",
            {"hinoki": (100.0, 101.0)},
            scenarios=(*report.SCENARIOS, "json-1k", "json-10k"),
        )
        page = page_of(root / "documented")

    table = page[page.index('<table class="bench-matrix"') :]
    names = re.findall(
        r'<th scope="row">([^<]+)</th>', table[: table.index("</table>")]
    )
    assert names == ["Health", "JSON 1 KB", "JSON 10 KB", "List", "Detail", "Create"]


def test_legacy_oha_results_are_labelled_and_not_mixed_with_bombardier() -> None:
    with results_root() as root:
        write_legacy_results(root / "legacy")
        write_results(root / "current", "kiso", {"hinoki-kiso": (100.0, 101.0)})

        legacy = page_of(root / "legacy")
        mixed = report.format_warning([root / "legacy", root / "current"])
        alone = report.format_warning([root / "current"])

    assert "sampled with `oha`" in legacy
    assert "not comparable" in legacy
    assert "| Load generator | oha 1.17 |" in legacy
    assert "| Memory | not recorded |" in legacy
    assert "different load generators" in mixed
    assert alone == ""
