import copy
import sqlite3
import tempfile
import unittest
from pathlib import Path

import yaml
from playwright.sync_api import sync_playwright

from edurec_mappings.anonymize import anonymize
from edurec_mappings.apply import (
    APPLIED,
    Item,
    apply,
    comment_problem,
    load_applied,
    load_queue,
    panel_html,
    select,
    stale_reason,
)
from edurec_mappings.browser import BUTTONS, CANCEL, COMMENTS, Applier, NotInQueueError, button_for
from edurec_mappings.cli import DOWNLOAD_TABLES, forget_downloads, main, parse_apply_args
from edurec_mappings.models import (
    NOT_IN_QUEUE,
    PENDING,
    Applied,
    Clicked,
    Decision,
    Left,
    Request,
    Skipped,
)
from edurec_mappings.parse import DETAIL, document, dump, hydrate, save
from tests.test_extract import records

STARTED = "2026-09-22T10:00:00+00:00"


def decision(request, verdict="approve", **overrides):
    values = {
        "source_export": "anon",
        "source_started_at": STARTED,
        "request_id": request.request_id,
        "course": "CS 1 (PU) -> CS3243",
        "verdict": verdict,
        "comment": "Approved: the syllabus covers search & <planning>.",
        "overlap_percentage": 85,
        "decision_confidence": "high",
        "overlap": ["Search"],
        "missing_from_pu": ["Planning"],
        "concerns": ["Weight of exam < 50%"],
    }
    return Decision(**{**values, **overrides})


def make_run(directory, count=4):
    """An export of `count` requests, its anonymized copy and a decision for each."""
    run, anon = Path(directory) / "run", Path(directory) / "run-anonymized"
    requests = [copy.deepcopy(r) for _, r in records()[:count]]
    if count > 1:  # The first two become parts of one many-to-one mapping.
        requests[1].group_id = requests[0].group_id
        requests[0].mapping_type = requests[1].mapping_type = "Many to One"
    data = document()
    data.collection.started_at = STARTED
    data.requests = requests
    save(data, run)
    save(anonymize(data), anon)
    for request in requests:
        path = anon / "decisions" / f"{request.request_id}.yaml"
        path.parent.mkdir(exist_ok=True)
        path.write_text(dump(decision(request).to_dict()))
    return run, anon / "decisions", requests


class FakeSite:
    """Scripted reviewer: `outcomes` maps request_id to what the human does."""

    def __init__(self, outcomes, live=None, status_after="Approved"):
        self.outcomes = outcomes
        self.live = live or {}
        self.status_after = status_after
        self.opened = []
        self.prepared = []
        self.current = ""

    def open(self, request):
        self.opened.append(request.request_id)
        live = self.live.get(request.request_id, request)
        if isinstance(live, Exception):
            raise live
        live = copy.deepcopy(live)
        live.status = live.status or PENDING
        self.current = live.request_id
        return live

    def prepare(self, decision, panel, dry_run):
        self.prepared.append((decision.request_id, panel, dry_run))
        live = self.live.get(self.current)
        return decision.prefill(live.comments if live else None)

    def await_action(self):
        outcome = self.outcomes[self.current]
        return outcome() if callable(outcome) else outcome

    def status(self, request):
        return self.status_after


class ApplyTests(unittest.TestCase):
    def test_hydrate_is_the_inverse_of_plain(self):
        _, request = records()[0]
        loaded = hydrate(Request, yaml.safe_load(dump(request.to_dict())))
        self.assertEqual(loaded, request)
        self.assertEqual(hydrate(Decision, decision(request).to_dict()), decision(request))
        with self.assertRaises(ValueError):
            hydrate(Decision, ["not", "a", "record"])

    def test_decisions_from_another_export_or_unknown_request_are_skipped(self):
        with tempfile.TemporaryDirectory() as directory:
            run, decisions, requests = make_run(directory)
            stale = decisions / f"{requests[0].request_id}.yaml"
            stale.write_text(dump(decision(requests[0], source_started_at="older").to_dict()))
            orphan = copy.deepcopy(requests[1])
            orphan.request_id = "0" * 24
            (decisions / "000000000000000000000000.yaml").write_text(
                dump(decision(orphan).to_dict())
            )
            queue, rejected = load_queue(run, decisions)
            self.assertEqual(
                [item.request.request_id for item in queue],
                [r.request_id for r in requests[1:]],
            )
            self.assertEqual(sorted(rejected), [orphan.request_id, requests[0].request_id])
            self.assertIn("older", rejected[requests[0].request_id])
            self.assertIn("no requests/", rejected[orphan.request_id])
            self.assertEqual(queue[0].request.identity.student_id, requests[1].identity.student_id)

    def test_mismatched_run_and_decisions_are_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            run, decisions, _ = make_run(directory)
            inventory = yaml.safe_load((run / "inventory.yaml").read_text())
            inventory["collection"]["started_at"] = "other"
            (run / "inventory.yaml").write_text(dump(inventory))
            with self.assertRaisesRegex(RuntimeError, "different export"):
                load_queue(run, decisions)

    def test_queue_keeps_siblings_together_and_honours_filters(self):
        with tempfile.TemporaryDirectory() as directory:
            run, decisions, requests = make_run(directory)
            path = decisions / f"{requests[1].request_id}.yaml"
            path.write_text(dump(decision(requests[1], verdict="reject").to_dict()))
            queue, _ = load_queue(run, decisions)
            ids = [item.request.request_id for item in queue]
            sibling_positions = [
                ids.index(requests[0].request_id),
                ids.index(requests[1].request_id),
            ]
            self.assertEqual(abs(sibling_positions[0] - sibling_positions[1]), 1)
            self.assertEqual(set(ids), {r.request_id for r in requests})
            log = [
                Applied(
                    request_id=requests[2].request_id,
                    verdict_recommended="approve",
                    action="approve",
                    comment_submitted="x",
                    comment_edited=False,
                    status_before=PENDING,
                    status_after="Approved",
                    applied_at=STARTED,
                    dry_run=False,
                ),
                Applied(
                    request_id=requests[3].request_id,
                    verdict_recommended="approve",
                    action="skip",
                    comment_submitted=None,
                    comment_edited=False,
                    status_before=PENDING,
                    status_after=None,
                    applied_at=STARTED,
                    dry_run=False,
                    reason="reviewer skipped",
                ),
            ]
            remaining = select(queue, log)
            self.assertEqual(
                {item.request.request_id for item in remaining},
                {r.request_id for r in (requests[0], requests[1], requests[3])},
                "Applied requests are skipped; skipped ones are offered again",
            )
            self.assertEqual(
                [i.request.request_id for i in select(queue, [], verdicts=["reject"])],
                [requests[1].request_id],
            )
            self.assertEqual(
                [i.request.request_id for i in select(queue, [], request_ids=[ids[2]])], [ids[2]]
            )

    def test_applied_log_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / APPLIED
            self.assertEqual(load_applied(path), [])
            entry = Applied(
                request_id="abc",
                verdict_recommended="reject",
                action="skip",
                comment_submitted=None,
                comment_edited=False,
                status_before=PENDING,
                status_after=None,
                applied_at=STARTED,
                dry_run=True,
                reason="comment is empty",
            )
            path.write_text(dump([entry.to_dict()]))
            self.assertEqual(load_applied(path), [entry])

    def test_freshness_comparison(self):
        _, exported = records()[0]
        live = copy.deepcopy(exported)
        live.status = PENDING
        self.assertIsNone(stale_reason(exported, live))
        changed = copy.deepcopy(live)
        changed.status = "Approved"
        self.assertIn("Approved", stale_reason(exported, changed) or "")
        changed = copy.deepcopy(live)
        changed.nus_course.number = "9999"
        self.assertIn("nus_course.number", stale_reason(exported, changed) or "")
        changed = copy.deepcopy(live)
        changed.identity.sequence = "7"
        self.assertIn("sequence", stale_reason(exported, changed) or "")

    def test_comment_sanity(self):
        self.assertIsNone(comment_problem("Consider remapping to CS5242."))
        self.assertIn("empty", comment_problem("  ") or "")
        self.assertIn("[", comment_problem("Fill in [course]") or "")
        self.assertIn("XXXX", comment_problem("Consider remapping to CSXXXX.") or "")

    def test_verdict_button_mapping(self):
        self.assertEqual(len(BUTTONS), 4)
        for button, verdict in BUTTONS.items():
            self.assertEqual(button_for(verdict), button)
        self.assertEqual(BUTTONS["N_SR_EXT_STD_DW_APPROVE_PB"], "approve")
        self.assertEqual(BUTTONS["N_SR_EXT_STD_DW_REQUEST_BTN"], "request remapping")
        self.assertNotIn(CANCEL, BUTTONS)

    def test_panel_html_shows_the_decision_and_escapes_content(self):
        with tempfile.TemporaryDirectory() as directory:
            run, decisions, requests = make_run(directory)
            queue, _ = load_queue(run, decisions)
            item = next(i for i in queue if i.request.request_id == requests[0].request_id)
            item.decision.remap_target = "CS5242"
            item.decision.remap_analysis = "Better fit <b>"
            item.decision.fallback_verdict = "request remapping"
            item.decision.fallback_comment = "Planning is missing. Consider remapping to CS5242."
            item.decision.fallback_rationale = "Overlap is close to the 70% <threshold>."
            sibling = requests[1].request_id
            log = [
                Applied(
                    request_id=sibling,
                    verdict_recommended="approve",
                    action="reject",
                    comment_submitted="x",
                    comment_edited=True,
                    status_before=PENDING,
                    status_after="Rejected",
                    applied_at=STARTED,
                    dry_run=False,
                )
            ]
            html = panel_html(item, 3, 12, log, dry_run=True)
            for expected in (
                requests[0].request_id,
                "CS 1 (PU) -&gt; CS3243",
                "3 of 12",
                "approve",
                "85%",
                "high",
                "Search",
                "Planning",
                "Weight of exam &lt; 50%",
                "CS5242",
                "Better fit &lt;b&gt;",
                "Fallback if you disagree",
                "request remapping",
                "Planning is missing. Consider remapping to CS5242.",
                "70% &lt;threshold&gt;",
                "search &amp; &lt;planning&gt;",
                sibling,
                "reject",
                "Dry run",
                "different action",
                'id="edurec-apply-skip"',
                "fresh",
            ):
                self.assertIn(expected, html)
            self.assertNotIn("<b>", html)
            self.assertNotIn("<planning>", html)
            self.assertNotIn("Dry run", panel_html(item, 1, 1, [], dry_run=False))

    def test_loop_logs_every_outcome_and_stops_when_unverified(self):
        with tempfile.TemporaryDirectory() as directory:
            run, decisions, requests = make_run(directory)
            approve, reject = button_for("approve"), button_for("reject")
            ids = [r.request_id for r in requests]
            edited = "Approved: the syllabus covers search & <planning>. Also fine."
            stale = copy.deepcopy(requests[3])
            stale.status = "Approved"
            site = FakeSite(
                {
                    ids[0]: Clicked(approve, decision(requests[0]).comment),
                    ids[1]: Clicked(reject, edited),
                    ids[2]: Skipped(),
                },
                live={ids[3]: stale},
            )
            log = {e.request_id: e for e in apply(site, run, decisions)}
            self.assertEqual(len(site.prepared), 3, "The stale request never gets a panel")
            self.assertEqual([log[i].action for i in ids], ["approve", "reject", "skip", "skip"])
            self.assertEqual(log[ids[0]].comment_edited, False)
            self.assertEqual(log[ids[1]].comment_edited, True)
            self.assertEqual(log[ids[1]].comment_submitted, edited)
            self.assertEqual(
                (log[ids[0]].status_before, log[ids[0]].status_after), (PENDING, "Approved")
            )
            self.assertIn("reviewer", log[ids[2]].reason or "")
            self.assertIn("Approved", log[ids[3]].reason or "")
            self.assertEqual({e.request_id: e for e in load_applied(decisions / APPLIED)}, log)

            # A second session offers only the skipped ones and records a cancel.
            site = FakeSite({ids[2]: Clicked("#ICList", None), ids[3]: Skipped()})
            second = {e.request_id: e for e in apply(site, run, decisions)}
            self.assertEqual(sorted(site.opened), sorted(ids[2:]))
            self.assertEqual({e.action for e in second.values()}, {"skip"})
            self.assertIn("Cancel", second[ids[2]].reason or "")
            self.assertEqual(len(load_applied(decisions / APPLIED)), 6)

    def test_dry_run_and_unverified_submission(self):
        with tempfile.TemporaryDirectory() as directory:
            run, decisions, requests = make_run(directory, count=2)
            ids = [r.request_id for r in requests]
            site = FakeSite({ids[0]: Skipped(), ids[1]: Skipped()})
            log = apply(site, run, decisions, dry_run=True)
            self.assertTrue(all(e.dry_run and e.action == "skip" for e in log))
            self.assertTrue(all(dry for _, _, dry in site.prepared))
            outcomes = {i: Clicked(button_for("approve"), "c") for i in ids}
            site = FakeSite(outcomes, status_after=PENDING)
            with self.assertRaisesRegex(RuntimeError, "not verified"):
                apply(site, run, decisions)
            entries = load_applied(decisions / APPLIED)
            self.assertEqual(entries[-1].action, "approve")
            self.assertIn("not verified", entries[-1].reason or "")
            self.assertEqual(len(entries), 3)

    def test_leaving_the_page_and_a_vanished_request_are_logged_as_skips(self):
        with tempfile.TemporaryDirectory() as directory:
            run, decisions, requests = make_run(directory, count=3)
            ids = [r.request_id for r in requests]
            site = FakeSite(
                {ids[0]: Left(), ids[2]: Skipped()},
                live={ids[1]: NotInQueueError("gone")},
            )
            log = {e.request_id: e for e in apply(site, run, decisions)}
            self.assertEqual(
                sorted(site.opened), sorted(ids), "The loop continues past a vanished request"
            )
            self.assertEqual([log[i].action for i in ids], ["skip"] * 3)
            self.assertEqual(log[ids[0]].reason, "reviewer left the page")
            self.assertEqual(log[ids[1]].reason, "no longer in the approval queue")
            self.assertIsNone(log[ids[1]].status_before)
            self.assertEqual(sorted(r for r, _, _ in site.prepared), sorted([ids[0], ids[2]]))
            with self.assertRaisesRegex(RuntimeError, "boom"):
                apply(FakeSite({}, live=dict.fromkeys(ids, RuntimeError("boom"))), run, decisions)

    def test_request_that_left_the_queue_counts_as_verified(self):
        with tempfile.TemporaryDirectory() as directory:
            run, decisions, requests = make_run(directory, count=1)
            remap = button_for("request remapping")
            site = FakeSite(
                {requests[0].request_id: Clicked(remap, "c")}, status_after=NOT_IN_QUEUE
            )
            (entry,) = apply(site, run, decisions)
            self.assertEqual(entry.action, "request remapping")
            self.assertEqual(entry.status_after, NOT_IN_QUEUE)
            self.assertIsNone(entry.reason)

    def test_bad_comment_is_never_prefilled(self):
        with tempfile.TemporaryDirectory() as directory:
            run, decisions, requests = make_run(directory, count=1)
            path = decisions / f"{requests[0].request_id}.yaml"
            path.write_text(dump(decision(requests[0], comment="Use [code]").to_dict()))
            site = FakeSite({})
            log = apply(site, run, decisions)
            self.assertEqual(site.prepared, [])
            self.assertEqual(log[0].action, "skip")
            self.assertIn("[", log[0].reason or "")

    def test_injected_hook_reports_skip_confirm_click_and_cancel(self):
        buttons = "".join(
            f'<input type="button" id="{i}" value="{v.title()}" '
            'onclick="submitAction_win0(document.win0,this.id,event);">'
            for i, v in [*BUTTONS.items(), (CANCEL, "Cancel")]
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
                ("N_EXSP_MOD_DT_N_MOD_APPR_STATUS$0", PENDING),
            ]
        )
        page_html = f"""<html><body><form name="win0" id="N_EXSP_MOD_APPR">
            <input id="ICStateNum" value="1">{fields}
            <textarea id="{COMMENTS}">prior</textarea>{buttons}
            </form><script>
            window.changes = 0;
            document.addEventListener('change', () => window.changes++);
            window.isLoaderInProcess = () => false;
            window.submitAction_win0 = (_, action) => {{
                if (action === '{CANCEL}') action = '#ICList';
                const box = document.getElementById('{COMMENTS}');
                const body = 'ICAction=' + encodeURIComponent(action) +
                    '&' + encodeURIComponent('{COMMENTS}') + '=' + encodeURIComponent(box.value);
                fetch('/post', {{method: 'POST', body,
                    headers: {{'content-type': 'application/x-www-form-urlencoded'}}}})
                    .then(() => {{ document.getElementById('ICStateNum').value += 'x'; }});
            }};
            </script></body></html>"""
        _, request = records()[0]
        advice = decision(request)
        panel = '<button id="edurec-apply-skip">Skip</button>'
        approve, reject = button_for("approve"), button_for("reject")
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_context().new_page()
            page.route(
                "**/*", lambda route: route.fulfill(body=page_html, content_type="text/html")
            )
            page.goto("https://local.test/detail")
            site = Applier(page.context)

            def later(script, delay=100):
                page.evaluate(f"setTimeout(() => {{ {script} }}, {delay})")

            def press(button_id):
                return f"document.getElementById('{button_id}').click();"

            entered = site.prepare(advice, panel, dry_run=False)
            self.assertEqual(entered, advice.comment + "\n\nprior", "new comment on top")
            self.assertEqual(page.locator(f'[id="{COMMENTS}"]').input_value(), entered)
            self.assertEqual(page.evaluate("window.changes"), 1, "change event dispatched")
            self.assertEqual(page.locator("#edurec-apply-panel").count(), 1)
            self.assertIn("outline", page.locator(f"#{approve}").get_attribute("style") or "")
            later(press("edurec-apply-skip"))
            self.assertEqual(site.await_action(), Skipped())
            self.assertEqual(page.locator(f'[id="{COMMENTS}"]').input_value(), "prior")

            site.prepare(advice, panel, dry_run=True)
            self.assertTrue(all(page.locator(f"#{b}").is_disabled() for b in BUTTONS))
            self.assertFalse(page.locator(f"#{CANCEL}").is_disabled())
            later(press("edurec-apply-skip"))
            self.assertEqual(site.await_action(), Skipped())

            # A PeopleSoft re-render replaces the form (same ids), drops the panel, the hook
            # and the disabled state, and bumps ICStateNum; the box keeps the reviewer's text.
            rerender = f"""
                const form = document.getElementById('N_EXSP_MOD_APPR');
                const value = document.getElementById('{COMMENTS}').value;
                const state = document.getElementById('ICStateNum').value;
                document.getElementById('edurec-apply-panel').remove();
                form.innerHTML = form.innerHTML;
                document.getElementById('{COMMENTS}').value = value;
                document.getElementById('ICStateNum').value = state + 'r';"""
            site.prepare(advice, panel, dry_run=True)
            later(f"document.getElementById('{COMMENTS}').value = 'kept'; " + rerender)
            later(
                f"""window.restored = {{
                    panels: document.querySelectorAll('#edurec-apply-panel').length,
                    disabled: [...'{",".join(BUTTONS)}'.split(',')].map(
                        id => document.getElementById(id).disabled),
                    value: document.getElementById('{COMMENTS}').value}};""",
                300,
            )
            later(press("edurec-apply-skip"), 500)
            self.assertEqual(site.await_action(), Skipped())
            self.assertEqual(
                page.evaluate("window.restored"),
                {"panels": 1, "disabled": [True] * len(BUTTONS), "value": "kept"},
            )

            site.prepare(advice, panel, dry_run=False)
            later(f"document.getElementById('{DETAIL}').remove(); " + rerender)
            self.assertEqual(site.await_action(), Left())
            page.evaluate(f"document.body.insertAdjacentHTML('beforeend', '{fields}')")

            messages = []
            posts = []

            def dismiss(dialog):
                messages.append(dialog.message)
                dialog.dismiss()

            page.on("dialog", dismiss)
            page.on("request", lambda r: posts.append(r) if r.method == "POST" else None)
            site.prepare(advice, panel, dry_run=True)
            # A stale page may have lost `disabled` but keep the hook: the click is still blocked.
            later(f"document.getElementById('{approve}').disabled = false; " + press(approve))
            later(press("edurec-apply-skip"), 300)
            self.assertEqual(site.await_action(), Skipped())
            self.assertEqual(messages, ["Dry run: nothing is submitted"])
            self.assertEqual(posts, [])

            messages.clear()
            site.prepare(advice, panel, dry_run=False)
            later(press(reject))
            later(press("edurec-apply-skip"), 300)
            self.assertEqual(site.await_action(), Skipped(), "A dismissed confirm aborts the click")
            self.assertEqual(messages, ["Recommended: approve. Submit Reject anyway?"])
            self.assertEqual(posts, [])

            site.prepare(advice, panel, dry_run=False)
            later(f"document.getElementById('{COMMENTS}').value = 'edited'; " + press(approve))
            self.assertEqual(site.await_action(), Clicked(approve, "edited"))

            site.prepare(advice, panel, dry_run=False)
            later(press(CANCEL))
            # The mock page never navigates, so the box still holds the edited text.
            self.assertEqual(site.await_action(), Clicked("#ICList", advice.comment + "\n\nedited"))
            browser.close()

    def test_apply_arguments(self):
        args = parse_apply_args(["--run", "out/x", "--request-id", "a", "--verdict", "reject"])
        self.assertEqual(args.decisions, str(Path("out/x-anonymized/decisions")))
        self.assertEqual((args.request_ids, args.verdicts), (["a"], ["reject"]))
        self.assertFalse(args.dry_run)
        self.assertEqual(args.timeout_ms, 60000)
        with self.assertRaises(SystemExit):
            parse_apply_args(["--verdict", "maybe"])
        with self.assertRaises(SystemExit):
            main(["apply", "--verdict", "maybe"])


if __name__ == "__main__":
    unittest.main()


class PrefillTests(unittest.TestCase):
    def test_decision_comment_goes_on_top_of_existing_text(self):
        _, request = records()[0]
        advice = decision(request)
        self.assertEqual(advice.prefill(None), advice.comment)
        self.assertEqual(advice.prefill("  "), advice.comment)
        self.assertEqual(advice.prefill("older note\n"), advice.comment + "\n\nolder note")

    def test_panel_shows_the_existing_comment(self):
        _, request = records()[0]
        item = Item(decision(request), request)
        html = panel_html(item, 1, 1, [], False, existing="kept <below>")
        self.assertIn("Existing comment", html)
        self.assertIn("kept &lt;below&gt;", html)
        self.assertNotIn("Existing comment", panel_html(item, 1, 1, [], False))


def test_forget_downloads_clears_history_tables(tmp_path):
    history = tmp_path / "Default" / "History"
    history.parent.mkdir()
    with sqlite3.connect(history) as connection:
        for table in DOWNLOAD_TABLES:
            connection.execute(f"CREATE TABLE {table} (id INTEGER)")
            connection.execute(f"INSERT INTO {table} VALUES (1)")
    forget_downloads(tmp_path)
    with sqlite3.connect(history) as connection:
        assert all(
            connection.execute(f"SELECT count(*) FROM {table}").fetchone() == (0,)
            for table in DOWNLOAD_TABLES
        )
    forget_downloads(tmp_path / "missing")  # No profile yet: nothing to do.
