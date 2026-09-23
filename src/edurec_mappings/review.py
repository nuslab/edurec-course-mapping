"""Show each proposal on its EduRec detail page and record what the reviewer submitted.

The reviewer presses EduRec's own buttons or the panel's Skip; the program never does.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from functools import partial
from operator import attrgetter
from pathlib import Path
from typing import NamedTuple, Protocol

from jinja2 import Environment, PackageLoader, StrictUndefined

from .browser import BUTTONS, NotInQueueError
from .models import (
    AMBER,
    GREEN,
    NOT_IN_QUEUE,
    PENDING,
    RED,
    VERDICT_COLOURS,
    Confidence,
    Outcome,
    Proposal,
    Reaction,
    Request,
    Skipped,
    display,
    plain,
)
from .store import OUTCOMES, PROPOSALS, Store, Version, dump, now, read, write_atomic

UNVERIFIED = "submission observed, not yet verified"
"""Reason on the provisional entry written the moment the reviewer's click is seen."""
FRESHNESS_FIELDS = (
    "partner_course.subject",
    "partner_course.number",
    "nus_course.subject",
    "nus_course.number",
    "identity.mapping_number",
    "identity.sequence",
)
CONFIDENCE_COLOURS: dict[Confidence, str] = {"high": GREEN, "medium": AMBER, "low": RED}
OVERLAP_GOOD = 70
"""Overlap percentage from which the header badge is green; amber from `OVERLAP_FAIR`."""
OVERLAP_FAIR = 40
TEMPLATES = Environment(
    loader=PackageLoader("edurec_mappings"), autoescape=True, undefined=StrictUndefined
)
TEMPLATES.filters["display"] = display
PANEL = TEMPLATES.get_template("panel.html")


class Progress(NamedTuple):
    """The header's counters: the session position and how far the store is."""

    position: int
    total: int
    """Requests in this session's queue."""
    submitted: int
    """Requests whose latest version was reviewed with a verdict, across all sessions."""
    requests: int
    """Requests in the store."""


class Site(Protocol):
    """The browser surface `review` needs; `Reviewer` implements it against the live site."""

    def open(self, request: Request) -> Request:
        """Reopen the request; raises `NotInQueueError` when it left the approval queue."""

    def prepare(self, proposal: Proposal, panel: str, dry_run: bool) -> None:
        """Pre-fill the comment box and inject the panel."""

    def await_action(self) -> Reaction:
        """The reviewer's click, or a skip: the panel's Skip or the detail page going away."""

    def status(self, request: Request) -> str | None:
        """The live status after the click; `NOT_IN_QUEUE` when the request left the queue."""


@dataclass
class Item:
    """A proposal with the request version it was made on, the real student ID restored."""

    proposal: Proposal
    request: Request
    version: str
    """The request version's content hash, which names its proposal and outcome files."""


def submitted(outcomes: Mapping[str, Outcome]) -> dict[str, Outcome]:
    """The outcomes that record a submitted verdict, not a request that left the queue."""
    return {key: entry for key, entry in outcomes.items() if entry.action != NOT_IN_QUEUE}


def action_of(entry: Outcome | Skipped) -> str:
    """What happened, for the session log: the outcome's action, or `skip`."""
    return "skip" if isinstance(entry, Skipped) else entry.action


def load_outcomes(store: Store, latest: Iterable[Version]) -> dict[str, Outcome]:
    """The review of each request's latest version, by request id."""
    outcomes: dict[str, Outcome] = {}
    for version in latest:
        path = store.file(OUTCOMES, version.request_id, version.hash)
        if path.exists():
            outcomes[version.request_id] = read(Outcome, path)
    return outcomes


def load_queue(store: Store, latest: Iterable[Version]) -> list[Item]:
    """Latest versions with a proposal and no outcome, in store order (siblings consecutive).

    The stored request is anonymized; its real student ID comes from
    `private/identities.yaml` so that EduRec can be searched for it.
    """
    identities = store.identities()
    items: list[Item] = []
    for version in latest:
        request_id = version.request_id
        path = store.file(PROPOSALS, request_id, version.hash)
        if not path.exists() or store.file(OUTCOMES, request_id, version.hash).exists():
            continue
        if request_id not in identities:
            raise RuntimeError(f"{request_id} has no student ID in the store's identities")
        request = replace(
            version.request,
            identity=replace(version.request.identity, student_id=identities[request_id]),
        )
        items.append(Item(read(Proposal, path), request, version.hash))
    return items


def select(
    queue: Iterable[Item], request_ids: Iterable[str] = (), verdicts: Iterable[str] = ()
) -> list[Item]:
    """Apply the CLI filters."""
    ids, wanted = set(request_ids), set(verdicts)
    return [
        item
        for item in queue
        if (not ids or item.request.request_id in ids)
        and (not wanted or item.proposal.verdict in wanted)
    ]


def stale_reason(exported: Request, live: Request) -> str | None:
    if live.status != PENDING:
        return f"live status is {live.status!r}, not {PENDING!r}"
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


def overlap_colour(percentage: int) -> str:
    if percentage >= OVERLAP_GOOD:
        return GREEN
    return AMBER if percentage >= OVERLAP_FAIR else RED


def panel_html(
    item: Item,
    progress: Progress,
    outcomes: Mapping[str, Outcome],
    dry_run: bool,
    *,
    existing: str | None = None,
    courses: Mapping[str, str] | None = None,
) -> str:
    """The reviewer's panel from `templates/panel.html`; every value is HTML-escaped.

    `outcomes` and `courses` describe the siblings, keyed by request id.
    """
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
        verdict_colours=VERDICT_COLOURS,
        confidence_colours=CONFIDENCE_COLOURS,
        overlap_colour=overlap_colour(proposal.overlap_percentage),
    )


def review_item(
    site: Site,
    item: Item,
    *,
    progress: Progress,
    outcomes: Mapping[str, Outcome],
    courses: Mapping[str, str],
    dry_run: bool,
    record: Callable[[Outcome], None] = lambda entry: None,
) -> Outcome | Skipped:
    """Show one proposal to the reviewer and return what happened.

    `record` is called with a provisional entry as soon as an EduRec button is
    seen pressed, before the submission is verified, so that a failure after
    the click still leaves the verdict on record.
    """
    proposal, exported = item.proposal, item.request
    try:
        live = site.open(exported)
    except NotInQueueError:
        return Outcome(action=NOT_IN_QUEUE, comment=None, recorded_at=now())
    reason = stale_reason(exported, live) or comment_problem(proposal.comment)
    if reason:
        return Skipped(reason)
    panel = panel_html(item, progress, outcomes, dry_run, existing=live.comments, courses=courses)
    site.prepare(proposal, panel, dry_run)
    clicked = site.await_action()
    if isinstance(clicked, Skipped):
        return clicked
    verdict = BUTTONS.get(clicked.action)
    if verdict is None:
        return Skipped("Cancel pressed in EduRec")
    record(Outcome(action=verdict, comment=clicked.comment, recorded_at=now(), reason=UNVERIFIED))
    status_after = site.status(exported)  # Anything but None or pending verifies the submission.
    return Outcome(
        action=verdict,
        comment=clicked.comment,
        recorded_at=now(),
        reason=(
            f"submission not verified: live status is {status_after!r}"
            if status_after in (None, PENDING)
            else None
        ),
    )


def review(
    site: Site,
    root: str | Path,
    *,
    request_ids: Iterable[str] = (),
    verdicts: Iterable[str] = (),
    dry_run: bool = False,
) -> dict[str, Outcome | Skipped]:
    """Walk the queue with the reviewer; returns what happened this session, by request id.

    Each outcome is written before the next request is opened, which closes that
    request version; skips are offered again next session. A submission that could
    not be verified is stored with a reason and stops the session.
    """
    store = Store(root)
    latest = store.latest()
    outcomes = load_outcomes(store, latest)
    courses = {version.request_id: version.request.course for version in latest}
    queue = select(load_queue(store, latest), request_ids, verdicts)

    def record(entry: Outcome, request_id: str, version: str) -> None:
        outcomes[request_id] = entry
        if not dry_run:
            write_atomic(store.file(OUTCOMES, request_id, version), dump(plain(entry)))

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
                record=partial(record, request_id=request_id, version=item.version),
            )
        except Exception as error:
            # A queued version has no review, so one present now is the provisional one.
            provisional = outcomes.get(request_id)
            if provisional is not None:
                record(
                    replace(provisional, reason=f"{UNVERIFIED}: {error}"), request_id, item.version
                )
            raise
        if isinstance(entry, Outcome):
            record(entry, request_id, item.version)
        session[request_id] = entry
        detail = f" ({entry.reason})" if entry.reason else ""
        print(
            f"{position}/{len(queue)} {request_id} {item.request.course}: "
            f"{action_of(entry)}{detail}",
            flush=True,
        )
        if isinstance(entry, Outcome) and entry.reason:
            raise RuntimeError(f"{request_id}: {entry.reason}")
    counts = Counter(action_of(entry) for entry in session.values())
    summary = ", ".join(f"{count} {action}" for action, count in sorted(counts.items()))
    target = "nothing stored (dry run)" if dry_run else f"→ {store.root / OUTCOMES}"
    print(f"Reviewed {len(session)} of {len(queue)} queued: {summary or 'nothing'} {target}")
    return session
