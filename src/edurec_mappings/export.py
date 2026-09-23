"""Cap-aware extraction loop collecting the requests of one export in memory."""

from __future__ import annotations

import json
from dataclasses import astuple, replace
from typing import Protocol

from .models import (
    TERM_PATTERN,
    ExportResult,
    Listing,
    ListRow,
    Partition,
    RangeField,
    Request,
    plain,
)


class ExportSite(Protocol):
    """The navigation surface `export` needs; `EduRec` implements it against the live site."""

    def search(self, partition: Partition) -> Listing: ...
    def next_page(self) -> Listing: ...
    def request(self, row: ListRow) -> Request: ...
    def back(self) -> Listing: ...


def subdivide(partition: Partition, rows: list[ListRow]) -> list[Partition]:
    if partition.term_low != partition.term_high:
        terms = sorted(
            {int(r.term_code or "") for r in rows if TERM_PATTERN.fullmatch(r.term_code or "")}
        )
        if not terms:
            raise RuntimeError("Cannot partition capped search: no valid four-digit term codes")
        return partition.split("term", terms[len(terms) // 2])
    students = sorted(
        {
            student
            for r in rows
            if (student := r.student_id)
            and (partition.student_low is None or student > partition.student_low)
            and (partition.student_high is None or student < partition.student_high)
        }
    )
    if students:
        pivot = students[len(students) // 2]
        return [replace(partition, student_high=pivot), replace(partition, student_low=pivot)]
    # A single student can also exceed the cap. The live form limits mapping
    # group and sequence to three digits; search those complete domains next.
    fields: tuple[RangeField, ...] = ("group", "sequence")
    for field in fields:
        low, high = partition.bounds(field)
        if low != high:
            return partition.split(field, (low + high) // 2)
    raise RuntimeError(
        "An indivisible search is still capped; refusing to label truncated output complete"
    )


def validate_rows(partition: Partition, rows: list[ListRow]) -> None:
    for row in rows:
        code, student = row.term_code or "", row.student_id or ""
        if (
            not TERM_PATTERN.fullmatch(code)
            or not partition.contains("term", code)
            or not student
            or (partition.student_low is not None and student < partition.student_low)
            or (partition.student_high is not None and student > partition.student_high)
        ):
            raise RuntimeError(
                "EduRec returned rows outside the requested partition; search may be stale"
            )


def page_fingerprint(page: Listing) -> str:
    """A results page by its displayed values; row actions change per render."""
    return json.dumps({**plain(page), "rows": [row.cells() for row in page.rows]}, sort_keys=True)


def restore_list(site: ExportSite, current: Listing, total: int) -> Listing:
    """Return from a detail to the same results page, paging forward if EduRec reset to page 1."""
    restored = site.back()
    expected_first = current.span().first
    while (
        restored.span().first < expected_first
        and restored.has_next
        and restored.span().total == total
    ):
        previous_first = restored.span().first
        restored = site.next_page()
        if restored.span().first <= previous_first:
            raise RuntimeError("Pagination did not advance while restoring the list")
    if page_fingerprint(restored) != page_fingerprint(current):
        raise RuntimeError(
            "Results changed after returning from a detail: "
            f"expected range {current.counter}, got {restored.counter}"
        )
    return restored


class Extraction:
    """One export's state: the requests so far and the identities already collected."""

    def __init__(
        self, site: ExportSite, result: ExportResult, reassigned_to: str, limit: int | None
    ) -> None:
        self.site = site
        self.result = result
        self.reassigned_to = reassigned_to.casefold()
        self.limit = limit
        self.seen: set[tuple[str, ...]] = set()

    def scan(self, partition: Partition, current: Listing, total: int) -> bool:
        """Walk every page of an uncapped search; True when the limit stopped it."""
        visited = 0
        seen_pages: set[str] = set()
        while current.rows:
            validate_rows(partition, current.rows)
            fingerprint = page_fingerprint(current)
            counter = current.span()
            if fingerprint in seen_pages or counter.first != visited + 1:
                raise RuntimeError("Repeated or skipped results page")
            if counter.total != total or current.capped:
                raise RuntimeError("Search results changed during pagination")
            seen_pages.add(fingerprint)
            # Index into `current` each time: returning from a detail re-renders the
            # list with fresh row action IDs, and `current` is replaced accordingly.
            for index in range(len(current.rows)):
                row = current.rows[index]
                visited += 1
                if (
                    self.reassigned_to
                    and (row.reassigned_to or "").casefold() != self.reassigned_to
                ):
                    continue
                self.collect(partition, row)
                current = restore_list(self.site, current, total)
                if self.limit is not None and len(self.seen) >= self.limit:
                    return True
            if not current.has_next:
                break
            current = self.site.next_page()
        if visited != total:
            raise RuntimeError(f"Visited {visited} rows but the search reported {total}")
        return False

    def collect(self, partition: Partition, row: ListRow) -> None:
        """Open the row's detail and record it unless an earlier row already had it."""
        request = self.site.request(row)
        identity = request.identity
        if not partition.contains("group", identity.group) or not partition.contains(
            "sequence", identity.sequence
        ):
            raise RuntimeError("Mapping detail is outside the requested search partition")
        key = astuple(identity)
        if key in self.seen:
            return
        self.seen.add(key)
        self.result.requests.append(request)
        print(f"Extracted {len(self.seen)} unique requests", flush=True)


def export(
    site: ExportSite,
    result: ExportResult,
    *,
    reassigned_to: str = "",
    limit: int | None = None,
    terms: list[str] | None = None,
) -> None:
    """Collect every matching request into `result`, which keeps what was collected on error."""
    if terms is not None and not terms:
        raise ValueError("The configured term list must not be empty")
    run = Extraction(site, result, reassigned_to, limit)
    queue = (
        [Partition(term_low=int(code), term_high=int(code)) for code in terms]
        if terms is not None
        else [Partition()]
    )
    seen_partitions: set[Partition] = set()
    try:
        while queue:
            partition = queue.pop(0)
            if partition in seen_partitions:
                raise RuntimeError("Repeated search partition; refusing an incomplete export")
            seen_partitions.add(partition)
            current = site.search(partition)
            validate_rows(partition, current.rows)
            total = current.span().total
            print(
                f"Search terms {partition.term_low:04d}-{partition.term_high:04d}: "
                f"{total} rows{' (capped; subdividing)' if current.capped else ''}",
                flush=True,
            )
            if current.capped:
                queue[0:0] = subdivide(partition, current.rows)
                continue
            if run.scan(partition, current, total):
                result.status = "limit_reached"
                return
        result.status = "complete"
    except BaseException as exc:
        result.status = "interrupted"
        result.error = str(exc) or type(exc).__name__
        raise
