import tempfile
import unittest
from pathlib import Path

import yaml
from bs4 import BeautifulSoup

from edurec_mappings.parse import detail, document, listing, reset, save

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def fixture(name):
    return BeautifulSoup((FIXTURES / name).read_text(), "html.parser")


class ExtractionTests(unittest.TestCase):
    def test_list_pagination_and_actual_actions(self):
        result = listing(fixture("main.html"))
        self.assertEqual(result.range, (1, 100, 300))
        self.assertEqual(len(result.rows), 100)
        self.assertTrue(result.has_next)
        self.assertEqual(result.rows[0].action, "#ICRow271")
        self.assertEqual(result.rows[0].student_id, "A0000001X")
        self.assertEqual(len({r.student_id for r in result.rows}), 100)
        self.assertEqual(result.rows[0].nus_number, "3243")
        self.assertEqual(result.rows[0].partner_number, "1001")

    def test_full_detail_and_missing_values(self):
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
        self.assertTrue(result.many_to_one)
        self.assertEqual(result.status, "Pending Approval")

    def test_group_identity_uses_student_and_not_sequence(self):
        soup = fixture("individual.html")
        soup.find(id="N_EXSP_WKST_HDR_EMPLID").string = "TEST_STUDENT_A"
        first = detail(soup)
        soup.find(id="N_EXSP_MOD_DT_TRNSFR_EQVLNCY_SEQ$0").string = "2"
        second = detail(soup)
        self.assertEqual(first.group_id, second.group_id)
        self.assertNotEqual(first.request_id, second.request_id)
        soup.find(id="N_EXSP_WKST_HDR_EMPLID").string = "TEST_STUDENT_B"
        third = detail(soup)
        self.assertNotEqual(first.group_id, third.group_id)

    def test_request_id_ignores_editable_content(self):
        soup = fixture("individual.html")
        first = detail(soup)
        soup.find(id="N_EXSP_MOD_DT_N_MOD_COMMENTS$0").string = "Reviewer added a comment"
        soup.find(id="N_EXSP_MOD_DT_N_URL$0").string = "https://example.org/new-syllabus"
        second = detail(soup)
        self.assertEqual((first.request_id, first.group_id), (second.request_id, second.group_id))

    def test_missing_identity_is_rejected(self):
        soup = fixture("individual.html")
        soup.find(id="N_EXSP_WKST_HDR_EMPLID").string = ""
        with self.assertRaisesRegex(ValueError, "student ID"):
            detail(soup)

    def test_run_directory_layout_and_no_session_tokens(self):
        data = document()
        data.requests.append(detail(fixture("individual.html")))
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory) / "run"
            save(data, run)
            inventory = yaml.safe_load((run / "inventory.yaml").read_text())
            self.assertEqual(inventory, data.inventory())
            self.assertNotIn("requests", inventory, "Requests live in one file each")
            self.assertEqual(inventory["schema_version"], 3)
            self.assertEqual(inventory["mapping_groups"][0]["completeness"], "unverified")
            (path,) = (run / "requests").iterdir()
            self.assertEqual(path.name, f"{data.requests[0].request_id}.yaml")
            content = path.read_text()
            result = yaml.safe_load(content)
            self.assertEqual(result, data.requests[0].to_dict())
            self.assertNotIn("ICSID", content)
            self.assertNotIn("&id", content, "Records must not be emitted as YAML aliases")
            self.assertEqual(result["related_request_ids"], [])
            self.assertNotIn("name", result["student"])
            self.assertEqual(
                result["partner_course"]["assessments"][0].keys(),
                {"method", "weight_percent", "remark"},
            )

    def test_reset_clears_a_previous_run_only(self):
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

    def test_wrong_page_is_rejected(self):
        with self.assertRaises(ValueError):
            detail(fixture("main.html"))
        with self.assertRaises(ValueError):
            listing(fixture("individual.html"))


if __name__ == "__main__":
    unittest.main()
