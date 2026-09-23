"""Navigation of the Course Mapping Approval component through Playwright.

`EduRec` is read-only; `Applier` adds what the apply stage needs to assist a human
reviewer without ever pressing an EduRec action button itself.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Collection
from dataclasses import replace
from urllib.parse import parse_qs

from bs4 import BeautifulSoup
from playwright.sync_api import BrowserContext, Frame, Locator, Response

from .models import (
    NOT_IN_QUEUE,
    PENDING,
    Clicked,
    Decision,
    Left,
    Listing,
    ListRow,
    Outcome,
    Partition,
    RangeField,
    Request,
    Skipped,
    Tab,
    Verdict,
    as_dict,
    display,
)
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
COMMENTS = "N_EXSP_MOD_DT_N_MOD_COMMENTS$0"
CANCEL = "N_SR_EXT_STD_DW_CANCEL_PB"
BUTTONS: dict[str, Verdict] = {
    "N_SR_EXT_STD_DW_APPROVE_PB": "approve",
    "N_SR_EXT_STD_DW_REJECT_PB": "reject",
    "N_SR_EXT_STD_DW_REQUEST_BTN": "request remapping",
    "N_SR_EXT_STD_DW_MORE_PB": "request for more information",
}
"""The detail page's action buttons; Cancel posts `#ICList` instead of its own id."""
VERDICT_COLOURS: dict[Verdict, str] = {
    "approve": "#2e7d32",
    "reject": "#c62828",
    "request remapping": "#ef6c00",
    "request for more information": "#ef6c00",
}
"""Colour of the verdict pill and of the matching EduRec button's outline."""
REVIEWER_ACTIONS = frozenset({*BUTTONS, "#ICList"})
CAP = 300  # Observed server cap; exactly 300 rows is treated as capped.
TERM_PATTERN = re.compile(r"\d{4}")
ROW_ACTION = re.compile(r"#ICRow\d+")
SETTLED = """({old, target}) => {
    const state = document.getElementById('ICStateNum');
    return state && state.value !== old &&
        !(typeof isLoaderInProcess === 'function' && isLoaderInProcess()) &&
        (!target || document.getElementById(target));
}"""
# The reviewer pressed the panel's Skip, or the state changed: an EduRec button was
# pressed, or PeopleSoft re-rendered the page on an innocuous interaction.
SIGNALLED = """(old) => {
    const apply = window.__edurecApply;
    const state = document.getElementById('ICStateNum');
    return !!(apply && apply.skipped) || !!(state && state.value !== old);
}"""
SKIPPED = (
    "() => { const a = window.__edurecApply || {}; return a.skipped ? a.state.skipReason : null; }"
)
SET_COMMENT = """({id, value}) => {
    const box = document.getElementById(id);
    if (!box) throw new Error('Comment box not found');
    box.value = value;
    for (const type of ['input', 'change']) box.dispatchEvent(new Event(type, {bubbles: true}));
}"""
INSTALL = """({panel, buttons, cancel, verdicts, prefills, colours, dryRun, comments, fresh}) => {
    const prior = window.__edurecApply;
    prior?.unhook();
    const hooks = [];
    const listen = (target, type, handler) => {
        target.addEventListener(type, handler, true);
        hooks.push([target, type, handler]);
    };
    const initial = {selected: 'recommended', viewed: 'recommended', scrollTop: 0, skipReason: ''};
    const state = !fresh && prior ? prior.state : initial;
    const apply = window.__edurecApply = {
        skipped: false, clicked: null, state,
        unhook: () => hooks.forEach(([t, type, h]) => t.removeEventListener(type, h, true)),
    };
    const box = document.getElementById(comments);
    document.getElementById('edurec-apply-panel')?.remove();
    const host = document.createElement('div');
    host.id = 'edurec-apply-panel';
    const root = host.attachShadow({mode: 'open'});
    root.innerHTML = panel;
    document.body.appendChild(host);
    const $ = selector => root.querySelector(selector);
    const $$ = selector => [...root.querySelectorAll(selector)];
    const block = event => { event.preventDefault(); event.stopImmediatePropagation(); };

    const names = {recommended: 'Recommended', fallback: 'Fallback'};
    const modified = () => !!box && box.value.trim() !== (prefills[state.selected] || '').trim();
    const mark = () => { for (const el of $$('.modified')) el.hidden = !modified(); };
    const fill = value => {
        if (!box) return;
        box.value = value;
        for (const type of ['input', 'change']) box.dispatchEvent(new Event(type, {bubbles: true}));
        mark();
    };
    // Viewing a tab shows its pane; selecting one decides the comment, outline and pill.
    const view = tab => {
        state.viewed = tab;
        for (const el of $$('.tab, .pane')) el.classList.toggle('active', el.dataset.tab === tab);
    };
    const choose = tab => {
        state.selected = tab;
        for (const el of $$('.pill[data-tab]')) {
            el.classList.toggle('active', el.dataset.tab === tab);
        }
        for (const el of $$('.pane')) el.classList.toggle('selected', el.dataset.tab === tab);
        for (const el of $$('.select')) {
            el.disabled = el.closest('.pane').dataset.tab === tab;
            el.textContent = el.disabled ? 'Selected' : 'Select';
        }
        for (const [id, verdict] of Object.entries(buttons)) {
            const button = document.getElementById(id);
            const active = verdict === verdicts[tab];
            if (button) button.style.outline = active ? `3px solid ${colours[verdict]}` : '';
        }
        mark();
    };
    const select = tab => {
        if (tab === state.selected) return;
        const question = `The comment box differs from the ${names[state.selected]} comment. ` +
            `Replace it with the ${names[tab]} comment?`;
        if (modified() && !window.confirm(question)) return;
        choose(tab);
        fill(prefills[tab]);
    };

    for (const tab of $$('.tab')) tab.onclick = () => view(tab.dataset.tab);
    for (const el of $$('.select')) el.onclick = () => select(el.closest('.pane').dataset.tab);
    for (const reset of $$('.reset')) reset.onclick = () => fill(prefills[state.selected]);
    if (box) listen(box, 'input', mark);
    const reason = $('#reason');
    reason.value = state.skipReason;
    reason.oninput = () => { state.skipReason = reason.value; };
    $('#skip').onclick = () => { apply.skipped = true; };
    choose(state.selected);
    view(state.viewed);
    const body = $('#body');  // Scroll after the tab is shown, or anchoring shifts the offset.
    body.scrollTop = state.scrollTop;
    body.onscroll = () => { state.scrollTop = body.scrollTop; };

    for (const id of [...Object.keys(buttons), cancel]) {
        const button = document.getElementById(id);
        if (!button) continue;
        if (id !== cancel) button.disabled = dryRun;
        listen(button, 'click', event => {
            const verdict = buttons[id];
            if (dryRun && verdict) {
                block(event);
                window.alert('Dry run: nothing is submitted');
                return;
            }
            if (verdict && verdict !== verdicts[state.selected]) {
                if (verdict === verdicts.fallback) {
                    const question =
                        'This matches the fallback. Switch to the fallback comment and submit?';
                    if (!window.confirm(question)) return block(event);
                    choose('fallback');
                    fill(prefills.fallback);
                } else {
                    const label =
                        state.selected === 'fallback' ? 'Fallback selected' : 'Recommended';
                    const question =
                        `${label}: ${verdicts[state.selected]}. Submit ${button.value} anyway?`;
                    if (!window.confirm(question)) return block(event);
                }
            }
            apply.clicked = {id, comment: box ? box.value : null, tab: state.selected};
        });
    }
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
    """Drives the approval component read-only: search, paging, open and return actions."""

    def __init__(self, context: BrowserContext, timeout: float = 60) -> None:
        self.context = context
        self.timeout = timeout * 1000

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
        with frame.page.expect_response(posted({action}), timeout=self.timeout) as pending:
            if trigger:
                trigger()
            else:
                frame.evaluate("a => submitAction_win0(document.win0, a)", action)
        self.settle(pending.value, old_state, target)

    def settle(self, response: Response, old_state: str, target: str | None = None) -> None:
        response.finished()
        if response.status >= 400:
            raise RuntimeError(f"EduRec action failed: HTTP {response.status}")
        self.frame().wait_for_function(
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


class NotInQueueError(RuntimeError):
    """The request's identity search found no row: it left the approval queue."""


def button_for(verdict: Verdict) -> str:
    return next(button for button, value in BUTTONS.items() if value == verdict)


def posted(actions: Collection[str]) -> Callable[[Response], bool]:
    """Match the PeopleSoft postback whose `ICAction` is one of `actions`."""

    def matches(response: Response) -> bool:
        request = response.request
        if request.method != "POST":
            return False
        action = parse_qs(request.post_data or "").get("ICAction", [])
        return len(action) == 1 and action[0] in actions

    return matches


class Applier(EduRec):
    """Assists the reviewer on a detail page; the reviewer presses EduRec's own buttons.

    How the click is detected: `prepare` injects the decision panel (in a shadow
    root, so page CSS cannot restyle it) and a capture-phase click hook on the
    five buttons. Clicking a panel tab only previews its pane; the tab whose
    "Select" button was pressed (it then reads "Selected" and is disabled)
    decides which comment is in the box and which button is outlined. The hook
    only observes: it records the button id, the comment box's value and the
    selected tab, asks for confirmation when
    the verdict differs from the selected tab's (offering to select the
    fallback when the button matches it), and in a dry run the action buttons
    are disabled and their clicks blocked outright. The selected and viewed
    tabs, the panel's scroll position and a typed skip reason live on
    `window.__edurecApply.state` and survive a re-install.
    `await_action`
    collects the page's responses and waits until either the panel's Skip flag
    is set or `ICStateNum` changes, which only a PeopleSoft postback does. The
    posted `ICAction` of the collected response, not the hook, says which button
    was pressed, so nothing is missed when the postback races the hook's report,
    and a dismissed unsaved-changes dialog on Cancel leaves the wait intact.

    PeopleSoft also re-renders the page on innocuous interactions (collapsing a
    section, sorting a grid, tabbing out of a changed field), which strips the
    panel, the hook and the dry-run state. A state change without a recognised
    post is therefore not an error: while the detail still shows as pending the
    panel and hook are re-installed (the comment box keeps its current text) and
    the wait resumes; once the detail is gone the outcome is `Left`.
    """

    prior_comment: str = ""
    install: dict[str, object]

    def prepare(self, decision: Decision, panel: str, dry_run: bool) -> dict[Tab, str]:
        frame = self.frame()
        self.prior_comment = frame.locator(f'[id="{COMMENTS}"]').input_value()
        prefills = decision.prefills(self.prior_comment)
        self.comment(prefills["recommended"])
        # The hook compares and quotes verdicts in their display form only.
        verdicts = {
            "recommended": display(decision.verdict),
            "fallback": display(decision.fallback_verdict) if decision.fallback_verdict else None,
        }
        self.install = {
            "panel": panel,
            "buttons": {id: display(verdict) for id, verdict in BUTTONS.items()},
            "cancel": CANCEL,
            "verdicts": verdicts,
            "prefills": prefills,
            "colours": {display(verdict): colour for verdict, colour in VERDICT_COLOURS.items()},
            "dryRun": dry_run,
            "comments": COMMENTS,
            "fresh": False,
        }
        frame.evaluate(INSTALL, {**self.install, "fresh": True})
        return prefills

    def comment(self, value: str) -> None:
        # No inline onchange: dispatch the events PeopleSoft's delegated handlers listen for.
        self.frame().evaluate(SET_COMMENT, {"id": COMMENTS, "value": value})

    def pending_detail(self) -> bool:
        """Whether the frame still shows a detail page in "Pending Approval"."""
        try:
            return detail(self.soup()).status == PENDING
        except ValueError:
            return False

    def await_action(self) -> Outcome:
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
                frame.wait_for_function(SIGNALLED, arg=previous, timeout=0)
                current = self.state()  # Read before `seen`: a response precedes its DOM update.
                skipped = frame.evaluate(SKIPPED)
                if skipped is not None:
                    self.comment(self.prior_comment)
                    return Skipped(str(skipped))
                if seen:
                    self.settle(seen[0], previous)
                    break
                if not self.pending_detail():
                    return Left()
                frame.evaluate(INSTALL, self.install)
                previous = current
        finally:
            frame.page.remove_listener("response", collect)
        form = parse_qs(seen[0].request.post_data or "")
        hooked = frame.evaluate("() => (window.__edurecApply || {}).clicked")
        if isinstance(hooked, dict):
            return Clicked(form["ICAction"][0], hooked["comment"], hooked["tab"])
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
