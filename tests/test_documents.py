import io
import unittest
from typing import NoReturn

from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from edurec_mappings.documents import direct_url, document_name, fetch_documents, find_urls
from edurec_mappings.models import Fetched, LinkedDocument
from edurec_mappings.parse import detail
from tests.test_parse import fixture


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


class DocumentTests(unittest.TestCase):
    def test_urls_found_in_detail_fields_without_duplicates(self) -> None:
        request = detail(fixture("individual.html"))
        partner = request.partner_course
        partner.syllabus = (partner.syllabus or "") + (
            " See https://example.org/outline.html), and http://example.org/a."
        )
        request.comments = "https://example.org/outline.html again"
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
        self.assertEqual(
            direct_url("https://drive.google.com/drive/folders/xyz"),
            "https://drive.google.com/drive/folders/xyz",
        )

    def test_pdf_html_and_failures_are_recorded(self) -> None:
        request = detail(fixture("individual.html"))
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
        fetch_documents(request, fetch, cache)
        docs = request.linked_documents or []
        self.assertEqual([d.status for d in docs], ["fetched", "fetched", "failed"])
        self.assertIn("Syllabus week one", docs[0].text or "")
        self.assertEqual((docs[0].kind, docs[0].pages), ("pdf", 1))
        self.assertEqual((docs[1].text, docs[1].kind, docs[1].title), ("T\nOutline", "html", "T"))
        self.assertEqual(docs[1].bytes, len(b"T\nOutline"))
        self.assertEqual(docs[1].path, f"documents/{document_name('https://example.org/page')}")
        self.assertEqual(docs[2].error, "HTTP 404")
        self.assertIsNone(docs[2].path)
        fetch_documents(request, fetch, cache)
        self.assertEqual(len(calls), 3, "Cached URLs must not be fetched again")
        self.assertIsNot((request.linked_documents or [])[0], cache[docs[0].url])

    def test_fetch_errors_never_abort_collection(self) -> None:
        request = detail(fixture("individual.html"))

        def fetch(url: str) -> NoReturn:
            raise ConnectionError("network down")

        fetch_documents(request, fetch)
        (document,) = request.linked_documents or []
        self.assertEqual((document.status, document.error), ("failed", "network down"))

    def test_script_shells_are_rendered_when_a_renderer_is_given(self) -> None:
        request = detail(fixture("individual.html"))
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

        fetch_documents(request, fetch, {}, render)
        docs = {d.url.rsplit("/", 1)[-1]: d for d in request.linked_documents or []}
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
