from __future__ import annotations

import copy
import sqlite3
import tempfile
import unittest
from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, TypedDict

import yaml
from playwright.sync_api import Dialog, sync_playwright

from edurec_mappings.anonymize import anonymize
from edurec_mappings.browser import (
    BUTTONS,
    CANCEL,
    COMMENTS,
    NotInQueueError,
    Reviewer,
)
from edurec_mappings.cli import DOWNLOAD_TABLES, forget_downloads, main, parse_args
from edurec_mappings.models import (
    NOT_IN_QUEUE,
    PENDING,
    Clicked,
    Confidence,
    Decision,
    Identity,
    Left,
    Outcome,
    Request,
    Reviewed,
    Skipped,
    Tab,
    Verdict,
    hydrate,
    plain,
)
from edurec_mappings.parse import DETAIL, digest
from edurec_mappings.review import (
    OVERLAP_FAIR,
    OVERLAP_GOOD,
    REVIEWED,
    UNVERIFIED,
    VANISHED,
    Item,
    Progress,
    comment_problem,
    comment_source,
    course_names,
    load_queue,
    load_reviewed,
    overlap_colour,
    panel_html,
    review,
    select,
    stale_reason,
)
from edurec_mappings.store import document, dump, save
from tests.test_export import records

if TYPE_CHECKING:
    from typing_extensions import Unpack

STARTED = "2026-09-22T10:00:00+00:00"


class DecisionFields(TypedDict, total=False):
    source_export: str
    source_started_at: str
    course: str
    comment: str
    overlap_percentage: int
    decision_confidence: Confidence
    overlap: list[str]
    missing_from_pu: list[str]
    extra_in_pu: list[str]
    concerns: list[str]
    remap_target: str | None
    remap_analysis: str | None
    fallback_verdict: Verdict | None
    fallback_comment: str | None
    fallback_rationale: str | None


class Fallback(TypedDict):
    fallback_verdict: Verdict
    fallback_comment: str
    fallback_rationale: str


class PanelState(TypedDict):
    viewed: str | None
    selected: str | None
    pill: str
    modified: list[str]
    markers: dict[str, list[list[str | bool]]]
    reason: str
    scrollTop: int
    outlined: dict[str, str]


def button_for(verdict: Verdict) -> str:
    return next(button for button, value in BUTTONS.items() if value == verdict)


PROGRESS = Progress(position=3, total=12, submitted=5, requests=104)
GREEN, AMBER, RED = "#2e7d32", "#ef6c00", "#c62828"
FALLBACK: Fallback = {
    "fallback_verdict": "request remapping",
    "fallback_comment": "Planning is missing. Consider remapping to CS5242.",
    "fallback_rationale": "Overlap is close to the 70% <threshold>.",
}


def ordered(html: str, *needles: str) -> None:
    """Assert every needle occurs in `html`, in the given order."""
    position = -1
    for needle in needles:
        found = html.find(needle, position + 1)
        assert found > position, f"{needle!r} missing or out of order"
        position = found


def decision(
    request: Request, verdict: Verdict = "approve", **overrides: Unpack[DecisionFields]
) -> Decision:
    values = Decision(
        source_export="anon",
        source_started_at=STARTED,
        request_id=request.request_id,
        course="CS 1 (PU) -> CS3243",
        verdict=verdict,
        comment="Approved: the syllabus covers search & <planning>.",
        overlap_percentage=85,
        decision_confidence="high",
        overlap=["Search"],
        missing_from_pu=["Planning"],
        concerns=["Weight of exam < 50%"],
    )
    return replace(values, **overrides)


def make_run(directory: str, count: int = 4) -> tuple[Path, Path, list[Request]]:
    """An export of `count` requests, its anonymized copy and a decision for each."""
    run, anon = Path(directory) / "run", Path(directory) / "run-anonymized"
    requests = [copy.deepcopy(r) for _, r in records()[:count]]
    if count > 1:  # The first two become parts of one many-to-one mapping.
        requests[1].group_id = requests[0].group_id
        requests[0].mapping_type = requests[1].mapping_type = "Many to One"
    data = document()
    data.collection.started_at = STARTED
    data.requests = requests
    save(data, run)
    save(anonymize(data), anon)
    for request in requests:
        path = anon / "decisions" / f"{request.request_id}.yaml"
        path.parent.mkdir(exist_ok=True)
        path.write_text(dump(plain(decision(request))))
    return run, anon / "decisions", requests


class FakeSite:
    """Scripted reviewer: `outcomes` maps request_id to what the human does."""

    def __init__(
        self,
        outcomes: Mapping[str, Outcome | Callable[[], Outcome]],
        live: Mapping[str, Request | Exception] | None = None,
        status_after: str | Exception = "Approved",
    ) -> None:
        self.outcomes = outcomes
        self.live = live or {}
        self.status_after = status_after
        """A string, or an exception to raise from `status`."""
        self.opened: list[str] = []
        self.prepared: list[tuple[str, str, bool]] = []
        self.current = ""

    def open(self, request: Request) -> Request:
        self.opened.append(request.request_id)
        live = self.live.get(request.request_id, request)
        if isinstance(live, Exception):
            raise live
        live = copy.deepcopy(live)
        live.status = live.status or PENDING
        self.current = live.request_id
        return live

    def prepare(self, decision: Decision, panel: str, dry_run: bool) -> dict[Tab, str]:
        self.prepared.append((decision.request_id, panel, dry_run))
        live = self.live.get(self.current)
        assert not isinstance(live, Exception)
        return decision.prefills(live.comments if live else None)

    def await_action(self) -> Outcome:
        outcome = self.outcomes[self.current]
        return outcome() if callable(outcome) else outcome

    def status(self, request: Request) -> str:
        if isinstance(self.status_after, Exception):
            raise self.status_after
        return self.status_after


class ReviewTests(unittest.TestCase):
    def test_hydrate_is_the_inverse_of_plain(self) -> None:
        _, request = records()[0]
        loaded = hydrate(Request, yaml.safe_load(dump(plain(request))))
        self.assertEqual(loaded, request)
        self.assertEqual(hydrate(Decision, plain(decision(request))), decision(request))
        with self.assertRaises(ValueError):
            hydrate(Decision, ["not", "a", "record"])

    def test_request_ids_are_stable(self) -> None:
        identity = Identity(
            student_id="A0000001X",
            academic_career="Undergraduate",
            partner_university="Technical University of Munich",
            study_program="SEP",
            term="2025/2026 Semester 1",
            mapping_number="1",
            sequence="2",
        )
        # Decision files name requests by this digest; a change orphans them all.
        self.assertEqual(digest(plain(identity)), "de5da59c3dd5b0f985ad04da")

    def test_malformed_decisions_are_refused(self) -> None:
        _, request = records()[0]
        valid = plain(decision(request))
        for field, value in [
            ("verdict", "Approve"),
            ("decision_confidence", "High"),
            ("overlap_percentage", "80"),
            ("overlap_percentage", 80.5),
            ("concerns", "one concern"),
            ("fallback_verdit", "reject"),
            # An unquoted YAML timestamp loads as a datetime, which JSON cannot hold.
            ("source_started_at", datetime(2026, 9, 22, tzinfo=timezone.utc)),
        ]:
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                hydrate(Decision, {**valid, field: value})

    def test_malformed_decision_file_is_named(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run, decisions, requests = make_run(directory)
            path = decisions / f"{requests[0].request_id}.yaml"
            path.write_text(dump({**plain(decision(requests[0])), "verdict": "Approve"}))
            with self.assertRaisesRegex(ValueError, path.name):
                load_queue(run, decisions)

    def test_decisions_from_another_export_or_unknown_request_are_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run, decisions, requests = make_run(directory)
            stale = decisions / f"{requests[0].request_id}.yaml"
            stale.write_text(dump(plain(decision(requests[0], source_started_at="older"))))
            orphan = copy.deepcopy(requests[1])
            orphan.request_id = "0" * 24
            (decisions / "000000000000000000000000.yaml").write_text(dump(plain(decision(orphan))))
            queue, rejected = load_queue(run, decisions)
            self.assertEqual(
                [item.request.request_id for item in queue],
                [r.request_id for r in requests[1:]],
            )
            self.assertEqual(sorted(rejected), [orphan.request_id, requests[0].request_id])
            self.assertIn("older", rejected[requests[0].request_id])
            self.assertIn("no requests/", rejected[orphan.request_id])
            self.assertEqual(queue[0].request.identity.student_id, requests[1].identity.student_id)

    def test_mismatched_run_and_decisions_are_refused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run, decisions, _ = make_run(directory)
            inventory = yaml.safe_load((run / "inventory.yaml").read_text())
            inventory["collection"]["started_at"] = "other"
            (run / "inventory.yaml").write_text(dump(inventory))
            with self.assertRaisesRegex(RuntimeError, "different export"):
                load_queue(run, decisions)

    def test_queue_keeps_siblings_together_and_honours_filters(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run, decisions, requests = make_run(directory)
            path = decisions / f"{requests[1].request_id}.yaml"
            path.write_text(dump(plain(decision(requests[1], verdict="reject"))))
            queue, _ = load_queue(run, decisions)
            ids = [item.request.request_id for item in queue]
            sibling_positions = [
                ids.index(requests[0].request_id),
                ids.index(requests[1].request_id),
            ]
            self.assertEqual(abs(sibling_positions[0] - sibling_positions[1]), 1)
            self.assertEqual(set(ids), {r.request_id for r in requests})
            log = [
                Reviewed(
                    request_id=requests[2].request_id,
                    verdict_recommended="approve",
                    action="approve",
                    comment_submitted="x",
                    comment_edited=False,
                    status_before=PENDING,
                    status_after="Approved",
                    reviewed_at=STARTED,
                    dry_run=False,
                ),
                Reviewed(
                    request_id=requests[3].request_id,
                    verdict_recommended="approve",
                    action="skip",
                    comment_submitted=None,
                    comment_edited=False,
                    status_before=PENDING,
                    status_after=None,
                    reviewed_at=STARTED,
                    dry_run=False,
                    reason="reviewer skipped",
                ),
            ]
            remaining = select(queue, log)
            self.assertEqual(
                {item.request.request_id for item in remaining},
                {r.request_id for r in (requests[0], requests[1], requests[3])},
                "Submitted requests are skipped; skipped ones are offered again",
            )
            self.assertEqual(
                [i.request.request_id for i in select(queue, [], verdicts=["reject"])],
                [requests[1].request_id],
            )
            self.assertEqual(
                [i.request.request_id for i in select(queue, [], request_ids=[ids[2]])], [ids[2]]
            )

    def test_entries_from_before_the_export_do_not_block_a_resubmission(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run, decisions, requests = make_run(directory)
            queue, _ = load_queue(run, decisions)
            earlier = "2000-01-01T00:00:00+00:00"
            log = [
                Reviewed(
                    request_id=requests[0].request_id,
                    verdict_recommended="request remapping",
                    action="request remapping",
                    comment_submitted="x",
                    comment_edited=False,
                    status_before=PENDING,
                    status_after="not in approval queue",
                    reviewed_at=earlier,
                    dry_run=False,
                ),
                Reviewed(
                    request_id=requests[1].request_id,
                    verdict_recommended="approve",
                    action="skip",
                    comment_submitted=None,
                    comment_edited=False,
                    status_before=PENDING,
                    status_after=None,
                    reviewed_at=earlier,
                    dry_run=False,
                    reason=VANISHED,
                ),
                Reviewed(
                    request_id=requests[2].request_id,
                    verdict_recommended="approve",
                    action="approve",
                    comment_submitted="x",
                    comment_edited=False,
                    status_before=PENDING,
                    status_after="Approved",
                    reviewed_at=STARTED,
                    dry_run=False,
                ),
            ]
            remaining = {item.request.request_id for item in select(queue, log, started=STARTED)}
            self.assertEqual(
                remaining,
                {r.request_id for r in requests if r is not requests[2]},
                "Entries older than the export are a previous round; only entries since count",
            )
            self.assertEqual(
                {item.request.request_id for item in select(queue, log)},
                {requests[3].request_id},
                "Without an export start, every entry counts as before",
            )

    def test_reviewed_log_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / REVIEWED
            self.assertEqual(load_reviewed(path), [])
            entry = Reviewed(
                request_id="abc",
                verdict_recommended="reject",
                action="skip",
                comment_submitted=None,
                comment_edited=False,
                status_before=PENDING,
                status_after=None,
                reviewed_at=STARTED,
                dry_run=True,
                reason="comment is empty",
            )
            path.write_text(dump([plain(entry)]))
            self.assertEqual(load_reviewed(path), [entry])

    def test_freshness_comparison(self) -> None:
        _, exported = records()[0]
        live = copy.deepcopy(exported)
        live.status = PENDING
        self.assertIsNone(stale_reason(exported, live))
        changed = copy.deepcopy(live)
        changed.status = "Approved"
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

    def test_verdict_button_mapping(self) -> None:
        self.assertEqual(len(BUTTONS), 4)
        for button, verdict in BUTTONS.items():
            self.assertEqual(button_for(verdict), button)
        self.assertEqual(BUTTONS["N_SR_EXT_STD_DW_APPROVE_PB"], "approve")
        self.assertEqual(BUTTONS["N_SR_EXT_STD_DW_REQUEST_BTN"], "request remapping")
        self.assertNotIn(CANCEL, BUTTONS)

    def test_panel_html_shows_the_decision_in_order_and_escapes_content(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run, decisions, requests = make_run(directory)
            queue, _ = load_queue(run, decisions)
            item = next(i for i in queue if i.request.request_id == requests[0].request_id)
            item.decision.remap_target = "CS5242"
            item.decision.remap_analysis = "Better fit <b>"
            item.decision.extra_in_pu = ["Robotics"]
            for key, value in FALLBACK.items():
                setattr(item.decision, key, value)
            sibling = requests[1].request_id
            log = [
                Reviewed(
                    request_id=sibling,
                    verdict_recommended="approve",
                    action="reject",
                    comment_submitted="x",
                    comment_edited=True,
                    status_before=PENDING,
                    status_after="Rejected",
                    reviewed_at=STARTED,
                    dry_run=False,
                )
            ]
            courses = {sibling: "CS 2 (PU) -> CS3243"}
            html = panel_html(
                item, PROGRESS, log, dry_run=True, existing="kept <below>", courses=courses
            )
            ordered(
                html,
                "<style>",
                f'class="pill active" data-tab="recommended" style="background:{GREEN}"',
                ">Approve<",
                f'class="pill" data-tab="fallback" style="background:{AMBER}"',
                ">Request Remapping<",
                f'style="background:{GREEN}">85% Overlap<',
                "CS 1 (PU) -&gt; CS3243",
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
                "Missing from PU",
                "Planning",
                "Extra in PU",
                "Robotics",
                "Siblings",
                "CS 2 (PU) -&gt; CS3243",
                "reject",
                "different action",  # warning badge on the sibling entry only
                'id="reason"',
                'id="skip"',
            )
            self.assertEqual(html.count("pill active"), 1, "one verdict pill, in the header only")
            self.assertEqual(html.count('class="pill" data-tab'), 1)
            self.assertEqual(html.count("different action"), 1)
            self.assertEqual(html.count("Target:"), 1)
            self.assertEqual(html.count("Better fit"), 1)
            self.assertEqual(html.count('class="select"'), 2)
            self.assertEqual(html.count(">Selected<"), 1)
            self.assertEqual(html.count("Confidence<"), 1, "confidence sits in the pane only")
            self.assertLess(html.index("Confidence<"), html.index("search &amp;"))
            self.assertGreater(html.index("Confidence<"), html.index("</header>"))
            self.assertEqual(html.count('class="pane'), 2)
            self.assertLess(html.index("Reset comment"), html.index("Target:"))
            self.assertNotIn("remapping<", html, "verdicts are shown in title case")
            self.assertNotIn("Details", html)
            self.assertNotIn("decided", html)
            self.assertNotIn(requests[0].request_id, html)
            self.assertNotIn(sibling, html)
            self.assertNotIn('class="selected"', html)
            self.assertNotIn("Better fit <b>", html)
            self.assertNotIn("<planning>", html)
            self.assertNotIn("fresh", html)
            self.assertNotIn("Dry run", panel_html(item, PROGRESS, [], dry_run=False))

    def test_panel_without_fallback_verdict_offers_no_selection_and_names_siblings_by_id(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run, decisions, requests = make_run(directory)
            queue, _ = load_queue(run, decisions)
            item = next(i for i in queue if i.request.request_id == requests[0].request_id)
            item.decision.fallback_rationale = "No defensible alternative."
            item.decision.concerns = []
            html = panel_html(item, PROGRESS, [], dry_run=False)
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
            item.decision.fallback_rationale = None
            self.assertIn("No fallback", panel_html(item, PROGRESS, [], dry_run=False))
            self.assertIn("Previous comments", panel_html(item, PROGRESS, [], False, existing="x"))
            self.assertNotIn("Previous comments", html)
            self.assertIn(f"{requests[1].request_id}: not yet submitted", html)
            self.assertNotIn("different action", html)

    def test_header_badges_colour_confidence_and_overlap(self) -> None:
        _, request = records()[0]
        self.assertEqual((OVERLAP_GOOD, OVERLAP_FAIR), (70, 40))
        self.assertEqual(overlap_colour(OVERLAP_GOOD), GREEN)
        self.assertEqual(overlap_colour(OVERLAP_GOOD - 1), AMBER)
        self.assertEqual(overlap_colour(OVERLAP_FAIR), AMBER)
        self.assertEqual(overlap_colour(OVERLAP_FAIR - 1), RED)
        cases: list[tuple[Confidence, int, tuple[str, str]]] = [
            ("medium", 55, (AMBER, AMBER)),
            ("low", 20, (RED, RED)),
        ]
        for confidence, overlap, colours in cases:
            advice = decision(request, decision_confidence=confidence, overlap_percentage=overlap)
            html = panel_html(Item(advice, request), PROGRESS, [], dry_run=False)
            badge = f'style="background:{colours[0]}">{confidence.title()} Confidence<'
            self.assertIn(badge, html)
            self.assertGreater(html.index(badge), html.index("</header>"), "not in the header")
            self.assertIn(f'style="background:{colours[1]}">{overlap}% Overlap<', html)

    def test_course_names_come_from_the_decision_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _, decisions, requests = make_run(directory, count=2)
            names = course_names(decisions)
            self.assertEqual(names, {r.request_id: "CS 1 (PU) -> CS3243" for r in requests})

    def test_comment_source_follows_the_selected_tab(self) -> None:
        _, request = records()[0]
        advice = decision(request, **FALLBACK)
        prefills = advice.prefills("older")
        self.assertEqual(list(prefills), ["recommended", "fallback"])
        self.assertEqual(prefills["fallback"], FALLBACK["fallback_comment"] + "\n\nolder")
        self.assertEqual(
            comment_source(prefills["recommended"], "recommended", prefills), "recommended"
        )
        self.assertEqual(
            comment_source(prefills["fallback"] + "\n", "fallback", prefills), "fallback"
        )
        self.assertEqual(comment_source(prefills["fallback"], "recommended", prefills), "edited")
        self.assertEqual(comment_source("typed", "fallback", prefills), "edited")
        # Without the hook's report the text alone decides.
        self.assertEqual(comment_source(prefills["fallback"], None, prefills), "fallback")
        self.assertEqual(comment_source(None, None, prefills), "edited")
        self.assertEqual(list(decision(request).prefills(None)), ["recommended"])

    def test_loop_logs_every_outcome_and_stops_when_unverified(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run, decisions, requests = make_run(directory)
            approve, reject = button_for("approve"), button_for("reject")
            ids = [r.request_id for r in requests]
            edited = "Approved: the syllabus covers search & <planning>. Also fine."
            stale = copy.deepcopy(requests[3])
            stale.status = "Approved"
            site = FakeSite(
                {
                    ids[0]: Clicked(approve, decision(requests[0]).comment),
                    ids[1]: Clicked(reject, edited),
                    ids[2]: Skipped("busy"),
                },
                live={ids[3]: stale},
            )
            log = {e.request_id: e for e in review(site, run, decisions)}
            self.assertEqual(len(site.prepared), 3, "The stale request never gets a panel")
            self.assertEqual([log[i].action for i in ids], ["approve", "reject", "skip", "skip"])
            self.assertEqual(log[ids[0]].comment_edited, False)
            self.assertEqual(log[ids[0]].comment_source, "recommended")
            self.assertEqual(log[ids[1]].comment_edited, True)
            self.assertEqual(log[ids[1]].comment_source, "edited")
            self.assertEqual(log[ids[1]].comment_submitted, edited)
            self.assertIsNone(log[ids[2]].comment_source)
            self.assertEqual(log[ids[2]].reason, "skipped by the reviewer: busy")
            self.assertEqual(
                (log[ids[0]].status_before, log[ids[0]].status_after), (PENDING, "Approved")
            )
            self.assertIn("Approved", log[ids[3]].reason or "")
            self.assertEqual({e.request_id: e for e in load_reviewed(decisions / REVIEWED)}, log)

            # A second session offers only the skipped ones and records a cancel.
            site = FakeSite({ids[2]: Clicked("#ICList", None), ids[3]: Skipped()})
            second = {e.request_id: e for e in review(site, run, decisions)}
            self.assertEqual(sorted(site.opened), sorted(ids[2:]))
            self.assertEqual({e.action for e in second.values()}, {"skip"})
            self.assertIn("Cancel", second[ids[2]].reason or "")
            self.assertEqual(second[ids[3]].reason, "skipped by the reviewer")
            self.assertEqual(len(load_reviewed(decisions / REVIEWED)), 6)
            panels = [panel for _, panel, _ in site.prepared]
            ordered(panels[0], "width:50%", "1 of 2 this session &middot; 2 of 4 overall")
            ordered(panels[1], "width:100%", "2 of 2 this session &middot; 2 of 4 overall")

    def test_dry_run_and_unverified_submission(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run, decisions, requests = make_run(directory, count=2)
            ids = [r.request_id for r in requests]
            site = FakeSite({ids[0]: Skipped(), ids[1]: Skipped()})
            log = review(site, run, decisions, dry_run=True)
            self.assertTrue(all(e.dry_run and e.action == "skip" for e in log))
            self.assertTrue(all(dry for _, _, dry in site.prepared))
            outcomes = {i: Clicked(button_for("approve"), "c") for i in ids}
            site = FakeSite(outcomes, status_after=PENDING)
            with self.assertRaisesRegex(RuntimeError, "not verified"):
                review(site, run, decisions)
            entries = load_reviewed(decisions / REVIEWED)
            self.assertEqual(entries[-1].action, "approve")
            self.assertIn("not verified", entries[-1].reason or "")
            self.assertEqual(len(entries), 3)

    def test_leaving_the_page_and_a_vanished_request_are_logged_as_skips(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run, decisions, requests = make_run(directory, count=3)
            ids = [r.request_id for r in requests]
            site = FakeSite(
                {ids[0]: Left(), ids[2]: Skipped()},
                live={ids[1]: NotInQueueError("gone")},
            )
            log = {e.request_id: e for e in review(site, run, decisions)}
            self.assertEqual(
                sorted(site.opened), sorted(ids), "The loop continues past a vanished request"
            )
            self.assertEqual([log[i].action for i in ids], ["skip"] * 3)
            self.assertEqual(log[ids[0]].reason, "reviewer left the page")
            self.assertEqual(log[ids[1]].reason, "no longer in the approval queue")
            self.assertIsNone(log[ids[1]].status_before)
            self.assertEqual(sorted(r for r, _, _ in site.prepared), sorted([ids[0], ids[2]]))
            self.assertEqual(log[ids[1]].reason, VANISHED)
            # A vanished request is final: the next session neither opens nor re-logs it.
            site = FakeSite({ids[0]: Skipped(), ids[2]: Skipped()})
            second = review(site, run, decisions)
            self.assertEqual(sorted(site.opened), sorted([ids[0], ids[2]]))
            self.assertEqual(sorted(e.request_id for e in second), sorted([ids[0], ids[2]]))
            self.assertEqual(len(load_reviewed(decisions / REVIEWED)), 5)
            with self.assertRaisesRegex(RuntimeError, "boom"):
                review(FakeSite({}, live=dict.fromkeys(ids, RuntimeError("boom"))), run, decisions)

    def test_click_is_on_record_before_verification_and_survives_a_crash(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run, decisions, requests = make_run(directory, count=2)
            ids = [r.request_id for r in requests]
            comment = decision(requests[0]).comment
            outcomes = {i: Clicked(button_for("approve"), comment) for i in ids}
            site = FakeSite(outcomes, status_after=RuntimeError("browser went away"))
            with self.assertRaisesRegex(RuntimeError, "browser went away"):
                review(site, run, decisions)
            (entry,) = load_reviewed(decisions / REVIEWED)
            first, other = site.opened[0], next(i for i in ids if i != site.opened[0])
            self.assertEqual((entry.request_id, entry.action), (first, "approve"))
            self.assertEqual(entry.comment_submitted, comment)
            self.assertEqual(entry.reason, f"{UNVERIFIED}: browser went away")
            self.assertIsNone(entry.status_after)
            # The provisional entry is final: the next session offers only the other request.
            site = FakeSite(outcomes)
            log = review(site, run, decisions)
            self.assertEqual([e.request_id for e in log], [other])
            self.assertEqual(site.opened, [other])
            entries = load_reviewed(decisions / REVIEWED)
            self.assertEqual([e.request_id for e in entries], [first, other])
            self.assertEqual(entries[1].reason, None)
            self.assertEqual(entries[1].status_after, "Approved")

    def test_request_that_left_the_queue_counts_as_verified(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run, decisions, requests = make_run(directory, count=1)
            remap = button_for("request remapping")
            advice = decision(requests[0], **FALLBACK)
            (decisions / f"{requests[0].request_id}.yaml").write_text(dump(plain(advice)))
            outcome = Clicked(remap, advice.prefills(None)["fallback"], "fallback")
            site = FakeSite({requests[0].request_id: outcome}, status_after=NOT_IN_QUEUE)
            (entry,) = review(site, run, decisions)
            self.assertEqual(entry.action, "request remapping")
            self.assertEqual(entry.comment_source, "fallback")
            self.assertFalse(entry.comment_edited)
            self.assertEqual(entry.status_after, NOT_IN_QUEUE)
            self.assertIsNone(entry.reason)

    def test_bad_comment_is_never_prefilled(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run, decisions, requests = make_run(directory, count=1)
            path = decisions / f"{requests[0].request_id}.yaml"
            path.write_text(dump(plain(decision(requests[0], comment="Use [code]"))))
            site = FakeSite({})
            log = review(site, run, decisions)
            self.assertEqual(site.prepared, [])
            self.assertEqual(log[0].action, "skip")
            self.assertIn("[", log[0].reason or "")

    def test_injected_hook_reports_skip_confirm_click_and_cancel(self) -> None:
        buttons = "".join(
            f'<input type="button" id="{i}" value="{v.title()}" '
            'onclick="submitAction_win0(document.win0,this.id,event);">'
            for i, v in [*BUTTONS.items(), (CANCEL, "Cancel")]
        )
        fields = "".join(
            f'<span id="{i}">{v}</span>'
            for i, v in [
                ("N_EXSP_WKST_HDR_EMPLID", "A1"),
                ("ACAD_CAR_TBL_DESCR", "UGRD"),
                ("EXT_ORG_TBL_N_FORMAL_DESCR", "PU"),
                ("N_EXT_PRG_VW_DESCRFORMAL", "SEP"),
                ("TERM_TBL_DESCR", "2025/2026 Semester 2"),
                (DETAIL, "1"),
                ("N_EXSP_MOD_DT_TRNSFR_EQVLNCY_SEQ$0", "1"),
                ("N_EXSP_MOD_DT_N_MOD_APPR_STATUS$0", PENDING),
            ]
        )
        page_html = f"""<html><body><form name="win0" id="N_EXSP_MOD_APPR">
            <input id="ICStateNum" value="1">{fields}
            <textarea id="{COMMENTS}">prior</textarea>{buttons}
            </form><style>button, div {{ color: red !important; }}</style><script>
            window.changes = 0;
            document.addEventListener('change', () => window.changes++);
            window.isLoaderInProcess = () => false;
            window.submitAction_win0 = (_, action) => {{
                if (action === '{CANCEL}') action = '#ICList';
                const box = document.getElementById('{COMMENTS}');
                const body = 'ICAction=' + encodeURIComponent(action) +
                    '&' + encodeURIComponent('{COMMENTS}') + '=' + encodeURIComponent(box.value);
                fetch('/post', {{method: 'POST', body,
                    headers: {{'content-type': 'application/x-www-form-urlencoded'}}}})
                    .then(() => {{ document.getElementById('ICStateNum').value += 'x'; }});
            }};
            </script></body></html>"""
        _, request = records()[0]
        # Enough overlap lines to make the panel body scroll in a 720px-high viewport.
        advice = decision(request, overlap=[f"Topic {n}" for n in range(80)], **FALLBACK)
        panel = panel_html(Item(advice, request), PROGRESS, [], dry_run=False)
        approve, reject = button_for("approve"), button_for("reject")
        remap = button_for("request remapping")
        recommended, fallback = advice.prefills("prior").values()
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_context().new_page()
            page.route(
                "**/*", lambda route: route.fulfill(body=page_html, content_type="text/html")
            )
            page.goto("https://local.test/detail")
            site = Reviewer(page.context)
            box = page.locator(f'[id="{COMMENTS}"]')
            messages, posts, answers = [], [], []  # type: list[str], list[object], list[bool]

            def answer(dialog: Dialog) -> None:
                messages.append(dialog.message)
                if answers and answers.pop(0):
                    dialog.accept()
                else:
                    dialog.dismiss()

            page.on("dialog", answer)
            page.on("request", lambda r: posts.append(r) if r.method == "POST" else None)

            def later(script: str, delay: int = 100) -> None:
                page.evaluate(f"setTimeout(() => {{ {script} }}, {delay})")

            def press(button_id: str) -> str:
                return f"document.getElementById('{button_id}').click();"

            def in_panel(selector: str) -> str:
                host = "document.getElementById('edurec-review-panel')"
                return f"{host}.shadowRoot.querySelector('{selector}')"

            def panel_state() -> PanelState:
                root = "document.getElementById('edurec-review-panel').shadowRoot"
                state: PanelState = page.evaluate(f"""() => ({{
                    viewed: {in_panel(".tab.active")}?.dataset.tab,
                    selected: {in_panel(".pane.selected")}?.dataset.tab,
                    pill: {in_panel(".pill.active")}.textContent,
                    modified: [...{root}.querySelectorAll('.pane')].filter(pane =>
                        [...pane.querySelectorAll('.modified')].some(el =>
                            !el.hidden && getComputedStyle(el).display !== 'none')
                    ).map(pane => pane.dataset.tab),
                    markers: Object.fromEntries([...{root}.querySelectorAll('.pane')].map(
                        pane => [pane.dataset.tab, [...pane.querySelectorAll('.select')].map(
                            el => [el.textContent, el.disabled])])),
                    reason: {in_panel("#reason")}.value,
                    scrollTop: {in_panel("#body")}.scrollTop,
                    outlined: Object.fromEntries('{",".join(BUTTONS)}'.split(',').map(
                        id => [id, document.getElementById(id).style.outline])),
                }})""")
                return state

            def outlined() -> set[str]:
                state = panel_state()["outlined"]
                return {id for id, outline in state.items() if outline}

            def set_box(value: str) -> None:
                page.evaluate(
                    f"""() => {{ const box = document.getElementById('{COMMENTS}');
                    box.value = {value!r}; box.dispatchEvent(new Event('input')); }}"""
                )

            entered = site.prepare(advice, panel, dry_run=False)
            self.assertEqual(entered, {"recommended": recommended, "fallback": fallback})
            self.assertEqual(recommended, advice.comment + "\n\nprior", "new comment on top")
            self.assertEqual(box.input_value(), recommended)
            self.assertEqual(page.evaluate("window.changes"), 1, "change event dispatched")
            self.assertEqual(page.locator("#edurec-review-panel").count(), 1)
            self.assertTrue(
                page.evaluate("!!document.getElementById('edurec-review-panel').shadowRoot")
            )
            self.assertEqual(
                page.evaluate(f"getComputedStyle({in_panel('#skip')}).color"),
                "rgb(0, 0, 0)",
                "page CSS does not leak into the panel",
            )
            self.assertEqual(outlined(), {approve})
            self.assertIn("rgb(46, 125, 50)", panel_state()["outlined"][approve])
            self.assertEqual(
                (panel_state()["viewed"], panel_state()["selected"]), ("recommended", "recommended")
            )
            self.assertEqual(panel_state()["pill"], "Approve")
            self.assertEqual(panel_state()["modified"], [])
            markers = {"recommended": [["Selected", True]], "fallback": [["Select", False]]}
            self.assertEqual(panel_state()["markers"], markers)

            # The modified indicator follows the box, in the selected pane only; Reset
            # from either pane restores the selected prefill.
            set_box("typed")
            self.assertEqual(panel_state()["modified"], ["recommended"])
            page.evaluate(f"{in_panel('.pane[data-tab=fallback] .reset')}.click()")
            self.assertEqual(box.input_value(), recommended)
            self.assertEqual(panel_state()["modified"], [])

            # Clicking a tab only previews it: box, outline, pill and markers stay put.
            page.evaluate(f"{in_panel('.tab[data-tab=fallback]')}.click()")
            self.assertEqual(messages, [])
            self.assertEqual(
                (panel_state()["viewed"], panel_state()["selected"]), ("fallback", "recommended")
            )
            self.assertEqual(box.input_value(), recommended)
            self.assertEqual(panel_state()["pill"], "Approve")
            self.assertEqual(outlined(), {approve})
            self.assertEqual(panel_state()["markers"], markers)

            # Select swaps the comment, re-targets the outline, pill and markers.
            page.evaluate(f"{in_panel('.pane[data-tab=fallback] .select')}.click()")
            self.assertEqual(messages, [])
            self.assertEqual(box.input_value(), fallback)
            self.assertEqual(panel_state()["selected"], "fallback")
            self.assertEqual(panel_state()["pill"], "Request Remapping", "pill follows selection")
            self.assertEqual(outlined(), {remap})
            self.assertIn("rgb(239, 108, 0)", panel_state()["outlined"][remap])
            self.assertEqual(
                panel_state()["markers"],
                {"recommended": [["Select", False]], "fallback": [["Selected", True]]},
            )
            self.assertEqual(panel_state()["modified"], [])

            # Selecting over an edited box asks first; a dismissed confirm changes nothing.
            set_box("typed")
            self.assertEqual(panel_state()["modified"], ["fallback"])
            page.evaluate(f"{in_panel('.tab[data-tab=recommended]')}.click()")
            page.evaluate(f"{in_panel('.pane[data-tab=recommended] .select')}.click()")
            self.assertEqual(
                messages,
                [
                    "The comment box differs from the Fallback comment. "
                    "Replace it with the Recommended comment?"
                ],
            )
            self.assertEqual(box.input_value(), "typed")
            self.assertEqual(panel_state()["selected"], "fallback")
            self.assertEqual(outlined(), {remap})
            answers.append(True)
            page.evaluate(f"{in_panel('.pane[data-tab=recommended] .select')}.click()")
            self.assertEqual(box.input_value(), recommended)
            self.assertEqual(panel_state()["selected"], "recommended")
            self.assertEqual(panel_state()["pill"], "Approve")
            self.assertEqual(outlined(), {approve})
            self.assertEqual(panel_state()["markers"], markers)
            messages.clear()

            # Selected tab, viewed tab, scroll position and skip reason survive a re-render.
            rerender = f"""
                const form = document.getElementById('N_EXSP_MOD_APPR');
                const value = document.getElementById('{COMMENTS}').value;
                const state = document.getElementById('ICStateNum').value;
                document.getElementById('edurec-review-panel').remove();
                form.innerHTML = form.innerHTML;
                document.getElementById('{COMMENTS}').value = value;
                document.getElementById('ICStateNum').value = state + 'r';"""
            page.evaluate(f"{in_panel('.pane[data-tab=fallback] .select')}.click()")
            page.evaluate(f"{in_panel('.tab[data-tab=recommended]')}.click()")
            page.evaluate(f"{in_panel('#body')}.scrollTop = 150")
            page.evaluate(f"{in_panel('#body')}.dispatchEvent(new Event('scroll'))")
            page.evaluate(
                f"""() => {{ const reason = {in_panel("#reason")};
                reason.value = 'later'; reason.dispatchEvent(new Event('input')); }}"""
            )
            self.assertEqual(panel_state()["scrollTop"], 150, "the body scrolls")
            later(f"document.getElementById('{COMMENTS}').value = 'kept'; " + rerender)
            later(f"window.restored = ({in_panel('#skip')} ? 1 : 0)", 300)
            later(f"{in_panel('#skip')}.click()", 500)
            self.assertEqual(site.await_action(), Skipped("later"))
            self.assertEqual(page.evaluate("window.restored"), 1)
            state = panel_state()
            self.assertEqual(
                (state["selected"], state["viewed"], state["reason"], state["scrollTop"]),
                ("fallback", "recommended", "later", 150),
            )
            self.assertEqual(state["pill"], "Request Remapping")
            self.assertEqual(
                state["markers"],
                {"recommended": [["Select", False]], "fallback": [["Selected", True]]},
                "the buttons follow the restored selection",
            )
            self.assertEqual(outlined(), {remap})
            self.assertEqual(box.input_value(), "prior", "Skip restores the prior comment")

            # A fresh prepare starts from the recommended tab with an empty reason.
            site.prepare(advice, panel, dry_run=True)
            state = panel_state()
            self.assertEqual(
                (state["selected"], state["viewed"], state["reason"], state["scrollTop"]),
                ("recommended", "recommended", "", 0),
            )
            self.assertTrue(all(page.locator(f"#{b}").is_disabled() for b in BUTTONS))
            self.assertFalse(page.locator(f"#{CANCEL}").is_disabled())
            later(f"{in_panel('#skip')}.click()")
            self.assertEqual(site.await_action(), Skipped())

            # A re-render also restores the dry-run state; the box keeps the reviewer's text.
            site.prepare(advice, panel, dry_run=True)
            later(f"document.getElementById('{COMMENTS}').value = 'kept'; " + rerender)
            later(
                f"""window.restored = {{
                    panels: document.querySelectorAll('#edurec-review-panel').length,
                    disabled: [...'{",".join(BUTTONS)}'.split(',')].map(
                        id => document.getElementById(id).disabled),
                    value: document.getElementById('{COMMENTS}').value}};""",
                300,
            )
            later(f"{in_panel('#skip')}.click()", 500)
            self.assertEqual(site.await_action(), Skipped())
            self.assertEqual(
                page.evaluate("window.restored"),
                {"panels": 1, "disabled": [True] * len(BUTTONS), "value": "kept"},
            )

            site.prepare(advice, panel, dry_run=False)
            later(f"document.getElementById('{DETAIL}').remove(); " + rerender)
            self.assertEqual(site.await_action(), Left())
            page.evaluate(f"document.body.insertAdjacentHTML('beforeend', '{fields}')")

            site.prepare(advice, panel, dry_run=True)
            # A stale page may have lost `disabled` but keep the hook: the click is still blocked.
            later(f"document.getElementById('{approve}').disabled = false; " + press(approve))
            later(f"{in_panel('#skip')}.click()", 300)
            self.assertEqual(site.await_action(), Skipped())
            self.assertEqual(messages, ["Dry run: nothing is submitted"])
            self.assertEqual(posts, [])

            messages.clear()
            site.prepare(advice, panel, dry_run=False)
            later(press(reject))
            later(f"{in_panel('#skip')}.click()", 300)
            self.assertEqual(site.await_action(), Skipped(), "A dismissed confirm aborts the click")
            self.assertEqual(messages, ["Recommended: Approve. Submit Reject anyway?"])
            self.assertEqual(posts, [])

            # The fallback's button offers to switch comments: no keeps the box and blocks.
            messages.clear()
            site.prepare(advice, panel, dry_run=False)
            later(press(remap))
            later(f"{in_panel('#skip')}.click()", 300)
            self.assertEqual(site.await_action(), Skipped())
            self.assertEqual(
                messages, ["This matches the fallback. Switch to the fallback comment and submit?"]
            )
            self.assertEqual(posts, [])

            # Yes selects the fallback, swaps in its comment, and the click goes through.
            messages.clear()
            set_box("prior")
            site.prepare(advice, panel, dry_run=False)
            answers.append(True)
            later(press(remap))
            self.assertEqual(site.await_action(), Clicked(remap, fallback, "fallback"))
            self.assertEqual(len(posts), 1)
            self.assertEqual(box.input_value(), fallback)
            self.assertEqual(panel_state()["selected"], "fallback")
            self.assertEqual(panel_state()["pill"], "Request Remapping")

            # With the fallback selected its own button needs no confirm; the other one does.
            messages.clear()
            site.prepare(advice, panel, dry_run=False)
            page.evaluate(f"{in_panel('.pane[data-tab=fallback] .select')}.click()")
            later(press(approve))
            later(f"{in_panel('#skip')}.click()", 300)
            self.assertEqual(site.await_action(), Skipped())
            self.assertEqual(
                messages, ["Fallback selected: Request Remapping. Submit Approve anyway?"]
            )

            # Merely viewing the fallback changes nothing: the selected tab is what counts.
            messages.clear()
            set_box("prior")
            site.prepare(advice, panel, dry_run=False)
            page.evaluate(f"{in_panel('.tab[data-tab=fallback]')}.click()")
            later(press(approve))
            self.assertEqual(site.await_action(), Clicked(approve, recommended, "recommended"))
            self.assertEqual(messages, [])

            site.prepare(advice, panel, dry_run=False)
            later(f"document.getElementById('{COMMENTS}').value = 'edited'; " + press(approve))
            self.assertEqual(site.await_action(), Clicked(approve, "edited", "recommended"))

            site.prepare(advice, panel, dry_run=False)
            later(press(CANCEL))
            # The mock page never navigates, so the box still holds the edited text.
            self.assertEqual(
                site.await_action(),
                Clicked("#ICList", advice.comment + "\n\nedited", "recommended"),
            )
            browser.close()

    def test_review_arguments(self) -> None:
        args = parse_args(["review", "--run", "out/x", "--request-id", "a", "--verdict", "reject"])
        self.assertEqual(args.decisions, str(Path("out/x-anonymized/decisions")))
        self.assertEqual((args.request_ids, args.verdicts), (["a"], ["reject"]))
        self.assertFalse(args.dry_run)
        self.assertEqual(args.timeout_ms, 60000)
        with self.assertRaises(SystemExit):
            parse_args(["review", "--verdict", "maybe"])
        with self.assertRaises(SystemExit):
            main(["review", "--verdict", "maybe"])


if __name__ == "__main__":
    unittest.main()


class PrefillTests(unittest.TestCase):
    def test_decision_comment_goes_on_top_of_existing_text(self) -> None:
        _, request = records()[0]
        advice = decision(request)
        self.assertEqual(advice.prefills(None)["recommended"], advice.comment)
        self.assertEqual(advice.prefills("  ")["recommended"], advice.comment)
        self.assertEqual(
            advice.prefills("older note\n")["recommended"], advice.comment + "\n\nolder note"
        )

    def test_panel_shows_the_existing_comment(self) -> None:
        _, request = records()[0]
        item = Item(decision(request), request)
        html = panel_html(item, Progress(1, 1, 0, 1), [], False, existing="kept <below>")
        self.assertIn("Previous comments", html)
        self.assertIn("kept &lt;below&gt;", html)
        self.assertNotIn("Previous comments", panel_html(item, Progress(1, 1, 0, 1), [], False))


def test_forget_downloads_clears_history_tables(tmp_path: Path) -> None:
    history = tmp_path / "Default" / "History"
    history.parent.mkdir()
    with sqlite3.connect(history) as connection:
        for table in DOWNLOAD_TABLES:
            connection.execute(f"CREATE TABLE {table} (id INTEGER)")
            connection.execute(f"INSERT INTO {table} VALUES (1)")
    forget_downloads(tmp_path)
    with sqlite3.connect(history) as connection:
        assert all(
            connection.execute(f"SELECT count(*) FROM {table}").fetchone() == (0,)
            for table in DOWNLOAD_TABLES
        )
    forget_downloads(tmp_path / "missing")  # No profile yet: nothing to do.
