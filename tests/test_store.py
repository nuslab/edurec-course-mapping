import copy
import hmac
import stat
import tempfile
import unittest
from collections.abc import Callable
from pathlib import Path

import yaml

from edurec_mappings.anonymize import anonymize, pseudonym, request_id
from edurec_mappings.cli import run_pending
from edurec_mappings.models import SCHEMA_VERSION, Identity, LinkedDocument, Request, plain
from edurec_mappings.store import IDENTITIES, PROPOSALS, SECRET, Store, content_hash
from tests.test_cli import namespace, quietly
from tests.test_export import records

IDENTITY = Identity(
    student_id="A0000001X",
    academic_career="Undergraduate",
    partner_university="Technical University of Munich",
    study_program="SEP",
    term="2025/2026 Semester 1",
    mapping_number="1",
    sequence="2",
)


def sample(count: int = 3) -> list[Request]:
    return [copy.deepcopy(r) for _, r in records()[:count]]


def with_document(request: Request, text: str | None) -> Request:
    request = copy.deepcopy(request)
    request.linked_documents = [
        LinkedDocument(
            "https://example.org/s.pdf",
            status="fetched" if text else "failed",
            text=text,
            bytes=len(text or ""),
            path="documents/abc.txt" if text else None,
        )
    ]
    return request


class KeyTests(unittest.TestCase):
    def test_identifiers_are_keyed_hmacs(self) -> None:
        self.assertEqual(request_id(b"k", IDENTITY), request_id(b"k", copy.deepcopy(IDENTITY)))
        self.assertNotEqual(request_id(b"k", IDENTITY), request_id(b"other", IDENTITY))
        self.assertEqual(len(request_id(b"k", IDENTITY)), 24)
        expected = hmac.new(b"k", b"A0000001X", "sha256").hexdigest()[:12]
        self.assertEqual(pseudonym(b"k", "A0000001X"), f"student-{expected}")

    def test_anonymize_leaves_the_original_untouched(self) -> None:
        (request,) = sample(1)
        before = plain(request)
        anonymous = anonymize(request, b"k")
        self.assertEqual(plain(request), before)
        self.assertEqual(anonymous.request_id, request_id(b"k", request.identity))
        self.assertTrue(anonymous.identity.student_id.startswith("student-"))

    def test_content_hash_covers_content_and_document_text_only(self) -> None:
        (request,) = sample(1)
        base = content_hash(request)
        same: list[Callable[[Request], None]] = [
            lambda r: setattr(r, "status", "Approved"),
            lambda r: setattr(r, "created_at", "2026-01-01T00:00:00+00:00"),
            lambda r: setattr(r, "schema_version", 99),
        ]
        different: list[Callable[[Request], None]] = [
            lambda r: setattr(r, "comments", "new comment"),
            lambda r: setattr(r, "related_request_ids", ["x"]),
        ]
        for change in same:
            changed = copy.deepcopy(request)
            change(changed)
            self.assertEqual(content_hash(changed), base)
        for change in different:
            changed = copy.deepcopy(request)
            change(changed)
            self.assertNotEqual(content_hash(changed), base)
        fetched = with_document(request, "Week 1")
        self.assertNotEqual(content_hash(fetched), base)
        self.assertNotEqual(content_hash(with_document(request, "Week 2")), content_hash(fetched))
        metadata = copy.deepcopy(fetched)
        assert metadata.linked_documents is not None
        metadata.linked_documents[0].bytes, metadata.linked_documents[0].title = 1, "T"
        self.assertEqual(content_hash(metadata), content_hash(fetched))
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
        added = self.store.save([with_document(r, "Week 1: search") for r in requests])
        self.assertEqual(len(added), 3)
        self.assertEqual(
            [p.relative_to(self.root).as_posix() for p in self.files()],
            sorted(v.path() for v in added),
        )
        secret = self.root / SECRET
        self.assertEqual(stat.S_IMODE(secret.stat().st_mode), 0o600)
        self.assertEqual((self.root / "documents" / "abc.txt").read_text(), "Week 1: search")
        stored = yaml.safe_load(self.files()[0].read_text())
        self.assertEqual(stored["schema_version"], SCHEMA_VERSION)
        self.assertEqual(list(stored)[:3], ["schema_version", "created_at", "request_id"])
        self.assertNotIn("text", stored["linked_documents"][0])
        text = "".join(p.read_text() for p in self.files())
        for request in requests:
            self.assertNotIn(request.identity.student_id, text)
        identities = self.store.identities()
        self.assertEqual(
            identities,
            {v.request_id: r.identity.student_id for v, r in zip(added, requests, strict=True)},
        )

    def test_ids_are_stable_across_exports_and_unchanged_requests_are_not_rewritten(self) -> None:
        first = self.store.save(sample())
        secret = (self.root / SECRET).read_text()
        before = {p: p.stat().st_mtime_ns for p in self.files()}
        self.assertEqual(self.store.save(sample()), [])
        self.assertEqual({p: p.stat().st_mtime_ns for p in self.files()}, before)
        self.assertEqual((self.root / SECRET).read_text(), secret, "never regenerated")
        self.assertEqual([v.request_id for v in self.store.latest()], [v.request_id for v in first])

    def test_a_changed_request_adds_a_version_that_becomes_latest(self) -> None:
        (old,) = self.store.save(sample(1))
        changed = sample(1)
        changed[0].comments = "Please add the syllabus"
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

    def test_identities_are_merged(self) -> None:
        requests = sample(3)
        self.store.save(requests[:1])
        self.store.save(requests[1:])
        self.assertEqual(len(self.store.identities()), 3)
        self.assertEqual(
            yaml.safe_load((self.root / IDENTITIES).read_text()), self.store.identities()
        )

    def test_pending_is_the_unproposed_latest_versions_with_siblings_together(self) -> None:
        requests = sample(3)
        requests[2].identity = copy.deepcopy(requests[0].identity)
        requests[2].identity.sequence = "2"
        added = self.store.save(requests)
        self.assertEqual(added[0].request.related_request_ids, [added[2].request_id])
        pending = self.store.pending()
        self.assertEqual(
            [v.request_id for v in pending],
            [added[0].request_id, added[2].request_id, added[1].request_id],
        )
        proposed = self.root / added[1].path(PROPOSALS)
        proposed.parent.mkdir(parents=True)
        proposed.write_text("verdict: approve\n")
        self.assertNotIn(added[1].request_id, [v.request_id for v in self.store.pending()])
        out = quietly(run_pending, namespace(store=str(self.root)))
        self.assertEqual(out.split(), [added[0].path(), added[2].path()])
        # A new version of a proposed request is pending again.
        changed = sample(2)[1]
        changed.comments = "edited"
        self.store.save([changed])
        self.assertIn(added[1].request_id, [v.request_id for v in self.store.pending()])
