"""Navigation of the Course Mapping Approval component through Playwright."""

from __future__ import annotations

import re
from collections.abc import Callable, Collection
from pathlib import Path
from typing import Literal, TypedDict
from urllib.parse import parse_qs

from bs4 import BeautifulSoup
from playwright.sync_api import BrowserContext, Frame, Locator, Response
from playwright.sync_api import Error as PlaywrightError

from .models import (
    PENDING_APPROVAL,
    Clicked,
    GridCounter,
    Listing,
    ListRow,
    Partition,
    RangeField,
    Reaction,
    Request,
    Skipped,
    Tab,
    Verdict,
)
from .parse import (
    DETAIL,
    GRID,
    NEXT,
    VIEW_ALL,
    approval_status,
    can_expand,
    parse_detail,
    parse_listing,
)

PREFIX = "N_EXSP_MOD_VW2_"
SEARCH = "PTS_CFG_CL_WRK_PTS_SRCH_BTN"
COMPONENT = (
    "https://edurec.nus.edu.sg/psp/cs90prd/EMPLOYEE/SA/c/N_STUDENT_RECORDS.N_EXSP_MOD_APPR.GBL"
)
TARGET_FRAME = 'iframe[name="TargetContent"]'
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
COMMENT_BOX = "N_EXSP_MOD_DT_N_MOD_COMMENTS$0"
CANCEL = "N_SR_EXT_STD_DW_CANCEL_PB"
BUTTONS: dict[str, Verdict] = {
    "N_SR_EXT_STD_DW_APPROVE_PB": "approve",
    "N_SR_EXT_STD_DW_REJECT_PB": "reject",
    "N_SR_EXT_STD_DW_REQUEST_BTN": "request_remapping",
    "N_SR_EXT_STD_DW_MORE_PB": "request_more_information",
}
"""The detail page's action buttons; Cancel posts `#ICList` instead of its own id."""
BACK_TO_LIST = "#ICList"
WATCHED_ACTIONS = frozenset({*BUTTONS, BACK_TO_LIST})
NOT_IN_QUEUE = "not in approval queue"
"""The live status of a request that left the approval queue."""
SKIPPED = "skipped on the panel"
CAP = 300  # Observed server cap; exactly 300 rows is treated as capped.
ROW_ACTION = re.compile(r"#ICRow\d+")
SCRIPT = Path(__file__).with_name("page.js").read_text(encoding="utf-8")
Command = Literal["settled", "signalled", "skipped", "clicked", "comment", "install"]


def run_script(frame: Frame, command: Command, **args: object) -> object:
    return frame.evaluate(SCRIPT, {"command": command, **args})


def wait_for_script(frame: Frame, command: Command, timeout: float, **args: object) -> None:
    frame.wait_for_function(SCRIPT, arg={"command": command, **args}, timeout=timeout)


class PanelSetup(TypedDict):
    panel: str
    comments: dict[Tab, str]
    """The proposal's comment per tab; the fallback only when there is one."""
    verdicts: dict[Tab, Verdict | None]
    labels: dict[Verdict, str]
    colours: dict[Verdict, str]
    dry_run: bool


class Install(TypedDict):
    panel: str
    buttons: dict[str, Verdict]
    cancel: str
    comment_box: str
    verdicts: dict[Tab, Verdict | None]
    labels: dict[Verdict, str]
    prefills: dict[Tab, str]
    colours: dict[Verdict, str]
    dry_run: bool


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


def open_component(context: BrowserContext, timeout_ms: float) -> None:
    page = context.pages[0]
    page.goto(COMPONENT, timeout=timeout_ms)
    page.frame_locator(TARGET_FRAME).locator(APPROVAL_FORM).wait_for(
        state="attached", timeout=timeout_ms
    )


def ensure_approval(context: BrowserContext, timeout_ms: float) -> None:
    """Require the approval form, opening the component when only the dashboard is up."""
    try:
        mapping_frame(context)
    except ApprovalNotLoadedError:
        open_component(context, timeout_ms)
        mapping_frame(context)


def approval_ready(context: BrowserContext) -> bool:
    try:
        mapping_frame(context)
    except RuntimeError, PlaywrightError:
        return False
    return True


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
        allowed = {SEARCH, NEXT, VIEW_ALL, BACK_TO_LIST, *(PREFIX + f + "$op" for f in FIELDS)}
        if action not in allowed and not ROW_ACTION.fullmatch(action):
            raise ValueError("Action not allowed: " + action)
        frame = self.frame()
        old_state = self.state()

        # State numbers may change before a response has updated the DOM.
        # Await the response for this exact action, then the settled page.
        with frame.page.expect_response(posted({action}), timeout=self.timeout_ms) as response:
            if trigger:
                trigger()
            else:
                frame.evaluate("a => submitAction_win0(document.win0, a)", action)
        self.settle(response.value, old_state, target)

    def settle(self, response: Response, old_state: str, target: str | None = None) -> None:
        # No `response.finished()`: the settled wait already implies it, and Playwright leaves
        # its internal task pending, to fail with "Target closed" when the browser closes.
        if response.status >= 400:
            raise RuntimeError(f"EduRec action failed: HTTP {response.status}")
        wait_for_script(self.frame(), "settled", self.timeout_ms, old=old_state, target=target)

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
            result = parse_listing(soup)
            counter = result.span()
            if len(result.rows) != counter.last - counter.first + 1 or counter.last > counter.total:
                raise RuntimeError("Result counter and rows disagree")
            result.capped = bool(cap) or counter.total >= CAP
            return result
        if "No matching values were found." in page_text:
            return Listing(rows=[], counter=GridCounter(0, 0, 0), has_next=False)
        raise RuntimeError("Neither results nor an explicit no-matches message was found")

    def next_page(self) -> Listing:
        self.transition(NEXT, target=GRID)
        return self.read_list()

    def request(self, row: ListRow) -> Request:
        self.transition(row.row_action, target=DETAIL)
        request = parse_detail(self.soup(), row.term_code)
        checks = [
            (row.student_id, request.identity.student_id),
            (row.partner_subject, request.partner_course.subject),
            (row.partner_number, request.partner_course.number),
            (row.nus_subject, request.nus_course.subject),
            (row.nus_number, request.nus_course.number),
        ]
        if any(a and a != b for a, b in checks):
            raise RuntimeError("Detail does not match the selected row")
        return request

    def back(self) -> Listing:
        self.transition(BACK_TO_LIST, target=GRID)
        return self.read_list(expand=True)

    def open(self, request: Request) -> Request:
        """Reopen an exported request by its identity; the search must yield exactly one row."""
        if self.soup().find(id=DETAIL) is not None:
            self.back()
        identity = request.identity
        found = self.search(
            Partition(
                term_low=int(identity.term_code),
                term_high=int(identity.term_code),
                student_low=identity.student_id,
                student_high=identity.student_id,
                group_low=int(identity.group),
                group_high=int(identity.group),
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
    def matches(response: Response) -> bool:
        request = response.request
        if request.method != "POST":
            return False
        action = parse_qs(request.post_data or "").get("ICAction", [])
        return len(action) == 1 and action[0] in actions

    return matches


def stack(comment: str, existing: str | None) -> str:
    below = (existing or "").strip()
    return f"{comment.strip()}\n\n{below}" if below else comment.strip()


class ReviewPage(EduRec):
    """The review panel on a detail page; the program never presses EduRec's buttons.

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

    def prepare(self, setup: PanelSetup) -> None:
        frame = self.frame()
        self.prior_comment = frame.locator(f'[id="{COMMENT_BOX}"]').input_value()
        prefills = {tab: stack(text, self.prior_comment) for tab, text in setup["comments"].items()}
        self.fill_comment(prefills["recommended"])
        self.install = Install(
            panel=setup["panel"],
            buttons=BUTTONS,
            cancel=CANCEL,
            comment_box=COMMENT_BOX,
            verdicts=setup["verdicts"],
            labels=setup["labels"],
            prefills=prefills,
            colours=setup["colours"],
            dry_run=setup["dry_run"],
        )
        run_script(frame, "install", **self.install, fresh=True)

    def fill_comment(self, value: str) -> None:
        run_script(self.frame(), "comment", id=COMMENT_BOX, value=value)

    def pending_detail(self) -> bool:
        try:
            return approval_status(self.soup()) == PENDING_APPROVAL
        except ValueError:
            return False

    def await_action(self) -> Reaction:
        frame = self.frame()
        seen: list[Response] = []
        matches = posted(WATCHED_ACTIONS)

        def on_response(response: Response) -> None:
            if matches(response):
                seen.append(response)

        frame.page.on("response", on_response)
        try:
            previous = self.state()
            while True:
                wait_for_script(frame, "signalled", 0, old=previous)
                current = self.state()  # Read before `seen`: a response precedes its DOM update.
                skipped = run_script(frame, "skipped")
                if skipped is not None:
                    self.fill_comment(self.prior_comment)
                    why = str(skipped).strip()
                    return Skipped(f"{SKIPPED}: {why}" if why else SKIPPED)
                if seen:
                    self.settle(seen[0], previous)
                    break
                if not self.pending_detail():
                    return Skipped("left the detail page")
                run_script(frame, "install", **self.install, fresh=False)
                previous = current
        finally:
            frame.page.remove_listener("response", on_response)
        form = parse_qs(seen[0].request.post_data or "")
        verdict = BUTTONS.get(form["ICAction"][0])
        hooked = run_script(frame, "clicked")
        if isinstance(hooked, dict):
            return Clicked(verdict, hooked["comment"])
        return Clicked(verdict, form.get(COMMENT_BOX, [None])[0])

    def status(self, request: Request) -> str | None:
        """The request's live status: from the open detail, or after reopening it.

        A submission that removed the request from the approval queue (Request
        Remapping, Request More Information) reports `NOT_IN_QUEUE`.
        """
        soup = self.soup()
        if soup.find(id=DETAIL) is not None:
            return approval_status(soup)
        try:
            return self.open(request).approval_status
        except NotInQueueError:
            return NOT_IN_QUEUE
