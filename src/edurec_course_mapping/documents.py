"""Fetch and extract text from URLs referenced in a mapping request's course details."""

from __future__ import annotations

import io
import math
import os
import re
import signal
import subprocess
import sys
import time
import zipfile
from collections.abc import Callable
from contextlib import suppress
from dataclasses import replace
from typing import NamedTuple
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from xml.etree import ElementTree

import pypdfium2
from bs4 import BeautifulSoup
from playwright.sync_api import APIRequestContext, BrowserContext
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from .models import LinkedDocument, Request

URL_PATTERN = re.compile(r"https?://[^\s<>\"'　]+", re.I)
TRAILING = ".,;:!?)]}>'\""
MAX_BYTES = 25 * 1024 * 1024
MAX_TEXT_BYTES = 200 * 1024
"""Longer texts are whole textbooks rather than syllabi; they are recorded but not kept."""
MIN_HTML_TEXT = 1000
"""HTML pages with less extracted text than this are treated as script-rendered shells."""
RENDER_SETTLE_MS = 5000
"""How long a rendered page is given after network idle for scripts to fill the DOM."""
RENDER_MARGIN_S = 30
"""Time a render process gets beyond its page timeout and settle to start and exit."""
MAX_FOLDER_FILES = 20
RETRY_STATUSES = frozenset({429, 502, 503, 504})
RETRY_DELAY_S = 10
"""Rate limits and overloaded gateways get one more try after this pause."""
BLOCK_PAGE_TEXT = 3000
"""Pages shorter than this whose title names a sign-in or bot check are not the document."""
BLOCK_TITLE = re.compile(
    r"sign[- ]?in|log[- ]?in|anmeld|request rejected|content blocked|access denied", re.I
)
CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
DRIVE_FILE = re.compile(r"^/file/d/([\w-]+)")
DRIVE_FOLDER = re.compile(r"^/drive/(?:u/\d+/)?folders/([\w-]+)")
# `/d/e/<id>/pub` is a published copy, readable as HTML but without an export endpoint.
GOOGLE_DOC = re.compile(r"^/(document|presentation|spreadsheets)/d/(?!e/)([\w-]+)")
WORD = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
DOC_EXPORT = {"document": "txt", "presentation": "pdf", "spreadsheets": "csv"}


class Fetched(NamedTuple):
    status: int
    content_type: str | None
    body: bytes


Fetcher = Callable[[str], Fetched]
Renderer = Callable[[str], bytes]
"""Loads a URL in a browser page, runs its scripts and returns the rendered HTML."""
BundleFile = tuple[str, str | None, bytes | Exception]
"""A file's name, content type and bytes, or why it could not be downloaded."""


def find_urls(request: Request) -> list[str]:
    """Return unique URLs, in order of appearance, from the request's course detail text."""
    partner = request.partner_course
    sources = [
        partner.supporting_url,
        partner.synopsis,
        partner.other_information,
        partner.title,
        request.prerequisites,
        request.review_comments,
    ]
    found: list[str] = []
    for value in sources:
        for match in URL_PATTERN.findall(value or ""):
            url = match.rstrip(TRAILING)
            # Keep a closing parenthesis that balances one inside the URL.
            if match[len(url) : len(url) + 1] == ")" and url.count("(") > url.count(")"):
                url = match[: len(url) + 1]
            if url and url not in found:
                found.append(url)
    return found


def direct_url(url: str) -> str:
    """Rewrite share links that return a viewer page instead of the file.

    Drive and Docs viewer pages need a signed-in browser even for files shared with
    anyone, but their download and export endpoints serve such files anonymously.
    """
    parts = urlsplit(url)
    host = parts.netloc.lower()
    if host.endswith("dropbox.com"):
        query = dict(parse_qsl(parts.query, keep_blank_values=True))
        query["dl"] = "1"
        return urlunsplit(parts._replace(query=urlencode(query)))
    if host == "drive.google.com" and (match := DRIVE_FILE.match(parts.path)):
        return f"https://drive.google.com/uc?export=download&id={match.group(1)}"
    if host == "docs.google.com" and (match := GOOGLE_DOC.match(parts.path)):
        kind, doc_id = match.groups()
        return f"https://docs.google.com/{kind}/d/{doc_id}/export?format={DOC_EXPORT[kind]}"
    return url


def pdf_text(data: bytes) -> tuple[str, int]:
    document = pypdfium2.PdfDocument(data)
    try:
        pages = [page.get_textpage().get_text_range().strip() for page in document]
        return "\n\n".join(p for p in pages if p), len(pages)
    finally:
        document.close()


def docx_text(data: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        root = ElementTree.fromstring(archive.read("word/document.xml"))
    paragraphs = ("".join(t.text or "" for t in p.iter(f"{WORD}t")) for p in root.iter(f"{WORD}p"))
    return "\n".join(p.strip() for p in paragraphs if p.strip())


def bundle_text(files: list[BundleFile]) -> str:
    """The text of each file under a `=== name ===` heading; unreadable files say why.

    Empty when no file had text, so a folder of images is recorded as empty.
    """
    parts: list[str] = []
    readable = False
    for name, content_type, data in sorted(files, key=lambda file: file[0]):
        try:
            if isinstance(data, Exception):
                raise data
            text = extract_text(name, content_type, data, bundles=False).text or ""
        except Exception as error:
            text = f"[Not read: {error_summary(error)}]"
        else:
            readable = readable or bool(text.strip())
            text = text or "[No extractable text]"
        parts.append(f"=== {name} ===\n{text}")
    return "\n\n".join(parts) if readable else ""


def zip_files(data: bytes) -> list[BundleFile]:
    files: list[BundleFile] = []
    total = 0
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        for member in archive.infolist():
            if member.is_dir():
                continue
            total += member.file_size
            if total > MAX_BYTES:
                raise ValueError(f"Archive exceeds {MAX_BYTES} bytes uncompressed")
            files.append((member.filename, None, archive.read(member)))
    return files


def html_text(data: bytes, encoding: str | None = None) -> tuple[str, str | None]:
    soup = BeautifulSoup(data, "html.parser", from_encoding=encoding)
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    lines = [line.strip() for line in soup.get_text("\n").splitlines()]
    title = soup.title.string.strip() if soup.title and soup.title.string else None
    return "\n".join(line for line in lines if line), title


def extract_text(
    url: str, content_type: str | None, data: bytes, bundles: bool = True
) -> LinkedDocument:
    """The document's text, kind, title and page count; the status is left to the caller.

    A zip archive is read file by file unless `bundles` is false, which stops nesting.
    """
    header = content_type or ""
    kind = header.split(";")[0].strip().lower()
    charset_match = re.search(r"charset=([\w-]+)", header, re.I)
    charset = charset_match.group(1) if charset_match else None
    head = data[:256].lstrip().lower()
    if kind == "application/pdf" or data.startswith(b"%PDF-"):
        text, pages = pdf_text(data)
        return LinkedDocument(url, kind="pdf", text=text, page_count=pages)
    if data.startswith(b"PK"):
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            is_docx = "word/document.xml" in archive.namelist()
        if is_docx:
            return LinkedDocument(url, kind="docx", text=docx_text(data))
        if bundles:
            return LinkedDocument(url, kind="zip", text=bundle_text(zip_files(data)))
    if kind in ("text/html", "application/xhtml+xml") or head.startswith(
        (b"<!doctype html", b"<html")
    ):
        text, title = html_text(data, charset)
        return LinkedDocument(url, kind="html", text=text, title=title)
    if kind.startswith("text/"):
        return LinkedDocument(url, kind="text", text=data.decode(charset or "utf-8", "replace"))
    raise ValueError(f"Unsupported content type: {kind or 'unknown'}")


def rendered_text(target: str, render: Renderer, extracted: LinkedDocument) -> LinkedDocument:
    """Re-read a page in a browser when its HTML carried too little text to be the document.

    Single-page catalogues (Korea University, NYCU, TUMonline) serve a loading shell and
    fill it in with scripts; the rendered page replaces the shell only when it says more.
    """
    shell = extracted.text or ""
    # A `#/` route is chosen by scripts; the plain fetch drops the fragment and gets the index.
    routed = "#/" in target
    if extracted.kind != "html" or (len(shell) >= MIN_HTML_TEXT and not routed):
        return extracted
    try:
        text, title = html_text(render(target))
    except Exception:
        return extracted
    if len(text) <= len(shell):
        return extracted
    return replace(extracted, text=text, title=title or extracted.title)


def error_summary(error: BaseException) -> str:
    return str(error).splitlines()[0] if str(error) else type(error).__name__


def clean(text: str) -> str:
    """Normalise line ends, drop control characters and repair UTF-16 surrogates.

    PDF text layers map bullets and ligatures to control characters, and can split
    characters outside the Basic Multilingual Plane into surrogate halves, which cannot be
    encoded as UTF-8; pairs are joined and lone halves replaced.
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = CONTROL.sub("", text)
    return text.encode("utf-16", "surrogatepass").decode("utf-16", "replace")


def download(url: str, fetch: Fetcher) -> tuple[str | None, bytes]:
    status, content_type, data = fetch(url)
    if status in RETRY_STATUSES:
        time.sleep(RETRY_DELAY_S)
        status, content_type, data = fetch(url)
    if status >= 400:
        raise ValueError(f"HTTP {status}")
    if len(data) > MAX_BYTES:
        raise ValueError(f"Document exceeds {MAX_BYTES} bytes")
    return content_type, data


def folder_listing(folder_id: str, fetch: Fetcher) -> BeautifulSoup:
    """A shared Drive folder's embeddable view.

    The folder page lists its files only after scripts run, but the embeddable view is
    plain HTML linking each file, which then downloads like a shared file link.
    """
    _, listing = download(f"https://drive.google.com/embeddedfolderview?id={folder_id}", fetch)
    return BeautifulSoup(listing, "html.parser")


def folder_files(
    listing: BeautifulSoup, fetch: Fetcher, recursive: bool = False, prefix: str = ""
) -> list[BundleFile]:
    """The first files at the top of a listed folder, or with `recursive` every file below it."""
    entries = listing.select(".flip-entry")
    files: list[BundleFile] = []
    for entry in entries if recursive else entries[:MAX_FOLDER_FILES]:
        link, name = entry.select_one("a[href]"), entry.select_one(".flip-entry-title")
        if link is None or name is None:
            continue
        href, path = str(link["href"]), prefix + name.get_text(strip=True)
        if subfolder := DRIVE_FOLDER.match(urlsplit(href).path):
            if recursive:
                try:
                    sublisting = folder_listing(subfolder.group(1), fetch)
                    files += folder_files(sublisting, fetch, True, f"{path}/")
                except Exception as error:
                    files.append((f"{path}/", None, error))
            continue
        content_type: str | None = None
        data: bytes | Exception
        try:
            content_type, data = download(direct_url(href), fetch)
        except Exception as error:
            data = error
        files.append((path, content_type, data))
    return files


def drive_folder(
    url: str, folder_id: str, fetch: Fetcher, recursive: bool = False
) -> LinkedDocument:
    listing = folder_listing(folder_id, fetch)
    files = folder_files(listing, fetch, recursive)
    if not files:
        raise ValueError("Folder lists no files")
    title = listing.title.string.strip() if listing.title and listing.title.string else None
    return LinkedDocument(url, kind="folder", text=bundle_text(files), title=title)


def fetch_document(
    url: str, fetch: Fetcher, render: Renderer | None = None, all_files: bool = False
) -> LinkedDocument:
    """Fetch one URL; every failure is recorded in the result rather than raised."""
    target = direct_url(url)
    parts = urlsplit(url)
    folder = DRIVE_FOLDER.match(parts.path) if parts.netloc.lower() == "drive.google.com" else None
    try:
        if folder:
            record = drive_folder(url, folder.group(1), fetch, all_files)
        else:
            record = extract_text(url, *download(target, fetch))
            if render is not None:
                record = rendered_text(target, render, record)
        text = clean(record.text or "")
        if (
            record.kind == "html"
            and len(text) < BLOCK_PAGE_TEXT
            and BLOCK_TITLE.search(record.title or "")
        ):
            raise ValueError(f"Sign-in or block page: {record.title}")
        record = replace(record, text=text, text_bytes=len(text.encode()))
    except Exception as error:
        return LinkedDocument(url, error=error_summary(error))
    if not record.text:
        return replace(record, status="empty", error="No extractable text", text=None)
    if (record.text_bytes or 0) > MAX_TEXT_BYTES:
        too_large = f"Extracted text exceeds {MAX_TEXT_BYTES} bytes"
        return replace(record, status="too_large", error=too_large, text=None)
    return replace(record, status="fetched")


def scrape(
    requests: list[Request],
    documents: dict[str, LinkedDocument],
    fetch: Fetcher,
    render: Renderer | None = None,
) -> None:
    """Fetch every URL in the requests once into `documents`, keyed by URL."""
    for index, request in enumerate(requests, 1):
        for url in find_urls(request):
            if url not in documents:
                documents[url] = fetch_document(url, fetch, render)
        print(f"Scraped URLs for {index}/{len(requests)} requests", flush=True)


def playwright_fetcher(requests: APIRequestContext, timeout: float) -> Fetcher:
    def fetch(url: str) -> Fetched:
        response = requests.get(url, timeout=timeout, max_redirects=10)
        try:
            return Fetched(response.status, response.headers.get("content-type"), response.body())
        finally:
            response.dispose()

    return fetch


def playwright_renderer(
    context: BrowserContext, timeout: float, settle_ms: float = RENDER_SETTLE_MS
) -> Renderer:
    """Open the URL in a page of the context so its scripts run, and return the DOM as HTML."""

    def render(url: str) -> bytes:
        page = context.new_page()
        try:
            # Long-polling pages never go idle; read whatever has loaded by the deadline.
            with suppress(PlaywrightTimeoutError):
                page.goto(url, wait_until="networkidle", timeout=timeout)
            page.wait_for_timeout(settle_ms)
            return page.content().encode()
        finally:
            page.close()

    return render


def run_with_deadline(command: list[str], deadline_s: float) -> bytes:
    """Run a command and return its stdout; kill it and its children at the deadline."""
    with subprocess.Popen(
        command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=True
    ) as process:
        try:
            output, _ = process.communicate(timeout=deadline_s)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.communicate()
            raise TimeoutError(f"Render exceeded {deadline_s:.0f} s") from None
    if process.returncode:
        raise RuntimeError(f"Render exited with status {process.returncode}")
    return output


def process_renderer(
    proxy: str | None, timeout: float, settle_ms: float = RENDER_SETTLE_MS
) -> Renderer:
    """Render each URL with `render --html` in its own headless browser, killed at a deadline.

    A page that keeps its scripts busy can block Playwright calls that take no timeout,
    such as reading the DOM, and would stall the whole export in the export's own browser.
    The render browser is not signed in, which public catalogue pages do not need.
    """
    deadline_s = (timeout + settle_ms) / 1000 + RENDER_MARGIN_S
    options = ["--timeout", str(math.ceil(timeout / 1000)), "--settle", str(settle_ms / 1000)]
    if proxy:
        options += ["--proxy", proxy]

    def render(url: str) -> bytes:
        command = [sys.executable, "-m", "edurec_course_mapping", "render", "--html", *options, url]
        return run_with_deadline(command, deadline_s)

    return render
