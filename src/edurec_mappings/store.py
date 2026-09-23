"""The append-only store: anonymized request versions, proposals, outcomes and scraped documents.

```
<store>/requests/<request_id>/<hash>.yaml    anonymized request versions
<store>/proposals/<request_id>/<hash>.yaml   written by the course-mapping advisor
<store>/outcomes/<request_id>/<hash>.yaml    written by `review`
<store>/documents/<url_hash>.txt             latest text per URL
<store>/private/identities.yaml              request_id -> real student ID, for `review`
<store>/private/secret                       the key of every request_id and pseudonym
```

A request version is named by a digest of its content, so a proposal or outcome
stays attached to exactly the content it was made on. Nothing is ever deleted.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
from collections.abc import Callable, Hashable, Iterable, Iterator
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TypeVar

import yaml

from .anonymize import anonymize
from .models import Request, hydrate, plain

REQUESTS = "requests"
PROPOSALS = "proposals"
OUTCOMES = "outcomes"
DOCUMENTS = "documents"
PRIVATE = "private"
IDENTITIES = f"{PRIVATE}/identities.yaml"
SECRET = f"{PRIVATE}/secret"
UNHASHED = {"schema_version", "created_at", "status", "linked_documents"}
"""Request fields outside the content hash; linked documents enter it as text digests."""

T = TypeVar("T")


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def write_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(content, encoding="utf-8")
    temp.replace(path)


def dump(value: object) -> str:
    return yaml.safe_dump(value, allow_unicode=True, sort_keys=False)


def load(path: Path) -> object:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def content_hash(request: Request) -> str:
    """Digest of what a proposal rests on: the request and the text of its linked documents.

    Fetch metadata, the EduRec status and the storage fields are left out. The text
    is only in memory while exporting, so the hash is computed before a version is written.
    """
    content = plain(request, exclude=UNHASHED)
    if request.linked_documents is not None:
        content["linked_documents"] = [
            {"url": linked.url, "text": None if linked.text is None else sha256(linked.text)}
            for linked in request.linked_documents
        ]
    return sha256(json.dumps(content, sort_keys=True))[:16]


def link_siblings(requests: list[Request]) -> None:
    """Tell each request about the other given requests in its mapping group."""
    groups: dict[tuple[str, ...], list[str]] = {}
    for request in requests:
        groups.setdefault(request.identity.mapping, []).append(request.request_id)
    for request in requests:
        siblings = groups[request.identity.mapping]
        request.related_request_ids = [r for r in siblings if r != request.request_id]


def grouped(items: Iterable[T], key: Callable[[T], Hashable]) -> list[T]:
    """`items` with equal keys moved together, at the position of the first of them."""
    items = list(items)
    first: dict[Hashable, int] = {}
    for index, item in enumerate(items):
        first.setdefault(key(item), index)
    return sorted(items, key=lambda item: first[key(item)])


@dataclass(frozen=True)
class Version:
    """One stored version of a request, named by its content hash."""

    request: Request
    hash: str

    @property
    def request_id(self) -> str:
        return self.request.request_id

    def path(self, kind: str = REQUESTS) -> str:
        """The version's file under `kind` (requests, proposals or outcomes), store-relative."""
        return f"{kind}/{self.request_id}/{self.hash}.yaml"


class Store:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def file(self, kind: str, request_id: str, version: str) -> Path:
        return self.root / kind / request_id / f"{version}.yaml"

    def secret(self) -> bytes:
        """The key of every identifier, generated on first use and never replaced."""
        path = self.root / SECRET
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as file:
                file.write(secrets.token_hex(32))
        return path.read_text(encoding="utf-8").strip().encode()

    def identities(self) -> dict[str, str]:
        """Real student ID by request_id, for reopening requests in EduRec."""
        path = self.root / IDENTITIES
        data = load(path) if path.exists() else {}
        if not isinstance(data, dict):
            raise ValueError(f"{path} is not a mapping")
        return {str(key): str(value) for key, value in data.items()}

    def save(self, requests: Iterable[Request]) -> list[Version]:
        """Store the export's requests; returns the versions that were new.

        Each request is anonymized and hashed; it is written as a new version unless its
        latest stored version has the same hash. Document text is (over)written per URL,
        and the real student IDs are merged into `private/identities.yaml` first.
        """
        requests = list(requests)
        secret = self.secret()
        stored = [anonymize(request, secret) for request in requests]
        link_siblings(stored)
        identities = self.identities()
        identities.update(
            (anonymous.request_id, request.identity.student_id)
            for request, anonymous in zip(requests, stored, strict=True)
        )
        write_atomic(self.root / IDENTITIES, dump(dict(sorted(identities.items()))))
        latest = {version.request_id: version.hash for version in self.latest()}
        added: list[Version] = []
        for request in stored:
            for linked in request.linked_documents or []:
                if linked.text is not None and linked.path:
                    write_atomic(self.root / linked.path, linked.text)
            digest = content_hash(request)
            if latest.get(request.request_id) == digest:
                continue
            request.created_at = now()
            write_atomic(self.file(REQUESTS, request.request_id, digest), dump(plain(request)))
            added.append(Version(request, digest))
        return added

    def versions(self) -> Iterator[Version]:
        for path in sorted((self.root / REQUESTS).glob("*/*.yaml")):
            try:
                request = hydrate(Request, load(path))
            except ValueError as error:
                raise ValueError(f"{path}: {error}") from error
            yield Version(request, path.stem)

    def latest(self) -> list[Version]:
        """The newest version of every request, oldest first, sibling parts consecutive."""
        newest: dict[str, Version] = {}
        for version in self.versions():
            current = newest.get(version.request_id)
            if current is None or (version.request.created_at or "") > (
                current.request.created_at or ""
            ):
                newest[version.request_id] = version
        ordered = sorted(newest.values(), key=lambda v: (v.request.created_at or "", v.request_id))
        return grouped(ordered, lambda v: v.request.identity.mapping)

    def pending(self) -> list[Version]:
        """Latest versions without a proposal file for that version."""
        return [
            version
            for version in self.latest()
            if not self.file(PROPOSALS, version.request_id, version.hash).exists()
        ]
