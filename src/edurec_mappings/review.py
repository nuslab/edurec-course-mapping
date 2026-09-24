"""Show each proposal on its EduRec detail page and record what was submitted."""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from functools import partial
from operator import attrgetter
from pathlib import Path
from typing import NamedTuple, Protocol

from jinja2 import Environment, PackageLoader, StrictUndefined

from .edurec import NOT_IN_QUEUE, NotInQueueError, PanelSetup
from .models import (
    PENDING_APPROVAL,
    Confidence,
    Outcome,
    Proposal,
    Reaction,
    Request,
    Skipped,
    Tab,
    Verdict,
)
from .store import OUTCOMES, Store, Version, now

VERDICT_LABELS: dict[Verdict, str] = {
    "approve": "Approve",
    "reject": "Reject",
    "request_remapping": "Request Remapping",
    "request_more_information": "Request More Information",
}
GREEN, AMBER, RED = "#2e7d32", "#ef6c00", "#c62828"
VERDICT_COLOURS: dict[Verdict, str] = {
    "approve": GREEN,
    "reject": RED,
    "request_remapping": AMBER,
    "request_more_information": AMBER,
}
"""Colour of the verdict pill and of the matching EduRec button's outline."""
CONFIDENCE_COLOURS: dict[Confidence, str] = {"high": GREEN, "medium": AMBER, "low": RED}
OVERLAP_GOOD = 70
"""Overlap percentage from which the header badge is green; amber from `OVERLAP_FAIR`."""
OVERLAP_FAIR = 40
FRESHNESS_FIELDS = (
    "partner_course.subject",
    "partner_course.number",
    "nus_course.subject",
    "nus_course.number",
    "identity.group",
    "identity.sequence",
)
TEMPLATES = Environment(
    loader=PackageLoader("edurec_mappings"), autoescape=True, undefined=StrictUndefined
)
PANEL = TEMPLATES.get_template("panel.html")


class Progress(NamedTuple):
    position: int
    total: int
    """Requests in this session's queue."""
    submitted: int
    """Requests whose latest version was reviewed with a verdict, across all sessions."""
    requests: int
    """Requests in the store."""


class ReviewSite(Protocol):
    """The browser surface `review` needs."""

    def open(self, request: Request) -> Request:
        """Reopen the request; raises `NotInQueueError` when it left the approval queue."""

    def prepare(self, setup: PanelSetup) -> None: ...

    def await_action(self) -> Reaction: ...

    def status(self, request: Request) -> str | None:
        """The live status after the click; `NOT_IN_QUEUE` when the request left the queue."""


@dataclass
class QueueItem:
    """A proposal with the request version it was made on, the real student ID restored."""

    proposal: Proposal
    request: Request
    version: Version


def submitted(outcomes: Mapping[str, Outcome]) -> dict[str, Outcome]:
    return {key: entry for key, entry in outcomes.items() if entry.verdict is not None}


def action_of(entry: Outcome | Skipped) -> str:
    if isinstance(entry, Skipped):
        return "skip"
    return entry.verdict or NOT_IN_QUEUE


def load_outcomes(store: Store, latest: Iterable[Version]) -> dict[str, Outcome]:
    outcomes: dict[str, Outcome] = {}
    for version in latest:
        outcome = store.outcome(version)
        if outcome is not None:
            outcomes[version.request_id] = outcome
    return outcomes


def load_queue(
    store: Store, latest: Iterable[Version], outcomes: Mapping[str, Outcome]
) -> list[QueueItem]:
    """Latest versions with a proposal and no outcome, in store order (siblings consecutive).

    The stored request is pseudonymized; its real student ID comes from
    `private/student_ids.yaml` so that EduRec can be searched for it.
    """
    student_ids = store.student_ids()
    items: list[QueueItem] = []
    for version in latest:
        request_id = version.request_id
        if request_id in outcomes:
            continue
        proposal = store.proposal(version)
        if proposal is None:
            continue
        if request_id not in student_ids:
            raise RuntimeError(f"{request_id} has no student ID in the store")
        request = replace(
            version.request,
            identity=replace(version.request.identity, student_id=student_ids[request_id]),
        )
        items.append(QueueItem(proposal, request, version))
    return items


def filter_queue(
    queue: Iterable[QueueItem], request_ids: Iterable[str] = (), verdicts: Iterable[str] = ()
) -> list[QueueItem]:
    ids, wanted = set(request_ids), set(verdicts)
    return [
        item
        for item in queue
        if (not ids or item.request.request_id in ids)
        and (not wanted or item.proposal.verdict in wanted)
    ]


def stale_reason(exported: Request, live: Request) -> str | None:
    if live.approval_status != PENDING_APPROVAL:
        return f"live status is {live.approval_status!r}, not {PENDING_APPROVAL!r}"
    for field in FRESHNESS_FIELDS:
        before, after = attrgetter(field)(exported), attrgetter(field)(live)
        if before != after:
            return f"{field} changed from {before!r} to {after!r}"
    return None


def comment_problem(comment: str) -> str | None:
    if not comment.strip():
        return "comment is empty"
    for marker in ("[", "XXXX"):
        if marker in comment:
            return f"comment contains {marker!r}"
    return None


def overlap_colour(percent: int) -> str:
    if percent >= OVERLAP_GOOD:
        return GREEN
    return AMBER if percent >= OVERLAP_FAIR else RED


def panel_html(
    item: QueueItem,
    progress: Progress,
    outcomes: Mapping[str, Outcome],
    dry_run: bool,
    *,
    existing: str | None = None,
    courses: Mapping[str, str] | None = None,
) -> str:
    """`outcomes` and `courses` describe the siblings, keyed by request id."""
    proposal = item.proposal
    return PANEL.render(
        proposal=proposal,
        request=item.request,
        progress=progress,
        done=round(100 * progress.position / progress.total) if progress.total else 0,
        dry_run=dry_run,
        existing=(existing or "").strip(),
        courses=courses or {},
        latest=submitted(outcomes),
        verdict_labels=VERDICT_LABELS,
        verdict_colours=VERDICT_COLOURS,
        confidence_colours=CONFIDENCE_COLOURS,
        overlap_colour=overlap_colour(proposal.overlap_percent),
    )


def panel_setup(proposal: Proposal, panel: str, dry_run: bool) -> PanelSetup:
    comments: dict[Tab, str] = {"recommended": proposal.comment}
    verdicts: dict[Tab, Verdict | None] = {"recommended": proposal.verdict, "fallback": None}
    if proposal.fallback:
        comments["fallback"] = proposal.fallback.comment
        verdicts["fallback"] = proposal.fallback.verdict
    return PanelSetup(
        panel=panel,
        comments=comments,
        verdicts=verdicts,
        labels=VERDICT_LABELS,
        colours=VERDICT_COLOURS,
        dry_run=dry_run,
    )


def review_item(
    site: ReviewSite,
    item: QueueItem,
    *,
    progress: Progress,
    outcomes: Mapping[str, Outcome],
    courses: Mapping[str, str],
    dry_run: bool,
    record: Callable[[Outcome], None] = lambda entry: None,
) -> Outcome | Skipped:
    """Show one proposal and return what happened.

    `record` is called with an unverified outcome as soon as an EduRec button is seen
    pressed, so that a failure after the click still leaves the verdict on record.
    """
    proposal, exported = item.proposal, item.request
    try:
        live = site.open(exported)
    except NotInQueueError:
        return Outcome(verdict=None, comment=None, verified=True, recorded_at=now())
    reason = stale_reason(exported, live) or comment_problem(proposal.comment)
    if reason:
        return Skipped(reason)
    panel = panel_html(
        item, progress, outcomes, dry_run, existing=live.review_comments, courses=courses
    )
    site.prepare(panel_setup(proposal, panel, dry_run))
    clicked = site.await_action()
    if isinstance(clicked, Skipped):
        return clicked
    if clicked.verdict is None:
        return Skipped("Cancel pressed in EduRec")
    submission = Outcome(
        verdict=clicked.verdict, comment=clicked.comment, verified=False, recorded_at=now()
    )
    record(submission)
    status_after = site.status(exported)  # Anything but None or pending verifies the submission.
    if status_after in (None, PENDING_APPROVAL):
        return replace(submission, note=f"live status is {status_after!r}")
    return replace(submission, verified=True, recorded_at=now())


def review(
    site: ReviewSite,
    root: str | Path,
    *,
    request_ids: Iterable[str] = (),
    verdicts: Iterable[str] = (),
    dry_run: bool = False,
) -> dict[str, Outcome | Skipped]:
    """Walk the queue; returns what happened this session, by request id.

    Each outcome is written before the next request is opened, which closes that
    request version; skips are offered again next session. A submission that could
    not be verified is stored as such and stops the session.
    """
    store = Store(root)
    latest = store.latest()
    outcomes = load_outcomes(store, latest)
    courses = {version.request_id: version.request.course for version in latest}
    queue = filter_queue(load_queue(store, latest, outcomes), request_ids, verdicts)

    def record(entry: Outcome, version: Version) -> None:
        outcomes[version.request_id] = entry
        if not dry_run:
            store.save_outcome(version, entry)

    session: dict[str, Outcome | Skipped] = {}
    for position, item in enumerate(queue, 1):
        request_id = item.request.request_id
        progress = Progress(position, len(queue), len(submitted(outcomes)), len(latest))
        try:
            entry = review_item(
                site,
                item,
                progress=progress,
                outcomes=outcomes,
                courses=courses,
                dry_run=dry_run,
                record=partial(record, version=item.version),
            )
        except Exception as error:
            # A queued version has no outcome, so one present now is the unverified click.
            unverified = outcomes.get(request_id)
            if unverified is not None:
                record(replace(unverified, note=str(error)), item.version)
            raise
        if isinstance(entry, Outcome):
            record(entry, item.version)
        session[request_id] = entry
        if isinstance(entry, Skipped):
            detail = f" ({entry.reason})"
        else:
            detail = "" if entry.verified else f" (not verified: {entry.note})"
        print(
            f"{position}/{len(queue)} {request_id} {item.request.course}: "
            f"{action_of(entry)}{detail}",
            flush=True,
        )
        if isinstance(entry, Outcome) and not entry.verified:
            raise RuntimeError(f"{request_id}: submission not verified: {entry.note}")
    counts = Counter(action_of(entry) for entry in session.values())
    summary = ", ".join(f"{count} {action}" for action, count in sorted(counts.items()))
    target = "nothing stored (dry run)" if dry_run else f"in {store.root / OUTCOMES}"
    print(f"Reviewed {len(session)} of {len(queue)} queued: {summary or 'nothing'} {target}")
    return session
