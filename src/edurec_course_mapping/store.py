"""The append-only store: pseudonymized request versions, proposals, outcomes and scraped documents.

```
<store>/requests/<request_id>/<hash>.yaml    pseudonymized request versions
<store>/proposals/<request_id>/<hash>.yaml   written by a reviewer or an agent
<store>/outcomes/<request_id>/<hash>.yaml    written by `review`
<store>/documents/<url_hash>/<hash>.md       fetch results per URL: front matter and text
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
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path

import yaml

from .documents import find_urls
from .models import DocumentReference, LinkedDocument, Outcome, Proposal, Request, hydrate, plain
from .pseudonymize import pseudonymize

REQUESTS = "requests"
PROPOSALS = "proposals"
OUTCOMES = "outcomes"
DOCUMENTS = "documents"
PRIVATE = "private"
STUDENT_IDS = f"{PRIVATE}/student_ids.yaml"
HMAC_KEY = f"{PRIVATE}/hmac_key"
UNHASHED = {"schema_version", "created_at", "approval_status"}
"""Request fields outside the content hash."""
FRONT_MATTER = "---\n"


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


def write_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(content, encoding="utf-8")
    temp.replace(path)


def dump(value: object) -> str:
    return yaml.safe_dump(value, allow_unicode=True, sort_keys=False)


def load(path: Path) -> object:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def read[T](cls: type[T], path: Path) -> T:
    """Load a record file; a `ValueError` names the file."""
    try:
        return hydrate(cls, load(path))
    except ValueError as error:
        raise ValueError(f"{path}: {error}") from error


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def document_folder(url: str) -> str:
    """Store-relative folder of a URL's results; the same URL always maps to one folder."""
    return f"{DOCUMENTS}/{sha256(url)[:16]}"


def document_hash(document: LinkedDocument) -> str:
    """Digest of a fetch result: what was read, not when."""
    content = plain(document, exclude={"fetched_at"}) | {"text": document.text}
    return sha256(json.dumps(content, sort_keys=True))[:16]


def dump_document(document: LinkedDocument) -> str:
    return f"{FRONT_MATTER}{dump(plain(document))}{FRONT_MATTER}{document.text or ''}"


def read_document(path: Path) -> LinkedDocument:
    """Load a document file; a `ValueError` names the file."""
    content = path.read_text(encoding="utf-8")
    header, separator, body = content.removeprefix(FRONT_MATTER).partition(f"\n{FRONT_MATTER}")
    try:
        if not content.startswith(FRONT_MATTER) or not separator:
            raise ValueError("no front matter")
        document = hydrate(LinkedDocument, yaml.safe_load(header))
    except ValueError as error:
        raise ValueError(f"{path}: {error}") from error
    return replace(document, text=body if document.status == "fetched" else None)


def content_hash(request: Request) -> str:
    """Digest of what a proposal rests on, including the stored documents it refers to."""
    return sha256(json.dumps(plain(request, exclude=UNHASHED), sort_keys=True))[:16]


def link_siblings(requests: list[Request]) -> None:
    """Tell each request about the other given requests in its mapping group."""
    groups: dict[tuple[str, ...], list[str]] = {}
    for request in requests:
        groups.setdefault(request.identity.group_key, []).append(request.request_id)
    for request in requests:
        siblings = groups[request.identity.group_key]
        request.sibling_request_ids = [r for r in siblings if r != request.request_id]


def grouped[T](items: Iterable[T], key: Callable[[T], Hashable]) -> list[T]:
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

    def save(
        self, requests: Iterable[Request], documents: Iterable[LinkedDocument] = ()
    ) -> list[Version]:
        """Store the export's documents and requests; returns the versions that were new.

        Each request is pseudonymized, refers to the stored documents of its URLs and is
        hashed; it is written as a new version unless its latest stored version has the
        same hash. The real student IDs are merged into `private/student_ids.yaml` first.
        """
        requests = list(requests)
        key = self.hmac_key()
        stored = [pseudonymize(request, key) for request in requests]
        link_siblings(stored)
        student_ids = self.student_ids()
        student_ids.update(
            (pseudonymous.request_id, request.identity.student_id)
            for request, pseudonymous in zip(requests, stored, strict=True)
        )
        write_atomic(self.root / STUDENT_IDS, dump(dict(sorted(student_ids.items()))))
        self.save_documents(documents)
        latest = {version.request_id: version.hash for version in self.latest()}
        added: list[Version] = []
        for request in stored:
            request.documents = [self.reference(url) for url in find_urls(request)]
            digest = content_hash(request)
            if latest.get(request.request_id) == digest:
                continue
            request.created_at = now()
            version = Version(request, digest)
            write_atomic(self.file(version), dump(plain(request)))
            added.append(version)
        return added

    def documents(self, url: str) -> list[tuple[str, LinkedDocument]]:
        """The URL's recorded results with their store-relative paths, oldest first."""
        folder = self.root / document_folder(url)
        found = [(path, read_document(path)) for path in folder.glob("*.md")]
        found.sort(key=lambda item: item[1].fetched_at or "")
        return [(path.relative_to(self.root).as_posix(), document) for path, document in found]

    def reference(self, url: str) -> DocumentReference:
        readable = [path for path, stored in self.documents(url) if stored.status != "failed"]
        return DocumentReference(url, readable[-1] if readable else None)

    def save_documents(self, documents: Iterable[LinkedDocument]) -> None:
        """Record each fetch result unless it repeats the newest one.

        A readable result is compared with the newest readable one, so a failure in
        between is no change; returning to older content rewrites that file as the newest.
        """
        for document in documents:
            history = [stored for _, stored in self.documents(document.url)]
            if document.status != "failed":
                history = [stored for stored in history if stored.status != "failed"]
            digest = document_hash(document)
            if history and document_hash(history[-1]) == digest:
                continue
            path = self.root / document_folder(document.url) / f"{digest}.md"
            write_atomic(path, dump_document(replace(document, fetched_at=now())))

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
