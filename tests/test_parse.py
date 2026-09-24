import unittest
from pathlib import Path

from bs4 import BeautifulSoup, Tag

from edurec_course_mapping.models import GridCounter, Request
from edurec_course_mapping.parse import (
    GRID,
    VIEW_ALL,
    approval_status,
    can_expand,
    parse_detail,
    parse_listing,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"
TERM_CODE = "2620"


def fixture(name: str) -> BeautifulSoup:
    return BeautifulSoup((FIXTURES / name).read_text(), "html.parser")


def tag(soup: BeautifulSoup, element_id: str, name: str | None = None) -> Tag:
    found = soup.find(name, id=element_id)
    assert isinstance(found, Tag)
    return found


def detail(soup: BeautifulSoup | None = None) -> Request:
    """The detail fixture, or `soup`, as opened from a row of term `TERM_CODE`."""
    return parse_detail(soup or fixture("individual.html"), TERM_CODE)


class ParseTests(unittest.TestCase):
    def test_list_pagination_and_actual_actions(self) -> None:
        result = parse_listing(fixture("main.html"))
        self.assertEqual(result.counter, GridCounter(1, 100, 300))
        self.assertEqual(len(result.rows), 100)
        self.assertTrue(result.has_next)
        self.assertEqual(result.rows[0].row_action, "#ICRow271")
        self.assertEqual(result.rows[0].student_id, "A0000001X")
        self.assertEqual(len({r.student_id for r in result.rows}), 100)
        self.assertEqual(result.rows[0].nus_number, "3243")
        self.assertEqual(result.rows[0].partner_number, "1001")

    def test_full_detail_and_missing_values(self) -> None:
        result = detail()
        partner = result.partner_course
        self.assertEqual(partner.title, "Artificial Intelligence")
        self.assertIn("Markov decision processes", partner.synopsis or "")
        self.assertEqual(result.nus_course.number, "3243")
        self.assertEqual(partner.credits, "2.50")
        self.assertIsNone(partner.instruction_weeks)
        self.assertEqual(len(partner.contact_hours), 4)
        self.assertEqual(len(partner.assessments), 4)
        self.assertEqual(partner.assessments[0].weight_percent, "0.00")
        self.assertIn("&st=", partner.supporting_url or "")
        self.assertEqual(result.identity.student_id, "A0000000X")
        self.assertEqual(result.identity.term_code, TERM_CODE)
        self.assertEqual(result.mapping_type, "Many to One")
        self.assertEqual(result.approval_status, "Pending Approval")
        self.assertEqual(approval_status(fixture("individual.html")), "Pending Approval")

    def test_missing_identity_is_rejected(self) -> None:
        soup = fixture("individual.html")
        tag(soup, "N_EXSP_WKST_HDR_EMPLID").string = ""
        with self.assertRaisesRegex(ValueError, "student ID"):
            detail(soup)
        with self.assertRaisesRegex(ValueError, "term code"):
            parse_detail(fixture("individual.html"), None)

    def test_wrong_page_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            detail(fixture("main.html"))
        with self.assertRaises(ValueError):
            approval_status(fixture("main.html"))
        with self.assertRaises(ValueError):
            parse_listing(fixture("individual.html"))

    def test_single_row_counter_has_no_range_dash(self) -> None:
        html = (
            '<div id="win0divPTS_CFG_CL_STD_RSLGP$0"><span class="PSGRIDCOUNTER">1 of 1</span>'
            f'</div><table id="{GRID}"><tr onclick="x(\'#ICRow0\')">'
            + "<td>a</td>" * 14
            + "</tr></table>"
        )
        page = parse_listing(BeautifulSoup(html, "html.parser"))
        self.assertEqual(page.counter, GridCounter(1, 1, 1))
        self.assertEqual(len(page.rows), 1)

    def test_expands_only_when_a_larger_view_is_offered(self) -> None:
        soup = fixture("main.html")
        self.assertFalse(can_expand(soup), "Snapshot already shows 100 rows")
        for label in ("View 100", "View All"):
            tag(soup, VIEW_ALL, "a").string = label
            self.assertTrue(can_expand(soup))
