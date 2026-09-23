from __future__ import annotations

import copy
import tempfile
import unittest
from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import datetime, timezone
from html import escape
from pathlib import Path
from typing import TYPE_CHECKING, TypedDict

import yaml
from playwright.sync_api import Dialog, sync_playwright

from edurec_mappings.browser import (
    BUTTONS,
    CANCEL,
    COMMENTS,
    SKIPPED,
    NotInQueueError,
    Reviewer,
)
from edurec_mappings.models import (
    NOT_IN_QUEUE,
    PENDING,
    Clicked,
    Confidence,
    Outcome,
    Proposal,
    Reaction,
    Request,
    Skipped,
    Verdict,
    hydrate,
    plain,
)
from edurec_mappings.parse import DETAIL
from edurec_mappings.review import (
    OVERLAP_FAIR,
    OVERLAP_GOOD,
    UNVERIFIED,
    Item,
    Progress,
    action_of,
    comment_problem,
    load_queue,
    overlap_colour,
    panel_html,
    review,
    select,
    stale_reason,
)
from edurec_mappings.store import OUTCOMES, PROPOSALS, Store, Version, dump, load
from tests.test_export import records

if TYPE_CHECKING:
    from typing_extensions import Unpack

STARTED = "2026-09-22T10:00:00+00:00"


class ProposalFields(TypedDict, total=False):
    comment: str
    overlap_percentage: int
    confidence: Confidence
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


def proposal(
    request: Request, verdict: Verdict = "approve", **overrides: Unpack[ProposalFields]
) -> Proposal:
    values = Proposal(
        verdict=verdict,
        comment="Approved: the syllabus covers search & <planning>.",
        overlap_percentage=85,
        confidence="high",
        overlap=["Search"],
        missing_from_pu=["Planning"],
        concerns=["Weight of exam < 50%"],
    )
    return replace(values, **overrides)


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
    path = store.root / version.path(PROPOSALS)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dump(advice if isinstance(advice, dict) else plain(advice)))
    return path


def stored_outcomes(store: Store) -> dict[str, Outcome]:
    return {
        path.parent.name: hydrate(Outcome, load(path))
        for path in (store.root / OUTCOMES).glob("*/*.yaml")
    }


def queue_of(store: Store) -> list[Item]:
    return load_queue(store, store.latest())


def ids_of(versions: list[Version]) -> list[str]:
    return [version.request_id for version in versions]


class FakeSite:
    """Scripted reviewer: `reactions` maps request_id to what the human does."""

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
        self.current = ""

    def open(self, request: Request) -> Request:
        self.opened.append(request.request_id)
        self.students.append(request.identity.student_id)
        live = self.live.get(request.request_id, request)
        if isinstance(live, Exception):
            raise live
        live = copy.deepcopy(live)
        live.status = live.status or PENDING
        self.current = live.request_id
        return live

    def prepare(self, proposal: Proposal, panel: str, dry_run: bool) -> None:
        self.prepared.append((self.current, panel, dry_run))

    def await_action(self) -> Reaction:
        reaction = self.reactions[self.current]
        return reaction() if callable(reaction) else reaction

    def status(self, request: Request) -> str:
        if isinstance(self.status_after, Exception):
            raise self.status_after
        return self.status_after


class ReviewTests(unittest.TestCase):
    def test_hydrate_is_the_inverse_of_plain(self) -> None:
        _, request = records()[0]
        loaded = hydrate(Request, yaml.safe_load(dump(plain(request))))
        self.assertEqual(loaded, request)
        self.assertEqual(hydrate(Proposal, plain(proposal(request))), proposal(request))
        with self.assertRaises(ValueError):
            hydrate(Proposal, ["not", "a", "record"])

    def test_malformed_proposals_are_refused(self) -> None:
        _, request = records()[0]
        valid = plain(proposal(request))
        for field, value in [
            ("verdict", "Approve"),
            ("confidence", "High"),
            ("overlap_percentage", "80"),
            ("overlap_percentage", 80.5),
            ("concerns", "one concern"),
            ("fallback_verdit", "reject"),
            # An unquoted YAML timestamp loads as a datetime, which JSON cannot hold.
            ("comment", datetime(2026, 9, 22, tzinfo=timezone.utc)),
        ]:
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                hydrate(Proposal, {**valid, field: value})

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
            (store.root / versions[3].path(PROPOSALS)).unlink()
            outcome = store.root / versions[2].path(OUTCOMES)
            outcome.parent.mkdir(parents=True)
            outcome.write_text("{}")
            queue = queue_of(store)
            self.assertEqual([item.request.request_id for item in queue], ids[:2])
            self.assertEqual([item.version for item in queue], [v.hash for v in versions[:2]])
            real = [r.identity.student_id for _, r in records()[:2]]
            self.assertEqual([item.request.identity.student_id for item in queue], real)
            # A changed request is a new version: its old proposal no longer applies.
            changed = copy.deepcopy(records()[0][1])
            changed.identity = replace(changed.identity, sequence="2")
            changed.comments = "resubmitted"
            (new,) = store.save([changed])
            self.assertEqual(new.request_id, ids[1])
            self.assertEqual([item.request.request_id for item in queue_of(store)], ids[:1])
            write_proposal(store, new, proposal(new.request))
            self.assertEqual(
                [(i.request.request_id, i.version) for i in queue_of(store)],
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
                [i.request.request_id for i in select(queue, verdicts=["reject"])],
                [versions[1].request_id],
            )
            self.assertEqual(
                [i.request.request_id for i in select(queue, request_ids=[ids[2]])], [ids[2]]
            )

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

    def test_panel_html_shows_the_proposal_in_order_and_escapes_content(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store, versions = make_store(directory)
            first = versions[0].request_id
            item = next(i for i in queue_of(store) if i.request.request_id == first)
            item.proposal.remap_target = "CS5242"
            item.proposal.remap_analysis = "Better fit <b>"
            item.proposal.extra_in_pu = ["Robotics"]
            for key, value in FALLBACK.items():
                setattr(item.proposal, key, value)
            sibling = versions[1].request_id
            outcomes = {
                sibling: Outcome(
                    action="reject",
                    comment="x",
                    recorded_at=STARTED,
                )
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
            first = versions[0].request_id
            item = next(i for i in queue_of(store) if i.request.request_id == first)
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
            self.assertNotIn("different action", html)

    def test_header_badges_colour_confidence_and_overlap(self) -> None:
        _, request = records()[0]
        self.assertEqual(overlap_colour(OVERLAP_GOOD), GREEN)
        self.assertEqual(overlap_colour(OVERLAP_GOOD - 1), AMBER)
        self.assertEqual(overlap_colour(OVERLAP_FAIR), AMBER)
        self.assertEqual(overlap_colour(OVERLAP_FAIR - 1), RED)
        cases: list[tuple[Confidence, int, tuple[str, str]]] = [
            ("medium", 55, (AMBER, AMBER)),
            ("low", 20, (RED, RED)),
        ]
        for confidence, overlap, colours in cases:
            advice = proposal(request, confidence=confidence, overlap_percentage=overlap)
            html = panel_html(Item(advice, request, "hash"), PROGRESS, {}, dry_run=False)
            badge = f'style="background:{colours[0]}">{confidence.title()} Confidence<'
            self.assertIn(badge, html)
            self.assertGreater(html.index(badge), html.index("</header>"), "not in the header")
            self.assertIn(f'style="background:{colours[1]}">{overlap}% Overlap<', html)

    def test_course_is_derived_from_the_request(self) -> None:
        _, request = records()[0]
        self.assertEqual(
            request.course, "EXU 1001 (Example College) -> CS3243"
        )

    def test_prefills_offer_the_fallback_only_when_there_is_one(self) -> None:
        _, request = records()[0]
        prefills = proposal(request, **FALLBACK).prefills("older")
        self.assertEqual(list(prefills), ["recommended", "fallback"])
        self.assertEqual(prefills["fallback"], FALLBACK["fallback_comment"] + "\n\nolder")
        self.assertEqual(list(proposal(request).prefills(None)), ["recommended"])

    def test_proposal_comment_goes_on_top_of_existing_text(self) -> None:
        _, request = records()[0]
        advice = proposal(request)
        self.assertEqual(advice.prefills(None)["recommended"], advice.comment)
        self.assertEqual(advice.prefills("  ")["recommended"], advice.comment)
        self.assertEqual(
            advice.prefills("older note\n")["recommended"], advice.comment + "\n\nolder note"
        )

    def test_loop_stores_submissions_only_and_stops_when_unverified(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store, versions = make_store(directory)
            requests = [v.request for v in versions]
            approve, reject = button_for("approve"), button_for("reject")
            ids = ids_of(versions)
            edited = "Approved: the syllabus covers search & <planning>. Also fine."
            stale = copy.deepcopy(requests[3])
            stale.status = "Approved"
            site = FakeSite(
                {
                    ids[0]: Clicked(approve, proposal(requests[0]).comment),
                    ids[1]: Clicked(reject, edited),
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
            rejected = log[ids[1]]
            assert isinstance(rejected, Outcome)
            self.assertEqual(rejected.comment, edited)
            self.assertEqual(log[ids[2]].reason, "skipped by the reviewer: busy")
            self.assertIsNone(log[ids[0]].reason, "A status other than pending verifies")
            self.assertIn("Approved", log[ids[3]].reason or "")
            self.assertEqual(stored_outcomes(store), {i: log[i] for i in ids[:2]})
            self.assertTrue((store.root / versions[0].path(OUTCOMES)).exists())

            # A second session offers only the skipped ones and records a cancel.
            site = FakeSite({ids[2]: Clicked("#ICList", None), ids[3]: Skipped(SKIPPED)})
            second = review(site, store.root)
            self.assertEqual(sorted(site.opened), sorted(ids[2:]))
            self.assertEqual({action_of(e) for e in second.values()}, {"skip"})
            self.assertIn("Cancel", second[ids[2]].reason or "")
            self.assertEqual(second[ids[3]].reason, SKIPPED)
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
            outcomes = {i: Clicked(button_for("approve"), "c") for i in ids}
            site = FakeSite(outcomes, status_after=PENDING)
            with self.assertRaisesRegex(RuntimeError, "not verified"):
                review(site, store.root)
            (entry,) = stored_outcomes(store).values()
            self.assertEqual(entry.action, "approve")
            self.assertIn("not verified", entry.reason or "")

    def test_leaving_the_page_skips_and_a_vanished_request_is_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store, versions = make_store(directory, count=3)
            ids = ids_of(versions)
            site = FakeSite(
                {ids[0]: Skipped("reviewer left the page"), ids[2]: Skipped(SKIPPED)},
                live={ids[1]: NotInQueueError("gone")},
            )
            log = review(site, store.root)
            self.assertEqual(
                sorted(site.opened), sorted(ids), "The loop continues past a vanished request"
            )
            self.assertEqual([action_of(log[i]) for i in ids], ["skip", NOT_IN_QUEUE, "skip"])
            self.assertEqual(log[ids[0]].reason, "reviewer left the page")
            self.assertEqual(sorted(r for r, _, _ in site.prepared), sorted([ids[0], ids[2]]))
            self.assertIsNone(log[ids[1]].reason)
            self.assertEqual(stored_outcomes(store), {ids[1]: log[ids[1]]})
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
            outcomes = {i: Clicked(button_for("approve"), comment) for i in ids}
            site = FakeSite(outcomes, status_after=RuntimeError("browser went away"))
            with self.assertRaisesRegex(RuntimeError, "browser went away"):
                review(site, store.root)
            ((stored_id, entry),) = stored_outcomes(store).items()
            first, other = site.opened[0], next(i for i in ids if i != site.opened[0])
            self.assertEqual((stored_id, entry.action), (first, "approve"))
            self.assertEqual(entry.comment, comment)
            self.assertEqual(entry.reason, f"{UNVERIFIED}: browser went away")
            # The provisional outcome is final: the next session offers only the other request.
            site = FakeSite(outcomes)
            log = review(site, store.root)
            self.assertEqual(list(log), [other])
            self.assertEqual(site.opened, [other])
            stored = stored_outcomes(store)
            self.assertEqual(sorted(stored), sorted(ids))
            self.assertEqual(stored[other].reason, None)

    def test_request_that_left_the_queue_counts_as_verified(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store, (version,) = make_store(directory, count=1)
            remap = button_for("request remapping")
            advice = proposal(version.request, **FALLBACK)
            write_proposal(store, version, advice)
            outcome = Clicked(remap, advice.prefills(None)["fallback"])
            site = FakeSite({version.request_id: outcome}, status_after=NOT_IN_QUEUE)
            (entry,) = review(site, store.root).values()
            assert isinstance(entry, Outcome)
            self.assertEqual(entry.action, "request remapping")
            self.assertEqual(entry.comment, FALLBACK["fallback_comment"])
            self.assertIsNone(entry.reason)

    def test_bad_comment_is_never_prefilled(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store, (version,) = make_store(directory, count=1)
            write_proposal(store, version, proposal(version.request, comment="Use [code]"))
            site = FakeSite({})
            (entry,) = review(site, store.root).values()
            self.assertEqual(site.prepared, [])
            self.assertIsInstance(entry, Skipped)
            self.assertIn("[", entry.reason or "")
            self.assertEqual(stored_outcomes(store), {})

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
        advice = proposal(request, overlap=[f"Topic {n}" for n in range(80)], **FALLBACK)
        panel = panel_html(Item(advice, request, "hash"), PROGRESS, {}, dry_run=False)
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

            site.prepare(advice, panel, dry_run=False)
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
            self.assertEqual(site.await_action(), Skipped(f"{SKIPPED}: later"))
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
            self.assertEqual(site.await_action(), Skipped(SKIPPED))

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
            self.assertEqual(site.await_action(), Skipped(SKIPPED))
            self.assertEqual(
                page.evaluate("window.restored"),
                {"panels": 1, "disabled": [True] * len(BUTTONS), "value": "kept"},
            )

            site.prepare(advice, panel, dry_run=False)
            later(f"document.getElementById('{DETAIL}').remove(); " + rerender)
            self.assertEqual(site.await_action(), Skipped("reviewer left the page"))
            page.evaluate(f"document.body.insertAdjacentHTML('beforeend', '{fields}')")

            site.prepare(advice, panel, dry_run=True)
            # A stale page may have lost `disabled` but keep the hook: the click is still blocked.
            later(f"document.getElementById('{approve}').disabled = false; " + press(approve))
            later(f"{in_panel('#skip')}.click()", 300)
            self.assertEqual(site.await_action(), Skipped(SKIPPED))
            self.assertEqual(messages, ["Dry run: nothing is submitted"])
            self.assertEqual(posts, [])

            messages.clear()
            site.prepare(advice, panel, dry_run=False)
            later(press(reject))
            later(f"{in_panel('#skip')}.click()", 300)
            self.assertEqual(
                site.await_action(), Skipped(SKIPPED), "A dismissed confirm aborts the click"
            )
            self.assertEqual(messages, ["Recommended: Approve. Submit Reject anyway?"])
            self.assertEqual(posts, [])

            # The fallback's button offers to switch comments: no keeps the box and blocks.
            messages.clear()
            site.prepare(advice, panel, dry_run=False)
            later(press(remap))
            later(f"{in_panel('#skip')}.click()", 300)
            self.assertEqual(site.await_action(), Skipped(SKIPPED))
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
            self.assertEqual(site.await_action(), Clicked(remap, fallback))
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
            self.assertEqual(site.await_action(), Skipped(SKIPPED))
            self.assertEqual(
                messages, ["Fallback selected: Request Remapping. Submit Approve anyway?"]
            )

            # Merely viewing the fallback changes nothing: the selected tab is what counts.
            messages.clear()
            set_box("prior")
            site.prepare(advice, panel, dry_run=False)
            page.evaluate(f"{in_panel('.tab[data-tab=fallback]')}.click()")
            later(press(approve))
            self.assertEqual(site.await_action(), Clicked(approve, recommended))
            self.assertEqual(messages, [])

            site.prepare(advice, panel, dry_run=False)
            later(f"document.getElementById('{COMMENTS}').value = 'edited'; " + press(approve))
            self.assertEqual(site.await_action(), Clicked(approve, "edited"))

            site.prepare(advice, panel, dry_run=False)
            later(press(CANCEL))
            # The mock page never navigates, so the box still holds the edited text.
            self.assertEqual(
                site.await_action(),
                Clicked("#ICList", advice.comment + "\n\nedited"),
            )
            browser.close()
