import io
import sys
import time
import unittest
import zipfile
from typing import NoReturn
from unittest.mock import patch

from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from edurec_course_mapping.documents import (
    Fetched,
    clean,
    direct_url,
    error_summary,
    fetch_document,
    find_urls,
    process_renderer,
    run_with_deadline,
    scrape,
)
from edurec_course_mapping.models import LinkedDocument
from tests.test_parse import detail


def pdf_bytes(text: str) -> bytes:
    writer = PdfWriter()
    page = writer.add_blank_page(width=200, height=200)

    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    page[NameObject("/Resources")] = DictionaryObject(
        {NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})}
    )
    stream = DecodedStreamObject()
    stream.set_data(f"BT /F1 12 Tf 20 100 Td ({text}) Tj ET".encode())
    page[NameObject("/Contents")] = writer._add_object(stream)
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def zip_bytes(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in files.items():
            archive.writestr(name, data)
    return buffer.getvalue()


def docx_bytes(*paragraphs: str) -> bytes:
    body = "".join(f"<w:p><w:r><w:t>{p}</w:t></w:r></w:p>" for p in paragraphs)
    xml = (
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body>{body}</w:body></w:document>"
    )
    return zip_bytes({"word/document.xml": xml.encode()})


def folder_entry(href: str, title: str) -> str:
    return (
        f'<div class="flip-entry"><div class="flip-entry-info"><a href="{href}">'
        f'<div class="flip-entry-title">{title}</div></a></div></div>'
    )


class DocumentTests(unittest.TestCase):
    def test_urls_found_in_detail_fields_without_duplicates(self) -> None:
        request = detail()
        partner = request.partner_course
        partner.synopsis = (partner.synopsis or "") + (
            " See https://example.org/outline.html), and http://example.org/a."
        )
        request.review_comments = "https://example.org/outline.html again"
        self.assertEqual(
            find_urls(request),
            [partner.supporting_url, "https://example.org/outline.html", "http://example.org/a"],
        )
        self.assertTrue(direct_url(partner.supporting_url or "").endswith("&dl=1"))
        self.assertEqual(direct_url("https://example.org/a?x=1"), "https://example.org/a?x=1")
        self.assertEqual(
            direct_url("https://drive.google.com/file/d/1-GlZKPY_x/view?usp=sharing"),
            "https://drive.google.com/uc?export=download&id=1-GlZKPY_x",
        )
        self.assertEqual(
            direct_url("https://docs.google.com/document/d/1yWVPs/edit"),
            "https://docs.google.com/document/d/1yWVPs/export?format=txt",
        )
        self.assertEqual(
            direct_url("https://docs.google.com/presentation/d/abc/edit#slide=1"),
            "https://docs.google.com/presentation/d/abc/export?format=pdf",
        )
        published = "https://docs.google.com/document/d/e/2PACX-1vT/pub"
        self.assertEqual(direct_url(published), published)
        self.assertEqual(
            direct_url("https://drive.google.com/drive/folders/xyz"),
            "https://drive.google.com/drive/folders/xyz",
        )

    def test_pdf_html_and_failures_are_recorded(self) -> None:
        request = detail()
        request.partner_course.other_information = (
            "https://example.org/page https://example.org/missing"
        )
        calls = []

        def fetch(url: str) -> Fetched:
            calls.append(url)
            if "dropbox" in url:
                return Fetched(200, "application/pdf", pdf_bytes("Syllabus week one"))
            if url.endswith("/page"):
                return Fetched(
                    200,
                    "text/html; charset=utf-8",
                    b"<html><head><title>T</title><script>x()</script></head><body><p>Outline</p></body></html>",
                )
            return Fetched(404, "text/html", b"gone")

        cache: dict[str, LinkedDocument] = {}
        scrape([request], cache, fetch)
        docs = list(cache.values())
        self.assertEqual([d.status for d in docs], ["fetched", "fetched", "failed"])
        self.assertIn("Syllabus week one", docs[0].text or "")
        self.assertEqual((docs[0].kind, docs[0].page_count), ("pdf", 1))
        self.assertEqual((docs[1].text, docs[1].kind, docs[1].title), ("T\nOutline", "html", "T"))
        self.assertEqual(docs[1].text_bytes, len(b"T\nOutline"))
        self.assertEqual(docs[2].error, "HTTP 404")
        self.assertIsNone(docs[2].text)
        scrape([request], cache, fetch)
        self.assertEqual(len(calls), 3, "Cached URLs must not be fetched again")

    def test_fetch_errors_never_abort_collection(self) -> None:
        request = detail()

        def fetch(url: str) -> NoReturn:
            raise ConnectionError("network down")

        documents: dict[str, LinkedDocument] = {}
        scrape([request], documents, fetch)
        (document,) = documents.values()
        self.assertEqual((document.status, document.error), ("failed", "network down"))

    def test_script_shells_are_rendered_when_a_renderer_is_given(self) -> None:
        request = detail()
        request.partner_course.other_information = (
            "https://example.org/shell https://example.org/broken-shell https://example.org/full"
        )
        shell = b"<html><head><title>Loading</title></head><body><img src=l.gif></body></html>"
        full = b"<html><body>" + b"<p>Week one outline</p>" * 100 + b"</body></html>"
        rendered = []

        def fetch(url: str) -> Fetched:
            if "dropbox" in url:
                return Fetched(200, "application/pdf", pdf_bytes("Syllabus week one"))
            return Fetched(200, "text/html", full if url.endswith("/full") else shell)

        def render(url: str) -> bytes:
            rendered.append(url)
            if url.endswith("/broken-shell"):
                raise TimeoutError("navigation timed out")
            return (
                b"<html><head><title>DATA303</title></head><body>"
                b"<p>Course Schedule per Week</p><p>Variational Autoencoder</p></body></html>"
            )

        documents: dict[str, LinkedDocument] = {}
        scrape([request], documents, fetch, render)
        docs = {url.rsplit("/", 1)[-1]: d for url, d in documents.items()}
        # Only short HTML pages are rendered; the PDF and the full page are not.
        self.assertEqual(
            rendered, ["https://example.org/shell", "https://example.org/broken-shell"]
        )
        self.assertEqual(docs["shell"].status, "fetched")
        self.assertEqual(docs["shell"].title, "DATA303")
        self.assertIn("Variational Autoencoder", docs["shell"].text or "")
        # A failed render keeps what the plain fetch returned.
        broken = docs["broken-shell"]
        self.assertEqual((broken.status, broken.text), ("fetched", "Loading"))
        self.assertIn("Week one outline", docs["full"].text or "")

    def test_error_summary_is_the_first_line_or_the_type(self) -> None:
        self.assertEqual(error_summary(ValueError("HTTP 404\ndetail")), "HTTP 404")
        self.assertEqual(error_summary(TimeoutError()), "TimeoutError")

    def test_pdf_text_is_cleaned(self) -> None:
        self.assertEqual(clean("\ud835\udc65 and \ud835"), "\U0001d465 and \ufffd")
        self.assertEqual(clean("\x1f Data\r\nQuality\rWeek\t1\n"), " Data\nQuality\nWeek\t1\n")

    def test_docx_and_zip_files_are_read_file_by_file(self) -> None:
        archive = zip_bytes(
            {
                "b/outline.docx": docx_bytes("Week 1", "Search"),
                "a/scan.png": b"\x89PNG",
                "c/slides.pdf": pdf_bytes("Planning"),
            }
        )
        responses = {
            "https://example.org/a.docx": Fetched(200, None, docx_bytes("Week 1", "Search")),
            "https://example.org/all.zip": Fetched(200, "application/zip", archive),
            "https://example.org/pics.zip": Fetched(
                200, "application/zip", zip_bytes({"x.png": b"\x89PNG"})
            ),
        }
        docs = {url: fetch_document(url, responses.__getitem__) for url in responses}
        docx = docs["https://example.org/a.docx"]
        self.assertEqual((docx.status, docx.kind, docx.text), ("fetched", "docx", "Week 1\nSearch"))
        bundle = docs["https://example.org/all.zip"]
        self.assertEqual((bundle.status, bundle.kind), ("fetched", "zip"))
        text = bundle.text or ""
        self.assertIn("=== a/scan.png ===\n[Not read: Unsupported content type: unknown]", text)
        self.assertIn("=== b/outline.docx ===\nWeek 1\nSearch", text)
        self.assertIn("Planning", text)
        self.assertLess(text.index("a/scan.png"), text.index("b/outline.docx"))
        self.assertEqual(docs["https://example.org/pics.zip"].status, "empty")

    def test_drive_folders_are_read_through_the_embedded_view(self) -> None:
        listing = (
            "<html><head><title>CS3244 Mapping</title></head><body>"
            + folder_entry("https://drive.google.com/file/d/F1/view?usp=drive_web", "Syllabus.pdf")
            + folder_entry("https://drive.google.com/file/d/F2/view", "Gone.pdf")
            + folder_entry("https://drive.google.com/drive/folders/SUB", "Old")
            + "</body></html>"
        )
        calls = []

        def fetch(url: str) -> Fetched:
            calls.append(url)
            if url == "https://drive.google.com/embeddedfolderview?id=FOLDER":
                return Fetched(200, "text/html", listing.encode())
            if url.endswith("id=F1"):
                return Fetched(200, "application/pdf", pdf_bytes("Week one"))
            return Fetched(404, "text/html", b"")

        doc = fetch_document("https://drive.google.com/drive/folders/FOLDER?usp=sharing", fetch)
        self.assertEqual((doc.status, doc.kind, doc.title), ("fetched", "folder", "CS3244 Mapping"))
        self.assertIn("=== Gone.pdf ===\n[Not read: HTTP 404]", doc.text or "")
        self.assertIn("=== Syllabus.pdf ===\nWeek one", doc.text or "")
        self.assertEqual(len(calls), 3, "Subfolders are not followed")

    def test_all_files_reads_every_file_of_every_subfolder(self) -> None:
        def listing(title: str, *entries: str) -> bytes:
            return f"<html><title>{title}</title><body>{''.join(entries)}</body></html>".encode()

        top = [
            folder_entry(f"https://drive.google.com/file/d/T{i}/view", f"{i:02}.txt")
            for i in range(21)
        ]
        pages = {
            "FOLDER": listing(
                "Course", *top, folder_entry("https://drive.google.com/drive/folders/SUB", "Weeks")
            ),
            "SUB": listing(
                "Weeks",
                folder_entry("https://drive.google.com/drive/folders/DEEP", "Late"),
                folder_entry("https://drive.google.com/file/d/W1/view", "Week 1.txt"),
            ),
            "DEEP": listing(
                "Late", folder_entry("https://drive.google.com/file/d/W9/view", "Week 9.txt")
            ),
        }

        def fetch(url: str) -> Fetched:
            query = url.rpartition("id=")[2]
            if query in pages:
                return Fetched(200, "text/html", pages[query])
            return Fetched(200, "text/plain", f"text of {query}".encode())

        url = "https://drive.google.com/drive/folders/FOLDER"
        doc = fetch_document(url, fetch, all_files=True)
        self.assertEqual((doc.status, doc.title), ("fetched", "Course"))
        text = doc.text or ""
        self.assertIn("=== 20.txt ===\ntext of T20", text)
        self.assertIn("=== Weeks/Week 1.txt ===\ntext of W1", text)
        self.assertIn("=== Weeks/Late/Week 9.txt ===\ntext of W9", text)
        self.assertNotIn("20.txt", fetch_document(url, fetch).text or "")

    def test_sign_in_and_bot_block_pages_are_failures(self) -> None:
        def fetch(url: str) -> Fetched:
            title = "Google Drive: Sign-in" if "drive" in url else "Request Rejected"
            return Fetched(200, "text/html", f"<title>{title}</title><p>Sign in</p>".encode())

        drive = fetch_document("https://drive.google.com/file/d/X/view", fetch)
        self.assertEqual(
            (drive.status, drive.error), ("failed", "Sign-in or block page: Google Drive: Sign-in")
        )
        self.assertEqual(fetch_document("https://example.org/c", fetch).status, "failed")

    def test_hash_routes_are_rendered_even_when_the_index_is_long(self) -> None:
        index = b"<html><body>" + b"<p>Admissions and faculties</p>" * 100 + b"</body></html>"
        course = index.replace(b"<body>", b"<body><h1>EXU1001 Artificial Intelligence</h1>")
        rendered = []

        def render(url: str) -> bytes:
            rendered.append(url)
            return course

        def fetch(url: str) -> Fetched:
            return Fetched(200, "text/html", index)

        routed = "https://example.org/catalog#/courses/EXU1001"
        self.assertIn("EXU1001", fetch_document(routed, fetch, render).text or "")
        fetch_document("https://example.org/catalog", fetch, render)
        self.assertEqual(rendered, [routed])

    def test_rate_limits_are_retried_once(self) -> None:
        statuses = iter([429, 200, 503, 503])

        def fetch(url: str) -> Fetched:
            return Fetched(next(statuses), "text/plain", b"Outline")

        with patch("edurec_course_mapping.documents.time.sleep") as sleep:
            self.assertEqual(fetch_document("https://example.org/a", fetch).status, "fetched")
            self.assertEqual(fetch_document("https://example.org/b", fetch).error, "HTTP 503")
        self.assertEqual(sleep.call_count, 2)

    def test_renders_run_in_a_process_killed_at_the_deadline(self) -> None:
        self.assertEqual(run_with_deadline([sys.executable, "-c", "print('ok')"], 30), b"ok\n")
        with self.assertRaisesRegex(RuntimeError, "status 3"):
            run_with_deadline([sys.executable, "-c", "raise SystemExit(3)"], 30)
        # The child's own children (Playwright's driver and browser) must not hold it open.
        started = time.monotonic()
        with self.assertRaisesRegex(TimeoutError, "exceeded 1 s"):
            run_with_deadline(["sh", "-c", "sleep 30 & sleep 30"], 1)
        self.assertLess(time.monotonic() - started, 10)

    def test_process_renderer_runs_render_html_with_the_export_settings(self) -> None:
        with patch(
            "edurec_course_mapping.documents.run_with_deadline", return_value=b"<p>x</p>"
        ) as run:
            html = process_renderer("socks5://proxy:1080", 60000, 5000)("https://example.org/#/c")
        self.assertEqual(html, b"<p>x</p>")
        command, deadline = run.call_args.args
        self.assertEqual(
            " ".join(command[1:]),
            "-m edurec_course_mapping render --html --timeout 60 --settle 5.0"
            " --proxy socks5://proxy:1080 https://example.org/#/c",
        )
        self.assertEqual(deadline, 95)
