"""Bounded offset pagination and first-seen deduplication."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator, Sequence
from typing import TypeVar

T = TypeVar("T")
K = TypeVar("K")

FetchPage = Callable[[int, int], Sequence[T]]
FetchNumberedPage = Callable[[int], Sequence[T]]


def paginate_offset(
    fetch: FetchPage[T],
    *,
    page_size: int,
    max_pages: int,
    start_offset: int = 0,
) -> Iterator[T]:
    if page_size <= 0:
        raise ValueError("page_size must be positive")
    if max_pages <= 0:
        raise ValueError("max_pages must be positive")
    offset = start_offset
    for _ in range(max_pages):
        page = list(fetch(offset, page_size))
        if not page:
            return
        yield from page
        if len(page) < page_size:
            return
        offset += page_size


def paginate_pages(
    fetch: FetchNumberedPage[T],
    *,
    max_pages: int,
    key: Callable[[T], K],
    start_page: int = 1,
) -> Iterator[T]:
    if max_pages <= 0:
        raise ValueError("max_pages must be positive")
    if start_page <= 0:
        raise ValueError("start_page must be positive")
    seen: set[K] = set()
    for page_num in range(start_page, start_page + max_pages):
        page = list(fetch(page_num))
        if not page:
            return
        new_items = [item for item in page if key(item) not in seen]
        if not new_items:
            return
        for item in new_items:
            seen.add(key(item))
            yield item


def unique_by(items: Iterable[T], *, key: Callable[[T], K]) -> list[T]:
    seen: set[K] = set()
    result: list[T] = []
    for item in items:
        marker = key(item)
        if marker in seen:
            continue
        seen.add(marker)
        result.append(item)
    return result
