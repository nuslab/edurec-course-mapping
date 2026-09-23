"""Orchestration in `cli` and `export.restore_list`, driven through fakes."""

import argparse
import io
import unittest
from collections.abc import Callable
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from playwright.sync_api import Error as PlaywrightError

from edurec_mappings import cli
from edurec_mappings.browser import COMPONENT, ApprovalNotLoadedError
from edurec_mappings.export import restore_list
from edurec_mappings.models import Listing, ListRow

NOT_LOADED = ApprovalNotLoadedError(
    "Course Mapping Approval is not loaded. Open the approval component."
)
DUPLICATES = RuntimeError("Found 2 Course Mapping Approval frames.")


def namespace(**overrides: object) -> argparse.Namespace:
    values: dict[str, object] = {
        "ready": False,
        "cdp_url": None,
        "timeout": 60,
        "timeout_ms": 60000,
        "command": "export",
        "reassign_id": " ",
        "term": "",
        "rows": None,
        "terms": ["2620"],
        "scrape_urls": False,
        "anonymize": False,
        "anonymized_run": "run-anonymized",
        "run": "run",
        "decisions": "run-anonymized/decisions",
        "request_ids": [],
        "verdicts": [],
        "dry_run": False,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def quietly(function: Callable[..., object], *args: object) -> str:
    with redirect_stdout(io.StringIO()) as out:
        function(*args)
    return out.getvalue()


class AwaitApprovalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.frame = self.patch("mapping_frame")
        self.open_component = self.patch("open_component")
        self.approval_ready = self.patch("approval_ready", return_value=False)
        self.context = mock.MagicMock()
        self.wait_for_enter = self.patch("wait_for_enter", return_value=True)

    def patch(self, name: str, return_value: object = mock.DEFAULT) -> mock.MagicMock:
        patcher = mock.patch.object(cli, name, return_value=return_value)
        self.addCleanup(patcher.stop)
        started: mock.MagicMock = patcher.start()
        return started

    def test_ready_with_form_present_does_not_navigate(self) -> None:
        cli.await_approval(self.context, namespace(ready=True))
        self.open_component.assert_not_called()

    def test_ready_opens_the_component_when_only_the_dashboard_is_up(self) -> None:
        self.frame.side_effect = [NOT_LOADED, None]
        cli.await_approval(self.context, namespace(ready=True))
        self.open_component.assert_called_once_with(self.context, 60000)
        self.assertEqual(self.frame.call_count, 2)

    def test_ready_propagates_other_frame_errors(self) -> None:
        self.frame.side_effect = DUPLICATES
        with self.assertRaises(RuntimeError):
            cli.await_approval(self.context, namespace(ready=True))
        self.open_component.assert_not_called()

    def test_detected_login_proceeds_without_enter(self) -> None:
        self.approval_ready.return_value = True
        out = quietly(cli.await_approval, self.context, namespace())
        self.assertIn("Course Mapping Approval detected", out)
        self.wait_for_enter.assert_not_called()

    def test_polls_until_enter_then_opens_the_component(self) -> None:
        self.wait_for_enter.side_effect = [False, True]
        self.frame.side_effect = [NOT_LOADED, None]
        quietly(cli.await_approval, self.context, namespace())
        self.open_component.assert_called_once_with(self.context, 60000)

    def test_failed_retry_keeps_waiting(self) -> None:
        self.approval_ready.side_effect = [False, True]
        self.frame.side_effect = DUPLICATES
        out = quietly(cli.await_approval, self.context, namespace())
        self.assertIn("Not ready: Found 2 Course Mapping Approval frames.", out)
        self.assertIn("Course Mapping Approval detected", out)

    def test_navigation_error_on_retry_keeps_waiting(self) -> None:
        self.approval_ready.side_effect = [False, True]
        self.frame.side_effect = NOT_LOADED
        self.open_component.side_effect = PlaywrightError("Timeout 60000ms exceeded.\ndetail")
        out = quietly(cli.await_approval, self.context, namespace())
        self.assertIn("Not ready: Timeout 60000ms exceeded.\n", out)


class ConnectTests(unittest.TestCase):
    def setUp(self) -> None:
        patcher = mock.patch.object(cli, "await_approval")
        self.await_approval = patcher.start()
        self.addCleanup(patcher.stop)
        self.context = mock.MagicMock()
        self.page = self.context.pages[0]

    def test_attached_browser_is_not_navigated(self) -> None:
        args = namespace(cdp_url="http://localhost:9222")
        cli.connect(self.context, args)
        self.page.goto.assert_not_called()
        self.await_approval.assert_called_once_with(self.context, args)

    def test_launched_browser_opens_the_component(self) -> None:
        cli.connect(self.context, namespace())
        self.page.goto.assert_called_once_with(COMPONENT, timeout=60000)

    def test_navigation_failure_is_left_to_the_user_unless_ready(self) -> None:
        self.page.goto.side_effect = PlaywrightError("net::ERR_PROXY\nstack")
        out = quietly(cli.connect, self.context, namespace())
        self.assertIn("Initial navigation failed: net::ERR_PROXY\n", out)
        self.await_approval.assert_called_once()
        with self.assertRaises(PlaywrightError):
            cli.connect(self.context, namespace(ready=True))


class RunTests(unittest.TestCase):
    def setUp(self) -> None:
        self.mocks: dict[str, mock.MagicMock] = {}
        for name in (
            "connect",
            "EduRec",
            "export",
            "playwright_fetcher",
            "playwright_renderer",
            "scrape",
            "checkpoint",
            "reset",
            "save",
            "anonymize",
            "Reviewer",
            "review",
            "hold_open",
        ):
            patcher = mock.patch.object(cli, name)
            self.mocks[name] = patcher.start()
            self.addCleanup(patcher.stop)
        result = self.mocks["export"].return_value
        result.collection.status, result.requests = "complete", [1, 2]
        self.context = mock.MagicMock()

    def assert_dialog_hook_removed(self) -> None:
        self.context.on.assert_called_once_with("dialog", cli.manual_dialog)
        self.context.remove_listener.assert_called_with("dialog", cli.manual_dialog)

    def test_run_export_extracts_with_the_parsed_filters(self) -> None:
        out = quietly(cli.run_export, self.context, namespace())
        self.mocks["EduRec"].assert_called_once_with(self.context, 60000)
        self.mocks["export"].assert_called_once_with(
            self.mocks["EduRec"].return_value,
            "run",
            reassign_id="",
            rows=None,
            terms=["2620"],
        )
        self.assertIn("complete: 2 requests → run", out)
        self.mocks["scrape"].assert_not_called()
        self.mocks["save"].assert_not_called()
        self.assert_dialog_hook_removed()

    def test_run_scrapes_and_anonymizes_when_asked(self) -> None:
        quietly(cli.run_export, self.context, namespace(scrape_urls=True, anonymize=True))
        self.mocks["scrape"].assert_called_once()
        self.mocks["reset"].assert_called_once_with("run-anonymized")
        self.mocks["save"].assert_called_once_with(
            self.mocks["anonymize"].return_value, "run-anonymized"
        )

    def test_run_holds_the_browser_open_on_error(self) -> None:
        self.mocks["export"].side_effect = RuntimeError("lost session")
        with self.assertRaises(RuntimeError):
            cli.run_export(self.context, namespace())
        self.mocks["hold_open"].assert_called_once_with(
            mock.ANY, "Collection stopped: lost session. Checkpoint retained."
        )
        self.assert_dialog_hook_removed()

    def test_run_review_passes_the_filters_and_reports_errors(self) -> None:
        args = namespace(request_ids=["r1"], verdicts=["approve"], dry_run=True)
        quietly(cli.run_review, self.context, args)
        self.mocks["Reviewer"].assert_called_once_with(self.context, 60000)
        self.mocks["review"].assert_called_once_with(
            self.mocks["Reviewer"].return_value,
            "run",
            "run-anonymized/decisions",
            request_ids=["r1"],
            verdicts=["approve"],
            dry_run=True,
        )
        self.mocks["review"].side_effect = RuntimeError("boom")
        with redirect_stdout(io.StringIO()) as out, self.assertRaises(RuntimeError):
            cli.run_review(self.context, args)
        self.assertIn("Review stopped: boom. Log retained.", out.getvalue())
        self.mocks["hold_open"].assert_not_called()


class ParseArgsTests(unittest.TestCase):
    def test_explicit_term_is_left_to_extract_and_blank_reads_the_configuration(self) -> None:
        self.assertEqual(cli.parse_args(["export", "--term", "2610"]).terms, ["2610"])
        self.assertTrue(cli.parse_args(["export"]).terms)

    def test_a_command_is_required(self) -> None:
        for argv in ([], ["--term", "2610"]):
            with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                cli.parse_args(argv)


class RenderTests(unittest.TestCase):
    def test_fetch_prints_title_and_text_and_closes_the_browser(self) -> None:
        html = b"<html><title>Syllabus</title><body><p>Week 1</p></body></html>"
        with (
            mock.patch.object(cli, "sync_playwright") as playwright,
            mock.patch.object(cli, "playwright_renderer", return_value=lambda url: html) as make,
        ):
            browser = playwright.return_value.__enter__.return_value.chromium.launch.return_value
            args = cli.parse_args(["fetch", "https://example.com", "--settle", "2"])
            out = quietly(cli.run_fetch, args)
        self.assertEqual(out, "Syllabus\nSyllabus\nWeek 1\n")
        make.assert_called_once_with(browser.new_context.return_value, 90000, 2000)
        browser.close.assert_called_once()


def page(start: int, end: int, total: int, has_next: bool = True, name: str = "row") -> Listing:
    rows = [ListRow(action=f"#ICRow{i}", student_id=f"{name}{i}") for i in range(start, end + 1)]
    return Listing(rows=rows, range=(start, end, total), has_next=has_next)


class RestoreListTests(unittest.TestCase):
    def test_pages_forward_after_a_reset_to_page_one(self) -> None:
        site = mock.Mock()
        site.back.return_value = page(1, 2, 4)
        site.next_page.return_value = page(3, 4, 4, has_next=False)
        restored = restore_list(site, page(3, 4, 4, has_next=False), 4)
        self.assertEqual(restored.range, (3, 4, 4))

    def test_pagination_that_does_not_advance_is_an_error(self) -> None:
        site = mock.Mock()
        site.back.return_value = page(1, 2, 4)
        site.next_page.return_value = page(1, 2, 4)
        with self.assertRaisesRegex(RuntimeError, "did not advance"):
            restore_list(site, page(3, 4, 4, has_next=False), 4)

    def test_changed_results_are_an_error(self) -> None:
        site = mock.Mock()
        site.back.return_value = page(1, 2, 4, name="other")
        with self.assertRaisesRegex(RuntimeError, "Results changed"):
            restore_list(site, page(1, 2, 4), 4)
