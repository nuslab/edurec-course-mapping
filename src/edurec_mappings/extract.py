"""Cap-aware extraction loop with checkpoints to the run directory."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from .browser import list_identity, subdivide, validate_rows
from .models import (
    Document,
    Filters,
    Listing,
    ListPage,
    ListRow,
    Partition,
    Request,
    SearchAudit,
)
from .parse import document, reset, save


class Site(Protocol):
    """The navigation surface `extract` needs; `EduRec` implements it against the live site."""

    def search(self, partition: Partition) -> Listing: ...
    def next_page(self) -> Listing: ...
    def request(self, row: ListRow) -> Request: ...
    def back(self) -> Listing: ...


class Checkpoint:
    """Rewrite the inventory and, when given, the one request that changed."""

    def __init__(self, data: Document, output: str | Path) -> None:
        self.data = data
        self.output = output

    def __call__(self, request: Request | None = None) -> None:
        save(self.data, self.output, [request] if request else [])


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


def extract(
    site: Site,
    output: str | Path,
    *,
    reassign_id: str = "",
    term: str = "",
    rows: int | None = None,
    terms: list[str] | None = None,
) -> Document:
    selected_terms = [term] if term else terms
    if selected_terms is not None and not selected_terms:
        raise ValueError("The configured term list must not be empty")
    data = document()
    meta = data.collection
    meta.filters = Filters(
        reassign_id=reassign_id or None,
        term=term or None,
        terms=selected_terms,
        rows=rows,
    )
    reset(output)
    checkpoint = Checkpoint(data, output)
    queue = (
        [Partition(term_low=int(code), term_high=int(code)) for code in selected_terms]
        if selected_terms is not None
        else [Partition()]
    )
    seen_partitions: set[Partition] = set()
    known: set[str] = set()
    try:
        while queue:
            partition = queue.pop(0)
            if partition in seen_partitions:
                raise RuntimeError("Repeated search partition; refusing an incomplete export")
            seen_partitions.add(partition)
            current = site.search(partition)
            validate_rows(partition, current.rows)
            total = current.span()[2]
            audit = SearchAudit(criteria=partition, reported_rows=total, capped=current.capped)
            meta.search_partitions.append(audit)
            print(
                f"Search terms {partition.term_low:04d}-{partition.term_high:04d}: "
                f"{total} rows{' (capped; subdividing)' if current.capped else ''}",
                flush=True,
            )
            if current.capped:
                queue[0:0] = subdivide(partition, current.rows)
                audit.status = "subdivided"
                checkpoint()
                continue
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
                data.list_pages.append(ListPage.of(partition, current))
                # Index into `current` each time: returning from a detail re-renders the
                # list with fresh row action IDs, and `current` is replaced accordingly.
                for index in range(len(current.rows)):
                    row = current.rows[index]
                    visited += 1
                    meta.scanned_rows += 1
                    if (
                        reassign_id
                        and (row.reassigned_to or "").casefold() != reassign_id.casefold()
                    ):
                        continue
                    request = site.request(row)
                    identity = request.identity
                    if not partition.contains(
                        "group", identity.mapping_number
                    ) or not partition.contains("sequence", identity.sequence):
                        raise RuntimeError(
                            "Mapping detail is outside the requested search partition"
                        )
                    if request.request_id in known:
                        meta.duplicate_details += 1
                        checkpoint()
                    else:
                        known.add(request.request_id)
                        data.requests.append(request)
                        meta.unique_requests = len(known)
                        print(f"Extracted {len(known)} unique requests", flush=True)
                        checkpoint(request)
                    current = restore_list(site, current, total)
                    if rows is not None and len(known) >= rows:
                        audit.status = "row_limit_reached"
                        meta.status = "row_limit_reached"
                        meta.all_request_details_collected = False
                        checkpoint()
                        return data
                if not current.has_next:
                    break
                current = site.next_page()
            if visited != total:
                raise RuntimeError(f"Visited {visited} rows but the search reported {total}")
            audit.status = "complete"
            checkpoint()
        meta.status = "complete"
        meta.all_request_details_collected = True
        meta.related_mapping_completeness = "unverified"
        checkpoint()
        return data
    except BaseException as exc:
        meta.status = "interrupted"
        meta.error = str(exc) or type(exc).__name__
        meta.all_request_details_collected = False
        checkpoint()
        raise
