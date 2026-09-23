"""Cap-aware extraction loop collecting the requests of one export in memory."""

from __future__ import annotations

from dataclasses import astuple
from typing import Protocol

from .browser import list_identity, subdivide, validate_rows
from .models import (
    Export,
    Listing,
    ListRow,
    Partition,
    Request,
)


class Site(Protocol):
    """The navigation surface `export` needs; `EduRec` implements it against the live site."""

    def search(self, partition: Partition) -> Listing: ...
    def next_page(self) -> Listing: ...
    def request(self, row: ListRow) -> Request: ...
    def back(self) -> Listing: ...


def restore_list(site: Site, current: Listing, total: int) -> Listing:
    """Return from a detail to the same results page, paging forward if EduRec reset to page 1."""
    restored = site.back()
    expected_start = current.span()[0]
    while restored.span()[0] < expected_start and restored.has_next and restored.span()[2] == total:
        previous_start = restored.span()[0]
        restored = site.next_page()
        if restored.span()[0] <= previous_start:
            raise RuntimeError("Pagination did not advance while restoring the list")
    if list_identity(restored) != list_identity(current):
        raise RuntimeError(
            "Results changed after returning from a detail: "
            f"expected range {current.range}, got {restored.range}"
        )
    return restored


class Extraction:
    """One export's state: the requests so far and the identities already collected."""

    def __init__(self, site: Site, data: Export, reassign_id: str, rows: int | None) -> None:
        self.site = site
        self.data = data
        self.reassign_id = reassign_id.casefold()
        self.rows = rows
        self.known: set[tuple[str, ...]] = set()

    def scan(self, partition: Partition, current: Listing, total: int) -> bool:
        """Walk every page of an uncapped search; True when the row limit stopped it."""
        visited = 0
        seen_pages: set[str] = set()
        while current.rows:
            validate_rows(partition, current.rows)
            fingerprint = list_identity(current)
            start, _, reported = current.span()
            if fingerprint in seen_pages or start != visited + 1:
                raise RuntimeError("Repeated or skipped results page")
            if reported != total or current.capped:
                raise RuntimeError("Search results changed during pagination")
            seen_pages.add(fingerprint)
            # Index into `current` each time: returning from a detail re-renders the
            # list with fresh row action IDs, and `current` is replaced accordingly.
            for index in range(len(current.rows)):
                row = current.rows[index]
                visited += 1
                if self.reassign_id and (row.reassigned_to or "").casefold() != self.reassign_id:
                    continue
                self.collect(partition, row)
                current = restore_list(self.site, current, total)
                if self.rows is not None and len(self.known) >= self.rows:
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
        if not partition.contains("group", identity.mapping_number) or not partition.contains(
            "sequence", identity.sequence
        ):
            raise RuntimeError("Mapping detail is outside the requested search partition")
        key = astuple(identity)
        if key in self.known:
            return
        self.known.add(key)
        self.data.requests.append(request)
        print(f"Extracted {len(self.known)} unique requests", flush=True)


def export(
    site: Site,
    data: Export,
    *,
    reassign_id: str = "",
    rows: int | None = None,
    terms: list[str] | None = None,
) -> None:
    """Collect every matching request into `data`, which keeps what was collected on error."""
    if terms is not None and not terms:
        raise ValueError("The configured term list must not be empty")
    run = Extraction(site, data, reassign_id, rows)
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
            total = current.span()[2]
            print(
                f"Search terms {partition.term_low:04d}-{partition.term_high:04d}: "
                f"{total} rows{' (capped; subdividing)' if current.capped else ''}",
                flush=True,
            )
            if current.capped:
                queue[0:0] = subdivide(partition, current.rows)
                continue
            if run.scan(partition, current, total):
                data.status = "row_limit_reached"
                return
        data.status = "complete"
    except BaseException as exc:
        data.status = "interrupted"
        data.error = str(exc) or type(exc).__name__
        raise
