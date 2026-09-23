"""Stage 4: show each decision to the reviewer on its EduRec detail page and log what they did.

The program reopens a request, pre-fills the comment box with the decision's
comment and injects a panel with the decision. The reviewer presses one of
EduRec's own buttons (or the panel's Skip); the program never does. After the
postback it verifies that the status left "Pending Approval" (a request that
disappeared from the approval queue counts as verified) and appends an
`Applied` entry to `decisions/applied.yaml`.

Only clicks made while the panel is shown are observed and logged.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, replace
from html import escape
from pathlib import Path
from typing import Protocol

import yaml

from .anonymize import anonymized_path
from .browser import BUTTONS, NotInQueueError
from .models import PENDING, Applied, Decision, Left, Outcome, Request, Skipped, plain
from .parse import INVENTORY, REQUESTS, dump, hydrate, now, write_atomic

DECISIONS = "decisions"
APPLIED = "applied.yaml"
FRESHNESS_FIELDS = (
    "partner_course.subject",
    "partner_course.number",
    "nus_course.subject",
    "nus_course.number",
    "identity.mapping_number",
    "identity.sequence",
)
PANEL_CSS = """
#edurec-apply-panel{position:fixed;top:8px;right:8px;width:360px;max-height:95vh;overflow:auto;
z-index:2147483647;background:#fffef5;border:2px solid #555;border-radius:6px;padding:10px;
font:13px/1.4 sans-serif;color:#222;box-shadow:0 2px 8px rgba(0,0,0,.3)}
#edurec-apply-panel h2{margin:0 0 6px;font-size:15px}
#edurec-apply-panel h3{margin:8px 0 2px;font-size:13px}
#edurec-apply-panel ul{margin:0;padding-left:18px}
#edurec-apply-panel .edurec-apply-verdict{font-size:16px;font-weight:bold;color:#fff;
background:#2e7d32;padding:4px 8px;border-radius:4px;display:inline-block}
#edurec-apply-panel .edurec-apply-badge{display:inline-block;padding:1px 6px;border-radius:3px;
background:#e0e0e0;margin-right:4px}
#edurec-apply-panel .edurec-apply-warn{background:#ffe082}
#edurec-apply-panel .edurec-apply-comment{white-space:pre-wrap;background:#f4f4f4;padding:4px}
#edurec-apply-panel button{margin-top:8px;padding:6px 12px;font-size:13px}
"""


class Site(Protocol):
    """The browser surface `apply` needs; `Applier` implements it against the live site."""

    def open(self, request: Request) -> Request:
        """Reopen the request; raises `NotInQueueError` when it left the approval queue."""

    def prepare(self, decision: Decision, panel: str, dry_run: bool) -> str:
        """Pre-fill the comment box and inject the panel; returns the text entered."""

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
    for path in sorted(decisions.glob("*.yaml")):
        if path.name == APPLIED:
            continue
        decision = hydrate(Decision, yaml.safe_load(path.read_text(encoding="utf-8")))
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


def load_applied(path: Path) -> list[Applied]:
    if not path.exists():
        return []
    entries = yaml.safe_load(path.read_text(encoding="utf-8")) or []
    return [hydrate(Applied, entry) for entry in entries]


def select(
    queue: Iterable[Item],
    log: Iterable[Applied],
    request_ids: Iterable[str] = (),
    verdicts: Iterable[str] = (),
) -> list[Item]:
    """Drop requests already applied (skips are offered again) and apply the CLI filters."""
    done = {entry.request_id for entry in log if entry.action != "skip"}
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


def badge(label: str, warn: bool = False) -> str:
    classes = "edurec-apply-badge edurec-apply-warn" if warn else "edurec-apply-badge"
    return f'<span class="{classes}">{escape(label)}</span>'


def panel_html(
    item: Item,
    position: int,
    total: int,
    log: Iterable[Applied],
    dry_run: bool,
    *,
    existing: str | None = None,
) -> str:
    """The reviewer's panel; every value is HTML-escaped."""
    decision, request = item.decision, item.request
    latest: dict[str, Applied] = {}
    for entry in log:
        if entry.action != "skip":
            latest[entry.request_id] = entry
    badges = [
        badge(f"{position} of {total}"),
        badge("fresh: live page matches the export"),
        badge("Dry run: action buttons disabled, only Skip advances", warn=True) if dry_run else "",
    ]
    parts = [
        f"<style>{PANEL_CSS}</style>",
        "<h2>Course mapping decision</h2>",
        "".join(badges),
        f"<p><small>{escape(request.request_id)}</small><br>{escape(decision.course)}</p>",
        f'<p><span class="edurec-apply-verdict">{escape(decision.verdict)}</span> '
        f"overlap {decision.overlap_percentage}%, confidence {escape(decision.decision_confidence)}"
        "</p>",
    ]
    for title, values in (
        ("Overlap", decision.overlap),
        ("Missing from PU", decision.missing_from_pu),
        ("Extra in PU", decision.extra_in_pu),
        ("Concerns", decision.concerns),
    ):
        if values:
            parts.append(f"<h3>{title}</h3><ul>")
            parts.extend(f"<li>{escape(v)}</li>" for v in values)
            parts.append("</ul>")
    if decision.remap_target:
        parts.append(f"<h3>Remap target</h3><p>{escape(decision.remap_target)}</p>")
    if decision.remap_analysis:
        parts.append(f"<p>{escape(decision.remap_analysis)}</p>")
    if decision.fallback_verdict or decision.fallback_rationale:
        parts.append("<h3>Fallback if you disagree (not applied)</h3>")
        if decision.fallback_verdict:
            fallback = escape(decision.fallback_verdict)
            parts.append(f'<p><span class="edurec-apply-verdict">{fallback}</span></p>')
        if decision.fallback_rationale:
            parts.append(f"<p>{escape(decision.fallback_rationale)}</p>")
        if decision.fallback_comment:
            comment = escape(decision.fallback_comment)
            parts.append(f'<div class="edurec-apply-comment">{comment}</div>')
    if request.related_request_ids:
        parts.append("<h3>Siblings (many-to-one)</h3><ul>")
        for sibling in request.related_request_ids:
            applied = latest.get(sibling)
            status = escape(applied.action) if applied else "not yet applied"
            if applied and applied.action != decision.verdict:
                status += " " + badge("different action", warn=True)
            parts.append(f"<li><small>{escape(sibling)}</small>: {status}</li>")
        parts.append("</ul>")
    parts.append(
        "<h3>Comment (pre-filled on top)</h3>"
        f'<div class="edurec-apply-comment">{escape(decision.comment)}</div>'
    )
    if existing and existing.strip():
        parts.append(
            "<h3>Existing comment (kept below)</h3>"
            f'<div class="edurec-apply-comment">{escape(existing.strip())}</div>'
        )
    parts.append('<button type="button" id="edurec-apply-skip">Skip this request</button>')
    return "".join(parts)


def review(
    site: Site, item: Item, *, position: int, total: int, log: list[Applied], dry_run: bool
) -> Applied:
    """Show one decision to the reviewer and return what happened."""
    decision, exported = item.decision, item.request
    try:
        live = site.open(exported)
    except NotInQueueError:
        live = None
    entry = Applied(
        request_id=exported.request_id,
        verdict_recommended=decision.verdict,
        action="skip",
        comment_submitted=None,
        comment_edited=False,
        status_before=live.status if live else None,
        status_after=None,
        applied_at=now(),
        dry_run=dry_run,
    )
    if live is None:
        return replace(entry, reason="no longer in the approval queue")
    reason = stale_reason(exported, live) or comment_problem(decision.comment)
    if reason:
        return replace(entry, reason=reason)
    panel = panel_html(item, position, total, log, dry_run, existing=live.comments)
    prefilled = site.prepare(decision, panel, dry_run)
    clicked = site.await_action()
    if isinstance(clicked, Skipped):
        return replace(entry, reason="skipped by the reviewer")
    if isinstance(clicked, Left):
        return replace(entry, reason="reviewer left the page")
    verdict = BUTTONS.get(clicked.action)
    if verdict is None:
        return replace(entry, reason="Cancel pressed in EduRec")
    status_after = site.status(exported)  # Anything but None or pending verifies the submission.
    return replace(
        entry,
        action=verdict,
        comment_submitted=clicked.comment,
        comment_edited=(clicked.comment or "").strip() != prefilled.strip(),
        status_after=status_after,
        applied_at=now(),
        reason=(
            f"submission not verified: live status is {status_after!r}"
            if status_after in (None, PENDING)
            else None
        ),
    )


def apply(
    site: Site,
    run: str | Path,
    decisions: str | Path | None = None,
    *,
    request_ids: Iterable[str] = (),
    verdicts: Iterable[str] = (),
    dry_run: bool = False,
) -> list[Applied]:
    """Walk the queue with the reviewer; returns this session's log entries.

    Every entry is written to `decisions/applied.yaml` before the next request is
    opened. A submission whose status did not leave "Pending Approval" is logged
    with its verdict and a reason, then the session stops: it is never retried.
    A request that already left the approval queue is logged as a skip.
    """
    run = Path(run)
    decisions = Path(decisions) if decisions else anonymized_path(run) / DECISIONS
    queue, rejected = load_queue(run, decisions)
    for request_id, why in rejected.items():
        print(f"Not applicable {request_id}: {why}", flush=True)
    log_path = decisions / APPLIED
    log = load_applied(log_path)
    queue = select(queue, log, request_ids, verdicts)
    session: list[Applied] = []
    for position, item in enumerate(queue, 1):
        entry = review(site, item, position=position, total=len(queue), log=log, dry_run=dry_run)
        log.append(entry)
        session.append(entry)
        write_atomic(log_path, dump(plain(log)))
        detail = f" ({entry.reason})" if entry.reason else ""
        print(
            f"{position}/{len(queue)} {entry.request_id} {item.decision.course}: "
            f"{entry.action}{detail}",
            flush=True,
        )
        if entry.reason and entry.action != "skip":
            raise RuntimeError(f"{entry.request_id}: {entry.reason}")
    counts: dict[str, int] = {}
    for entry in session:
        counts[entry.action] = counts.get(entry.action, 0) + 1
    summary = ", ".join(f"{count} {action}" for action, count in sorted(counts.items()))
    print(f"Applied {len(session)} of {len(queue)} queued: {summary or 'nothing'} → {log_path}")
    return session
