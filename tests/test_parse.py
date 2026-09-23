import tempfile
import unittest
from pathlib import Path

import yaml
from bs4 import BeautifulSoup, Tag

from edurec_mappings.models import LIST_COLUMNS, plain
from edurec_mappings.parse import GRID, detail, listing
from edurec_mappings.store import document, reset, save

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def fixture(name: str) -> BeautifulSoup:
    return BeautifulSoup((FIXTURES / name).read_text(), "html.parser")


def tag(soup: BeautifulSoup, element_id: str, name: str | None = None) -> Tag:
    found = soup.find(name, id=element_id)
    assert isinstance(found, Tag)
    return found


class ExtractionTests(unittest.TestCase):
    def test_list_pagination_and_actual_actions(self) -> None:
        result = listing(fixture("main.html"))
        self.assertEqual(result.range, (1, 100, 300))
        self.assertEqual(len(result.rows), 100)
        self.assertTrue(result.has_next)
        self.assertEqual(result.rows[0].action, "#ICRow271")
        self.assertEqual(result.rows[0].student_id, "A0000001X")
        self.assertEqual(len({r.student_id for r in result.rows}), 100)
        self.assertEqual(result.rows[0].nus_number, "3243")
        self.assertEqual(result.rows[0].partner_number, "1001")

    def test_full_detail_and_missing_values(self) -> None:
        result = detail(fixture("individual.html"))
        partner = result.partner_course
        self.assertEqual(partner.title, "Artificial Intelligence")
        self.assertIn("Markov decision processes", partner.syllabus or "")
        self.assertEqual(result.nus_course.number, "3243")
        self.assertEqual(partner.credits, "2.50")
        self.assertIsNone(partner.instruction_weeks)
        self.assertEqual(len(partner.contact_hours), 4)
        self.assertEqual(len(partner.assessments), 4)
        self.assertEqual(partner.assessments[0].weight_percent, "0.00")
        self.assertIn("&st=", partner.supporting_url or "")
        self.assertEqual(result.identity.student_id, "A0000000X")
        self.assertEqual(result.mapping_type, "Many to One")
        self.assertEqual(result.status, "Pending Approval")

    def test_group_identity_uses_student_and_not_sequence(self) -> None:
        soup = fixture("individual.html")
        tag(soup, "N_EXSP_WKST_HDR_EMPLID").string = "TEST_STUDENT_A"
        first = detail(soup)
        tag(soup, "N_EXSP_MOD_DT_TRNSFR_EQVLNCY_SEQ$0").string = "2"
        second = detail(soup)
        self.assertEqual(first.identity.mapping, second.identity.mapping)
        self.assertNotEqual(first.request_id, second.request_id)
        tag(soup, "N_EXSP_WKST_HDR_EMPLID").string = "TEST_STUDENT_B"
        third = detail(soup)
        self.assertNotEqual(first.identity.mapping, third.identity.mapping)

    def test_request_id_ignores_editable_content(self) -> None:
        soup = fixture("individual.html")
        first = detail(soup)
        tag(soup, "N_EXSP_MOD_DT_N_MOD_COMMENTS$0").string = "Reviewer added a comment"
        tag(soup, "N_EXSP_MOD_DT_N_URL$0").string = "https://example.org/new-syllabus"
        second = detail(soup)
        self.assertEqual(first.request_id, second.request_id)

    def test_missing_identity_is_rejected(self) -> None:
        soup = fixture("individual.html")
        tag(soup, "N_EXSP_WKST_HDR_EMPLID").string = ""
        with self.assertRaisesRegex(ValueError, "student ID"):
            detail(soup)

    def test_run_directory_layout_and_no_session_tokens(self) -> None:
        data = document()
        data.requests.append(detail(fixture("individual.html")))
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory) / "run"
            save(data, run)
            inventory = yaml.safe_load((run / "inventory.yaml").read_text())
            self.assertEqual(inventory, data.inventory())
            self.assertNotIn("requests", inventory, "Requests live in one file each")
            self.assertEqual(inventory["schema_version"], 5)
            (path,) = (run / "requests").iterdir()
            self.assertEqual(path.name, f"{data.requests[0].request_id}.yaml")
            content = path.read_text()
            result = yaml.safe_load(content)
            self.assertEqual(result, plain(data.requests[0]))
            self.assertNotIn("ICSID", content)
            self.assertNotIn("&id", content, "Records must not be emitted as YAML aliases")
            self.assertEqual(result["related_request_ids"], [])
            self.assertNotIn("name", result["student"])
            self.assertEqual(
                result["partner_course"]["assessments"][0].keys(),
                {"method", "weight_percent", "remark"},
            )

    def test_reset_clears_a_previous_run_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory) / "run"
            (run / "requests").mkdir(parents=True)
            (run / "requests" / "stale.yaml").write_text("old")
            (run / "documents").mkdir()
            (run / "inventory.yaml").write_text("old")
            (run / "decisions").mkdir()
            reset(run)
            self.assertEqual({p.name for p in run.iterdir()}, {"decisions"})
            reset(run / "missing")

    def test_wrong_page_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            detail(fixture("main.html"))
        with self.assertRaises(ValueError):
            listing(fixture("individual.html"))


if __name__ == "__main__":
    unittest.main()


def test_single_row_counter_has_no_range_dash() -> None:
    html = (
        '<div id="win0divPTS_CFG_CL_STD_RSLGP$0"><span class="PSGRIDCOUNTER">1 of 1</span></div>'
        f'<table id="{GRID}"><tr onclick="x(\'#ICRow0\')">' + "<td>a</td>" * 14 + "</tr></table>"
    )
    page = listing(BeautifulSoup(html, "html.parser"))
    assert page.range == (1, 1, 1)
    assert len(page.rows) == 1


class ListColumnsTests(unittest.TestCase):
    def test_columns_follow_the_grid_order(self) -> None:
        # `listing` zips cells with LIST_COLUMNS, so ListRow's field order is the grid's.
        self.assertEqual(
            LIST_COLUMNS,
            (
                "user_id",
                "submitted_at",
                "student_id",
                "student_name",
                "institution",
                "academic_career",
                "term_code",
                "study_program",
                "partner_university",
                "partner_subject",
                "partner_number",
                "nus_subject",
                "nus_number",
                "reassigned_to",
            ),
        )
