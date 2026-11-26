"""HTML for the benchmark page: plain data in, markup out.

Every number shown comes from a ``Cell`` built by ``report.py`` from retained
samples; nothing here knows a result. The markup is one line per element so it
survives Markdown's HTML-block rules.
"""

import html
from dataclasses import dataclass
from typing import NamedTuple


class SummaryCell(NamedTuple):
    """One comparison in the summary: the verdict, the figure, its word and the rest of the sentence."""

    verdict: str
    value: str
    word: str
    sentence: str


class SummaryRow(NamedTuple):
    """One summary test: the reference's requests per second and a cell per competitor (None if not measured)."""

    test: str
    value: str
    cells: list[SummaryCell | None]


@dataclass(frozen=True)
class Cell:
    """One stack in one request type."""

    key: str
    family: str
    label: str
    median: float
    low: float
    high: float
    p50_ms: float
    p99_ms: float
    cv_pct: float | None
    samples: tuple[float, ...]
    wrong: int
    mark: str = ""  # "best", "tie" or "" for a stable cell that is neither
    reason: str | None = None  # why the cell is unstable; None when it is stable
    versus: str = ""  # how this cell stands against the reference stack
    verdict: str = ""  # "reference", "higher", "lower", "tie" or "unsettled"


@dataclass(frozen=True)
class Scenario:
    key: str
    name: str
    request: str
    description: str
    cells: tuple[Cell, ...]
    reference: str = ""  # label of the stack every "vs" column compares with


def esc(text: str) -> str:
    return html.escape(text)


def rps(value: float) -> str:
    return f"{value:,.0f}"


def upper_median(values: tuple[float, ...]) -> float:
    """The statistic Django Bolt's comparison publishes: ``sorted(samples)[len // 2]``."""
    return sorted(values)[len(values) // 2]


def fmt_ms(value: float) -> str:
    return f"{value:,.0f}" if value >= 100 else f"{value:.1f}"


def cv_text(cell: Cell) -> str:
    return "n/a" if cell.cv_pct is None else f"{cell.cv_pct:.1f}%"


def status_badge(cell: Cell) -> str:
    if cell.reason:
        return '<span class="bench-badge is-unstable">Unstable</span>'
    if cell.mark == "best":
        return '<span class="bench-badge is-top">Highest</span>'
    if cell.mark == "tie":
        return '<span class="bench-badge is-tied">Tied with highest</span>'
    return "Stable"


def state_class(cell: Cell) -> str:
    if cell.reason:
        return "is-unstable"
    return {"best": "is-best", "tie": "is-tie"}.get(cell.mark, "")


CELL_SR = {
    "is-best": "Highest median. ",
    "is-tie": "Within 10% of the highest. ",
    "is-unstable": "Unstable, not compared. ",
}


def short_name(label: str) -> str:
    return label.replace(", native server", "")


def route_row(cell: Cell, row_max: float, short: str) -> str:
    state = state_class(cell)
    sr = f'<span class="bench-sr">{CELL_SR[state]}</span>' if state else ""
    badge = (
        status_badge(cell).replace("<span ", '<span aria-hidden="true" ', 1)
        if state
        else ""
    )
    width = cell.median / row_max * 100 if row_max else 0
    return (
        f'<tr class="{state}" data-stack="{esc(cell.family)}">'
        f'<th scope="row">{esc(cell.label)}</th>'
        f'<td class="bench-rps" data-label="Requests per second">{sr}<b class="bench-num">{rps(cell.median)}</b>{badge}'
        f'<span class="bench-meter" aria-hidden="true"><span style="width:{width:.1f}%"></span></span></td>'
        f'<td class="bench-vs is-{esc(cell.verdict)}" data-label="vs {esc(short)}">{esc(cell.versus)}</td>'
        f'<td data-label="p50 ms">{fmt_ms(cell.p50_ms)}</td><td data-label="p99 ms">{fmt_ms(cell.p99_ms)}</td>'
        f'<td class="bench-spread" data-label="Range and CV"><span>{rps(cell.low)}&ndash;{rps(cell.high)}</span>'
        f"<small>CV {cv_text(cell)}</small></td></tr>"
    )


def route_table(scenario: Scenario) -> str:
    row_max = max((cell.median for cell in scenario.cells), default=0.0)
    short = short_name(scenario.reference)
    rows = "".join(route_row(cell, row_max, short) for cell in scenario.cells)
    heads = "".join(
        f'<th scope="col">{name}</th>'
        for name in (
            "Stack",
            "Requests per second",
            f"vs {esc(short)}",
            "p50 ms",
            "p99 ms",
            "Lowest&ndash;highest",
        )
    )
    return (
        '<div class="bench-scroll" role="region" tabindex="0" '
        f'aria-label="{esc(scenario.name)}: requests per second by stack">'
        '<table class="bench-route">'
        f"<caption>{esc(scenario.name)}: median requests per second; each bar is relative to the highest median of the table "
        f"and starts at zero. Higher is better. A gap under 10% is a tie.</caption>"
        f"<thead><tr>{heads}</tr></thead><tbody>{rows}</tbody></table></div>"
    )


def exact_row(cell: Cell) -> str:
    return (
        f'<tr data-stack="{esc(cell.family)}" class="{state_class(cell)}">'
        f'<th scope="row">{esc(cell.label)}</th>'
        f"<td>{rps(cell.median)}</td><td>{rps(upper_median(cell.samples))}</td>"
        f"<td>{rps(cell.low)}&ndash;{rps(cell.high)}</td>"
        f"<td>{fmt_ms(cell.p50_ms)}</td><td>{fmt_ms(cell.p99_ms)}</td>"
        f"<td>{cv_text(cell)}</td><td>{cell.wrong}</td><td>{len(cell.samples)}</td>"
        f"<td>{status_badge(cell)}</td></tr>"
    )


def exact_table(scenario: Scenario) -> str:
    rows = "".join(exact_row(cell) for cell in scenario.cells)
    heads = "".join(
        f'<th scope="col">{name}</th>'
        for name in (
            "Median req/s",
            "Upper median",
            "Lowest&ndash;highest",
            "p50 ms",
            "p99 ms",
            "CV",
            "Unexpected",
            "Samples",
            "Status",
        )
    )
    return (
        '<div class="bench-scroll" role="region" tabindex="0" '
        f'aria-label="{esc(scenario.name)}: exact numbers">'
        '<table class="bench-exact">'
        f"<caption>{esc(scenario.name)}: median, upper median (the statistic Django Bolt publishes) "
        "and lowest to highest sample of requests per second, "
        "median p50 and p99 latency, run-to-run variation (CV) and status</caption>"
        f'<thead><tr><th scope="col">Stack</th>{heads}</tr></thead><tbody>{rows}</tbody></table></div>'
    )


def unstable_details(cells: tuple[Cell, ...]) -> str:
    parts = []
    for cell in cells:
        if not cell.reason:
            continue
        samples = ", ".join(rps(value) for value in sorted(cell.samples))
        parts.append(
            f'<details class="bench-unstable" data-stack="{esc(cell.family)}" open>'
            f"<summary>Why {esc(cell.label)} is marked unstable</summary>"
            f"<p>{esc(cell.reason)}</p>"
            f"<p>All {len(cell.samples)} samples, requests per second, lowest to highest: {samples}.</p></details>"
        )
    return "".join(parts)


def headline(cells: tuple[Cell, ...]) -> str:
    best = next((cell for cell in cells if cell.mark == "best"), None)
    if best is None:
        return "No stable result: every stack in this request type is unstable, so none is highlighted."
    text = f"Highest stable median: <b>{esc(best.label)}</b>, {rps(best.median)} requests/s."
    tied = [cell.label for cell in cells if cell.mark == "tie"]
    if tied:
        text += f" Within 10% of it, so tied: {esc(', '.join(tied))}."
    unstable = [cell.label for cell in cells if cell.reason]
    if unstable:
        text += f" Unstable, not compared: {esc(', '.join(unstable))}."
    return text


def panel(scenario: Scenario, prefix: str, selected: bool) -> str:
    key = f"{prefix}-{scenario.key}"
    return (
        f'<section class="bench-panel" role="tabpanel" id="{key}-panel" '
        f'aria-labelledby="{key}-tab" tabindex="0" data-selected="{str(selected).lower()}">'
        '<header class="bench-panel__head">'
        f'<p class="bench-kicker">{esc(scenario.name)}</p><h4>{esc(scenario.request)}</h4>'
        f"<p>{esc(scenario.description)}</p></header>"
        f'<p class="bench-headline">{headline(scenario.cells)}</p>'
        f"{route_table(scenario)}"
        '<details class="bench-details"><summary>Exact numbers</summary>'
        f"{exact_table(scenario)}</details>{unstable_details(scenario.cells)}</section>"
    )


def tabs(items: list[tuple[str, str]], prefix: str, label: str, extra: str = "") -> str:
    """A tab list; each item is (key, text), its panel id is ``{prefix}-{key}-panel``."""
    buttons = "".join(
        f'<button type="button" role="tab" id="{prefix}-{key}-tab" aria-controls="{prefix}-{key}-panel" '
        f'aria-selected="{str(index == 0).lower()}" tabindex="{0 if index == 0 else -1}">{text}</button>'
        for index, (key, text) in enumerate(items)
    )
    return f'<div class="bench-tabs{extra}" role="tablist" aria-label="{esc(label)}">{buttons}</div>'


def explorer(scenarios: list[Scenario], prefix: str) -> str:
    bar = tabs([(s.key, esc(s.name)) for s in scenarios], prefix, "Request type")
    panels = "".join(panel(s, prefix, index == 0) for index, s in enumerate(scenarios))
    return f'<div class="bench-explorer" data-bench-explorer>{bar}{panels}</div>'


def cmp_cell_html(cell: SummaryCell | None, label: str) -> str:
    """One comparison cell: the figure and its word, with the rest of the sentence for screen readers."""
    if cell is None:
        return (
            f'<td class="bench-cmp is-missing" data-label="{esc(label)}">'
            '<span aria-hidden="true">&mdash;</span><span class="bench-sr">not measured</span></td>'
        )
    return (
        f'<td class="bench-cmp is-{cell.verdict}" data-label="{esc(label)}">'
        f'<b class="bench-ratio">{esc(cell.value)}</b> <span class="bench-word">{esc(cell.word)}</span>'
        f'<span class="bench-sr">{esc(cell.sentence)}</span></td>'
    )


def summary_table(
    columns: list[tuple[str, str]],
    rows: list[SummaryRow],
    reference: tuple[str, str],
    conditions: str,
    tie_pct: int,
) -> str:
    """The summary: one row per test, the reference's requests per second, then one cell per competitor.

    `columns` and `reference` are (framework, server) pairs.
    """
    labels = [f"{framework} ({server})" for framework, server in columns]
    ref_label = f"{reference[0]} ({reference[1]})"
    head = "".join(
        f'<th scope="col"><span class="bench-col-name">{esc(framework)}</span> '
        f'<span class="bench-col-server">{esc(server)}</span></th>'
        for framework, server in columns
    )
    body = "".join(
        f'<tr><th scope="row">{esc(row.test)}</th><td data-label="{esc(ref_label)}">'
        f'<b class="bench-num">{esc(row.value)}</b> req/s</td>'
        + "".join(
            cmp_cell_html(cell, label)
            for cell, label in zip(row.cells, labels, strict=True)
        )
        + "</tr>"
        for row in rows
    )
    return (
        '<div class="bench-scroll" role="region" tabindex="0" aria-label="Summary">'
        f'<table class="bench-summary"><caption>{esc(conditions)}. Median requests per second of {esc(ref_label)} '
        "and how many times faster or slower it is than each framework (ratio of medians). "
        f"A gap under {tie_pct}% is a tie.</caption>"
        f'<thead><tr><th scope="col">Test</th><th scope="col">{esc(reference[0])} '
        f'<span class="bench-col-server">{esc(reference[1])}</span></th>{head}</tr></thead>'
        f"<tbody>{body}</tbody></table></div>"
    )
