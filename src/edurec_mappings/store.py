"""The append-only store: anonymized request versions, proposals, outcomes and scraped documents.

```
<store>/requests/<request_id>/<hash>.yaml    anonymized request versions
<store>/proposals/<request_id>/<hash>.yaml   written outside this package
<store>/outcomes/<request_id>/<hash>.yaml    written by `review`
<store>/documents/<url_hash>.txt             latest text per URL
<store>/private/student_ids.yaml             request_id -> real student ID, for `review`
<store>/private/hmac_key                     the key of every request_id and pseudonym
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
from .models import Outcome, Proposal, Request, hydrate, plain

REQUESTS = "requests"
PROPOSALS = "proposals"
OUTCOMES = "outcomes"
DOCUMENTS = "documents"
PRIVATE = "private"
STUDENT_IDS = f"{PRIVATE}/student_ids.yaml"
HMAC_KEY = f"{PRIVATE}/hmac_key"
UNHASHED = {"schema_version", "created_at", "approval_status", "documents"}
"""Request fields outside the content hash; documents enter it as text digests."""

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


def read(cls: type[T], path: Path) -> T:
    """Load a record file; a `ValueError` names the file."""
    try:
        return hydrate(cls, load(path))
    except ValueError as error:
        raise ValueError(f"{path}: {error}") from error


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def document_path(url: str) -> str:
    """Store-relative file for a URL's text; the same URL always maps to one file."""
    return f"{DOCUMENTS}/{sha256(url)[:16]}.txt"


def content_hash(request: Request) -> str:
    """Digest of what a proposal rests on: the request and the text of its documents.

    Fetch metadata, the EduRec status and the storage fields are left out. The text
    is only in memory while exporting, so the hash is computed before a version is written.
    """
    content = plain(request, exclude=UNHASHED)
    if request.documents is not None:
        content["documents"] = [
            {"url": document.url, "text": None if document.text is None else sha256(document.text)}
            for document in request.documents
        ]
    return sha256(json.dumps(content, sort_keys=True))[:16]


def link_siblings(requests: list[Request]) -> None:
    """Tell each request about the other given requests in its mapping group."""
    groups: dict[tuple[str, ...], list[str]] = {}
    for request in requests:
        groups.setdefault(request.identity.group_key, []).append(request.request_id)
    for request in requests:
        siblings = groups[request.identity.group_key]
        request.sibling_request_ids = [r for r in siblings if r != request.request_id]


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

    def file(self, version: Version, kind: str = REQUESTS) -> Path:
        return self.root / version.path(kind)

    def hmac_key(self) -> bytes:
        """The key of every identifier, generated on first use and never replaced."""
        path = self.root / HMAC_KEY
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as file:
                file.write(secrets.token_hex(32))
        return path.read_text(encoding="utf-8").strip().encode()

    def student_ids(self) -> dict[str, str]:
        """Real student ID by request_id, for reopening requests in EduRec."""
        path = self.root / STUDENT_IDS
        data = load(path) if path.exists() else {}
        if not isinstance(data, dict):
            raise ValueError(f"{path} is not a mapping")
        return {str(key): str(value) for key, value in data.items()}

    def save(self, requests: Iterable[Request]) -> list[Version]:
        """Store the export's requests; returns the versions that were new.

        Each request is anonymized and hashed; it is written as a new version unless its
        latest stored version has the same hash. Document text is (over)written per URL,
        and the real student IDs are merged into `private/student_ids.yaml` first.
        """
        requests = list(requests)
        key = self.hmac_key()
        stored = [anonymize(request, key) for request in requests]
        link_siblings(stored)
        student_ids = self.student_ids()
        student_ids.update(
            (anonymous.request_id, request.identity.student_id)
            for request, anonymous in zip(requests, stored, strict=True)
        )
        write_atomic(self.root / STUDENT_IDS, dump(dict(sorted(student_ids.items()))))
        latest = {version.request_id: version.hash for version in self.latest()}
        added: list[Version] = []
        for request in stored:
            for document in request.documents or []:
                if document.text is not None:
                    document.text_path = document_path(document.url)
                    write_atomic(self.root / document.text_path, document.text)
            digest = content_hash(request)
            if latest.get(request.request_id) == digest:
                continue
            request.created_at = now()
            version = Version(request, digest)
            write_atomic(self.file(version), dump(plain(request)))
            added.append(version)
        return added

    def versions(self) -> Iterator[Version]:
        for path in sorted((self.root / REQUESTS).glob("*/*.yaml")):
            yield Version(read(Request, path), path.stem)

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
        return grouped(ordered, lambda v: v.request.identity.group_key)

    def pending(self) -> list[Version]:
        return [version for version in self.latest() if not self.file(version, PROPOSALS).exists()]

    def proposal(self, version: Version) -> Proposal | None:
        path = self.file(version, PROPOSALS)
        return read(Proposal, path) if path.exists() else None

    def outcome(self, version: Version) -> Outcome | None:
        path = self.file(version, OUTCOMES)
        return read(Outcome, path) if path.exists() else None

    def save_outcome(self, version: Version, outcome: Outcome) -> None:
        write_atomic(self.file(version, OUTCOMES), dump(plain(outcome)))
