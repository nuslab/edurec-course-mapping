from __future__ import annotations

import copy
import tempfile
import unittest
from collections.abc import Callable, Mapping
from dataclasses import replace
from html import escape
from pathlib import Path
from typing import TYPE_CHECKING, TypedDict

from edurec_mappings.edurec import NOT_IN_QUEUE, SKIPPED, NotInQueueError, PanelSetup
from edurec_mappings.models import (
    PENDING_APPROVAL,
    Clicked,
    Confidence,
    Fallback,
    Outcome,
    Proposal,
    Reaction,
    Remap,
    Request,
    Skipped,
    Verdict,
    hydrate,
    plain,
)
from edurec_mappings.review import (
    OVERLAP_FAIR,
    OVERLAP_GOOD,
    Progress,
    QueueItem,
    action_of,
    comment_problem,
    filter_queue,
    load_outcomes,
    load_queue,
    overlap_colour,
    panel_html,
    panel_setup,
    proposal_problems,
    review,
    stale_reason,
)
from edurec_mappings.store import OUTCOMES, PROPOSALS, Store, Version, dump, load
from tests.test_export import records

if TYPE_CHECKING:
    from typing import Unpack

STARTED = "2026-09-22T10:00:00+00:00"


class ProposalFields(TypedDict, total=False):
    comment: str
    overlap_percent: int
    confidence: Confidence
    overlap_topics: list[str]
    missing_from_partner: list[str]
    extra_in_partner: list[str]
    concerns: list[str]
    remap: Remap | None
    fallback: Fallback | None
    fallback_rationale: str | None


PROGRESS = Progress(position=3, total=12, submitted=5, requests=104)
GREEN, AMBER, RED = "#2e7d32", "#ef6c00", "#c62828"
FALLBACK = Fallback(
    verdict="request_remapping", comment="Planning is missing. Consider remapping to CS5242."
)
RATIONALE = "Overlap is close to the 70% <threshold>."


def ordered(html: str, *needles: str) -> None:
    """Assert every needle occurs in `html`, in the given order."""
    position = -1
    for needle in needles:
        found = html.find(needle, position + 1)
        assert found > position, f"{needle!r} missing or out of order"
        position = found


def proposal(
    request: Request, verdict: Verdict = "approve", **overrides: Unpack[ProposalFields]
) -> Proposal:
    values = Proposal(
        verdict=verdict,
        comment="Approved: the syllabus covers search & <planning>.",
        overlap_percent=85,
        confidence="high",
        overlap_topics=["Search"],
        missing_from_partner=["Planning"],
        concerns=["Weight of exam < 50%"],
    )
    return replace(values, **overrides)


def with_fallback(request: Request) -> Proposal:
    return proposal(request, fallback=FALLBACK, fallback_rationale=RATIONALE)


def make_store(directory: str, count: int = 4) -> tuple[Store, list[Version]]:
    """A store of `count` requests with a proposal on each."""
    requests = [copy.deepcopy(r) for _, r in records()[:count]]
    if count > 1:  # The first two become parts of one many-to-one mapping.
        requests[1].identity = replace(requests[0].identity, sequence="2")
        requests[0].mapping_type = requests[1].mapping_type = "Many to One"
    store = Store(Path(directory) / "store")
    versions = store.save(requests)
    for version in versions:
        write_proposal(store, version, proposal(version.request))
    return store, versions


def write_proposal(store: Store, version: Version, advice: Proposal | dict[str, object]) -> Path:
    path = store.file(version, PROPOSALS)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dump(advice if isinstance(advice, dict) else plain(advice)))
    return path


def stored_outcomes(store: Store) -> dict[str, Outcome]:
    return {
        path.parent.name: hydrate(Outcome, load(path))
        for path in (store.root / OUTCOMES).glob("*/*.yaml")
    }


def queue_of(store: Store) -> list[QueueItem]:
    latest = store.latest()
    return load_queue(store, latest, load_outcomes(store, latest))


def item_of(store: Store, request_id: str) -> QueueItem:
    return next(i for i in queue_of(store) if i.request.request_id == request_id)


def ids_of(versions: list[Version]) -> list[str]:
    return [version.request_id for version in versions]


def skip_reason(entry: Outcome | Skipped) -> str:
    assert isinstance(entry, Skipped), entry
    return entry.reason


def outcome(entry: Outcome | Skipped) -> Outcome:
    assert isinstance(entry, Outcome), entry
    return entry


class FakeSite:
    """Scripted site: `reactions` maps request_id to the click or skip."""

    def __init__(
        self,
        reactions: Mapping[str, Reaction | Callable[[], Reaction]],
        live: Mapping[str, Request | Exception] | None = None,
        status_after: str | Exception = "Approved",
    ) -> None:
        self.reactions = reactions
        self.live = live or {}
        self.status_after = status_after
        """A string, or an exception to raise from `status`."""
        self.opened: list[str] = []
        self.students: list[str] = []
        self.prepared: list[tuple[str, str, bool]] = []
        self.setups: list[PanelSetup] = []
        self.current = ""

    def open(self, request: Request) -> Request:
        self.opened.append(request.request_id)
        self.students.append(request.identity.student_id)
        live = self.live.get(request.request_id, request)
        if isinstance(live, Exception):
            raise live
        live = copy.deepcopy(live)
        live.approval_status = live.approval_status or PENDING_APPROVAL
        self.current = live.request_id
        return live

    def prepare(self, setup: PanelSetup) -> None:
        self.prepared.append((self.current, setup["panel"], setup["dry_run"]))
        self.setups.append(setup)

    def await_action(self) -> Reaction:
        reaction = self.reactions[self.current]
        return reaction() if callable(reaction) else reaction

    def status(self, request: Request) -> str:
        if isinstance(self.status_after, Exception):
            raise self.status_after
        return self.status_after


class QueueTests(unittest.TestCase):
    def test_malformed_proposal_file_is_named(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store, versions = make_store(directory)
            advice = {**plain(proposal(versions[0].request)), "verdict": "Approve"}
            path = write_proposal(store, versions[0], advice)
            with self.assertRaisesRegex(ValueError, path.name):
                queue_of(store)

    def test_queue_is_the_proposed_latest_versions_without_outcome(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store, versions = make_store(directory)
            ids = ids_of(versions)
            store.file(versions[3], PROPOSALS).unlink()
            store.save_outcome(
                versions[2],
                Outcome(verdict="approve", comment="c", verified=True, recorded_at=STARTED),
            )
            queue = queue_of(store)
            self.assertEqual([item.request.request_id for item in queue], ids[:2])
            self.assertEqual([item.version for item in queue], versions[:2])
            real = [r.identity.student_id for _, r in records()[:2]]
            self.assertEqual([item.request.identity.student_id for item in queue], real)
            # A changed request is a new version: its old proposal no longer applies.
            changed = copy.deepcopy(records()[0][1])
            changed.identity = replace(changed.identity, sequence="2")
            changed.review_comments = "resubmitted"
            (new,) = store.save([changed])
            self.assertEqual(new.request_id, ids[1])
            self.assertEqual([item.request.request_id for item in queue_of(store)], ids[:1])
            write_proposal(store, new, proposal(new.request))
            self.assertEqual(
                [(i.request.request_id, i.version.hash) for i in queue_of(store)],
                [(ids[0], versions[0].hash), (ids[1], new.hash)],
            )

    def test_queue_keeps_siblings_together_and_honours_filters(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store, versions = make_store(directory)
            write_proposal(store, versions[1], proposal(versions[1].request, verdict="reject"))
            queue = queue_of(store)
            ids = [item.request.request_id for item in queue]
            first, second = ids.index(versions[0].request_id), ids.index(versions[1].request_id)
            self.assertEqual(abs(first - second), 1)
            self.assertEqual(set(ids), set(ids_of(versions)))
            self.assertEqual(
                [i.request.request_id for i in filter_queue(queue, verdicts=["reject"])],
                [versions[1].request_id],
            )
            self.assertEqual(
                [i.request.request_id for i in filter_queue(queue, request_ids=[ids[2]])],
                [ids[2]],
            )

    def test_freshness_comparison(self) -> None:
        _, exported = records()[0]
        live = copy.deepcopy(exported)
        live.approval_status = PENDING_APPROVAL
        self.assertIsNone(stale_reason(exported, live))
        changed = copy.deepcopy(live)
        changed.approval_status = "Approved"
        self.assertIn("Approved", stale_reason(exported, changed) or "")
        changed = copy.deepcopy(live)
        changed.nus_course.number = "9999"
        self.assertIn("nus_course.number", stale_reason(exported, changed) or "")
        changed = copy.deepcopy(live)
        changed.identity.sequence = "7"
        self.assertIn("sequence", stale_reason(exported, changed) or "")

    def test_comment_sanity(self) -> None:
        self.assertIsNone(comment_problem("Consider remapping to CS5242."))
        self.assertIn("empty", comment_problem("  ") or "")
        self.assertIn("[", comment_problem("Fill in [course]") or "")
        self.assertIn("XXXX", comment_problem("Consider remapping to CSXXXX.") or "")

    def test_proposal_problems_name_the_comment_fields(self) -> None:
        request = records()[0][1]
        self.assertEqual(proposal_problems(proposal(request)), [])
        unfinished = proposal(
            request, comment=" ", fallback=Fallback(verdict="reject", comment="Use [course]")
        )
        self.assertEqual(
            proposal_problems(unfinished),
            ["comment is empty", "fallback.comment contains '['"],
        )


class PanelTests(unittest.TestCase):
    def test_panel_html_shows_the_proposal_in_order_and_escapes_content(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store, versions = make_store(directory)
            first = versions[0].request_id
            item = item_of(store, first)
            item.proposal = replace(
                with_fallback(item.request),
                remap=Remap("CS5242", "Better fit <b>"),
                extra_in_partner=["Robotics"],
            )
            sibling = versions[1].request_id
            outcomes = {
                sibling: Outcome(verdict="reject", comment="x", verified=True, recorded_at=STARTED)
            }
            courses = {sibling: "CS 2 (PU) -> CS3243"}
            html = panel_html(
                item, PROGRESS, outcomes, dry_run=True, existing="kept <below>", courses=courses
            )
            ordered(
                html,
                "<style>",
                f'class="pill active" data-tab="recommended" style="background:{GREEN}"',
                ">Approve<",
                f'class="pill" data-tab="fallback" style="background:{AMBER}"',
                ">Request Remapping<",
                f'style="background:{GREEN}">85% Overlap<',
                escape(item.request.course),
                'class="bar"',
                "width:25%",
                "3 of 12 this session &middot; 5 of 104 overall",
                "Dry run",
                'class="tabs"',
                ">Recommended<",
                ">Fallback<",
                'class="pane active selected" data-tab="recommended"',
                "<p><b>Decision:</b> Approve</p>",
                f'<p><span class="pill" style="background:{GREEN}">High Confidence</span></p>',
                "search &amp; &lt;planning&gt;",  # the pane holds only the comment
                'class="tools"',
                "Reset comment",
                '<button type="button" class="select" disabled>Selected</button>',
                'class="modified"',
                'class="pane" data-tab="fallback"',
                "<p><b>Decision:</b> Request Remapping</p>",
                "70% &lt;threshold&gt;",  # the rationale explains the fallback, so it stays with it
                "Planning is missing. Consider remapping to CS5242.",
                "Reset comment",
                '<button type="button" class="select">Select</button>',
                "Previous comments",
                "kept &lt;below&gt;",
                "Remap",
                "<p><b>Target:</b> CS5242</p>",
                "Better fit &lt;b&gt;",
                "Concerns",
                "Weight of exam &lt; 50%",
                "Overlap",
                "Search",
                "Missing from partner course",
                "Planning",
                "Extra in partner course",
                "Robotics",
                "Siblings",
                "CS 2 (PU) -&gt; CS3243: Reject",
                "different verdict",  # warning badge on the sibling entry only
                'id="reason"',
                'id="skip"',
            )
            self.assertEqual(html.count("pill active"), 1, "one verdict pill, in the header only")
            self.assertEqual(html.count('class="pill" data-tab'), 1)
            self.assertEqual(html.count("different verdict"), 1)
            self.assertEqual(html.count("Target:"), 1)
            self.assertEqual(html.count("Better fit"), 1)
            self.assertEqual(html.count('class="select"'), 2)
            self.assertEqual(html.count(">Selected<"), 1)
            self.assertEqual(html.count("Confidence<"), 1, "confidence sits in the pane only")
            self.assertLess(html.index("Confidence<"), html.index("search &amp;"))
            self.assertGreater(html.index("Confidence<"), html.index("</header>"))
            self.assertEqual(html.count('class="pane'), 2)
            self.assertLess(html.index("Reset comment"), html.index("Target:"))
            self.assertNotIn("request_remapping", html, "verdicts are shown as labels")
            self.assertNotIn(first, html)
            self.assertNotIn(sibling, html)
            self.assertNotIn("Better fit <b>", html)
            self.assertNotIn("<planning>", html)
            self.assertNotIn("Dry run", panel_html(item, PROGRESS, {}, dry_run=False))

    def test_panel_without_fallback_verdict_offers_no_selection_and_names_siblings_by_id(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store, versions = make_store(directory)
            item = item_of(store, versions[0].request_id)
            item.proposal.fallback_rationale = "No defensible alternative."
            item.proposal.concerns = []
            html = panel_html(item, PROGRESS, {}, dry_run=False)
            ordered(
                html,
                'class="tabs"',
                ">Recommended<",
                ">Fallback<",
                'class="pane active selected" data-tab="recommended"',
                "<b>Decision:</b> Approve",
                ">Selected<",
                'class="pane" data-tab="fallback"',
                "<p>No fallback</p>",
                "No defensible alternative.",
                "Siblings",
            )
            self.assertEqual(html.count('class="pill'), 3, "verdict, overlap and confidence")
            self.assertEqual(html.count('class="select"'), 1, "the fallback is unselectable")
            self.assertEqual(html.count("Reset comment"), 1)
            self.assertNotIn("Decision:</b> No", html)
            self.assertNotIn("Remap", html)
            self.assertNotIn("Concerns", html)
            item.proposal.fallback_rationale = None
            self.assertIn("No fallback", panel_html(item, PROGRESS, {}, dry_run=False))
            self.assertIn("Previous comments", panel_html(item, PROGRESS, {}, False, existing="x"))
            self.assertNotIn("Previous comments", html)
            self.assertIn(f"{versions[1].request_id}: not yet submitted", html)
            self.assertNotIn("different verdict", html)

    def test_header_badges_colour_confidence_and_overlap(self) -> None:
        _, request = records()[0]
        version = Version(request, "hash")
        self.assertEqual(overlap_colour(OVERLAP_GOOD), GREEN)
        self.assertEqual(overlap_colour(OVERLAP_GOOD - 1), AMBER)
        self.assertEqual(overlap_colour(OVERLAP_FAIR), AMBER)
        self.assertEqual(overlap_colour(OVERLAP_FAIR - 1), RED)
        cases: list[tuple[Confidence, int, tuple[str, str]]] = [
            ("medium", 55, (AMBER, AMBER)),
            ("low", 20, (RED, RED)),
        ]
        for confidence, overlap, colours in cases:
            advice = proposal(request, confidence=confidence, overlap_percent=overlap)
            html = panel_html(QueueItem(advice, request, version), PROGRESS, {}, dry_run=False)
            badge = f'style="background:{colours[0]}">{confidence.title()} Confidence<'
            self.assertIn(badge, html)
            self.assertGreater(html.index(badge), html.index("</header>"), "not in the header")
            self.assertIn(f'style="background:{colours[1]}">{overlap}% Overlap<', html)

    def test_setup_offers_the_fallback_only_when_there_is_one(self) -> None:
        _, request = records()[0]
        setup = panel_setup(with_fallback(request), "<p>panel</p>", dry_run=True)
        self.assertEqual(
            setup["comments"],
            {"recommended": proposal(request).comment, "fallback": FALLBACK.comment},
        )
        self.assertEqual(
            setup["verdicts"], {"recommended": "approve", "fallback": "request_remapping"}
        )
        self.assertEqual((setup["panel"], setup["dry_run"]), ("<p>panel</p>", True))
        plain_setup = panel_setup(proposal(request), "", dry_run=False)
        self.assertEqual(list(plain_setup["comments"]), ["recommended"])
        self.assertIsNone(plain_setup["verdicts"]["fallback"])


class LoopTests(unittest.TestCase):
    def test_loop_stores_submissions_only_and_stops_when_unverified(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store, versions = make_store(directory)
            requests = [v.request for v in versions]
            ids = ids_of(versions)
            edited = "Approved: the syllabus covers search & <planning>. Also fine."
            stale = copy.deepcopy(requests[3])
            stale.approval_status = "Approved"
            site = FakeSite(
                {
                    ids[0]: Clicked("approve", proposal(requests[0]).comment),
                    ids[1]: Clicked("reject", edited),
                    ids[2]: Skipped(f"{SKIPPED}: busy"),
                },
                live={ids[3]: stale},
            )
            log = review(site, store.root)
            self.assertEqual(len(site.prepared), 3, "The stale request never gets a panel")
            self.assertEqual(
                site.students, [r.identity.student_id for _, r in records()[:4]], "real IDs"
            )
            self.assertEqual(
                [action_of(log[i]) for i in ids], ["approve", "reject", "skip", "skip"]
            )
            self.assertEqual(outcome(log[ids[1]]).comment, edited)
            self.assertEqual(skip_reason(log[ids[2]]), "skipped on the panel: busy")
            self.assertTrue(outcome(log[ids[0]]).verified, "A status other than pending verifies")
            self.assertIn("Approved", skip_reason(log[ids[3]]))
            self.assertEqual(stored_outcomes(store), {i: log[i] for i in ids[:2]})
            self.assertTrue(store.file(versions[0], OUTCOMES).exists())

            # A second session offers only the skipped ones and records a cancel.
            site = FakeSite({ids[2]: Clicked(None, None), ids[3]: Skipped(SKIPPED)})
            second = review(site, store.root)
            self.assertEqual(sorted(site.opened), sorted(ids[2:]))
            self.assertEqual({action_of(e) for e in second.values()}, {"skip"})
            self.assertIn("Cancel", skip_reason(second[ids[2]]))
            self.assertEqual(skip_reason(second[ids[3]]), SKIPPED)
            self.assertEqual(len(stored_outcomes(store)), 2, "skips are not stored")
            panels = [panel for _, panel, _ in site.prepared]
            ordered(panels[0], "width:50%", "1 of 2 this session &middot; 2 of 4 overall")
            ordered(panels[1], "width:100%", "2 of 2 this session &middot; 2 of 4 overall")

    def test_dry_run_and_unverified_submission(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store, versions = make_store(directory, count=2)
            ids = ids_of(versions)
            site = FakeSite({ids[0]: Skipped(SKIPPED), ids[1]: Skipped(SKIPPED)})
            log = review(site, store.root, dry_run=True)
            self.assertEqual([action_of(e) for e in log.values()], ["skip", "skip"])
            self.assertTrue(all(dry for _, _, dry in site.prepared))
            self.assertFalse((store.root / OUTCOMES).exists(), "A dry run stores nothing")
            clicks = {i: Clicked("approve", "c") for i in ids}
            site = FakeSite(clicks, status_after=PENDING_APPROVAL)
            with self.assertRaisesRegex(RuntimeError, "not verified"):
                review(site, store.root)
            (entry,) = stored_outcomes(store).values()
            self.assertEqual((entry.verdict, entry.verified), ("approve", False))
            self.assertIn(PENDING_APPROVAL, entry.note or "")

    def test_leaving_the_page_skips_and_a_vanished_request_is_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store, versions = make_store(directory, count=3)
            ids = ids_of(versions)
            site = FakeSite(
                {ids[0]: Skipped("left the detail page"), ids[2]: Skipped(SKIPPED)},
                live={ids[1]: NotInQueueError("gone")},
            )
            log = review(site, store.root)
            self.assertEqual(
                sorted(site.opened), sorted(ids), "The loop continues past a vanished request"
            )
            self.assertEqual([action_of(log[i]) for i in ids], ["skip", NOT_IN_QUEUE, "skip"])
            self.assertEqual(skip_reason(log[ids[0]]), "left the detail page")
            self.assertEqual(sorted(r for r, _, _ in site.prepared), sorted([ids[0], ids[2]]))
            vanished = outcome(log[ids[1]])
            self.assertEqual((vanished.verdict, vanished.verified), (None, True))
            self.assertEqual(stored_outcomes(store), {ids[1]: vanished})
            # A vanished request is closed: the next session neither opens nor re-stores it.
            site = FakeSite({ids[0]: Skipped(SKIPPED), ids[2]: Skipped(SKIPPED)})
            second = review(site, store.root)
            self.assertEqual(sorted(site.opened), sorted([ids[0], ids[2]]))
            self.assertEqual(sorted(second), sorted([ids[0], ids[2]]))
            self.assertEqual(len(stored_outcomes(store)), 1)
            with self.assertRaisesRegex(RuntimeError, "boom"):
                review(FakeSite({}, live=dict.fromkeys(ids, RuntimeError("boom"))), store.root)

    def test_click_is_on_record_before_verification_and_survives_a_crash(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store, versions = make_store(directory, count=2)
            ids = ids_of(versions)
            comment = proposal(versions[0].request).comment
            clicks = {i: Clicked("approve", comment) for i in ids}
            site = FakeSite(clicks, status_after=RuntimeError("browser went away"))
            with self.assertRaisesRegex(RuntimeError, "browser went away"):
                review(site, store.root)
            ((stored_id, entry),) = stored_outcomes(store).items()
            first, other = site.opened[0], next(i for i in ids if i != site.opened[0])
            self.assertEqual((stored_id, entry.verdict, entry.verified), (first, "approve", False))
            self.assertEqual(entry.comment, comment)
            self.assertEqual(entry.note, "browser went away")
            # The unverified outcome is final: the next session offers only the other request.
            site = FakeSite(clicks)
            log = review(site, store.root)
            self.assertEqual(list(log), [other])
            self.assertEqual(site.opened, [other])
            stored = stored_outcomes(store)
            self.assertEqual(sorted(stored), sorted(ids))
            self.assertTrue(stored[other].verified)
            self.assertIsNone(stored[other].note)

    def test_request_that_left_the_queue_counts_as_verified(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store, (version,) = make_store(directory, count=1)
            write_proposal(store, version, with_fallback(version.request))
            click = Clicked("request_remapping", FALLBACK.comment)
            site = FakeSite({version.request_id: click}, status_after=NOT_IN_QUEUE)
            entry = outcome(next(iter(review(site, store.root).values())))
            self.assertEqual(entry.verdict, "request_remapping")
            self.assertEqual(entry.comment, FALLBACK.comment)
            self.assertTrue(entry.verified)

    def test_bad_comment_is_never_prefilled(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store, (version,) = make_store(directory, count=1)
            write_proposal(store, version, proposal(version.request, comment="Use [code]"))
            site = FakeSite({})
            (entry,) = review(site, store.root).values()
            self.assertEqual(site.prepared, [])
            self.assertIn("[", skip_reason(entry))
            self.assertEqual(stored_outcomes(store), {})

    def test_bad_fallback_comment_is_withheld_but_the_recommendation_is_shown(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store, (version,) = make_store(directory, count=1)
            advice = with_fallback(version.request)
            assert advice.fallback is not None
            advice.fallback.comment = "Remap to CSXXXX"
            write_proposal(store, version, advice)
            site = FakeSite({version.request_id: Skipped("looked")})
            review(site, store.root)
            (setup,) = site.setups
            self.assertEqual(setup["verdicts"], {"recommended": advice.verdict, "fallback": None})
            self.assertEqual(setup["comments"], {"recommended": advice.comment})
            self.assertIn("Fallback hidden: comment contains &#39;XXXX&#39;", setup["panel"])
