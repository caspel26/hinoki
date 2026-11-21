"""HTML for the benchmark page: plain data in, markup out.

Every number shown comes from a ``Cell`` built by ``report.py`` from retained
samples; nothing here knows a result. The markup is one line per element so it
survives Markdown's HTML-block rules.
"""

import html
from dataclasses import dataclass


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


@dataclass(frozen=True)
class Scenario:
    key: str
    name: str
    request: str
    description: str
    cells: tuple[Cell, ...]
    versus: str = ""


@dataclass(frozen=True)
class Stack:
    family: str
    label: str


def esc(text: str) -> str:
    return html.escape(text)


def rps(value: float) -> str:
    return f"{value:,.0f}"


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


def value_axis(axis_max: float) -> str:
    ticks = "".join(f"<span>{rps(axis_max * share)}</span>" for share in (0, 0.5, 1))
    return f'<span class="bench-axis" aria-hidden="true"><span>{ticks}</span></span>'


def legend(stacks: list[Stack]) -> str:
    items = "".join(
        f'<li><button type="button" class="bench-chip" data-stack="{esc(stack.family)}" aria-pressed="true">'
        f'<span class="bench-swatch" aria-hidden="true"></span>{esc(stack.label)}</button></li>'
        for stack in stacks
    )
    return f'<ul class="bench-legend" aria-label="Stacks. Select one to hide or show it.">{items}</ul>'


CELL_SR = {
    "is-best": "Highest median. ",
    "is-tie": "Within 10% of the highest. ",
    "is-unstable": "Unstable, not compared. ",
}


def matrix_cell(cell: Cell | None, row_max: float) -> str:
    if cell is None:
        return '<td class="bench-cell is-missing">Not measured</td>'
    state = state_class(cell)
    sr = f'<span class="bench-sr">{CELL_SR[state]}</span>' if state else ""
    badge = (
        status_badge(cell).replace("<span ", '<span aria-hidden="true" ', 1)
        if state
        else ""
    )
    width = cell.median / row_max * 100 if row_max else 0
    return (
        f'<td class="{f"bench-cell {state}".strip()}" data-stack="{esc(cell.family)}" data-label="{esc(cell.label)}">'
        f'{sr}<b class="bench-num">{rps(cell.median)}</b>{badge}'
        f'<span class="bench-meter" aria-hidden="true"><span style="width:{width:.1f}%"></span></span></td>'
    )


def matrix_row(scenario: Scenario, stacks: list[Stack]) -> str:
    by_family = {cell.family: cell for cell in scenario.cells}
    row_max = max((cell.median for cell in scenario.cells), default=0.0)
    cells = "".join(matrix_cell(by_family.get(s.family), row_max) for s in stacks)
    return f'<tr><th scope="row">{esc(scenario.name)}</th>{cells}</tr>'


def matrix(
    table_id: str,
    stacks: list[Stack],
    scenarios: list[Scenario],
    filterable: bool = False,
) -> str:
    heads = "".join(
        f'<th scope="col" data-stack="{esc(s.family)}"><span class="bench-swatch" aria-hidden="true"></span>{esc(s.label)}</th>'
        for s in stacks
    )
    rows = "".join(matrix_row(scenario, stacks) for scenario in scenarios)
    flag = " data-bench-filterable" if filterable else ""
    return (
        '<div class="bench-scroll" role="region" aria-label="Median requests per second by request type and stack" tabindex="0">'
        f'<table class="bench-matrix" id="{table_id}"{flag}>'
        "<caption>Median requests per second. The bar in each cell is relative to the highest median in its row; "
        "the highest stable median is highlighted and medians within 10% of it are marked as tied.</caption>"
        f'<thead><tr><th scope="col">Request type</th>{heads}</tr></thead><tbody>{rows}</tbody></table></div>'
    )


def chart_row(cell: Cell, axis_max: float) -> str:
    state = state_class(cell)
    width = cell.median / axis_max * 100 if axis_max else 0
    left = cell.low / axis_max * 100 if axis_max else 0
    span = max((cell.high - cell.low) / axis_max * 100 if axis_max else 0, 0.4)
    badge = status_badge(cell) if state else ""
    return (
        f'<div class="bench-row {state}" data-stack="{esc(cell.family)}">'
        f'<span class="bench-name"><span class="bench-swatch" aria-hidden="true"></span>{esc(cell.label)}</span>'
        '<span class="bench-track">'
        f'<span class="bench-bar" style="width:{width:.2f}%"></span>'
        f'<span class="bench-range" style="left:{left:.2f}%;width:{span:.2f}%"></span></span>'
        f'<span class="bench-value">{rps(cell.median)}<small> req/s</small></span>'
        f'<span class="bench-flag">{badge}</span></div>'
    )


def chart(cells: tuple[Cell, ...]) -> str:
    axis_max = max((cell.high for cell in cells), default=0.0)
    rows = "".join(chart_row(cell, axis_max) for cell in cells)
    return (
        '<div class="bench-chart" aria-hidden="true">'
        '<span class="bench-gridlines"><span></span></span>'
        f"{rows}{value_axis(axis_max)}</div>"
    )


def exact_row(cell: Cell) -> str:
    return (
        f'<tr data-stack="{esc(cell.family)}" class="{state_class(cell)}">'
        f'<th scope="row">{esc(cell.label)}</th>'
        f"<td>{rps(cell.median)}</td><td>{rps(cell.low)}&ndash;{rps(cell.high)}</td>"
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
        f"<caption>{esc(scenario.name)}: median and lowest to highest sample of requests per second, "
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


def panel(scenario: Scenario, selected: bool) -> str:
    key = scenario.key
    versus = (
        f'<p class="bench-versus">{esc(scenario.versus)}</p>' if scenario.versus else ""
    )
    return (
        f'<section class="bench-panel" role="tabpanel" id="bench-request-{key}" '
        f'aria-labelledby="bench-tab-{key}" tabindex="0" data-selected="{str(selected).lower()}">'
        '<header class="bench-panel__head">'
        f'<p class="bench-kicker">{esc(scenario.name)}</p><h3>{esc(scenario.request)}</h3>'
        f"<p>{esc(scenario.description)}</p></header>"
        f'<p class="bench-headline">{headline(scenario.cells)}</p>{versus}'
        f"{chart(scenario.cells)}"
        '<p class="bench-note">Bars show the median and start at zero; the thin line is the lowest to highest sample. '
        "Higher means more requests served per second.</p>"
        f"{exact_table(scenario)}{unstable_details(scenario.cells)}</section>"
    )


def explorer(scenarios: list[Scenario]) -> str:
    tabs = "".join(
        f'<button type="button" role="tab" id="bench-tab-{s.key}" aria-controls="bench-request-{s.key}" '
        f'aria-selected="{str(index == 0).lower()}" tabindex="{0 if index == 0 else -1}">{esc(s.name)}</button>'
        for index, s in enumerate(scenarios)
    )
    panels = "".join(panel(s, index == 0) for index, s in enumerate(scenarios))
    return (
        '<div class="bench-explorer" data-bench-explorer data-bench-filterable>'
        f'<div class="bench-tabs" role="tablist" aria-label="Request type">{tabs}</div>{panels}</div>'
    )
