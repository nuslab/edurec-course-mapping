"""Extract EduRec course mapping approval requests and walk the reviewer through decisions.

`export` runs stages 1-3: 1. extract requests from EduRec (read-only navigation);
2. with --scrape-urls, fetch the URLs in the course details and store their text;
3. with --anonymize, write a pseudonymised copy. `review` is stage 4: walk the reviewer
through the decisions in EduRec. `fetch URL` prints the text of one page after its
scripts have run, for links the export could not read.
"""

from __future__ import annotations

import argparse
import select
import sqlite3
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import get_args

import yaml
from playwright.sync_api import BrowserContext, Dialog, sync_playwright
from playwright.sync_api import Error as PlaywrightError

from .anonymize import anonymize, anonymized_path
from .browser import (
    APPROVAL_FORM,
    COMPONENT,
    ApprovalNotLoadedError,
    EduRec,
    Reviewer,
    mapping_frame,
)
from .documents import html_text, playwright_fetcher, playwright_renderer, scrape
from .export import checkpoint, export
from .models import TERM_PATTERN, Verdict
from .review import review
from .store import reset, save

LOGIN_PROMPT = (
    "Log in and accept the policy if you agree. Collection starts automatically once "
    "the Course Mapping Approval form is visible; press Enter to retry immediately: "
)
POLL_SECONDS = 3.0
DOWNLOAD_TABLES = ("downloads", "downloads_url_chains", "downloads_slices")
REVIEW_NOTICE = (
    "Only clicks made while this program shows its panel are logged; "
    "do not act in EduRec after it stops."
)
VERDICTS: tuple[Verdict, ...] = get_args(Verdict)
DEFAULT_RUN = "../edurec-data/output/module-mappings"


def optional_rows(value: str) -> int | None:
    if not value.strip():
        return None
    try:
        number = int(value)
    except ValueError:
        number = 0
    if number < 1:
        raise argparse.ArgumentTypeError("Rows must be a positive integer or blank")
    return number


def term_code(value: str) -> str:
    value = value.strip()
    if value and not TERM_PATTERN.fullmatch(value):
        raise argparse.ArgumentTypeError("Term must be a four-digit code (e.g. 2620) or blank")
    return value


def configured_terms(path: str | Path) -> list[str]:
    config = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    values = config.get("terms") if isinstance(config, dict) else None
    if not isinstance(values, list) or not values:
        raise ValueError("Terms configuration must contain a nonempty 'terms' list")
    result: list[str] = []
    for value in values:
        code = term_code(str(value))
        if not code:
            raise ValueError("Configured terms cannot be blank")
        if code in result:
            raise ValueError(f"Duplicate configured term: {code}")
        result.append(code)
    return result


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="edurec-mappings", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")
    browser = argparse.ArgumentParser(add_help=False)
    add = browser.add_argument
    add("--cdp-url", help="Attach to the existing exploration browser")
    add("--profile", default="../edurec-data/browser-profile")
    add("--proxy", default="")
    add("--timeout", type=int, default=60)
    add("--ready", action="store_true", help="Skip the login prompt; approval must be open")

    export_parser = commands.add_parser(
        "export",
        parents=[browser],
        help="Extract the approval requests into a run directory",
        description="Extract the approval requests into a run directory (read-only navigation)",
    )
    add = export_parser.add_argument
    add("--reassign-id", default="", help="Exact ReassignID; blank = all")
    add("--term", type=term_code, default="", help="Four-digit term; blank = configured")
    add(
        "--terms-file",
        default=str(Path(__file__).with_name("terms.yaml")),
        help="YAML configuration listing terms to search when Term is blank",
    )
    add("--rows", type=optional_rows, help="Unique matching requests; blank = all")
    add(
        "--run",
        default=DEFAULT_RUN,
        help="Run directory: inventory.yaml, requests/ and documents/; also the checkpoint",
    )
    add("--scrape-urls", action="store_true", help="Fetch URLs in course details into documents/")
    add("--anonymize", action="store_true", help="Also write an anonymized copy of the run")
    add("--anonymized-run", help="Directory of the anonymized copy; default <run>-anonymized")

    review_parser = commands.add_parser(
        "review",
        parents=[browser],
        help="Walk the reviewer through the advisor's decisions",
        description="Walk the reviewer through the advisor's decisions on their EduRec pages",
    )
    add = review_parser.add_argument
    add("--run", default=DEFAULT_RUN, help="The original export")
    add("--decisions", help="Decision files; default <run>-anonymized/decisions")
    add("--request-id", dest="request_ids", action="append", default=[], help="Only these")
    add("--verdict", dest="verdicts", action="append", default=[], choices=VERDICTS)
    add("--dry-run", action="store_true", help="Disable the action buttons; only Skip advances")

    fetch_parser = commands.add_parser(
        "fetch",
        help="Print the text of one page after its scripts have run",
        description="Print the text of a page after its scripts have run (headless, no login)",
    )
    add = fetch_parser.add_argument
    add("url")
    add("--proxy", default="")
    add("--timeout", type=int, default=90)
    add("--settle", type=float, default=8, help="Seconds to wait after network idle")

    args = parser.parse_args(argv)
    args.timeout_ms = args.timeout * 1000  # Playwright expects milliseconds.
    if args.command == "export":
        try:
            # An explicit term overrides the configured list.
            args.terms = [args.term] if args.term else configured_terms(args.terms_file)
        except (OSError, ValueError, argparse.ArgumentTypeError, yaml.YAMLError) as error:
            export_parser.error(str(error))
        args.anonymized_run = args.anonymized_run or str(anonymized_path(args.run))
        if Path(args.anonymized_run).resolve() == Path(args.run).resolve():
            export_parser.error("The anonymized copy must use a different path from --run")
    elif args.command == "review":
        args.decisions = args.decisions or str(anonymized_path(args.run) / "decisions")
    return args


def run_fetch(args: argparse.Namespace) -> None:
    """Render one URL in a fresh headless browser and write its text to stdout."""
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(proxy={"server": args.proxy} if args.proxy else None)
        try:
            context = browser.new_context()
            render = playwright_renderer(context, args.timeout_ms, args.settle * 1000)
            text, title = html_text(render(args.url))
        finally:
            browser.close()
    if title:
        print(title)
    print(text)


@contextmanager
def browser_context(args: argparse.Namespace) -> Iterator[BrowserContext]:
    """Attach to a debugging endpoint (left open) or launch a persistent profile (closed)."""
    with sync_playwright() as playwright:
        if args.cdp_url:
            browser = playwright.chromium.connect_over_cdp(args.cdp_url, timeout=args.timeout_ms)
            if len(browser.contexts) != 1:
                raise RuntimeError("Expected one browser context")
            yield browser.contexts[0]
            return
        profile = Path(args.profile)
        forget_downloads(profile)
        context = playwright.chromium.launch_persistent_context(
            profile,
            headless=False,
            proxy={"server": args.proxy} if args.proxy else None,
            no_viewport=True,  # Let the page follow the window instead of a fixed 1280x720.
        )
        try:
            fit_to_screen(context)
            yield context
        finally:
            context.close()


def forget_downloads(profile: Path) -> None:
    """Drop the profile's download history before launch.

    Chromium 153 on arm64 segfaults when a URL with a completed download record is
    downloaded again, so a syllabus PDF opened in one session crashed the next.
    """
    history = profile / "Default" / "History"
    if not history.exists():
        return
    with sqlite3.connect(history) as connection:
        for table in DOWNLOAD_TABLES:
            connection.execute(f"DELETE FROM {table}")


def fit_to_screen(context: BrowserContext) -> None:
    """Size the window to the display; the VNC window manager ignores --start-maximized."""
    page = context.pages[0] if context.pages else context.new_page()
    screen = page.evaluate("() => ({width: screen.availWidth, height: screen.availHeight})")
    cdp = context.new_cdp_session(page)
    window = cdp.send("Browser.getWindowForTarget")
    cdp.send(
        "Browser.setWindowBounds",
        {"windowId": window["windowId"], "bounds": {"left": 0, "top": 0, **screen}},
    )
    cdp.detach()


def open_component(context: BrowserContext, timeout_ms: float) -> None:
    page = context.pages[0]
    page.goto(COMPONENT, timeout=timeout_ms)
    page.frame_locator('iframe[name="TargetContent"]').locator(APPROVAL_FORM).wait_for(
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
    """True when exactly one signed-in Course Mapping Approval form is present."""
    try:
        mapping_frame(context)
    except (RuntimeError, PlaywrightError):
        return False
    return True


def wait_for_enter(seconds: float) -> bool:
    """Return True if a line arrives on stdin within `seconds` (False on non-tty EOF)."""
    try:
        ready, _, _ = select.select([sys.stdin], [], [], seconds)
    except (OSError, ValueError):
        return False
    if not ready:
        return False
    return bool(sys.stdin.readline())


def await_approval(context: BrowserContext, args: argparse.Namespace) -> None:
    """Wait until the signed-in Course Mapping Approval search form is present.

    The form is polled every few seconds so a completed login proceeds on its own;
    pressing Enter retries at once, opening the component when only the dashboard is up.
    """
    if args.ready:
        ensure_approval(context, args.timeout_ms)
        return
    print(LOGIN_PROMPT, end="", flush=True)
    while True:
        if approval_ready(context):
            print("\nCourse Mapping Approval detected; starting collection.", flush=True)
            return
        if not wait_for_enter(POLL_SECONDS):
            continue
        try:
            ensure_approval(context, args.timeout_ms)
            return
        except (RuntimeError, PlaywrightError) as error:
            print(f"Not ready: {str(error).splitlines()[0]}")
            print(
                "Browser remains open. Finish signing in; collection starts when the form appears."
            )
            print(LOGIN_PROMPT, end="", flush=True)


def manual_dialog(_: Dialog) -> None:
    """Leave browser dialogs to the person at the VNC desktop instead of auto-dismissing."""
    print("Browser dialog waiting for your choice in VNC.", flush=True)


def connect(context: BrowserContext, args: argparse.Namespace) -> None:
    """Open the component unless attached, then wait for the signed-in approval form."""
    if not args.cdp_url:
        page = context.pages[0] if context.pages else context.new_page()
        try:
            page.goto(COMPONENT, timeout=args.timeout_ms)
        except PlaywrightError as error:
            if args.ready:
                raise
            print(f"Initial navigation failed: {str(error).splitlines()[0]}")
            print("Browser remains open. Retry navigation in VNC before continuing.")
    await_approval(context, args)


def hold_open(args: argparse.Namespace, message: str) -> None:
    if not args.ready and not args.cdp_url:
        print(message)
        input("Browser remains open for inspection. Press Enter to close: ")


def run_export(context: BrowserContext, args: argparse.Namespace) -> None:
    context.on("dialog", manual_dialog)
    try:
        connect(context, args)
        context.remove_listener("dialog", manual_dialog)
        site = EduRec(context, args.timeout_ms)
        result = export(
            site,
            args.run,
            reassign_id=args.reassign_id.strip(),
            rows=args.rows,
            terms=args.terms,
        )
        status, count = result.collection.status, len(result.requests)
        print(f"{status}: {count} requests → {args.run}")
        if args.scrape_urls:
            fetch = playwright_fetcher(context, args.timeout_ms)
            render = playwright_renderer(context, args.timeout_ms)
            scrape(result, fetch, checkpoint(result, args.run), render)
            print(f"Linked documents added → {args.run}")
        if args.anonymize:
            reset(args.anonymized_run)
            save(anonymize(result), args.anonymized_run)
            print(f"Anonymized copy → {args.anonymized_run}")
    except Exception as error:
        hold_open(args, f"Collection stopped: {error}. Checkpoint retained.")
        raise
    finally:
        context.remove_listener("dialog", manual_dialog)


def run_review(context: BrowserContext, args: argparse.Namespace) -> None:
    """Walk the decisions; on error the browser is closed at once, never held open.

    An unwatched detail page would let the reviewer act without anything being logged.
    """
    print(REVIEW_NOTICE, flush=True)
    # The panel's confirm() and PeopleSoft's unsaved-changes dialog are the reviewer's to answer.
    context.on("dialog", manual_dialog)
    try:
        connect(context, args)
        review(
            Reviewer(context, args.timeout_ms),
            args.run,
            args.decisions,
            request_ids=args.request_ids,
            verdicts=args.verdicts,
            dry_run=args.dry_run,
        )
    except Exception as error:
        print(f"Review stopped: {error}. Log retained.", flush=True)
        raise
    finally:
        context.remove_listener("dialog", manual_dialog)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    if args.command == "fetch":
        run_fetch(args)
        return
    with browser_context(args) as context:
        (run_export if args.command == "export" else run_review)(context, args)
