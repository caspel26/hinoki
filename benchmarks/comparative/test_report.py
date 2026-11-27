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
    *,
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
                "postgres": "PostgreSQL 16",
                "load_tool": "bombardier",
                "bombardier": "v1.2.6",
                "host": {"memory_bytes": 17179869184},
                "settings": {
                    "server_mode": mode,
                    "stacks": stacks,
                    "workers": 1,
                    "pool_per_worker": 8,
                    "server_cpus": server_cpus,
                    "load_cpus": "6-7" if server_cpus else None,
                    "concurrency": 100,
                    "duration_s": 10,
                    "restarts": restarts,
                    "warmup": {"requests": 1000, "connections": 10},
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
        "warmup_s": 10,
    }
    del env["settings"]["restarts"], env["settings"]["warmup"], env["host"]
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


X = "\u00d7"


def view_of(page: str, name: str, workers: int = 1) -> str:
    """The markup of one view (python, same-granian, other-runtimes) of one worker count."""
    start = page.index(f'id="bench-w{workers}-{name}"')
    return page[start : page.index("</div></section>", start)]


def panel_of(page: str, scenario: str, view: str = "python", workers: int = 1) -> str:
    start = page.index(f'id="bench-w{workers}-{view}-{scenario}-panel"')
    return page[start : page.index("</section>", start)]


def route_rows(panel: str) -> list[str]:
    return re.findall(r'<tr class="[^"]*" data-stack="[^"]*">.*?</tr>', panel)


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


def test_stack_labels_name_the_framework_and_the_server_the_same_way_everywhere() -> (
    None
):
    assert report.stack_label("hinoki") == "Hinoki (Granian)"
    assert report.stack_label("hinoki-granian") == "Hinoki (Granian)"
    assert report.stack_label("hinoki-kiso") == "Hinoki (Kiso, native server)"
    assert report.stack_label("fastapi") == "FastAPI (Uvicorn)"
    assert report.stack_label("fastapi-granian") == "FastAPI (Granian)"
    assert report.stack_label("ninja") == "Django Ninja (Uvicorn)"
    assert report.stack_label("litestar") == "Litestar (Uvicorn)"
    assert report.stack_label("litestar-granian") == "Litestar (Granian)"
    assert report.stack_label("bolt") == "Django Bolt (Actix, native server)"
    assert report.stack_family("fastapi-granian") == "fastapi"
    assert report.stack_family("litestar-granian") == "litestar"
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


def test_the_python_table_puts_kiso_first_and_compares_every_stack_with_it() -> None:
    with results_root() as root:
        page = page_of(*full_session(root))

    panel = panel_of(page, "json-1k")
    rows = route_rows(panel)
    labels = [re.search(r"<th scope=\"row\">([^<]+)", r)[1] for r in rows]
    assert labels == [
        "Hinoki (Kiso, native server)",
        "Hinoki (Granian)",
        "FastAPI (Uvicorn)",
    ]
    assert "vs Hinoki (Kiso)" in panel
    assert ">reference<" in rows[0]
    assert f">1.5{X} slower<" in rows[1]  # 100 against 150
    assert f">2.5{X} slower<" in rows[2]  # 60 against 150
    assert "Django Bolt (Actix" not in panel


def test_the_best_row_of_each_request_type_is_highlighted_for_text_readers_too() -> (
    None
):
    with results_root() as root:
        page = page_of(*full_session(root))

    for scenario in report.SCENARIOS:
        best = [
            r for r in route_rows(panel_of(page, scenario)) if 'class="is-best"' in r
        ]
        assert len(best) == 1
        assert "Hinoki (Kiso, native server)" in best[0]  # 150, the highest here
        assert '<span class="bench-sr">Highest median' in best[0]


def test_the_first_lines_say_what_was_measured_and_the_summary_how_kiso_compares() -> (
    None
):
    overrides = {
        ("hinoki-kiso", "json-1k"): (200.0, 201.0, 199.0),
        ("hinoki-kiso", "json-10k"): (200.0, 201.0, 199.0),
        ("hinoki-kiso", "db"): (200.0, 201.0, 199.0),
        ("hinoki-kiso", "articles"): (101.0, 102.0, 100.0),
        ("hinoki-kiso", "article"): (50.0, 51.0, 49.0),
        ("hinoki-kiso", "auth-me"): (50.0, 51.0, 49.0),
        ("hinoki-kiso", "auth-articles"): (50.0, 150.0, 250.0),
    }
    with results_root() as root:
        page = page_of(*full_session(root, overrides))

    lead = page[page.index('<div class="bench-lead"') :]
    lead = lead[: lead.index("</div>")]
    assert "Test CPU" in lead
    assert "8 logical CPUs" in lead
    assert "2026-11-21" in lead
    assert "100 concurrent connections" in lead
    assert "1 worker (single-process)" in lead
    assert "not a ranking" in lead.lower()
    assert (
        "Hinoki (Kiso, native server) against Hinoki (Granian): ahead in 3 of 7 "
        "request types, tied (within 10%) in 1, behind in 2, not settled in 1 "
        "(overlapping ranges or an unstable result)." in page
    )
    assert page.index("bench-lead") < page.index('<table class="bench-summary"')


def test_without_a_kiso_run_the_page_makes_no_kiso_claim() -> None:
    with results_root() as root:
        sessions = full_session(root)
        page = page_of(*sessions[:2])

    assert "Kiso" not in page
    assert "against Hinoki" not in page


def test_an_unstable_result_stays_visible_with_its_reason_and_every_sample() -> None:
    overrides = {("hinoki-kiso", "auth-articles"): (50.0, 100.0, 150.0, 60.0, 140.0)}
    with results_root() as root:
        page = page_of(*full_session(root, overrides))

    assert "withheld" not in page.lower()
    assert 'class="bench-badge is-unstable"' in page
    assert "Unstable" in page
    assert "coefficient of variation" in page.lower()
    panel = panel_of(page, "auth-articles")
    assert "Hinoki (Kiso, native server)" in panel
    assert "above 15%" in panel
    assert "All 5 samples" in panel
    assert "50, 60, 100, 140, 150" in panel
    assert "not settled" in panel  # nothing is compared with an unstable result


def test_an_unstable_stack_never_wins_the_highest_median_mark() -> None:
    overrides = {("hinoki-kiso", "auth-articles"): (10.0, 400.0, 900.0)}
    with results_root() as root:
        page = page_of(*full_session(root, overrides))

    best = [
        r for r in route_rows(panel_of(page, "auth-articles")) if 'class="is-best"' in r
    ]
    assert len(best) == 1
    assert "Hinoki (Granian)" in best[0]


def test_each_request_type_has_a_real_table_and_a_labelled_tab() -> None:
    with results_root() as root:
        page = page_of(*full_session(root))

    view = view_of(page, "python")
    assert view.count('role="tab"') == len(report.SCENARIOS)
    assert view.count('role="tabpanel"') == len(report.SCENARIOS)
    for scenario in report.SCENARIOS:
        assert f'aria-controls="bench-w1-python-{scenario}-panel"' in view
        assert f'id="bench-w1-python-{scenario}-tab"' in view
    assert view.count('<table class="bench-route">') == len(report.SCENARIOS)
    assert view.count("<caption>") == 2 * len(report.SCENARIOS)  # route and exact
    for heading in (
        "Requests per second",
        "p50 ms",
        "p99 ms",
        "Lowest&ndash;highest",
    ):
        assert f'<th scope="col">{heading}</th>' in view
    assert "bench-chart" not in page
    assert "bench-chip" not in page


def test_each_row_keeps_a_stable_family_key_for_its_colour() -> None:
    with results_root() as root:
        page = page_of(*full_session(root))

    rows = "".join(route_rows(panel_of(page, "db")))
    for family in ("hinoki", "hinoki-kiso", "fastapi"):
        assert rows.count(f'data-stack="{family}"') == 1


def test_the_same_server_view_follows_the_python_view_without_kiso() -> None:
    with results_root() as root:
        page = page_of(*full_session(root))

    assert page.index("<h3>Python frameworks</h3>") < page.index(
        "<h3>Same ASGI server (Granian)</h3>"
    )
    same = view_of(page, "same-granian")
    assert "FastAPI (Granian)" in same
    assert "Kiso" not in same
    assert "Each stack on its own server" not in page


def test_the_page_has_the_sections_a_reader_needs_in_order() -> None:
    with results_root() as root:
        page = page_of(*full_session(root))

    headings = [
        "# Benchmarks",
        "<h2>Results with 1 worker (single-process)</h2>",
        "<h3>Summary</h3>",
        "<h3>Python frameworks</h3>",
        "<h3>Same ASGI server (Granian)</h3>",
        "## Method and limits",
    ]
    positions = [page.index(heading) for heading in headings]
    assert positions == sorted(positions)
    assert page.index("FIELD NOTE") < page.index("# Benchmarks")
    assert not page.startswith("---")
    for title in (
        "Conditions",
        "Workloads, stacks and versions",
        "How the numbers are computed",
        "Reproduce",
    ):
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
        report.load_runs(Path(temporary), "hinoki", "db")


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
        env_path = root / "documented" / "environment.json"
        env = json.loads(env_path.read_text())
        env["settings"]["workers"] = 4
        env_path.write_text(json.dumps(env))
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

    kiso = run.SERVER_MODES["kiso"]
    command, _ = kiso["hinoki-kiso"]
    assert command[:2] == ["hinoki", "serve"]
    assert command[command.index("--server") + 1] == "kiso"
    others = set(run.SERVER_MODES["documented"]) | set(run.SERVER_MODES["same-granian"])
    assert not set(kiso) & others


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
                path = directory / f"hinoki-db-r{restart}-{number}.json"
                path.write_text(json.dumps(bombardier_sample(rate)))

        summary = report.run_summary(report.load_runs(directory, "hinoki", "db"), "200")

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
    assert "| Requests per sample | not fixed: 10 s per sample |" in page
    assert (
        "| Warm-up | 1,000 requests at 10 connections per scenario and start |" in page
    )
    assert "| Server processes | 1 per stack |" in page
    assert "| Database pool | 8 connections per process |" in page
    assert "| Memory | 16 GiB |" in page
    assert "| Date | 2026-11-21 06:00:00 +0100 |" in page
    assert (
        "run.py --server-mode documented --workers 1 --pool 8 --restarts 2 --runs 3 "
        "--duration 10 --out" in page
    )
    assert "2 server starts, each with 3 sample(s) of 10 s per scenario" in page
    assert "Each sample is a fixed duration" in page


def test_a_run_narrowed_to_some_routes_reports_only_those() -> None:
    with results_root() as root:
        directory = root / "documented"
        write_results(
            directory, "documented", {"hinoki": (100.0, 101.0)}, scenarios=("db",)
        )
        env = json.loads((directory / "environment.json").read_text())
        env["settings"]["scenarios"] = ["db"]
        (directory / "environment.json").write_text(json.dumps(env))

        reported = report.scenario_rows(directory)[0]
        page = page_of(directory)

    assert list(reported) == ["db"]
    assert "`GET /db`" in page
    assert "`GET /json-1k`" not in page


def test_scenarios_follow_bolts_order_without_a_database_first() -> None:
    with results_root() as root:
        write_results(root / "documented", "documented", {"hinoki": (100.0, 101.0)})
        page = page_of(root / "documented")

    names = re.findall(r'role="tab"[^>]*>([^<]+)</button>', page)
    assert names == [
        "JSON 1 KB",
        "JSON 10 KB",
        "Users",
        "Articles",
        "Article",
        "Auth: me",
        "Auth: articles",
    ]


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


JS_VERSIONS = {
    "runtimes": {"node": "26.12.0", "bun": "1.4.5"},
    "packages": {"elysia": "1.4.32", "hono": "4.13.21", "@hono/node-server": "2.1.11", "pg": "8.23.1"},
}  # fmt: skip


def with_javascript_run(root: Path) -> list[Path]:
    """A documented Python run and a later JavaScript run with its runtime versions."""
    write_results(
        root / "documented",
        "documented",
        {"hinoki": (100.0, 101.0, 99.0), "fastapi": (60.0, 61.0, 59.0)},
    )
    javascript = root / "javascript"
    write_results(
        javascript,
        "javascript",
        {"elysia-bun": (90.0, 91.0, 89.0), "hono-bun": (80.0, 81.0, 79.0), "hono-node": (70.0, 71.0, 69.0)},
        date="2026-11-21 20:10:00 +0100",
    )  # fmt: skip
    env = json.loads((javascript / "environment.json").read_text())
    (javascript / "environment.json").write_text(
        json.dumps({**env, "javascript": JS_VERSIONS})
    )
    return [root / "documented", javascript]


def test_javascript_stacks_are_labelled_by_framework_and_runtime() -> None:
    javascript = ("elysia-bun", "hono-bun", "hono-node")

    assert [report.stack_label(key) for key in javascript] == [
        "Elysia (Bun)",
        "Hono (Bun)",
        "Hono (Node.js)",
    ]
    assert {report.STACKS[key].group for key in javascript} == {"Other runtimes"}
    assert len({report.stack_family(key) for key in javascript}) == 3
    assert {report.STACKS[key].group for key in ("hinoki", "bolt")} == {"Python"}


def test_other_runtimes_are_their_own_view_after_the_python_ones() -> None:
    with results_root() as root:
        page = page_of(*with_javascript_run(root))

    assert page.index("<h3>Python frameworks</h3>") < page.index(
        "<h3>Other runtimes (Bun and Node.js)</h3>"
    )
    other = view_of(page, "other-runtimes")
    for label in ("Elysia (Bun)", "Hono (Bun)", "Hono (Node.js)"):
        assert label in other
    python = view_of(page, "python")
    assert "Elysia" not in python
    assert "JavaScript runtimes" in page


def test_the_highest_mark_and_cv_rule_apply_inside_the_other_runtimes_view() -> None:
    with results_root() as root:
        page = page_of(*with_javascript_run(root))

    other = view_of(page, "other-runtimes")
    assert other.count('<tr class="is-best" data-stack') == len(report.SCENARIOS)
    assert "Unstable" not in other


def test_one_worker_count_needs_no_tabs() -> None:
    with results_root() as root:
        page = page_of(*full_session(root))

    assert "bench-tabs--workers" not in page
    assert "bench-workerpanel" not in page
    assert "<h2>Results with 1 worker (single-process)</h2>" in page


def test_method_lists_javascript_runtimes_drivers_and_how_to_install_them() -> None:
    with results_root() as root:
        page = page_of(*with_javascript_run(root))

    assert "| JavaScript runtimes | Node.js 26.12.0; Bun 1.4.5 |" in page
    assert "| Hono on Node.js | node-postgres (pg)" in page
    assert (
        "hono 4.13.21, @hono/node-server 2.1.11, pg 8.23.1, Node.js 26.12.0 |" in page
    )
    assert "| Elysia on Bun | Bun.SQL" in page
    assert "elysia 1.4.32, Bun 1.4.5 |" in page
    assert "| Hono on Bun | Bun.SQL" in page
    assert "hono 4.13.21, Bun 1.4.5 |" in page
    assert "npm ci" in page
    assert "run.py --server-mode javascript" in page


def test_python_only_pages_do_not_mention_javascript() -> None:
    with results_root() as root:
        page = page_of(*full_session(root))

    for word in ("Elysia", "Hono", "Bun", "npm", "JavaScript"):
        assert word not in page


def test_exact_tables_add_the_upper_median_django_bolt_publishes() -> None:
    with results_root() as root:
        write_results(
            root / "documented",
            "documented",
            {"hinoki": (100.0, 200.0), "bolt": (300.0, 500.0, 400.0)},
        )
        page = page_of(root / "documented")

    panel = panel_of(page, "db")
    assert '<th scope="col">Upper median</th>' in panel
    hinoki = re.search(r'<tr data-stack="hinoki".*?</tr>', panel)
    bolt = re.search(r'<tr data-stack="bolt".*?</tr>', panel)
    assert hinoki is not None and bolt is not None
    assert "<td>150</td><td>200</td><td>100&ndash;200</td>" in hinoki[0]
    assert "<td>400</td><td>400</td><td>300&ndash;500</td>" in bolt[0]


def test_a_ratio_is_worded_in_the_direction_it_points() -> None:
    base = row(100.0, 99.0, 101.0)

    assert report.standing(row(240.0, 239.0, 241.0), base) == (
        "higher",
        f"2.4{X} faster",
    )
    assert report.standing(base, row(140.0, 139.0, 141.0)) == (
        "lower",
        f"1.4{X} slower",
    )
    assert report.standing(row(3200.0, 3190.0, 3210.0), base)[1] == f"32{X} faster"
    assert report.standing(row(95.0, 94.0, 96.0), base) == ("tie", "tie (within 10%)")
    assert report.standing(row(150.0, 90.0, 160.0), base) == (
        "unsettled",
        "not settled",
    )


def test_the_summary_shows_where_kiso_is_not_ahead_and_the_data_it_comes_from() -> None:
    with results_root() as root:
        write_results(
            root / "documented",
            "documented",
            {"bolt": (200.0, 201.0, 199.0), "litestar": (60.0, 61.0, 59.0)},
        )
        write_results(root / "kiso", "kiso", {"hinoki-kiso": (150.0, 151.0, 149.0)})
        page = page_of(root / "documented", root / "kiso")

    summary = page[page.index('<table class="bench-summary">') :]
    summary = summary[: summary.index("</table>")]
    text = re.sub(r"<[^>]+>", "", summary)
    assert (
        ">150</b> req/s" in summary
    )  # the median of the Kiso samples, not a typed number
    assert f"1.3{X} slower than Django Bolt (Actix, native server)" in text
    assert f"2.5{X} faster than Litestar (Uvicorn)" in text
    assert text.count("1 worker, 100 connections") == 1
    assert re.search(r'class="bench-cmp is-lower"[^>]*>(?:(?!</td>).)*Bolt', summary)
    assert re.search(
        r'class="bench-cmp is-higher"[^>]*>(?:(?!</td>).)*Litestar', summary
    )


def summary_cells(page: str) -> list[str]:
    summary = page[page.index('<table class="bench-summary">') :]
    return re.findall(r'<td class="bench-cmp[^>]*>.*?</td>', summary)


def test_a_tie_and_an_unsettled_pair_carry_no_ratio() -> None:
    with results_root() as root:
        write_results(
            root / "documented",
            "documented",
            {"bolt": (95.0, 94.0, 96.0), "litestar": (60.0, 20.0, 160.0)},
        )
        write_results(root / "kiso", "kiso", {"hinoki-kiso": (100.0, 99.0, 101.0)})
        page = page_of(root / "documented", root / "kiso")

    cells = {
        name: next(c for c in summary_cells(page) if name in c)
        for name in ("Bolt", "Litestar")
    }
    assert "is-tie" in cells["Bolt"] and "is-unsettled" in cells["Litestar"]
    assert "within 10%" in cells["Bolt"]
    assert "ranges overlap or CV" in cells["Litestar"]
    for cell in cells.values():
        assert X not in cell


def test_a_route_missing_a_competitor_shows_it_as_not_measured() -> None:
    with results_root() as root:
        write_results(root / "documented", "documented", {"bolt": (200.0,) * 3})
        write_results(root / "narrow", "documented", {"litestar": (60.0,) * 3})
        path = root / "narrow" / "environment.json"
        env = json.loads(path.read_text())
        env["settings"]["scenarios"] = ["json-1k", "auth-articles"]
        path.write_text(json.dumps(env))
        write_results(root / "kiso", "kiso", {"hinoki-kiso": (150.0,) * 3})
        page = page_of(root / "documented", root / "narrow", root / "kiso")

    missing = [c for c in summary_cells(page) if "is-missing" in c]
    assert len(missing) == 1
    assert "not measured" in missing[0] and "Litestar" in missing[0]
    assert X not in missing[0]


def two_worker_session(root: Path) -> list[Path]:
    """The same stacks measured with 1 worker and, in `w8/`, with 8."""
    dirs = []
    for workers, base, scale in ((1, root, 1.0), (8, root / "w8", 5.0)):
        write_results(
            base / "documented",
            "documented",
            {"hinoki": (100.0 * scale,) * 3, "bolt": (80.0 * scale,) * 3},
        )
        write_results(base / "kiso", "kiso", {"hinoki-kiso": (150.0 * scale,) * 3})
        for directory in (base / "documented", base / "kiso"):
            path = directory / "environment.json"
            env = json.loads(path.read_text())
            env["settings"]["workers"] = workers
            path.write_text(json.dumps(env))
            dirs.append(directory)
    return dirs


def test_one_and_eight_workers_share_a_page_behind_tabs_found_from_the_data() -> None:
    with results_root() as root:
        page = page_of(*two_worker_session(root))

    tabs = re.findall(r'id="bench-workers-(w\d)-tab"[^>]*>([^<]+)', page)
    assert tabs == [("w8", "8 workers "), ("w1", "1 worker ")]
    assert page.count('role="tabpanel" id="bench-workers-') == 2
    assert page.index('id="bench-w8-summary"') < page.index('id="bench-w1-summary"')
    eight = panel_of(page, "json-1k", workers=8)
    one = panel_of(page, "json-1k", workers=1)
    assert ">750</b>" in eight and ">150</b>" in one
    assert "8 workers, 100 connections" in page
    assert "1 worker, 100 connections" in page


def test_the_conditions_and_commands_tell_the_worker_counts_apart() -> None:
    with results_root() as root:
        page = page_of(*two_worker_session(root))

    assert "| Server processes | 1 per stack; 8 per stack |" in page
    assert "Command (kiso, 8 workers)" in page
    assert "--workers 8 --pool 8 --restarts" in page
    assert "--out results/YYYY-MM-DD/w8/kiso" in page
    assert "--out results/YYYY-MM-DD/kiso" in page
    assert (
        "python report.py results/YYYY-MM-DD/documented results/YYYY-MM-DD/kiso "
        "results/YYYY-MM-DD/w8/documented results/YYYY-MM-DD/w8/kiso"
    ) in page


def test_archived_run_report_links_to_its_public_raw_results() -> None:
    with results_root() as root:
        archived = root.parent / "2026-11-25"
        write_results(
            archived / "documented",
            "documented",
            {"hinoki": (100.0,) * 3},
        )
        write_results(
            archived / "w8" / "documented",
            "documented",
            {"hinoki": (500.0,) * 3},
        )
        path = archived / "w8" / "documented" / "environment.json"
        env = json.loads(path.read_text())
        env["settings"]["workers"] = 8
        path.write_text(json.dumps(env))
        page = page_of(archived / "documented", archived / "w8" / "documented")

    assert (
        "https://github.com/caspel26/hinoki/releases/download/bench-2026-11-25/" in page
    )
    assert "0db26f6e288a42f9532f9aad3e927e28690386ce0f262ee3ed1b456a4f32d7d5" in page
    assert "`benchmarks/comparative/results/w8/`" not in page
