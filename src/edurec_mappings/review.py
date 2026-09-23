"""Stage 4: show each decision to the reviewer on its EduRec detail page and log what they did.

The program reopens a request, pre-fills the comment box with the decision's
comment and injects a panel with the decision. The reviewer presses one of
EduRec's own buttons (or the panel's Skip); the program never does. After the
postback it verifies that the status left "Pending Approval" (a request that
disappeared from the approval queue counts as verified) and appends a
`Reviewed` entry to `decisions/reviewed.yaml`; a dry run logs nothing.

Only clicks made while the panel is shown are observed and logged.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import NamedTuple, Protocol

import yaml
from jinja2 import Environment, PackageLoader, StrictUndefined

from .anonymize import anonymized_path
from .browser import BUTTONS, NotInQueueError
from .models import (
    AMBER,
    GREEN,
    PENDING,
    RED,
    VERDICT_COLOURS,
    Confidence,
    Decision,
    Outcome,
    Request,
    Reviewed,
    Skipped,
    display,
    hydrate,
    plain,
)
from .store import INVENTORY, REQUESTS, dump, now, write_atomic

DECISIONS = "decisions"
REVIEWED = "reviewed.yaml"
VANISHED = "no longer in the approval queue"
"""Skip reason for a request that left the approval queue; final, since it cannot return."""
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


class Log:
    """`decisions/reviewed.yaml`: every change is written through at once, atomically.

    A dry run keeps its entries in memory only: it can only skip, and skips are
    offered again anyway.
    """

    def __init__(self, path: Path, dry_run: bool = False) -> None:
        self.path = path
        self.dry_run = dry_run
        self.entries = load_reviewed(path)

    def append(self, entry: Reviewed) -> None:
        self.entries.append(entry)
        self.flush()

    def replace_last(self, entry: Reviewed) -> None:
        self.entries[-1] = entry
        self.flush()

    def flush(self) -> None:
        if not self.dry_run:
            write_atomic(self.path, dump([plain(entry) for entry in self.entries]))


class Progress(NamedTuple):
    """The header's counters: the session position and how far the run is."""

    position: int
    total: int
    """Requests in this session's queue."""
    submitted: int
    """Requests logged with a verdict, across all sessions."""
    requests: int
    """Requests in the run."""


class Site(Protocol):
    """The browser surface `review` needs; `Reviewer` implements it against the live site."""

    def open(self, request: Request) -> Request:
        """Reopen the request; raises `NotInQueueError` when it left the approval queue."""

    def prepare(self, decision: Decision, panel: str, dry_run: bool) -> None:
        """Pre-fill the comment box and inject the panel."""

    def await_action(self) -> Outcome:
        """The reviewer's click, or a skip: the panel's Skip or the detail page going away."""

    def status(self, request: Request) -> str | None:
        """The live status after the click; `NOT_IN_QUEUE` when the request left the queue."""


@dataclass
class Item:
    """A decision paired with the original export's request it applies to."""

    decision: Decision
    request: Request


def started_at(run: Path) -> str:
    inventory = yaml.safe_load((run / INVENTORY).read_text(encoding="utf-8"))
    return str(inventory["started_at"])


def load_queue(run: str | Path, decisions: str | Path) -> tuple[list[Item], dict[str, str]]:
    """Pair each decision with its exported request, siblings consecutive.

    Returns the queue and, keyed by request id, why other decisions were left out:
    made from another export, or without a request file in `run`.
    """
    run, decisions = Path(run), Path(decisions)
    expected = started_at(decisions.parent)
    if started_at(run) != expected:
        raise RuntimeError(f"{decisions} belongs to a different export than {run}")
    items: list[Item] = []
    rejected: dict[str, str] = {}
    for path, data in decision_files(decisions):
        try:
            decision = hydrate(Decision, data)
        except ValueError as error:
            raise ValueError(f"{path}: {error}") from error
        request_path = run / REQUESTS / f"{decision.request_id}.yaml"
        if decision.source_started_at != expected:
            rejected[decision.request_id] = (
                f"decided on export {decision.source_started_at}, current is {expected}"
            )
        elif not request_path.exists():
            rejected[decision.request_id] = f"no requests/{request_path.name} in {run}"
        else:
            request = hydrate(Request, yaml.safe_load(request_path.read_text(encoding="utf-8")))
            items.append(Item(decision, request))
    first_seen: dict[tuple[str, ...], int] = {}
    for index, item in enumerate(items):
        first_seen.setdefault(item.request.identity.mapping, index)
    items.sort(key=lambda item: first_seen[item.request.identity.mapping])
    return items, rejected


def decision_files(decisions: Path) -> Iterator[tuple[Path, object]]:
    """Each decision file and its parsed content, in file-name order; the log is not one."""
    for path in sorted(decisions.glob("*.yaml")):
        if path.name != REVIEWED:
            yield path, yaml.safe_load(path.read_text(encoding="utf-8"))


def load_reviewed(path: Path) -> list[Reviewed]:
    if not path.exists():
        return []
    entries = yaml.safe_load(path.read_text(encoding="utf-8")) or []
    return [hydrate(Reviewed, entry) for entry in entries]


def since_export(log: Iterable[Reviewed], started: str | None) -> list[Reviewed]:
    """The log entries made after the export started.

    A request id survives resubmission: a request submitted before the export was
    taken and exported again as pending is a new round, so its earlier entries
    do not count against it. Entries whose timestamp cannot be read are kept.
    """
    if started is None:
        return list(log)
    try:
        cutoff = datetime.fromisoformat(started)
    except ValueError:
        return list(log)
    kept: list[Reviewed] = []
    for entry in log:
        try:
            if datetime.fromisoformat(entry.reviewed_at) < cutoff:
                continue
        except ValueError:
            pass
        kept.append(entry)
    return kept


def select(
    queue: Iterable[Item],
    log: Iterable[Reviewed],
    request_ids: Iterable[str] = (),
    verdicts: Iterable[str] = (),
    started: str | None = None,
) -> list[Item]:
    """Drop requests already submitted and apply the CLI filters.

    Skips are offered again, except a request that left the approval queue:
    it cannot come back, so its skip is final. Only entries made since the
    export started (`started`, the export's `started_at`) count: a request
    resubmitted after an earlier round keeps its id and is offered again.
    """
    log = since_export(log, started)
    done = {entry.request_id for entry in log if entry.action != "skip" or entry.reason == VANISHED}
    ids, wanted = set(request_ids), set(verdicts)
    return [
        item
        for item in queue
        if item.request.request_id not in done
        and (not ids or item.request.request_id in ids)
        and (not wanted or item.decision.verdict in wanted)
    ]


def stale_reason(exported: Request, live: Request) -> str | None:
    if live.status != PENDING:
        return f"live status is {live.status!r}, not {PENDING!r}"
    for field in FRESHNESS_FIELDS:
        before, after = exported, live
        for part in field.split("."):
            before, after = getattr(before, part), getattr(after, part)
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
    log: Iterable[Reviewed],
    dry_run: bool,
    *,
    existing: str | None = None,
    courses: Mapping[str, str] | None = None,
) -> str:
    """The reviewer's panel from `templates/panel.html`; every value is HTML-escaped.

    `courses` names the siblings, keyed by request id.
    """
    decision = item.decision
    return PANEL.render(
        decision=decision,
        request=item.request,
        progress=progress,
        done=round(100 * progress.position / progress.total) if progress.total else 0,
        dry_run=dry_run,
        existing=(existing or "").strip(),
        courses=courses or {},
        latest={entry.request_id: entry for entry in log if entry.action != "skip"},
        verdict_colours=VERDICT_COLOURS,
        confidence_colours=CONFIDENCE_COLOURS,
        overlap_colour=overlap_colour(decision.overlap_percentage),
    )


def review_item(
    site: Site,
    item: Item,
    *,
    progress: Progress,
    log: list[Reviewed],
    courses: Mapping[str, str],
    dry_run: bool,
    record: Callable[[Reviewed], None] = lambda entry: None,
) -> Reviewed:
    """Show one decision to the reviewer and return what happened.

    `record` is called with a provisional entry as soon as an EduRec button is
    seen pressed, before the submission is verified, so that a failure after
    the click still leaves the verdict on record.
    """
    decision, exported = item.decision, item.request
    try:
        live = site.open(exported)
    except NotInQueueError:
        live = None
    entry = Reviewed(
        request_id=exported.request_id,
        verdict_recommended=decision.verdict,
        action="skip",
        comment_submitted=None,
        reviewed_at=now(),
    )
    if live is None:
        return replace(entry, reason=VANISHED)
    reason = stale_reason(exported, live) or comment_problem(decision.comment)
    if reason:
        return replace(entry, reason=reason)
    panel = panel_html(item, progress, log, dry_run, existing=live.comments, courses=courses)
    site.prepare(decision, panel, dry_run)
    clicked = site.await_action()
    if isinstance(clicked, Skipped):
        return replace(entry, reason=clicked.reason)
    verdict = BUTTONS.get(clicked.action)
    if verdict is None:
        return replace(entry, reason="Cancel pressed in EduRec")
    record(
        replace(
            entry,
            action=verdict,
            comment_submitted=clicked.comment,
            reviewed_at=now(),
            reason=UNVERIFIED,
        )
    )
    status_after = site.status(exported)  # Anything but None or pending verifies the submission.
    return replace(
        entry,
        action=verdict,
        comment_submitted=clicked.comment,
        reviewed_at=now(),
        reason=(
            f"submission not verified: live status is {status_after!r}"
            if status_after in (None, PENDING)
            else None
        ),
    )


def review(
    site: Site,
    run: str | Path,
    decisions: str | Path | None = None,
    *,
    request_ids: Iterable[str] = (),
    verdicts: Iterable[str] = (),
    dry_run: bool = False,
) -> list[Reviewed]:
    """Walk the queue with the reviewer; returns this session's log entries.

    Every entry is written to `decisions/reviewed.yaml` (except in a dry run)
    before the next request is opened, and a provisional entry is written the
    moment a click is seen: an error after the click leaves the verdict on record
    with the error as reason, then the session stops. A submission whose status
    did not leave "Pending Approval" is logged with its verdict and a reason, then
    the session stops: it is never retried. A request that already left the approval queue is logged
    as a skip and not offered again.
    """
    run = Path(run)
    decisions = Path(decisions) if decisions else anonymized_path(run) / DECISIONS
    queue, rejected = load_queue(run, decisions)
    for request_id, why in rejected.items():
        print(f"Not applicable {request_id}: {why}", flush=True)
    log = Log(decisions / REVIEWED, dry_run)
    started = started_at(decisions.parent)
    vanished = {
        entry.request_id
        for entry in since_export(log.entries, started)
        if entry.action == "skip" and entry.reason == VANISHED
    }
    for request_id in sorted(vanished):
        print(f"Not applicable {request_id}: {VANISHED}", flush=True)
    courses = {item.request.request_id: item.request.course for item in queue}
    queue = select(queue, log.entries, request_ids, verdicts, started)
    requests = sum(1 for _ in (run / REQUESTS).glob("*.yaml"))
    session: list[Reviewed] = []
    for position, item in enumerate(queue, 1):
        submitted = {entry.request_id for entry in log.entries if entry.action != "skip"}
        progress = Progress(position, len(queue), len(submitted), requests)
        provisional: Reviewed | None = None

        def record(entry: Reviewed) -> None:
            nonlocal provisional
            provisional = entry
            log.append(entry)

        try:
            entry = review_item(
                site,
                item,
                progress=progress,
                log=log.entries,
                courses=courses,
                dry_run=dry_run,
                record=record,
            )
        except Exception as error:
            if provisional is not None:
                log.replace_last(replace(provisional, reason=f"{UNVERIFIED}: {error}"))
            raise
        if provisional is not None:
            log.replace_last(entry)
        else:
            log.append(entry)
        session.append(entry)
        detail = f" ({entry.reason})" if entry.reason else ""
        print(
            f"{position}/{len(queue)} {entry.request_id} {item.request.course}: "
            f"{entry.action}{detail}",
            flush=True,
        )
        if entry.reason and entry.action != "skip":
            raise RuntimeError(f"{entry.request_id}: {entry.reason}")
    counts = Counter(entry.action for entry in session)
    summary = ", ".join(f"{count} {action}" for action, count in sorted(counts.items()))
    target = "not logged (dry run)" if dry_run else f"→ {log.path}"
    print(f"Reviewed {len(session)} of {len(queue)} queued: {summary or 'nothing'} {target}")
    return session
