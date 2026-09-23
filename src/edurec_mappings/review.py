"""Stage 4: show each decision to the reviewer on its EduRec detail page and log what they did.

The program reopens a request, pre-fills the comment box with the decision's
comment and injects a panel with the decision. The reviewer presses one of
EduRec's own buttons (or the panel's Skip); the program never does. After the
postback it verifies that the status left "Pending Approval" (a request that
disappeared from the approval queue counts as verified) and appends an
`Reviewed` entry to `decisions/reviewed.yaml`.

Only clicks made while the panel is shown are observed and logged.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from html import escape
from pathlib import Path
from typing import NamedTuple, Protocol

import yaml

from .anonymize import anonymized_path
from .browser import BUTTONS, NotInQueueError
from .models import (
    AMBER,
    GREEN,
    PENDING,
    RED,
    VERDICT_COLOURS,
    CommentSource,
    Confidence,
    Decision,
    Left,
    Outcome,
    Request,
    Reviewed,
    Skipped,
    Tab,
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
PANEL_CSS = """
:host{position:fixed;top:8px;right:8px;width:360px;max-height:95vh;display:flex;
flex-direction:column;z-index:2147483647;background:#fffef5;border:2px solid #555;
border-radius:6px;font:13px/1.4 sans-serif;color:#222;box-shadow:0 2px 8px rgba(0,0,0,.3)}
header,footer{padding:8px 10px;flex:none}
header{border-bottom:1px solid #ccc}
footer{border-top:1px solid #ccc;display:flex;gap:6px}
#body{overflow:auto;padding:0 10px 8px;flex:1 1 auto}
p{margin:4px 0}
ul{margin:0;padding-left:18px}
.badges{display:flex;flex-wrap:wrap;gap:6px}
.pill{font-weight:bold;color:#fff;padding:2px 8px;border-radius:4px}
.pill[data-tab]:not(.active){display:none}
.course{margin-top:6px;font-size:12px;color:#444}
.bar{height:3px;margin:4px 0 2px;border-radius:2px;background:#ddd}
.bar span{display:block;height:100%;border-radius:2px;background:#555}
.progress{display:flex;justify-content:space-between;gap:6px;font-size:11px;color:#444}
.warn{padding:1px 6px;border-radius:3px;background:#ffe082;color:#222}
.label{margin:12px 0 2px;font-size:12px;letter-spacing:.06em;text-transform:uppercase;color:#444}
.comment{white-space:pre-wrap;background:#f4f4f4;padding:4px}
.tabs{display:flex;gap:2px;margin-top:12px}
.tab{padding:4px 10px;border:1px solid #bbb;border-bottom:none;border-radius:4px 4px 0 0;
background:#eee;color:#333;cursor:pointer;font:inherit}
.tab.active{background:#fff;font-weight:bold;position:relative;margin-bottom:-1px}
.pane{display:none;padding:6px 8px;border:1px solid #bbb;border-radius:0 4px 4px 4px;
background:#fff}
.pane.active{display:block}
.pane:not(.selected) .modified{display:none}
.tools{text-align:right;font-size:12px}
.tools button{font-size:12px;padding:2px 8px;margin-left:6px}
.modified{color:#b26a00;font-weight:bold;margin-left:6px}
button{padding:4px 10px;font:inherit;cursor:pointer}
#reason{flex:1;font:inherit;padding:4px}
"""
CONFIDENCE_COLOURS: dict[Confidence, str] = {"high": GREEN, "medium": AMBER, "low": RED}
OVERLAP_GOOD = 70
"""Overlap percentage from which the header badge is green; amber from `OVERLAP_FAIR`."""
OVERLAP_FAIR = 40


class Log:
    """`decisions/reviewed.yaml`: every change is written through at once, atomically."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.entries = load_reviewed(path)

    def append(self, entry: Reviewed) -> None:
        self.entries.append(entry)
        self.flush()

    def replace_last(self, entry: Reviewed) -> None:
        self.entries[-1] = entry
        self.flush()

    def flush(self) -> None:
        write_atomic(self.path, dump(plain(self.entries)))


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

    def prepare(self, decision: Decision, panel: str, dry_run: bool) -> Mapping[Tab, str]:
        """Pre-fill the comment box and inject the panel; returns each tab's pre-filled text."""

    def await_action(self) -> Outcome:
        """The reviewer's click, the panel's Skip, or the detail page going away."""

    def status(self, request: Request) -> str | None:
        """The live status after the click; `NOT_IN_QUEUE` when the request left the queue."""


@dataclass
class Item:
    """A decision paired with the original export's request it applies to."""

    decision: Decision
    request: Request


def started_at(run: Path) -> str:
    inventory = yaml.safe_load((run / INVENTORY).read_text(encoding="utf-8"))
    return str(inventory["collection"]["started_at"])


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
    for data in decision_files(decisions):
        decision = hydrate(Decision, data)
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
    first_seen: dict[str, int] = {}
    for index, item in enumerate(items):
        first_seen.setdefault(item.request.group_id, index)
    items.sort(key=lambda item: first_seen[item.request.group_id])
    return items, rejected


def decision_files(decisions: Path) -> Iterator[object]:
    """The parsed content of each decision file, in file-name order; the log is not one."""
    for path in sorted(decisions.glob("*.yaml")):
        if path.name != REVIEWED:
            yield yaml.safe_load(path.read_text(encoding="utf-8"))


def load_reviewed(path: Path) -> list[Reviewed]:
    if not path.exists():
        return []
    entries = yaml.safe_load(path.read_text(encoding="utf-8")) or []
    return [hydrate(Reviewed, entry) for entry in entries]


def course_names(decisions: Path) -> dict[str, str]:
    """Each decision's display course string, keyed by request id."""
    names: dict[str, str] = {}
    for data in decision_files(decisions):
        if isinstance(data, dict) and "request_id" in data:
            names[str(data["request_id"])] = str(data.get("course", ""))
    return names


def comment_source(
    comment: str | None, tab: Tab | None, prefills: Mapping[Tab, str]
) -> CommentSource:
    """Which pre-filled text was submitted: the selected tab's if it still matches, else `edited`.

    Without the hook's report of the selected tab, a comment equal to either
    pre-filled text is attributed to that tab.
    """
    text = (comment or "").strip()
    candidates: Iterable[Tab] = [tab] if tab is not None and tab in prefills else prefills
    return next((t for t in candidates if prefills[t].strip() == text), "edited")


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
    export started (`started`, the collection's `started_at`) count: a request
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


def warn(text: str) -> str:
    return f'<span class="warn">{escape(text)}</span>'


def pill(text: str, colour: str, tab: Tab | None = None, active: bool = True) -> str:
    """A coloured badge; with `tab` it shows only while that tab is active."""
    attrs = f'class="pill{" active" if tab and active else ""}"'
    if tab:
        attrs += f' data-tab="{tab}"'
    return f'<span {attrs} style="background:{colour}">{escape(text)}</span>'


def label(text: str) -> str:
    return f'<div class="label">{escape(text)}</div>'


def bullet_list(title: str, values: Iterable[str]) -> list[str]:
    items = [f"<li>{escape(v)}</li>" for v in values]
    return [label(title), "<ul>", *items, "</ul>"] if items else []


def pane(tab: Tab, verdict: str | None, comment: str, note: str = "", *, badge: str = "") -> str:
    """One tab's content: the Decision line, an optional badge and note, the comment and its tools.

    `verdict` is ready-made HTML for the Decision line. Recommended is shown and
    selected initially. A pane without a verdict (a fallback with none) reads
    "No fallback" and cannot be selected. The Select button's text and disabled
    state follow the selection; `choose` in the hook swaps them.
    """
    first = tab == "recommended"
    parts = [f'<section class="pane{" active selected" if first else ""}" data-tab="{tab}">']
    parts.append(f"<p><b>Decision:</b> {verdict}</p>" if verdict else "<p>No fallback</p>")
    if badge:
        parts.append(f"<p>{badge}</p>")
    if note:
        parts.append(f"<p>{escape(note)}</p>")
    if verdict:
        text, attrs = ("Selected", " disabled") if first else ("Select", "")
        parts += [
            f'<div class="comment">{escape(comment)}</div>',
            '<p class="tools"><button type="button" class="reset">Reset comment</button>',
            f'<button type="button" class="select"{attrs}>{text}</button>',
            '<span class="modified" hidden>modified</span></p>',
        ]
    parts.append("</section>")
    return "".join(parts)


def panel_html(
    item: Item,
    progress: Progress,
    log: Iterable[Reviewed],
    dry_run: bool,
    *,
    existing: str | None = None,
    courses: Mapping[str, str] | None = None,
) -> str:
    """The reviewer's panel, rendered into a shadow root; every value is HTML-escaped."""
    decision, request = item.decision, item.request
    courses = courses or {}
    latest: dict[str, Reviewed] = {}
    for entry in log:
        if entry.action != "skip":
            latest[entry.request_id] = entry
    verdict, fallback = display(decision.verdict), decision.fallback_verdict
    badges = [pill(verdict, VERDICT_COLOURS[decision.verdict], "recommended")]
    if fallback:
        badges.append(pill(display(fallback), VERDICT_COLOURS[fallback], "fallback", active=False))
    badges.append(
        pill(f"{decision.overlap_percentage}% Overlap", overlap_colour(decision.overlap_percentage))
    )
    confidence = pill(
        f"{display(decision.decision_confidence)} Confidence",
        CONFIDENCE_COLOURS[decision.decision_confidence],
    )
    done = round(100 * progress.position / progress.total) if progress.total else 0
    caption = (
        f"{progress.position} of {progress.total} this session &middot; "
        f"{progress.submitted} of {progress.requests} overall"
    )
    parts = [
        f"<style>{PANEL_CSS}</style>",
        "<header>",
        f'<div class="badges">{"".join(badges)}</div>',
        f'<div class="course">{escape(decision.course)}</div>',
        f'<div class="bar"><span style="width:{done}%"></span></div>',
        f'<div class="progress"><span>{caption}</span>',
        warn("Dry run: action buttons disabled, only Skip advances") if dry_run else "",
        "</div></header>",
        '<div id="body">',
    ]
    parts += [
        '<nav class="tabs">',
        '<button type="button" class="tab active" data-tab="recommended">Recommended</button>',
        '<button type="button" class="tab" data-tab="fallback">Fallback</button>',
        "</nav>",
        pane("recommended", escape(verdict), decision.comment, badge=confidence),
        pane(
            "fallback",
            escape(display(fallback)) if fallback else None,
            decision.fallback_comment or "",
            decision.fallback_rationale or "",
        ),
    ]
    if existing and existing.strip():
        parts += [
            label("Previous comments"),
            f'<div class="comment">{escape(existing.strip())}</div>',
        ]
    if decision.remap_target or decision.remap_analysis:
        parts.append(label("Remap"))
        if decision.remap_target:
            parts.append(f"<p><b>Target:</b> {escape(decision.remap_target)}</p>")
        if decision.remap_analysis:
            parts.append(f"<p>{escape(decision.remap_analysis)}</p>")
    parts += bullet_list("Concerns", decision.concerns)
    parts += bullet_list("Overlap", decision.overlap)
    parts += bullet_list("Missing from PU", decision.missing_from_pu)
    parts += bullet_list("Extra in PU", decision.extra_in_pu)
    if request.related_request_ids:
        parts += [label("Siblings (many-to-one)"), "<ul>"]
        for sibling in request.related_request_ids:
            logged = latest.get(sibling)
            status = escape(logged.action) if logged else "not yet submitted"
            if logged and logged.action != decision.verdict:
                status += " " + warn("different action")
            parts.append(f"<li>{escape(courses.get(sibling, sibling))}: {status}</li>")
        parts.append("</ul>")
    parts += [
        "</div>",
        "<footer>",
        '<input type="text" id="reason" placeholder="Skip reason (optional)">',
        '<button type="button" id="skip">Skip this request</button>',
        "</footer>",
    ]
    return "".join(parts)


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
        comment_edited=False,
        status_before=live.status if live else None,
        status_after=None,
        reviewed_at=now(),
        dry_run=dry_run,
    )
    if live is None:
        return replace(entry, reason=VANISHED)
    reason = stale_reason(exported, live) or comment_problem(decision.comment)
    if reason:
        return replace(entry, reason=reason)
    panel = panel_html(item, progress, log, dry_run, existing=live.comments, courses=courses)
    prefills = site.prepare(decision, panel, dry_run)
    clicked = site.await_action()
    if isinstance(clicked, Skipped):
        why = f": {clicked.reason.strip()}" if clicked.reason.strip() else ""
        return replace(entry, reason=f"skipped by the reviewer{why}")
    if isinstance(clicked, Left):
        return replace(entry, reason="reviewer left the page")
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
    source = comment_source(clicked.comment, clicked.source, prefills)
    return replace(
        entry,
        action=verdict,
        comment_submitted=clicked.comment,
        comment_edited=source == "edited",
        comment_source=source,
        status_after=status_after,
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

    Every entry is written to `decisions/reviewed.yaml` before the next request is
    opened, and a provisional entry is written the moment a click is seen: an
    error after the click leaves the verdict on record with the error as reason,
    then the session stops. A submission whose status did not leave "Pending
    Approval" is logged with its verdict and a reason, then the session stops: it
    is never retried. A request that already left the approval queue is logged
    as a skip and not offered again.
    """
    run = Path(run)
    decisions = Path(decisions) if decisions else anonymized_path(run) / DECISIONS
    queue, rejected = load_queue(run, decisions)
    for request_id, why in rejected.items():
        print(f"Not applicable {request_id}: {why}", flush=True)
    log = Log(decisions / REVIEWED)
    started = started_at(decisions.parent)
    vanished = {
        entry.request_id
        for entry in since_export(log.entries, started)
        if entry.action == "skip" and entry.reason == VANISHED
    }
    for request_id in sorted(vanished):
        print(f"Not applicable {request_id}: {VANISHED}", flush=True)
    queue = select(queue, log.entries, request_ids, verdicts, started)
    courses = course_names(decisions)
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
            f"{position}/{len(queue)} {entry.request_id} {item.decision.course}: "
            f"{entry.action}{detail}",
            flush=True,
        )
        if entry.reason and entry.action != "skip":
            raise RuntimeError(f"{entry.request_id}: {entry.reason}")
    counts = Counter(entry.action for entry in session)
    summary = ", ".join(f"{count} {action}" for action, count in sorted(counts.items()))
    print(f"Reviewed {len(session)} of {len(queue)} queued: {summary or 'nothing'} → {log.path}")
    return session
