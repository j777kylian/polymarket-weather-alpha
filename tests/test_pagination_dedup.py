from weather_alpha.collectors.pagination import paginate_offset, paginate_pages, unique_by


def test_offset_pagination_stops_on_empty_and_respects_max_pages() -> None:
    pages = {
        0: [{"id": "a"}, {"id": "b"}],
        2: [{"id": "c"}],
        4: [],
    }

    def fetch(offset: int, limit: int) -> list[dict[str, str]]:
        return pages.get(offset, [])

    items = list(paginate_offset(fetch, page_size=2, max_pages=10))
    assert [row["id"] for row in items] == ["a", "b", "c"]


def test_offset_pagination_bounded_by_max_pages() -> None:
    def fetch(offset: int, limit: int) -> list[dict[str, int]]:
        return [{"n": offset}]

    items = list(paginate_offset(fetch, page_size=1, max_pages=3))
    assert len(items) == 3


def test_dedup_preserves_first_occurrence() -> None:
    rows = [{"id": "1", "v": 1}, {"id": "2", "v": 2}, {"id": "1", "v": 99}]
    deduped = unique_by(rows, key=lambda row: row["id"])
    assert deduped == [{"id": "1", "v": 1}, {"id": "2", "v": 2}]


def test_paginate_pages_stops_on_empty_and_respects_max_pages() -> None:
    pages = {
        1: [{"id": "a"}],
        2: [{"id": "b"}],
        3: [{"id": "c"}],
    }

    def fetch(page: int) -> list[dict[str, str]]:
        return pages.get(page, [])

    items = list(paginate_pages(fetch, max_pages=2, key=lambda row: row["id"]))
    assert [row["id"] for row in items] == ["a", "b"]


def test_paginate_pages_stops_when_page_has_no_new_items() -> None:
    fetches: list[int] = []

    def fetch(page: int) -> list[dict[str, str]]:
        fetches.append(page)
        if page == 1:
            return [{"id": "a"}]
        return [{"id": "a"}]

    items = list(paginate_pages(fetch, max_pages=10, key=lambda row: row["id"]))
    assert [row["id"] for row in items] == ["a"]
    assert fetches == [1, 2]
