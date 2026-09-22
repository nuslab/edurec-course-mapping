import io
import unittest

from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from edurec_mappings.documents import direct_url, document_name, fetch_documents, find_urls
from edurec_mappings.models import LinkedDocument
from edurec_mappings.parse import detail, expand_action
from tests.test_parse import fixture


def pdf_bytes(text):
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
    def test_urls_found_in_detail_fields_without_duplicates(self):
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

    def test_pdf_html_and_failures_are_recorded(self):
        request = detail(fixture("individual.html"))
        request.partner_course.other_information = (
            "https://example.org/page https://example.org/missing"
        )
        calls = []

        def fetch(url):
            calls.append(url)
            if "dropbox" in url:
                return 200, "application/pdf", pdf_bytes("Syllabus week one")
            if url.endswith("/page"):
                return (
                    200,
                    "text/html; charset=utf-8",
                    b"<html><head><title>T</title><script>x()</script></head><body><p>Outline</p></body></html>",
                )
            return 404, "text/html", b"gone"

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
        self.assertEqual(request.partner_course.supporting_document_status, "fetched")
        fetch_documents(request, fetch, cache)
        self.assertEqual(len(calls), 3, "Cached URLs must not be fetched again")
        self.assertIsNot((request.linked_documents or [])[0], cache[docs[0].url])

    def test_fetch_errors_never_abort_collection(self):
        request = detail(fixture("individual.html"))

        def fetch(url):
            raise ConnectionError("network down")

        fetch_documents(request, fetch)
        (document,) = request.linked_documents or []
        self.assertEqual((document.status, document.error), ("failed", "network down"))
        self.assertEqual(request.partner_course.supporting_document_status, "failed")

    def test_expand_action_only_when_larger_view_is_offered(self):
        soup = fixture("main.html")
        self.assertIsNone(expand_action(soup), "Snapshot already shows 100 rows")
        soup.find("a", id="PTS_CFG_CL_STD_RSL$hviewall$0").string = "View 100"
        self.assertEqual(expand_action(soup), "PTS_CFG_CL_STD_RSL$hviewall$0")
        soup.find("a", id="PTS_CFG_CL_STD_RSL$hviewall$0").string = "View All"
        self.assertEqual(expand_action(soup), "PTS_CFG_CL_STD_RSL$hviewall$0")


if __name__ == "__main__":
    unittest.main()
