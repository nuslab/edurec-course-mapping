"""Navigation of the Course Mapping Approval component through Playwright.

`EduRec` is read-only; `Reviewer` adds what the review stage needs to assist a human
reviewer without ever pressing an EduRec action button itself.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Collection
from dataclasses import replace
from pathlib import Path
from typing import Literal, TypedDict
from urllib.parse import parse_qs

from bs4 import BeautifulSoup
from playwright.sync_api import BrowserContext, Frame, Locator, Response

from .models import (
    NOT_IN_QUEUE,
    PENDING,
    TERM_PATTERN,
    VERDICT_COLOURS,
    Clicked,
    Listing,
    ListRow,
    Partition,
    Proposal,
    RangeField,
    Reaction,
    Request,
    Skipped,
    Tab,
    Verdict,
    display,
    plain,
)
from .parse import DETAIL, GRID, NEXT, VIEW_ALL, can_expand, detail, listing

PREFIX = "N_EXSP_MOD_VW2_"
SEARCH = "PTS_CFG_CL_WRK_PTS_SRCH_BTN"
COMPONENT = (
    "https://edurec.nus.edu.sg/psp/cs90prd/EMPLOYEE/SA/c/N_STUDENT_RECORDS.N_EXSP_MOD_APPR.GBL"
)
APPROVAL_FORM = 'form[name="win0"][id="N_EXSP_MOD_APPR"]'
FIELDS = (
    "EMPLID",
    "INSTITUTION",
    "ACAD_CAREER",
    "STRM",
    "N_EXSP_CD",
    "EXT_ORG_ID",
    "TRNSFR_EQVLNCY_GRP",
    "TRNSFR_EQVLNCY_SEQ",
    "N_MOD_APPR_STATUS",
)
RANGE_FIELDS = {"STRM", "EMPLID", "TRNSFR_EQVLNCY_GRP", "TRNSFR_EQVLNCY_SEQ"}
COMMENTS = "N_EXSP_MOD_DT_N_MOD_COMMENTS$0"
CANCEL = "N_SR_EXT_STD_DW_CANCEL_PB"
BUTTONS: dict[str, Verdict] = {
    "N_SR_EXT_STD_DW_APPROVE_PB": "approve",
    "N_SR_EXT_STD_DW_REJECT_PB": "reject",
    "N_SR_EXT_STD_DW_REQUEST_BTN": "request remapping",
    "N_SR_EXT_STD_DW_MORE_PB": "request for more information",
}
"""The detail page's action buttons; Cancel posts `#ICList` instead of its own id."""
REVIEWER_ACTIONS = frozenset({*BUTTONS, "#ICList"})
SKIPPED = "skipped by the reviewer"
CAP = 300  # Observed server cap; exactly 300 rows is treated as capped.
ROW_ACTION = re.compile(r"#ICRow\d+")
SCRIPT = Path(__file__).with_name("page.js").read_text(encoding="utf-8")
Command = Literal["settled", "signalled", "skipped", "clicked", "comment", "install"]


def run(frame: Frame, command: Command, **args: object) -> object:
    """Run one of `page.js`'s commands in the frame and return its result."""
    return frame.evaluate(SCRIPT, {"command": command, **args})


def wait(frame: Frame, command: Command, timeout: float, **args: object) -> None:
    """Wait until one of `page.js`'s commands returns a truthy value."""
    frame.wait_for_function(SCRIPT, arg={"command": command, **args}, timeout=timeout)


class Install(TypedDict):
    """What `page.js` needs to draw the panel and hook the buttons; see `Reviewer`."""

    panel: str
    buttons: dict[str, str]
    """EduRec button id -> the verdict it submits, in display form."""
    cancel: str
    verdicts: dict[Tab, str | None]
    prefills: dict[Tab, str]
    colours: dict[str, str]
    dry_run: bool
    comments: str


class ApprovalNotLoadedError(RuntimeError):
    """No frame shows the Course Mapping Approval form, e.g. only the dashboard is open."""


class NotInQueueError(RuntimeError):
    """The request's identity search found no row: it left the approval queue."""


def mapping_frame(context: BrowserContext) -> Frame:
    candidates = [
        frame
        for page in context.pages
        for frame in page.frames
        if frame.locator(APPROVAL_FORM).count()
    ]
    if not candidates:
        raise ApprovalNotLoadedError(
            "Course Mapping Approval is not loaded. Open the approval component, "
            "not just the dashboard, and wait for its search form."
        )
    if len(candidates) != 1:
        raise RuntimeError(
            f"Found {len(candidates)} Course Mapping Approval frames. "
            "Close duplicate approval tabs/dialogs."
        )
    return candidates[0]


def subdivide(partition: Partition, rows: list[ListRow]) -> list[Partition]:
    if partition.term_low != partition.term_high:
        terms = sorted(
            {int(r.term_code or "") for r in rows if TERM_PATTERN.fullmatch(r.term_code or "")}
        )
        if not terms:
            raise RuntimeError("Cannot partition capped search: no valid four-digit term codes")
        return partition.split("term", terms[len(terms) // 2])
    students = sorted(
        {
            student
            for r in rows
            if (student := r.student_id)
            and (partition.student_low is None or student > partition.student_low)
            and (partition.student_high is None or student < partition.student_high)
        }
    )
    if students:
        pivot = students[len(students) // 2]
        return [replace(partition, student_high=pivot), replace(partition, student_low=pivot)]
    # A single student can also exceed the cap. The live form limits mapping
    # group and sequence to three digits; search those complete domains next.
    fields: tuple[RangeField, ...] = ("group", "sequence")
    for field in fields:
        low, high = partition.bounds(field)
        if low != high:
            return partition.split(field, (low + high) // 2)
    raise RuntimeError(
        "An indivisible search is still capped; refusing to label truncated output complete"
    )


def validate_rows(partition: Partition, rows: list[ListRow]) -> None:
    for row in rows:
        code, student = row.term_code or "", row.student_id or ""
        if (
            not TERM_PATTERN.fullmatch(code)
            or not partition.contains("term", code)
            or not student
            or (partition.student_low is not None and student < partition.student_low)
            or (partition.student_high is not None and student > partition.student_high)
        ):
            raise RuntimeError(
                "EduRec returned rows outside the requested partition; search may be stale"
            )


def list_identity(page: Listing) -> str:
    """Fingerprint a results page by its displayed values; row actions change per render."""
    return json.dumps({**plain(page), "rows": [row.cells() for row in page.rows]}, sort_keys=True)


class EduRec:
    """Drives the approval component read-only: search, paging, open and return actions."""

    def __init__(self, context: BrowserContext, timeout_ms: float = 60_000) -> None:
        self.context = context
        self.timeout_ms = timeout_ms

    def frame(self) -> Frame:
        return mapping_frame(self.context)

    def control(self, suffix: str) -> Locator:
        return self.frame().locator(f'[id="{PREFIX}{suffix}"]')

    def soup(self) -> BeautifulSoup:
        return BeautifulSoup(self.frame().content(), "html.parser")

    def state(self) -> str:
        return self.frame().locator('[id="ICStateNum"]').input_value()

    def transition(
        self, action: str, trigger: Callable[[], object] | None = None, target: str | None = None
    ) -> None:
        allowed = {SEARCH, NEXT, VIEW_ALL, "#ICList", *(PREFIX + f + "$op" for f in FIELDS)}
        if action not in allowed and not ROW_ACTION.fullmatch(action):
            raise ValueError("Action not allowed: " + action)
        frame = self.frame()
        old_state = self.state()

        # State numbers may change before a response has updated the DOM.
        # Await the response for this exact action, then the settled page.
        with frame.page.expect_response(posted({action}), timeout=self.timeout_ms) as pending:
            if trigger:
                trigger()
            else:
                frame.evaluate("a => submitAction_win0(document.win0, a)", action)
        self.settle(pending.value, old_state, target)

    def settle(self, response: Response, old_state: str, target: str | None = None) -> None:
        # No `response.finished()`: the settled wait already implies it, and Playwright leaves
        # its internal task pending, to fail with "Target closed" when the browser closes.
        if response.status >= 400:
            raise RuntimeError(f"EduRec action failed: HTTP {response.status}")
        wait(self.frame(), "settled", self.timeout_ms, old=old_state, target=target)

    def criterion(
        self, field: str, low: str | int | None = None, high: str | int | None = None
    ) -> None:
        # A missing bound denotes an unbounded string range; both missing clears.
        if low is None and high is None:
            operator, value = "=", ""
        elif low == high:
            operator, value = "=", str(low)
        elif low is None:
            operator, value = "<=", str(high)
        elif high is None:
            operator, value = ">=", str(low)
        else:
            operator, value = "between", str(low)
        select = self.control(field + "$op")
        selected_label = select.locator("option:checked").inner_text().strip()
        # Entering or leaving "between" makes PeopleSoft rebuild the form (the
        # "$to" field appears or disappears); fill only after that postback settles.
        if selected_label != operator and "between" in (operator, selected_label):
            self.transition(
                PREFIX + field + "$op",
                trigger=lambda: select.select_option(label=operator),
                target=PREFIX + field + "$to" if operator == "between" else None,
            )
        elif selected_label != operator:
            select.select_option(label=operator)
        self.control(field).fill(value)
        if operator == "between":
            self.control(field + "$to").fill(str(high))

    def search(self, partition: Partition) -> Listing:
        # Opening a detail repopulates the criteria; clear those this search leaves blank.
        for field in FIELDS:
            if field in RANGE_FIELDS:
                continue
            element = self.control(field)
            if element.evaluate("e => e.tagName") == "SELECT":
                element.select_option("")
                self.control(field + "$op").select_option(label="=")
            else:
                self.criterion(field)
        self.criterion("STRM", f"{partition.term_low:04d}", f"{partition.term_high:04d}")
        self.criterion("EMPLID", partition.student_low, partition.student_high)
        ranges: tuple[tuple[str, RangeField], ...] = (
            ("TRNSFR_EQVLNCY_GRP", "group"),
            ("TRNSFR_EQVLNCY_SEQ", "sequence"),
        )
        for field, key in ranges:
            if self.control(field).get_attribute("maxlength") != "3":
                raise RuntimeError(
                    f"Unexpected {field} domain; expected the verified three-digit field"
                )
            low, high = partition.bounds(key)
            if (low, high) == (0, 999):
                self.criterion(field)
            else:
                self.criterion(field, low, high)
        self.transition(SEARCH, trigger=lambda: self.frame().locator(f'[id="{SEARCH}"]').click())
        return self.read_list(expand=True)

    def read_list(self, expand: bool = False) -> Listing:
        soup = self.soup()
        if expand and can_expand(soup):
            # Show 100 rows per page so each detail round trip restores fewer pages.
            self.transition(VIEW_ALL, target=GRID)
            soup = self.soup()
        page_text = soup.get_text(" ", strip=True)
        cap = re.search(r"Only the first\s+([\d,]+)\s+rows can be displayed", page_text, re.I)
        if soup.find(id=GRID):
            result = listing(soup)
            start, end, total = result.span()
            if len(result.rows) != end - start + 1 or end > total:
                raise RuntimeError("Result counter and rows disagree")
            result.capped = bool(cap) or total >= CAP
            return result
        if "No matching values were found." in page_text:
            return Listing(rows=[], range=(0, 0, 0), has_next=False)
        raise RuntimeError("Neither results nor an explicit no-matches message was found")

    def next_page(self) -> Listing:
        self.transition(NEXT, target=GRID)
        return self.read_list()

    def request(self, row: ListRow) -> Request:
        self.transition(row.action, target=DETAIL)
        request = detail(self.soup())
        checks = [
            (row.student_id, request.identity.student_id),
            (row.partner_subject, request.partner_course.subject),
            (row.partner_number, request.partner_course.number),
            (row.nus_subject, request.nus_course.subject),
            (row.nus_number, request.nus_course.number),
        ]
        if any(a and a != b for a, b in checks):
            raise RuntimeError("Detail does not match the selected row")
        request.term_code = row.term_code
        return request

    def back(self) -> Listing:
        self.transition("#ICList", target=GRID)
        return self.read_list(expand=True)

    def open(self, request: Request) -> Request:
        """Reopen an exported request by its identity; the search must yield exactly one row."""
        if not request.term_code:
            raise RuntimeError(f"Request {request.request_id} has no term code to search by")
        if self.soup().find(id=DETAIL) is not None:
            self.back()
        identity = request.identity
        found = self.search(
            Partition(
                term_low=int(request.term_code),
                term_high=int(request.term_code),
                student_low=identity.student_id,
                student_high=identity.student_id,
                group_low=int(identity.mapping_number),
                group_high=int(identity.mapping_number),
                sequence_low=int(identity.sequence),
                sequence_high=int(identity.sequence),
            )
        )
        if not found.rows:
            raise NotInQueueError(
                f"Request {request.request_id} is no longer in the approval queue"
            )
        if len(found.rows) != 1:
            raise RuntimeError(
                f"Request {request.request_id} matched {len(found.rows)} rows instead of one"
            )
        return self.request(found.rows[0])


def posted(actions: Collection[str]) -> Callable[[Response], bool]:
    """Match the PeopleSoft postback whose `ICAction` is one of `actions`."""

    def matches(response: Response) -> bool:
        request = response.request
        if request.method != "POST":
            return False
        action = parse_qs(request.post_data or "").get("ICAction", [])
        return len(action) == 1 and action[0] in actions

    return matches


class Reviewer(EduRec):
    """Assists the reviewer on a detail page; the reviewer presses EduRec's own buttons.

    `prepare` injects the panel and a click hook on the action buttons (see `page.js`).
    `await_action` waits for the panel's Skip or an `ICStateNum` change. The posted
    `ICAction`, not the hook, says which button was pressed, so a postback that races
    the hook is not missed.

    PeopleSoft also re-renders the page on innocuous interactions, such as collapsing a
    section, which strips the panel and hook. While the detail is still pending they are
    re-installed and the wait resumes; once it is gone the request is skipped.
    """

    prior_comment: str = ""
    install: Install

    def prepare(self, proposal: Proposal, panel: str, dry_run: bool) -> None:
        frame = self.frame()
        self.prior_comment = frame.locator(f'[id="{COMMENTS}"]').input_value()
        prefills = proposal.prefills(self.prior_comment)
        self.comment(prefills["recommended"])
        # The hook compares and quotes verdicts in their display form only.
        verdicts: dict[Tab, str | None] = {
            "recommended": display(proposal.verdict),
            "fallback": display(proposal.fallback_verdict) if proposal.fallback_verdict else None,
        }
        self.install = Install(
            panel=panel,
            buttons={id: display(verdict) for id, verdict in BUTTONS.items()},
            cancel=CANCEL,
            verdicts=verdicts,
            prefills=prefills,
            colours={display(verdict): colour for verdict, colour in VERDICT_COLOURS.items()},
            dry_run=dry_run,
            comments=COMMENTS,
        )
        run(frame, "install", **self.install, fresh=True)

    def comment(self, value: str) -> None:
        run(self.frame(), "comment", id=COMMENTS, value=value)

    def pending_detail(self) -> bool:
        """Whether the frame still shows a detail page in "Pending Approval"."""
        try:
            return detail(self.soup()).status == PENDING
        except ValueError:
            return False

    def await_action(self) -> Reaction:
        frame = self.frame()
        seen: list[Response] = []
        matches = posted(REVIEWER_ACTIONS)

        def collect(response: Response) -> None:
            if matches(response):
                seen.append(response)

        frame.page.on("response", collect)
        try:
            previous = self.state()
            while True:
                wait(frame, "signalled", 0, old=previous)
                current = self.state()  # Read before `seen`: a response precedes its DOM update.
                skipped = run(frame, "skipped")
                if skipped is not None:
                    self.comment(self.prior_comment)
                    why = str(skipped).strip()
                    return Skipped(f"{SKIPPED}: {why}" if why else SKIPPED)
                if seen:
                    self.settle(seen[0], previous)
                    break
                if not self.pending_detail():
                    return Skipped("reviewer left the page")
                run(frame, "install", **self.install, fresh=False)
                previous = current
        finally:
            frame.page.remove_listener("response", collect)
        form = parse_qs(seen[0].request.post_data or "")
        hooked = run(frame, "clicked")
        if isinstance(hooked, dict):
            return Clicked(form["ICAction"][0], hooked["comment"])
        return Clicked(form["ICAction"][0], form.get(COMMENTS, [None])[0])

    def status(self, request: Request) -> str | None:
        """The request's live status: from the open detail, or after reopening it.

        A submission that removed the request from the approval queue (Request
        Remapping, Request More Information) reports `NOT_IN_QUEUE`.
        """
        soup = self.soup()
        if soup.find(id=DETAIL) is not None:
            return detail(soup).status
        try:
            return self.open(request).status
        except NotInQueueError:
            return NOT_IN_QUEUE
