"""Fetch and extract text from URLs referenced in a mapping request's course details."""

from __future__ import annotations

import hashlib
import io
import re
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, replace
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from bs4 import BeautifulSoup
from playwright.sync_api import BrowserContext
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from pypdf import PdfReader

from .models import Document, DocumentKind, Fetched, LinkedDocument, Request

URL_PATTERN = re.compile(r"https?://[^\s<>\"'　]+", re.I)
TRAILING = ".,;:!?)]}>'\""
MAX_BYTES = 25 * 1024 * 1024
MAX_TEXT_BYTES = 200 * 1024
"""Longer texts are whole textbooks rather than syllabi; they are recorded but not kept."""
MIN_HTML_TEXT = 1000
"""HTML pages with less extracted text than this are treated as script-rendered shells."""
RENDER_SETTLE_MS = 5000
"""How long a rendered page is given after network idle for scripts to fill the DOM."""

Fetcher = Callable[[str], Fetched]
Renderer = Callable[[str], bytes]
"""Loads a URL in a browser page, runs its scripts and returns the rendered HTML."""


@dataclass
class Extracted:
    """Text recovered from a downloaded document."""

    kind: DocumentKind
    text: str
    pages: int | None = None
    title: str | None = None


def find_urls(request: Request) -> list[str]:
    """Return unique URLs, in order of appearance, from the request's course detail text."""
    partner = request.partner_course
    sources = [
        partner.supporting_url,
        partner.syllabus,
        partner.other_information,
        partner.title,
        request.prerequisites,
        request.comments,
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


DRIVE_FILE = re.compile(r"^/file/d/([\w-]+)")
GOOGLE_DOC = re.compile(r"^/(document|presentation|spreadsheets)/d/([\w-]+)")
DOC_EXPORT = {"document": "txt", "presentation": "pdf", "spreadsheets": "csv"}


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
    reader = PdfReader(io.BytesIO(data))
    pages = [(page.extract_text() or "").strip() for page in reader.pages]
    return "\n\n".join(p for p in pages if p), len(reader.pages)


def html_text(data: bytes, encoding: str | None = None) -> tuple[str, str | None]:
    soup = BeautifulSoup(data, "html.parser", from_encoding=encoding)
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    lines = [line.strip() for line in soup.get_text("\n").splitlines()]
    title = soup.title.string.strip() if soup.title and soup.title.string else None
    return "\n".join(line for line in lines if line), title


def extract_text(content_type: str | None, data: bytes) -> Extracted:
    header = content_type or ""
    kind = header.split(";")[0].strip().lower()
    charset_match = re.search(r"charset=([\w-]+)", header, re.I)
    charset = charset_match.group(1) if charset_match else None
    head = data[:256].lstrip().lower()
    if kind == "application/pdf" or data.startswith(b"%PDF-"):
        text, pages = pdf_text(data)
        return Extracted("pdf", text, pages=pages)
    if kind in ("text/html", "application/xhtml+xml") or head.startswith(
        (b"<!doctype html", b"<html")
    ):
        text, title = html_text(data, charset)
        return Extracted("html", text, title=title)
    if kind.startswith("text/"):
        return Extracted("text", data.decode(charset or "utf-8", errors="replace"))
    raise ValueError(f"Unsupported content type: {kind or 'unknown'}")


def document_name(url: str) -> str:
    """File name for a URL's text under `documents/`; the same URL always maps to one file."""
    return f"{hashlib.sha256(url.encode()).hexdigest()[:16]}.txt"


def rendered_text(url: str, render: Renderer, extracted: Extracted) -> Extracted:
    """Re-read a page in a browser when its HTML carried too little text to be the document.

    Single-page catalogues (Korea University, NYCU, TUMonline) serve a loading shell and
    fill it in with scripts; the rendered page replaces the shell only when it says more.
    A render failure keeps the shell, so the record never gets worse than the plain fetch.
    """
    if extracted.kind != "html" or len(extracted.text) >= MIN_HTML_TEXT:
        return extracted
    try:
        text, title = html_text(render(url))
    except Exception:
        return extracted
    if len(text) <= len(extracted.text):
        return extracted
    return Extracted("html", text, title=title or extracted.title)


def fetch_document(url: str, fetch: Fetcher, render: Renderer | None = None) -> LinkedDocument:
    """Fetch one URL; every failure is recorded in the result rather than raised."""
    target = direct_url(url)
    record = LinkedDocument(url=url)
    try:
        status, content_type, data = fetch(target)
        if status >= 400:
            raise ValueError(f"HTTP {status}")
        if len(data) > MAX_BYTES:
            raise ValueError(f"Document exceeds {MAX_BYTES} bytes")
        extracted = extract_text(content_type, data)
        if render is not None:
            extracted = rendered_text(target, render, extracted)
        record.kind, record.title, record.pages = extracted.kind, extracted.title, extracted.pages
        record.bytes = len(extracted.text.encode())
        if not extracted.text:
            record.status, record.error = "empty", "No extractable text"
        elif record.bytes > MAX_TEXT_BYTES:
            record.status = "too_large"
            record.error = f"Extracted text exceeds {MAX_TEXT_BYTES} bytes"
        else:
            record.status, record.text = "fetched", extracted.text
            record.path = f"documents/{document_name(url)}"
    except Exception as error:
        record.error = str(error).splitlines()[0] if str(error) else type(error).__name__
    return record


def fetch_documents(
    request: Request,
    fetch: Fetcher,
    cache: dict[str, LinkedDocument] | None = None,
    render: Renderer | None = None,
) -> Request:
    """Attach `linked_documents` to the request in place."""
    cache = cache if cache is not None else {}
    documents: list[LinkedDocument] = []
    for url in find_urls(request):
        if url not in cache:
            cache[url] = fetch_document(url, fetch, render)
        documents.append(replace(cache[url]))
    request.linked_documents = documents
    partner = request.partner_course
    if partner.supporting_url:
        supporting = next((d for d in documents if d.url == partner.supporting_url), None)
        partner.supporting_document_status = supporting.status if supporting else "not_fetched"
    return request


def scrape(
    data: Document,
    fetch: Fetcher,
    checkpoint: Callable[[Request], None],
    render: Renderer | None = None,
) -> Document:
    """Stage 2: fetch every URL in the collected requests and add the text to the export.

    Each URL is fetched once per run; `checkpoint` is called with every request once
    its documents are known, so the run directory always reflects what was read so far.
    With `render`, HTML pages that arrive as script shells are re-read in a browser page.
    """
    cache: dict[str, LinkedDocument] = {}
    data.collection.linked_documents = "fetched_when_present"
    for index, request in enumerate(data.requests, 1):
        fetch_documents(request, fetch, cache, render)
        print(f"Scraped URLs for {index}/{len(data.requests)} requests", flush=True)
        checkpoint(request)
    return data


def playwright_fetcher(context: BrowserContext, timeout: float) -> Fetcher:
    """Fetch through the browser context so cookies and proxy settings apply."""

    def fetch(url: str) -> Fetched:
        response = context.request.get(url, timeout=timeout, max_redirects=10)
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
