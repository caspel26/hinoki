import pytest

from hinoki import ValidationError
from hinoki.pagination import Page, PageNumberPagination, PageParams
from hinoki.schema import encode

strategy = PageNumberPagination(page_size=2, max_page_size=5)


def parse(**query: str) -> PageParams:
    return strategy.parse({k: [v] for k, v in query.items()})


def codes(**query: str) -> dict[str, str]:
    with pytest.raises(ValidationError) as info:
        parse(**query)
    return {d.path: d.code for d in info.value.details}


def test_defaults_and_offsets() -> None:
    params = parse()
    assert (params.page, params.page_size, params.offset) == (1, 2, 0)
    assert parse(page="3", page_size="4").offset == 8


@pytest.mark.parametrize("value", ["0", "-1", "x", "1.5", " 2", "٣", ""])
def test_invalid_numbers_are_rejected(value: str) -> None:
    assert codes(page=value) == {"page": "type"}


def test_page_size_is_bounded_and_all_errors_are_reported() -> None:
    assert codes(page_size="6") == {"page_size": "le"}
    assert codes(page="0", page_size="9") == {"page": "type", "page_size": "le"}


def test_repeated_parameters_are_rejected() -> None:
    with pytest.raises(ValidationError):
        strategy.parse({"page": ["1", "2"]})


def test_slicing_totals_and_page_count() -> None:
    rows = list(range(5))
    first = Page.of(rows, parse())
    assert (first.items, first.total, first.pages) == ([0, 1], 5, 3)
    assert Page.of(rows, parse(page="3")).items == [4]


def test_empty_collection_and_pages_past_the_end() -> None:
    empty: Page[int] = Page.of([], parse())
    assert (empty.items, empty.total, empty.pages) == ([], 0, 0)
    assert Page.of([1, 2], parse(page="9")).items == []


def test_map_converts_items_and_keeps_totals() -> None:
    mapped = Page.of([1, 2, 3], parse()).map(str)
    assert (mapped.items, mapped.total) == (["1", "2"], 3)


def test_encode_renders_the_page_contract() -> None:
    dumped = encode(Page.of(["a", "b", "c"], parse(page="2")))
    assert dumped == {
        "items": ["c"],
        "page": 2,
        "page_size": 2,
        "total": 3,
        "pages": 2,
    }


def test_strategy_rejects_inconsistent_limits() -> None:
    with pytest.raises(ValueError, match="page_size"):
        PageNumberPagination(page_size=10, max_page_size=5)
