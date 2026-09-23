"""Cap-aware extraction loop with checkpoints to the run directory."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from .browser import list_identity, subdivide, validate_rows
from .models import (
    Document,
    Listing,
    ListRow,
    Partition,
    Request,
    SearchAudit,
)
from .store import document, reset, save


class Site(Protocol):
    """The navigation surface `export` needs; `EduRec` implements it against the live site."""

    def search(self, partition: Partition) -> Listing: ...
    def next_page(self) -> Listing: ...
    def request(self, row: ListRow) -> Request: ...
    def back(self) -> Listing: ...


Checkpoint = Callable[[Request | None], None]
"""Rewrite the inventory and, when given, the one request that changed."""


def checkpoint(data: Document, output: str | Path) -> Checkpoint:
    return lambda request: save(data, output, [request] if request else [])


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
    """One run's state: the export so far and the request ids already collected."""

    def __init__(
        self, site: Site, data: Document, checkpoint: Checkpoint, reassign_id: str, rows: int | None
    ) -> None:
        self.site = site
        self.data = data
        self.checkpoint = checkpoint
        self.reassign_id = reassign_id.casefold()
        self.rows = rows
        self.known: set[str] = set()

    def scan(self, partition: Partition, current: Listing, audit: SearchAudit) -> bool:
        """Walk every page of an uncapped search; True when the row limit stopped it."""
        total = audit.reported_rows
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
        if request.request_id in self.known:
            self.data.duplicate_details += 1
            self.checkpoint(None)
            return
        self.known.add(request.request_id)
        self.data.requests.append(request)
        print(f"Extracted {len(self.known)} unique requests", flush=True)
        self.checkpoint(request)


def export(
    site: Site,
    output: str | Path,
    *,
    reassign_id: str = "",
    rows: int | None = None,
    terms: list[str] | None = None,
) -> Document:
    if terms is not None and not terms:
        raise ValueError("The configured term list must not be empty")
    data = document()
    data.reassign_id, data.terms, data.row_limit = reassign_id or None, terms, rows
    reset(output)
    write = checkpoint(data, output)
    run = Extraction(site, data, write, reassign_id, rows)
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
            audit = SearchAudit(criteria=partition, reported_rows=total)
            data.search_partitions.append(audit)
            print(
                f"Search terms {partition.term_low:04d}-{partition.term_high:04d}: "
                f"{total} rows{' (capped; subdividing)' if current.capped else ''}",
                flush=True,
            )
            if current.capped:
                queue[0:0] = subdivide(partition, current.rows)
                audit.status = "subdivided"
                write(None)
                continue
            if run.scan(partition, current, audit):
                audit.status = "row_limit_reached"
                data.status = "row_limit_reached"
                write(None)
                return data
            audit.status = "complete"
            write(None)
        data.status = "complete"
        write(None)
        return data
    except BaseException as exc:
        data.status = "interrupted"
        data.error = str(exc) or type(exc).__name__
        write(None)
        raise
