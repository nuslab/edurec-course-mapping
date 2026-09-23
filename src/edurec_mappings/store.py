"""The run directory on disk: `inventory.yaml`, one YAML per request and scraped documents."""

from __future__ import annotations

import shutil
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path

import yaml

from .models import Document, Request, plain

INVENTORY = "inventory.yaml"
REQUESTS = "requests"
DOCUMENTS = "documents"


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def document() -> Document:
    return Document(started_at=now())


def link_siblings(requests: list[Request]) -> None:
    """Tell each request about the other collected requests in its mapping group."""
    groups: dict[tuple[str, ...], list[str]] = {}
    for request in requests:
        groups.setdefault(request.identity.mapping, []).append(request.request_id)
    for request in requests:
        siblings = groups[request.identity.mapping]
        request.related_request_ids = [r for r in siblings if r != request.request_id]


def write_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(content, encoding="utf-8")
    temp.replace(path)


def dump(value: object) -> str:
    return yaml.safe_dump(value, allow_unicode=True, sort_keys=False)


def reset(directory: str | Path) -> None:
    """Remove a previous run's files from `directory`, leaving anything else in place."""
    run = Path(directory)
    (run / INVENTORY).unlink(missing_ok=True)
    for name in (REQUESTS, DOCUMENTS):
        shutil.rmtree(run / name, ignore_errors=True)


def save(data: Document, directory: str | Path, requests: Iterable[Request] | None = None) -> None:
    """Write the run directory: `inventory.yaml`, one YAML per request, scraped text once per URL.

    `requests` limits the request files written to those that changed; the default
    writes them all.
    """
    run = Path(directory)
    link_siblings(data.requests)
    for request in data.requests if requests is None else requests:
        for linked in request.linked_documents or []:
            if linked.text is not None and linked.path and not (run / linked.path).exists():
                write_atomic(run / linked.path, linked.text)
        write_atomic(run / REQUESTS / f"{request.request_id}.yaml", dump(plain(request)))
    write_atomic(run / INVENTORY, dump(data.inventory()))
