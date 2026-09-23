"""The run directory on disk: `inventory.yaml`, one YAML per request and scraped documents."""

from __future__ import annotations

import shutil
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path

import yaml

from .models import Collection, Document, MappingGroup, Request, plain

INVENTORY = "inventory.yaml"
REQUESTS = "requests"
DOCUMENTS = "documents"


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def document() -> Document:
    return Document(collection=Collection(started_at=now()))


def mapping_groups(requests: list[Request]) -> list[MappingGroup]:
    """Group requests by mapping identity and tell each request about its collected siblings."""
    groups: dict[str, MappingGroup] = {}
    for request in requests:
        group = groups.setdefault(
            request.group_id,
            MappingGroup(
                group_id=request.group_id,
                request_ids=[],
                completeness="unverified" if request.many_to_one else "single_mapping",
            ),
        )
        group.request_ids.append(request.request_id)
    for request in requests:
        siblings = groups[request.group_id].request_ids
        request.related_request_ids = [r for r in siblings if r != request.request_id]
    return list(groups.values())


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
    data.mapping_groups = mapping_groups(data.requests)
    data.collection.updated_at = now()
    for request in data.requests if requests is None else requests:
        for linked in request.linked_documents or []:
            if linked.text is not None and linked.path and not (run / linked.path).exists():
                write_atomic(run / linked.path, linked.text)
        write_atomic(run / REQUESTS / f"{request.request_id}.yaml", dump(plain(request)))
    write_atomic(run / INVENTORY, dump(data.inventory()))
