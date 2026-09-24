import copy
import stat
import tempfile
import unittest
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from unittest import mock

import yaml

from edurec_mappings.cli import run_pending
from edurec_mappings.models import (
    SCHEMA_VERSION,
    DocumentReference,
    LinkedDocument,
    Outcome,
    Request,
    plain,
)
from edurec_mappings.store import (
    HMAC_KEY,
    PROPOSALS,
    STUDENT_IDS,
    Store,
    content_hash,
    document_folder,
    read_document,
)
from tests.test_cli import namespace, quietly
from tests.test_export import records
from tests.test_parse import detail

URL = "https://example.org/s.pdf"


def sample(count: int = 3) -> list[Request]:
    return [copy.deepcopy(r) for _, r in records()[:count]]


def linked(request: Request) -> Request:
    request = copy.deepcopy(request)
    request.partner_course.supporting_url = URL
    return request


def fetched(text: str | None, error: str = "HTTP 503") -> LinkedDocument:
    if text is None:
        return LinkedDocument(URL, error=error)
    return LinkedDocument(URL, status="fetched", kind="pdf", text=text, text_bytes=len(text))


class ContentHashTests(unittest.TestCase):
    def test_content_hash_covers_content_and_document_references_only(self) -> None:
        (request,) = sample(1)
        base = content_hash(request)
        same: list[Callable[[Request], None]] = [
            lambda r: setattr(r, "approval_status", "Approved"),
            lambda r: setattr(r, "created_at", "2026-01-01T00:00:00+00:00"),
            lambda r: setattr(r, "schema_version", 99),
        ]
        different: list[Callable[[Request], None]] = [
            lambda r: setattr(r, "review_comments", "new comment"),
            lambda r: setattr(r, "sibling_request_ids", ["x"]),
            lambda r: setattr(r, "documents", [DocumentReference(URL, "documents/u/a.md")]),
        ]
        for change in same:
            changed = copy.deepcopy(request)
            change(changed)
            self.assertEqual(content_hash(changed), base)
        for change in different:
            changed = copy.deepcopy(request)
            change(changed)
            self.assertNotEqual(content_hash(changed), base)
        self.assertEqual(len(base), 16)


class StoreTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name) / "store"
        self.store = Store(self.root)

    def files(self, kind: str = "requests") -> list[Path]:
        return sorted((self.root / kind).glob("*/*.yaml"))

    def test_layout_and_no_real_student_id_under_requests(self) -> None:
        requests = sample()
        added = self.store.save([linked(r) for r in requests], [fetched("Week 1: search")])
        self.assertEqual(len(added), 3)
        self.assertEqual(
            [p.relative_to(self.root).as_posix() for p in self.files()],
            sorted(v.path() for v in added),
        )
        key = self.root / HMAC_KEY
        self.assertEqual(stat.S_IMODE(key.stat().st_mode), 0o600)
        stored = yaml.safe_load(self.files()[0].read_text())
        self.assertEqual(stored["schema_version"], SCHEMA_VERSION)
        self.assertEqual(list(stored)[:3], ["schema_version", "created_at", "request_id"])
        (path,) = (self.root / document_folder(URL)).iterdir()
        self.assertEqual(
            stored["documents"], [{"url": URL, "path": path.relative_to(self.root).as_posix()}]
        )
        document = read_document(path)
        self.assertEqual((document.status, document.text), ("fetched", "Week 1: search"))
        self.assertTrue(path.read_text().startswith("---\nurl: "))
        text = "".join(p.read_text() for p in self.files())
        for request in requests:
            self.assertNotIn(request.identity.student_id, text)
        self.assertEqual(
            self.store.student_ids(),
            {v.request_id: r.identity.student_id for v, r in zip(added, requests, strict=True)},
        )

    def test_stored_request_has_no_session_tokens(self) -> None:
        (version,) = self.store.save([detail()])
        content = self.store.file(version).read_text()
        result = yaml.safe_load(content)
        self.assertEqual(result, plain(version.request))
        self.assertNotIn("ICSID", content)
        self.assertNotIn("&id", content, "Records must not be emitted as YAML aliases")
        self.assertEqual(result["sibling_request_ids"], [])
        self.assertNotIn("name", result["student"])
        self.assertEqual(
            result["partner_course"]["assessments"][0].keys(),
            {"method", "weight_percent", "remark"},
        )

    def test_ids_are_stable_across_exports_and_unchanged_requests_are_not_rewritten(self) -> None:
        first = self.store.save(sample())
        key = (self.root / HMAC_KEY).read_text()
        before = {p: p.stat().st_mtime_ns for p in self.files()}
        self.assertEqual(self.store.save(sample()), [])
        self.assertEqual({p: p.stat().st_mtime_ns for p in self.files()}, before)
        self.assertEqual((self.root / HMAC_KEY).read_text(), key, "never regenerated")
        self.assertEqual([v.request_id for v in self.store.latest()], [v.request_id for v in first])

    def test_a_changed_request_adds_a_version_that_becomes_latest(self) -> None:
        (old,) = self.store.save(sample(1))
        changed = sample(1)
        changed[0].review_comments = "Please add the syllabus"
        (new,) = self.store.save(changed)
        self.assertEqual(new.request_id, old.request_id)
        self.assertNotEqual(new.hash, old.hash)
        self.assertEqual(len(self.files()), 2, "nothing is deleted")
        (latest,) = self.store.latest()
        self.assertEqual(latest.hash, new.hash)
        # Reverting to the first content makes that version the latest again.
        (again,) = self.store.save(sample(1))
        self.assertEqual(again.hash, old.hash)
        self.assertEqual(self.store.latest()[0].hash, old.hash)

    def test_student_ids_are_merged(self) -> None:
        requests = sample(3)
        self.store.save(requests[:1])
        self.store.save(requests[1:])
        self.assertEqual(len(self.store.student_ids()), 3)
        self.assertEqual(
            yaml.safe_load((self.root / STUDENT_IDS).read_text()), self.store.student_ids()
        )

    def test_outcomes_round_trip_per_version(self) -> None:
        (version,) = self.store.save(sample(1))
        self.assertIsNone(self.store.outcome(version))
        outcome = Outcome(verdict="approve", comment="c", verified=True, recorded_at="t")
        self.store.save_outcome(version, outcome)
        self.assertEqual(self.store.outcome(version), outcome)
        self.assertIsNone(self.store.proposal(version))

    def test_pending_is_the_unproposed_latest_versions_with_siblings_together(self) -> None:
        requests = sample(3)
        requests[2].identity = copy.deepcopy(requests[0].identity)
        requests[2].identity.sequence = "2"
        added = self.store.save(requests)
        self.assertEqual(added[0].request.sibling_request_ids, [added[2].request_id])
        pending = self.store.pending()
        self.assertEqual(
            [v.request_id for v in pending],
            [added[0].request_id, added[2].request_id, added[1].request_id],
        )
        proposed = self.store.file(added[1], PROPOSALS)
        proposed.parent.mkdir(parents=True)
        proposed.write_text("verdict: approve\n")
        self.assertNotIn(added[1].request_id, [v.request_id for v in self.store.pending()])
        out = quietly(run_pending, namespace(store=str(self.root)))
        self.assertEqual(out.split(), [added[0].path(), added[2].path()])
        # A new version of a proposed request is pending again.
        changed = sample(2)[1]
        changed.review_comments = "edited"
        self.store.save([changed])
        self.assertIn(added[1].request_id, [v.request_id for v in self.store.pending()])


class StoredDocumentTests(unittest.TestCase):
    """A request version changes with the text of its documents and nothing else about them."""

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = Store(Path(directory.name) / "store")
        (self.request,) = sample(1)
        self.request = linked(self.request)

    def save(self, *documents: LinkedDocument) -> int:
        """Save the request with these fetch results; the number of new versions."""
        return len(self.store.save([copy.deepcopy(self.request)], documents))

    def current(self) -> str | None:
        (version,) = self.store.latest()
        return version.request.documents[0].path

    def statuses(self) -> list[str]:
        return [document.status for _, document in self.store.documents(URL)]

    def test_an_export_without_documents_keeps_the_version(self) -> None:
        self.assertEqual(self.save(fetched("Week 1")), 1)
        path = self.current()
        self.assertEqual(self.save(), 0)
        self.assertEqual(self.current(), path)

    def test_a_failed_fetch_is_recorded_but_keeps_the_version(self) -> None:
        self.save(fetched("Week 1"))
        path = self.current()
        self.assertEqual(self.save(fetched(None)), 0)
        self.assertEqual(self.current(), path)
        self.assertEqual(self.statuses(), ["fetched", "failed"])
        self.assertEqual(self.save(fetched(None)), 0)
        self.assertEqual(
            self.statuses(), ["fetched", "failed"], "a repeated failure is not rewritten"
        )
        self.assertEqual(self.save(fetched("Week 1")), 0, "the same text after a failure")
        self.assertEqual(self.statuses(), ["fetched", "failed"])

    def test_new_text_is_a_new_version_and_old_text_can_return(self) -> None:
        self.save(fetched("Week 1"))
        first = self.current()
        self.assertEqual(self.save(fetched("Week 2")), 1)
        self.assertNotEqual(self.current(), first)
        self.assertEqual(self.save(fetched("Week 1")), 1)
        self.assertEqual(self.current(), first)

    def test_a_url_read_for_the_first_time_is_a_new_version(self) -> None:
        self.assertEqual(self.save(fetched(None)), 1)
        self.assertIsNone(self.current())
        self.assertEqual(self.save(fetched(None, error="HTTP 404")), 0)
        self.assertEqual(self.statuses(), ["failed", "failed"], "a different error is recorded")
        self.assertEqual(self.save(fetched("Week 1")), 1)
        self.assertIsNotNone(self.current())

    def test_unreadable_results_are_kept_without_text(self) -> None:
        too_large = LinkedDocument(URL, status="too_large", error="big", text_bytes=10**6)
        self.save(too_large)
        path = self.current()
        assert path is not None
        self.assertEqual(
            read_document(self.store.root / path), replace(too_large, fetched_at=mock.ANY)
        )
