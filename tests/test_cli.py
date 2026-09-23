import argparse
import io
import sqlite3
import tempfile
import unittest
from collections.abc import Callable
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from playwright.sync_api import Error as PlaywrightError

from edurec_mappings import cli
from edurec_mappings.browser import COMPONENT, ApprovalNotLoadedError
from edurec_mappings.models import Export, LinkedDocument, Request
from tests.test_export import records

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
        "proxy": "",
        "store": "store",
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


def collected(count: int = 2) -> list[Request]:
    return [request for _, request in records()[:count]]


class RunTests(unittest.TestCase):
    def setUp(self) -> None:
        self.mocks: dict[str, mock.MagicMock] = {}
        for name in (
            "connect",
            "EduRec",
            "export",
            "playwright_fetcher",
            "process_renderer",
            "scrape",
            "Store",
            "Reviewer",
            "review",
            "hold_open",
        ):
            patcher = mock.patch.object(cli, name)
            self.mocks[name] = patcher.start()
            self.addCleanup(patcher.stop)

        def export(site: object, data: Export, **_: object) -> None:
            data.requests.extend(collected())
            data.status = "complete"

        self.mocks["export"].side_effect = export
        self.save = self.mocks["Store"].return_value.save
        self.save.return_value = ["new"]
        self.context = mock.MagicMock()

    def assert_dialog_hook_removed(self) -> None:
        self.context.on.assert_called_once_with("dialog", cli.manual_dialog)
        self.context.remove_listener.assert_called_with("dialog", cli.manual_dialog)

    def test_run_export_extracts_with_the_parsed_filters_and_stores(self) -> None:
        out = quietly(cli.run_export, self.context, namespace())
        self.mocks["EduRec"].assert_called_once_with(self.context, 60000)
        self.mocks["export"].assert_called_once_with(
            self.mocks["EduRec"].return_value,
            mock.ANY,
            reassign_id="",
            rows=None,
            terms=["2620"],
        )
        self.assertIn("complete: 2 requests collected", out)
        self.assertIn("2 requests stored, 1 new versions → store", out)
        self.mocks["Store"].assert_called_once_with("store")
        self.assertEqual(self.save.call_args.args[0], collected())
        self.mocks["scrape"].assert_not_called()
        self.assert_dialog_hook_removed()

    def test_run_scrapes_before_storing_when_asked(self) -> None:
        def scrape(requests: list[Request], *_: object) -> None:
            for request in requests:
                request.linked_documents = [LinkedDocument("https://example.org")]

        self.mocks["scrape"].side_effect = scrape
        quietly(cli.run_export, self.context, namespace(scrape_urls=True))
        (stored,) = self.save.call_args.args
        self.assertEqual(len(stored), 2)
        self.assertTrue(all(r.linked_documents for r in stored))

    def test_an_interrupted_export_stores_what_was_complete(self) -> None:
        def export(site: object, data: Export, **_: object) -> None:
            data.requests.extend(collected(1))
            raise RuntimeError("lost session")

        self.mocks["export"].side_effect = export
        with redirect_stdout(io.StringIO()), self.assertRaises(RuntimeError):
            cli.run_export(self.context, namespace())
        self.assertEqual(len(self.save.call_args.args[0]), 1)
        self.mocks["hold_open"].assert_called_once_with(
            mock.ANY, "Collection stopped: lost session."
        )
        self.assert_dialog_hook_removed()
        # With --scrape-urls a request is only complete once its documents are known.
        with redirect_stdout(io.StringIO()), self.assertRaises(RuntimeError):
            cli.run_export(self.context, namespace(scrape_urls=True))
        self.assertEqual(self.save.call_args.args[0], [])

    def test_run_review_passes_the_filters_and_reports_errors(self) -> None:
        args = namespace(request_ids=["r1"], verdicts=["approve"], dry_run=True)
        quietly(cli.run_review, self.context, args)
        self.mocks["Reviewer"].assert_called_once_with(self.context, 60000)
        self.mocks["review"].assert_called_once_with(
            self.mocks["Reviewer"].return_value,
            "store",
            request_ids=["r1"],
            verdicts=["approve"],
            dry_run=True,
        )
        self.mocks["review"].side_effect = RuntimeError("boom")
        with redirect_stdout(io.StringIO()) as out, self.assertRaises(RuntimeError):
            cli.run_review(self.context, args)
        self.assertIn("Review stopped: boom. Stored outcomes retained.", out.getvalue())
        self.mocks["hold_open"].assert_not_called()


class ParseArgsTests(unittest.TestCase):
    def test_explicit_term_is_left_to_extract_and_blank_reads_the_configuration(self) -> None:
        self.assertEqual(
            cli.parse_args(["export", "--store", "s", "--term", "2610"]).terms, ["2610"]
        )
        self.assertTrue(cli.parse_args(["export", "--store", "s"]).terms)

    def test_pending_takes_only_the_store(self) -> None:
        self.assertEqual(cli.parse_args(["pending", "--store", "s"]).store, "s")
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            cli.parse_args(["pending", "--store", "s", "--dry-run"])

    def test_a_command_is_required(self) -> None:
        for argv in ([], ["--term", "2610"]):
            with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                cli.parse_args(argv)

    def test_export_arguments(self) -> None:
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            cli.parse_args(["export"])
        self.assertFalse(cli.parse_args(["export", "--store", "s"]).scrape_urls)
        args = cli.parse_args(["export", "--scrape-urls", "--store", "s"])
        self.assertTrue(args.scrape_urls)
        self.assertEqual(args.store, "s")

    def test_review_arguments(self) -> None:
        args = cli.parse_args(
            ["review", "--store", "s", "--request-id", "a", "--verdict", "reject"]
        )
        self.assertEqual((args.request_ids, args.verdicts), (["a"], ["reject"]))
        self.assertFalse(args.dry_run)
        self.assertEqual(args.timeout_ms, 60000)
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            cli.parse_args(["review", "--store", "s", "--verdict", "maybe"])

    def test_blank_arguments_and_invalid_limits(self) -> None:
        self.assertIsNone(cli.optional_rows(""))
        self.assertEqual(cli.optional_rows("12"), 12)
        self.assertEqual(cli.term_code(""), "")
        self.assertEqual(cli.term_code(" 2610 "), "2610")
        for value in ("0", "-1", "abc"):
            with self.assertRaises(argparse.ArgumentTypeError):
                cli.optional_rows(value)
        with self.assertRaises(argparse.ArgumentTypeError):
            cli.term_code("26")

    def test_invalid_term_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "terms.yaml"
            for config in (
                "terms: []",
                "terms: [2610, 2610]",
                'terms: ["", 2610]',
                "terms: [false]",
                "terms: 2610",
            ):
                path.write_text(config)
                with self.assertRaises((ValueError, argparse.ArgumentTypeError)):
                    cli.configured_terms(path)


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

    def test_fetch_html_writes_the_rendered_page(self) -> None:
        html = "<html><title>Syllabus</title><body><p>Woche 1: Überblick</p></body></html>"
        with (
            mock.patch.object(cli, "sync_playwright"),
            mock.patch.object(cli, "playwright_renderer", return_value=lambda url: html.encode()),
        ):
            stdout = io.TextIOWrapper(io.BytesIO(), encoding="utf-8")
            with mock.patch("sys.stdout", stdout):
                cli.run_fetch(cli.parse_args(["fetch", "--html", "https://example.com"]))
            self.assertEqual(stdout.buffer.getvalue().decode(), html)


class ForgetDownloadsTests(unittest.TestCase):
    def test_clears_the_history_tables(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            profile = Path(directory)
            history = profile / "Default" / "History"
            history.parent.mkdir()
            with sqlite3.connect(history) as connection:
                for table in cli.DOWNLOAD_TABLES:
                    connection.execute(f"CREATE TABLE {table} (id INTEGER)")
                    connection.execute(f"INSERT INTO {table} VALUES (1)")
            cli.forget_downloads(profile)
            with sqlite3.connect(history) as connection:
                for table in cli.DOWNLOAD_TABLES:
                    count = connection.execute(f"SELECT count(*) FROM {table}").fetchone()
                    self.assertEqual(count, (0,))
            cli.forget_downloads(profile / "missing")
