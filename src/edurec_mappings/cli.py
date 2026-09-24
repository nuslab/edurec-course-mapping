"""Extract EduRec course mapping approval requests and review proposals for them.

`export` extracts requests from EduRec (read-only navigation), with --documents
fetches the URLs in their course details, and adds pseudonymized request versions to
the store. `pending` lists the request versions still awaiting a proposal. `review`
shows each proposal on its EduRec page. `render URL` prints the text of
one page after its scripts have run, for links the export could not read.
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
from playwright.sync_api import (
    APIRequest,
    APIRequestContext,
    BrowserContext,
    Dialog,
    Playwright,
    sync_playwright,
)
from playwright.sync_api import Error as PlaywrightError

from .documents import (
    error_summary,
    html_text,
    playwright_fetcher,
    playwright_renderer,
    process_renderer,
    scrape,
)
from .edurec import COMPONENT, EduRec, ReviewPage, approval_ready, ensure_approval
from .export import export
from .models import TERM_PATTERN, ExportResult, Verdict
from .review import review
from .store import Store

LOGIN_PROMPT = (
    "Log in and accept the policy if you agree. The program continues automatically once "
    "the Course Mapping Approval form is visible; press Enter to retry immediately: "
)
POLL_SECONDS = 3.0
DOWNLOAD_TABLES = ("downloads", "downloads_url_chains", "downloads_slices")
REVIEW_NOTICE = (
    "Only clicks made while this program shows its panel are logged; "
    "do not act in EduRec after it stops."
)
VERDICTS: tuple[Verdict, ...] = get_args(Verdict)


def optional_limit(value: str) -> int | None:
    if not value.strip():
        return None
    try:
        number = int(value)
    except ValueError:
        number = 0
    if number < 1:
        raise argparse.ArgumentTypeError("Limit must be a positive integer or blank")
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
    store = argparse.ArgumentParser(add_help=False)
    store.add_argument("--store", required=True, help="The append-only request store")
    browser = argparse.ArgumentParser(add_help=False, parents=[store])
    add = browser.add_argument
    add("--cdp-url", help="Attach to the existing exploration browser")
    add("--profile", default="../data/browser-profile")
    add("--proxy", default="")
    add("--timeout", type=int, default=60)
    add("--skip-login", action="store_true", help="Skip the login prompt; approval must be open")

    export_parser = commands.add_parser(
        "export",
        parents=[browser],
        help="Extract the approval requests into the store",
        description="Extract the approval requests into the store (read-only navigation)",
    )
    add = export_parser.add_argument
    add("--reassigned-to", default="", help="Exact ReassignID; blank = all")
    add("--term", type=term_code, default="", help="Four-digit term; blank = configured")
    add(
        "--terms-file",
        default=str(Path(__file__).with_name("terms.yaml")),
        help="YAML configuration listing terms to search when Term is blank",
    )
    add("--limit", type=optional_limit, help="Unique matching requests; blank = all")
    add("--documents", action="store_true", help="Fetch URLs in course details into documents/")

    commands.add_parser(
        "pending",
        parents=[store],
        help="List the request versions awaiting a proposal",
        description="Print requests/<request_id>/<hash>.yaml, relative to the store, for the "
        "latest version of every request without a proposal; sibling parts are consecutive",
    )

    review_parser = commands.add_parser(
        "review",
        parents=[browser],
        help="Show each proposal on its EduRec page",
        description="Show each proposal on its EduRec detail page with a panel for submitting it",
    )
    add = review_parser.add_argument
    add("--request-id", dest="request_ids", action="append", default=[], help="Only these")
    add("--verdict", dest="verdicts", action="append", default=[], choices=VERDICTS)
    add("--dry-run", action="store_true", help="Disable the action buttons; only Skip advances")

    render_parser = commands.add_parser(
        "render",
        help="Print the text of one page after its scripts have run",
        description="Print the text of a page after its scripts have run (headless, no login)",
    )
    add = render_parser.add_argument
    add("url")
    add("--proxy", default="")
    add("--timeout", type=int, default=90)
    add("--settle", type=float, default=8, help="Seconds to wait after network idle")
    add("--html", action="store_true", help="Write the rendered HTML instead of its text")

    args = parser.parse_args(argv)
    if "timeout" in args:
        args.timeout_ms = args.timeout * 1000
    if args.command == "export":
        try:
            # An explicit term overrides the configured list.
            args.terms = [args.term] if args.term else configured_terms(args.terms_file)
        except (OSError, ValueError, argparse.ArgumentTypeError, yaml.YAMLError) as error:
            export_parser.error(str(error))
    return args


def run_pending(args: argparse.Namespace) -> None:
    for version in Store(args.store).pending():
        print(version.path())


def run_render(args: argparse.Namespace) -> None:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(proxy={"server": args.proxy} if args.proxy else None)
        try:
            context = browser.new_context()
            render = playwright_renderer(context, args.timeout_ms, args.settle * 1000)
            html = render(args.url)
        finally:
            browser.close()
    if args.html:
        sys.stdout.buffer.write(html)
        return
    text, title = html_text(html)
    if title:
        print(title)
    print(text)


@contextmanager
def browser_context(playwright: Playwright, args: argparse.Namespace) -> Iterator[BrowserContext]:
    """Attach to a debugging endpoint (left open) or launch a persistent profile (closed)."""
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


@contextmanager
def signed_out_requests(
    factory: APIRequest, context: BrowserContext, args: argparse.Namespace
) -> Iterator[APIRequestContext]:
    """An HTTP client with the browser's user agent and proxy but none of its cookies.

    Linked URLs are entered by students; fetched with the EduRec session, a link to a
    page that only the signed-in account can open would have its text stored.
    """
    page = context.pages[0] if context.pages else context.new_page()
    requests = factory.new_context(
        user_agent=page.evaluate("() => navigator.userAgent"),
        proxy={"server": args.proxy} if args.proxy else None,
    )
    try:
        yield requests
    finally:
        requests.dispose()


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


def wait_for_enter(seconds: float) -> bool:
    """Return True if a line arrives on stdin within `seconds` (False on non-tty EOF)."""
    try:
        ready, _, _ = select.select([sys.stdin], [], [], seconds)
    except OSError, ValueError:
        return False
    if not ready:
        return False
    return bool(sys.stdin.readline())


def await_approval(context: BrowserContext, args: argparse.Namespace) -> None:
    """Wait until the signed-in Course Mapping Approval search form is present.

    The form is polled every few seconds so a completed login proceeds on its own;
    pressing Enter retries at once, opening the component when only the dashboard is up.
    """
    if args.skip_login:
        ensure_approval(context, args.timeout_ms)
        return
    print(LOGIN_PROMPT, end="", flush=True)
    while True:
        if approval_ready(context):
            print("\nCourse Mapping Approval detected.", flush=True)
            return
        if not wait_for_enter(POLL_SECONDS):
            continue
        try:
            ensure_approval(context, args.timeout_ms)
            return
        except (RuntimeError, PlaywrightError) as error:
            print(f"Not ready: {error_summary(error)}")
            print("Browser remains open. Finish signing in; this continues when the form appears.")
            print(LOGIN_PROMPT, end="", flush=True)


def manual_dialog(_: Dialog) -> None:
    """Leave browser dialogs open in VNC instead of auto-dismissing them."""
    print("Browser dialog waiting in VNC.", flush=True)


def connect(context: BrowserContext, args: argparse.Namespace) -> None:
    if not args.cdp_url:
        page = context.pages[0] if context.pages else context.new_page()
        try:
            page.goto(COMPONENT, timeout=args.timeout_ms)
        except PlaywrightError as error:
            if args.skip_login:
                raise
            print(f"Initial navigation failed: {error_summary(error)}")
            print("Browser remains open. Retry navigation in VNC before continuing.")
    await_approval(context, args)


def hold_open(args: argparse.Namespace, message: str) -> None:
    if not args.skip_login and not args.cdp_url:
        print(message)
        input("Browser remains open for inspection. Press Enter to close: ")


def collect(
    context: BrowserContext, args: argparse.Namespace, result: ExportResult, requests: APIRequest
) -> None:
    # Dialogs stay open while logging in; afterwards Playwright dismisses them.
    context.on("dialog", manual_dialog)
    try:
        connect(context, args)
    finally:
        context.remove_listener("dialog", manual_dialog)
    site = EduRec(context, args.timeout_ms)
    export(
        site,
        result,
        reassigned_to=args.reassigned_to.strip(),
        limit=args.limit,
        terms=args.terms,
    )
    print(f"{result.status}: {len(result.requests)} requests collected", flush=True)
    if args.documents:
        with signed_out_requests(requests, context, args) as client:
            fetch = playwright_fetcher(client, args.timeout_ms)
            scrape(result.requests, fetch, process_renderer(args.proxy or None, args.timeout_ms))


def persist(result: ExportResult, args: argparse.Namespace) -> None:
    """Store every collected request that is complete: with --documents, once fetched."""
    ready = [r for r in result.requests if not args.documents or r.documents is not None]
    added = Store(args.store).save(ready)
    print(f"{len(ready)} requests stored, {len(added)} new versions in {args.store}", flush=True)


def run_export(context: BrowserContext, args: argparse.Namespace, requests: APIRequest) -> None:
    """Collect, then store what was collected even when collection stopped early."""
    result = ExportResult()
    try:
        try:
            collect(context, args, result, requests)
        finally:
            persist(result, args)
    except Exception as error:
        hold_open(args, f"Collection stopped: {error}.")
        raise


def run_review(context: BrowserContext, args: argparse.Namespace) -> None:
    """Unlike export, close the browser on error: clicks on it would not be logged."""
    print(REVIEW_NOTICE, flush=True)
    # The panel's confirm() and PeopleSoft's unsaved-changes dialog stay open.
    context.on("dialog", manual_dialog)
    try:
        connect(context, args)
        review(
            ReviewPage(context, args.timeout_ms),
            args.store,
            request_ids=args.request_ids,
            verdicts=args.verdicts,
            dry_run=args.dry_run,
        )
    except Exception as error:
        print(f"Review stopped: {error}. Stored outcomes retained.", flush=True)
        raise
    finally:
        context.remove_listener("dialog", manual_dialog)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    if args.command == "render":
        run_render(args)
        return
    if args.command == "pending":
        run_pending(args)
        return
    with sync_playwright() as playwright, browser_context(playwright, args) as context:
        if args.command == "export":
            run_export(context, args, playwright.request)
        else:
            run_review(context, args)
