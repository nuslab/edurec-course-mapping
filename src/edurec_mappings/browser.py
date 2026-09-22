"""Read-only navigation of the Course Mapping Approval component through Playwright."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import replace
from urllib.parse import parse_qs

from bs4 import BeautifulSoup
from playwright.sync_api import BrowserContext, Frame, Locator, Response

from .models import Listing, ListRow, Partition, RangeField, Request, as_dict
from .parse import DETAIL, GRID, NEXT, VIEW_ALL, detail, digest, expand_action, listing

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
CAP = 300  # Observed server cap; exactly 300 rows is treated as capped.
TERM_PATTERN = re.compile(r"\d{4}")
ROW_ACTION = re.compile(r"#ICRow\d+")
SETTLED = """({old, target}) => {
    const state = document.getElementById('ICStateNum');
    return state && state.value !== old &&
        !(typeof isLoaderInProcess === 'function' && isLoaderInProcess()) &&
        (!target || document.getElementById(target));
}"""


def mapping_frame(context: BrowserContext) -> Frame:
    candidates = [
        frame
        for page in context.pages
        for frame in page.frames
        if frame.locator(APPROVAL_FORM).count()
    ]
    if not candidates:
        raise RuntimeError(
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
    return digest({**as_dict(page), "rows": [row.cells() for row in page.rows]})


class EduRec:
    """Drives the approval component. Only search, paging, open and return actions exist."""

    def __init__(self, context: BrowserContext, timeout: float = 60) -> None:
        self.context = context
        self.timeout = timeout * 1000

    def frame(self) -> Frame:
        return mapping_frame(self.context)

    def control(self, suffix: str) -> Locator:
        return self.frame().locator(f'[id="{PREFIX}{suffix}"]')

    def soup(self) -> BeautifulSoup:
        return BeautifulSoup(self.frame().content(), "html.parser")

    def transition(
        self, action: str, trigger: Callable[[], object] | None = None, target: str | None = None
    ) -> None:
        allowed = {SEARCH, NEXT, VIEW_ALL, "#ICList", *(PREFIX + f + "$op" for f in FIELDS)}
        if action not in allowed and not ROW_ACTION.fullmatch(action):
            raise ValueError("Action not allowed: " + action)
        frame = self.frame()
        old_state = frame.locator('[id="ICStateNum"]').input_value()

        # State numbers may change before a response has updated the DOM.
        # Await the response for this exact action, then the settled page.
        def response_matches(response: Response) -> bool:
            request = response.request
            return request.method == "POST" and parse_qs(request.post_data or "").get(
                "ICAction"
            ) == [action]

        with frame.page.expect_response(response_matches, timeout=self.timeout) as pending:
            if trigger:
                trigger()
            else:
                frame.evaluate("a => submitAction_win0(document.win0, a)", action)
        response = pending.value
        response.finished()
        if response.status >= 400:
            raise RuntimeError(f"EduRec action failed: HTTP {response.status}")
        frame.wait_for_function(
            SETTLED, arg={"old": old_state, "target": target}, timeout=self.timeout
        )

    def criterion(self, field: str, low: object = None, high: object = None) -> None:
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
        if operator == "between" and selected_label != operator:
            self.transition(
                PREFIX + field + "$op",
                trigger=lambda: select.select_option(label=operator),
                target=PREFIX + field + "$to",
            )
        elif selected_label != operator:
            select.select_option(label=operator)
        self.control(field).fill(value)
        if operator == "between":
            self.control(field + "$to").fill(str(high))

    def search(self, partition: Partition) -> Listing:
        # Clear stale values repopulated by opening details. Blank arguments must
        # not silently retain a prior student's, institution's or status filter.
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
        if expand and expand_action(soup):
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
        request.submitted_at = row.submitted_at
        request.reassigned_to = row.reassigned_to
        return request

    def back(self) -> Listing:
        self.transition("#ICList", target=GRID)
        return self.read_list(expand=True)
