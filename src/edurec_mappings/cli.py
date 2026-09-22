"""Extract EduRec course mapping approval requests to a run directory (read-only navigation).

Stages: 1. extract requests from EduRec; 2. with --scrape-urls, fetch the URLs in the
course details and store their text; 3. with --anonymize, write a pseudonymised copy.
"""

from __future__ import annotations

import argparse
import re
import select
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import yaml
from playwright.sync_api import BrowserContext, Dialog, sync_playwright
from playwright.sync_api import Error as PlaywrightError

from .anonymize import anonymize, anonymized_path
from .browser import APPROVAL_FORM, COMPONENT, EduRec, mapping_frame
from .documents import playwright_fetcher, scrape
from .extract import Checkpoint, extract
from .parse import reset, save

TERM_PATTERN = re.compile(r"\d{4}")
LOGIN_PROMPT = (
    "Log in and accept the policy if you agree. Collection starts automatically once "
    "the Course Mapping Approval form is visible; press Enter to retry immediately: "
)
POLL_SECONDS = 3.0


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
    add = parser.add_argument
    add("--reassign-id", "--ReassignID", default="", help="Exact ReassignID; blank = all")
    add("--term", "--Term", type=term_code, default="", help="Four-digit term; blank = configured")
    add(
        "--terms-file",
        default=str(Path(__file__).with_name("terms.yaml")),
        help="YAML configuration listing terms to search when Term is blank",
    )
    add("--rows", "--Rows", type=optional_rows, help="Unique matching requests; blank = all")
    add(
        "--output",
        default="../edurec-data/output/module-mappings",
        help="Run directory: inventory.yaml, requests/ and documents/; also the checkpoint",
    )
    add("--scrape-urls", action="store_true", help="Fetch URLs in course details into documents/")
    add("--anonymize", action="store_true", help="Also write an anonymized copy of the run")
    add(
        "--anonymized-output",
        help="Directory of the anonymized copy; default adds '-anonymized' to --output",
    )
    add("--cdp-url", help="Attach to the existing exploration browser")
    add("--profile", default="../edurec-data/browser-profile")
    add("--proxy", default="")
    add("--timeout", type=int, default=60)
    add("--ready", action="store_true", help="Skip the login prompt; approval must be open")
    args = parser.parse_args(argv)
    try:
        args.terms = [args.term] if args.term else configured_terms(args.terms_file)
    except (OSError, ValueError, argparse.ArgumentTypeError, yaml.YAMLError) as error:
        parser.error(str(error))
    args.anonymized_output = args.anonymized_output or str(anonymized_path(args.output))
    if Path(args.anonymized_output).resolve() == Path(args.output).resolve():
        parser.error("The anonymized copy must use a different path from --output")
    args.timeout_ms = args.timeout * 1000
    return args


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
        context = playwright.chromium.launch_persistent_context(
            args.profile, headless=False, proxy={"server": args.proxy} if args.proxy else None
        )
        try:
            yield context
        finally:
            context.close()


def open_component(context: BrowserContext, timeout_ms: float) -> None:
    page = context.pages[0]
    page.goto(COMPONENT, timeout=timeout_ms)
    page.frame_locator('iframe[name="TargetContent"]').locator(APPROVAL_FORM).wait_for(
        state="attached", timeout=timeout_ms
    )


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
        try:
            mapping_frame(context)
        except RuntimeError as error:
            if "not loaded" not in str(error):
                raise
            open_component(context, args.timeout_ms)
            mapping_frame(context)
        return
    print(LOGIN_PROMPT, end="", flush=True)
    while True:
        if approval_ready(context):
            print("\nCourse Mapping Approval detected; starting collection.", flush=True)
            return
        if not wait_for_enter(POLL_SECONDS):
            continue
        try:
            try:
                mapping_frame(context)
            except RuntimeError as error:
                if "not loaded" not in str(error):
                    raise
                open_component(context, args.timeout_ms)
                mapping_frame(context)
            return
        except (RuntimeError, PlaywrightError) as error:
            print(f"Not ready: {str(error).splitlines()[0]}")
            print(
                "Browser remains open. Finish signing in; collection starts when the form appears."
            )
            print(LOGIN_PROMPT, end="", flush=True)


def run(context: BrowserContext, args: argparse.Namespace) -> None:
    def manual_dialog(_: Dialog) -> None:
        print("Login dialog waiting for your choice in VNC.", flush=True)

    context.on("dialog", manual_dialog)
    try:
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
        context.remove_listener("dialog", manual_dialog)
        site = EduRec(context, args.timeout)
        result = extract(
            site,
            args.output,
            reassign_id=args.reassign_id.strip(),
            term=args.term,
            rows=args.rows,
            terms=args.terms,
        )
        status, count = result.collection.status, len(result.requests)
        print(f"{status}: {count} requests → {args.output}")
        if args.scrape_urls:
            fetch = playwright_fetcher(context, args.timeout_ms)
            scrape(result, fetch, Checkpoint(result, args.output))
            print(f"Linked documents added → {args.output}")
        if args.anonymize:
            reset(args.anonymized_output)
            save(anonymize(result), args.anonymized_output)
            print(f"Anonymized copy → {args.anonymized_output}")
    except Exception as error:
        if not args.ready and not args.cdp_url:
            print(f"Collection stopped: {error}")
            input(
                "Checkpoint retained. Browser remains open for inspection. Press Enter to close: "
            )
        raise
    finally:
        context.remove_listener("dialog", manual_dialog)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    with browser_context(args) as context:
        run(context, args)
