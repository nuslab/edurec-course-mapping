import unittest
from collections.abc import Callable
from dataclasses import replace
from typing import TypedDict
from unittest import mock

from playwright.sync_api import Dialog, sync_playwright
from playwright.sync_api import Error as PlaywrightError

from edurec_mappings import edurec
from edurec_mappings.edurec import (
    BUTTONS,
    CANCEL,
    COMMENT_BOX,
    SKIPPED,
    ApprovalNotLoadedError,
    EduRec,
    PanelSetup,
    ReviewPage,
    stack,
)
from edurec_mappings.models import PENDING_APPROVAL, Clicked, Skipped, Verdict
from edurec_mappings.parse import DETAIL
from edurec_mappings.review import VERDICT_LABELS, QueueItem, panel_html, panel_setup
from edurec_mappings.store import Version
from tests.test_export import records
from tests.test_parse import fixture, tag
from tests.test_review import FALLBACK, PROGRESS, with_fallback

NOT_LOADED = ApprovalNotLoadedError("Course Mapping Approval is not loaded.")


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


class ApprovalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.frame = self.patch("mapping_frame")
        self.open_component = self.patch("open_component")
        self.context = mock.MagicMock()

    def patch(self, name: str) -> mock.MagicMock:
        patcher = mock.patch.object(edurec, name)
        self.addCleanup(patcher.stop)
        started: mock.MagicMock = patcher.start()
        return started

    def test_form_present_does_not_navigate(self) -> None:
        edurec.ensure_approval(self.context, 60000)
        self.open_component.assert_not_called()

    def test_opens_the_component_when_only_the_dashboard_is_up(self) -> None:
        self.frame.side_effect = [NOT_LOADED, None]
        edurec.ensure_approval(self.context, 60000)
        self.open_component.assert_called_once_with(self.context, 60000)
        self.assertEqual(self.frame.call_count, 2)

    def test_other_frame_errors_propagate(self) -> None:
        self.frame.side_effect = RuntimeError("Found 2 Course Mapping Approval frames.")
        with self.assertRaises(RuntimeError):
            edurec.ensure_approval(self.context, 60000)
        self.open_component.assert_not_called()

    def test_ready_only_with_exactly_one_form(self) -> None:
        self.assertTrue(edurec.approval_ready(self.context))
        for error in (NOT_LOADED, RuntimeError("duplicates"), PlaywrightError("closed")):
            self.frame.side_effect = error
            self.assertFalse(edurec.approval_ready(self.context))


class EduRecTests(unittest.TestCase):
    def test_reusing_between_operator_does_not_trigger_form_rebuild(self) -> None:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            context = browser.new_context()
            page = context.new_page()
            page.set_content("""<form name="win0" id="N_EXSP_MOD_APPR">
                <select id="N_EXSP_MOD_VW2_STRM$op" onchange="window.rebuilds++">
                  <option value="2">=</option><option value="9" selected>between</option>
                </select>
                <input id="N_EXSP_MOD_VW2_STRM"><input id="N_EXSP_MOD_VW2_STRM$to">
                </form><script>window.rebuilds = 0;</script>""")
            site = EduRec(context)
            site.criterion("STRM", "0000", "9999")
            site.criterion("STRM", "0000", "2619")
            self.assertEqual(page.evaluate("window.rebuilds"), 0)
            self.assertEqual(site.control("STRM").input_value(), "0000")
            self.assertEqual(site.control("STRM$to").input_value(), "2619")
            browser.close()

    def test_leaving_between_operator_waits_for_form_rebuild(self) -> None:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            context = browser.new_context()
            page = context.new_page()
            page.set_content("""<form name="win0" id="N_EXSP_MOD_APPR">
                <select id="N_EXSP_MOD_VW2_EMPLID$op">
                  <option value="2">=</option><option value="5">&gt;=</option>
                  <option value="9" selected>between</option>
                </select>
                <input id="N_EXSP_MOD_VW2_EMPLID"><input id="N_EXSP_MOD_VW2_EMPLID$to">
                </form>""")
            site = EduRec(context)
            transitions = []

            def fake_transition(
                action: str,
                trigger: Callable[[], object] | None = None,
                target: str | None = None,
            ) -> None:
                transitions.append((action, target))
                if trigger:
                    trigger()

            with mock.patch.object(site, "transition", side_effect=fake_transition):
                site.criterion("EMPLID", "A0000500X", None)
            self.assertEqual(transitions, [("N_EXSP_MOD_VW2_EMPLID$op", None)])
            self.assertEqual(site.control("EMPLID").input_value(), "A0000500X")
            browser.close()

    def test_search_switches_grid_to_view_100(self) -> None:
        listing = fixture("main.html")
        tag(listing, "PTS_CFG_CL_STD_RSL$hviewall$0", "a").string = "View 100"
        for script in listing.find_all("script"):
            script.decompose()
        with sync_playwright() as p:
            browser = p.chromium.launch()
            context = browser.new_context()
            page = context.new_page()
            page.route(
                "**/*",
                lambda route: route.fulfill(
                    body="<html><body>"
                    + str(listing.find("form"))
                    + """
                <script>
                window.isLoaderInProcess = () => false;
                window.toggles = 0;
                window.submitAction_win0 = (_, action) => {
                    if (action !== 'PTS_CFG_CL_STD_RSL$hviewall$0') throw new Error(action);
                    const body = 'ICAction=' + encodeURIComponent(action);
                    fetch('/toggle', {method: 'POST', body}).then(() => {
                        window.toggles++;
                        const link = document.getElementById('PTS_CFG_CL_STD_RSL$hviewall$0');
                        link.textContent = 'View 10';
                        document.getElementById('ICStateNum').value = 'next';
                    });
                };
                </script></body></html>""",
                    content_type="text/html",
                ),
            )
            page.goto("https://local.test/approval")
            site = EduRec(context)
            first = site.read_list(expand=True)
            self.assertEqual(page.evaluate("window.toggles"), 1)
            self.assertEqual(len(first.rows), 100)
            site.read_list(expand=True)
            self.assertEqual(
                page.evaluate("window.toggles"), 1, "No toggle once View 10 is offered"
            )
            browser.close()


class ReviewPageTests(unittest.TestCase):
    def test_proposal_comment_goes_on_top_of_existing_text(self) -> None:
        self.assertEqual(stack(" New. ", None), "New.")
        self.assertEqual(stack("New.", "  "), "New.")
        self.assertEqual(stack("New.", "older note\n"), "New.\n\nolder note")

    def test_injected_hook_reports_skip_confirm_click_and_cancel(self) -> None:
        buttons = "".join(
            f'<input type="button" id="{i}" value="{label}" '
            'onclick="submitAction_win0(document.win0,this.id,event);">'
            for i, label in [
                *((i, VERDICT_LABELS[v]) for i, v in BUTTONS.items()),
                (CANCEL, "Cancel"),
            ]
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
                ("N_EXSP_MOD_DT_N_MOD_APPR_STATUS$0", PENDING_APPROVAL),
            ]
        )
        page_html = f"""<html><body><form name="win0" id="N_EXSP_MOD_APPR">
            <input id="ICStateNum" value="1">{fields}
            <textarea id="{COMMENT_BOX}">prior</textarea>{buttons}
            </form><style>button, div {{ color: red !important; }}</style><script>
            window.changes = 0;
            document.addEventListener('change', () => window.changes++);
            window.isLoaderInProcess = () => false;
            window.submitAction_win0 = (_, action) => {{
                if (action === '{CANCEL}') action = '#ICList';
                const box = document.getElementById('{COMMENT_BOX}');
                const body = 'ICAction=' + encodeURIComponent(action) +
                    '&' + encodeURIComponent('{COMMENT_BOX}') + '=' + encodeURIComponent(box.value);
                fetch('/post', {{method: 'POST', body,
                    headers: {{'content-type': 'application/x-www-form-urlencoded'}}}})
                    .then(() => {{ document.getElementById('ICStateNum').value += 'x'; }});
            }};
            </script></body></html>"""
        _, request = records()[0]
        # Enough overlap lines to make the panel body scroll in a 720px-high viewport.
        advice = replace(with_fallback(request), overlap_topics=[f"Topic {n}" for n in range(80)])
        item = QueueItem(advice, request, Version(request, "hash"))
        panel = panel_html(item, PROGRESS, {}, dry_run=False)
        approve, reject = button_for("approve"), button_for("reject")
        remap = button_for("request_remapping")
        recommended = stack(advice.comment, "prior")
        fallback = stack(FALLBACK.comment, "prior")

        def setup(dry_run: bool) -> PanelSetup:
            return panel_setup(advice, panel, dry_run)

        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_context().new_page()
            page.route(
                "**/*", lambda route: route.fulfill(body=page_html, content_type="text/html")
            )
            page.goto("https://local.test/detail")
            site = ReviewPage(page.context)
            box = page.locator(f'[id="{COMMENT_BOX}"]')
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
                    f"""() => {{ const box = document.getElementById('{COMMENT_BOX}');
                    box.value = {value!r}; box.dispatchEvent(new Event('input')); }}"""
                )

            site.prepare(setup(False))
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

            # Modified marker and Reset.
            set_box("typed")
            self.assertEqual(panel_state()["modified"], ["recommended"])
            page.evaluate(f"{in_panel('.pane[data-tab=fallback] .reset')}.click()")
            self.assertEqual(box.input_value(), recommended)
            self.assertEqual(panel_state()["modified"], [])

            # A tab click only previews.
            page.evaluate(f"{in_panel('.tab[data-tab=fallback]')}.click()")
            self.assertEqual(messages, [])
            self.assertEqual(
                (panel_state()["viewed"], panel_state()["selected"]), ("fallback", "recommended")
            )
            self.assertEqual(box.input_value(), recommended)
            self.assertEqual(panel_state()["pill"], "Approve")
            self.assertEqual(outlined(), {approve})
            self.assertEqual(panel_state()["markers"], markers)

            # Select.
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

            # Select over an edited box.
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

            # State survives a re-render.
            rerender = f"""
                const form = document.getElementById('N_EXSP_MOD_APPR');
                const value = document.getElementById('{COMMENT_BOX}').value;
                const state = document.getElementById('ICStateNum').value;
                document.getElementById('edurec-review-panel').remove();
                form.innerHTML = form.innerHTML;
                document.getElementById('{COMMENT_BOX}').value = value;
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
            later(f"document.getElementById('{COMMENT_BOX}').value = 'kept'; " + rerender)
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

            # A fresh prepare resets it.
            site.prepare(setup(True))
            state = panel_state()
            self.assertEqual(
                (state["selected"], state["viewed"], state["reason"], state["scrollTop"]),
                ("recommended", "recommended", "", 0),
            )
            self.assertTrue(all(page.locator(f"#{b}").is_disabled() for b in BUTTONS))
            self.assertFalse(page.locator(f"#{CANCEL}").is_disabled())
            later(f"{in_panel('#skip')}.click()")
            self.assertEqual(site.await_action(), Skipped(SKIPPED))

            # Dry-run state survives a re-render.
            site.prepare(setup(True))
            later(f"document.getElementById('{COMMENT_BOX}').value = 'kept'; " + rerender)
            later(
                f"""window.restored = {{
                    panels: document.querySelectorAll('#edurec-review-panel').length,
                    disabled: [...'{",".join(BUTTONS)}'.split(',')].map(
                        id => document.getElementById(id).disabled),
                    value: document.getElementById('{COMMENT_BOX}').value}};""",
                300,
            )
            later(f"{in_panel('#skip')}.click()", 500)
            self.assertEqual(site.await_action(), Skipped(SKIPPED))
            self.assertEqual(
                page.evaluate("window.restored"),
                {"panels": 1, "disabled": [True] * len(BUTTONS), "value": "kept"},
            )

            site.prepare(setup(False))
            later(f"document.getElementById('{DETAIL}').remove(); " + rerender)
            self.assertEqual(site.await_action(), Skipped("left the detail page"))
            page.evaluate(f"document.body.insertAdjacentHTML('beforeend', '{fields}')")

            site.prepare(setup(True))
            # A stale page may have lost `disabled` but keep the hook: the click is still blocked.
            later(f"document.getElementById('{approve}').disabled = false; " + press(approve))
            later(f"{in_panel('#skip')}.click()", 300)
            self.assertEqual(site.await_action(), Skipped(SKIPPED))
            self.assertEqual(messages, ["Dry run: nothing is submitted"])
            self.assertEqual(posts, [])

            # A live run leaves each button as EduRec rendered it, disabled ones included.
            ids = ",".join(BUTTONS)
            page.evaluate(
                f"'{ids}'.split(',').forEach(id => "
                f"document.getElementById(id).disabled = id === '{reject}')"
            )
            site.prepare(setup(False))
            self.assertTrue(page.locator(f"#{reject}").is_disabled())
            self.assertFalse(page.locator(f"#{approve}").is_disabled())
            later(f"{in_panel('#skip')}.click()")
            self.assertEqual(site.await_action(), Skipped(SKIPPED))
            page.evaluate(f"document.getElementById('{reject}').disabled = false")

            messages.clear()
            site.prepare(setup(False))
            later(press(reject))
            later(f"{in_panel('#skip')}.click()", 300)
            self.assertEqual(
                site.await_action(), Skipped(SKIPPED), "A dismissed confirm aborts the click"
            )
            self.assertEqual(messages, ["Recommended: Approve. Submit Reject anyway?"])
            self.assertEqual(posts, [])

            # The fallback's button offers to switch; declined.
            messages.clear()
            site.prepare(setup(False))
            later(press(remap))
            later(f"{in_panel('#skip')}.click()", 300)
            self.assertEqual(site.await_action(), Skipped(SKIPPED))
            self.assertEqual(
                messages, ["This matches the fallback. Switch to the fallback comment and submit?"]
            )
            self.assertEqual(posts, [])

            # Accepted.
            messages.clear()
            set_box("prior")
            site.prepare(setup(False))
            answers.append(True)
            later(press(remap))
            self.assertEqual(site.await_action(), Clicked("request_remapping", fallback))
            self.assertEqual(len(posts), 1)
            self.assertEqual(box.input_value(), fallback)
            self.assertEqual(panel_state()["selected"], "fallback")
            self.assertEqual(panel_state()["pill"], "Request Remapping")

            # With the fallback selected, the other button asks.
            messages.clear()
            site.prepare(setup(False))
            page.evaluate(f"{in_panel('.pane[data-tab=fallback] .select')}.click()")
            later(press(approve))
            later(f"{in_panel('#skip')}.click()", 300)
            self.assertEqual(site.await_action(), Skipped(SKIPPED))
            self.assertEqual(
                messages, ["Fallback selected: Request Remapping. Submit Approve anyway?"]
            )

            # Viewing the fallback is not selecting it.
            messages.clear()
            set_box("prior")
            site.prepare(setup(False))
            page.evaluate(f"{in_panel('.tab[data-tab=fallback]')}.click()")
            later(press(approve))
            self.assertEqual(site.await_action(), Clicked("approve", recommended))
            self.assertEqual(messages, [])

            site.prepare(setup(False))
            later(f"document.getElementById('{COMMENT_BOX}').value = 'edited'; " + press(approve))
            self.assertEqual(site.await_action(), Clicked("approve", "edited"))

            site.prepare(setup(False))
            later(press(CANCEL))
            # The mock page never navigates, so the box still holds the edited text.
            self.assertEqual(
                site.await_action(),
                Clicked(None, advice.comment + "\n\nedited"),
            )
            browser.close()
