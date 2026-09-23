import copy
import hmac
import unittest

from edurec_mappings.anonymize import anonymize, pseudonym, request_id
from edurec_mappings.models import Identity, plain
from tests.test_export import records
from tests.test_parse import detail, fixture, tag

IDENTITY = Identity(
    student_id="A0000001X",
    academic_career="Undergraduate",
    partner_university="Technical University of Munich",
    exchange_program="SEP",
    term="2025/2026 Semester 1",
    term_code="2610",
    group="1",
    sequence="2",
)


class KeyTests(unittest.TestCase):
    def test_identifiers_are_keyed_hmacs(self) -> None:
        self.assertEqual(request_id(b"k", IDENTITY), request_id(b"k", copy.deepcopy(IDENTITY)))
        self.assertNotEqual(request_id(b"k", IDENTITY), request_id(b"other", IDENTITY))
        self.assertEqual(len(request_id(b"k", IDENTITY)), 24)
        expected = hmac.new(b"k", b"A0000001X", "sha256").hexdigest()[:12]
        self.assertEqual(pseudonym(b"k", "A0000001X"), f"student-{expected}")

    def test_anonymize_leaves_the_original_untouched(self) -> None:
        _, request = records()[0]
        before = plain(request)
        anonymous = anonymize(request, b"k")
        self.assertEqual(plain(request), before)
        self.assertEqual(anonymous.request_id, request_id(b"k", request.identity))
        self.assertTrue(anonymous.identity.student_id.startswith("student-"))

    def test_group_key_uses_student_and_not_sequence(self) -> None:
        soup = fixture("individual.html")
        tag(soup, "N_EXSP_WKST_HDR_EMPLID").string = "TEST_STUDENT_A"
        first = detail(soup)
        tag(soup, "N_EXSP_MOD_DT_TRNSFR_EQVLNCY_SEQ$0").string = "2"
        second = detail(soup)
        self.assertEqual(first.identity.group_key, second.identity.group_key)
        self.assertNotEqual(request_id(b"k", first.identity), request_id(b"k", second.identity))
        tag(soup, "N_EXSP_WKST_HDR_EMPLID").string = "TEST_STUDENT_B"
        third = detail(soup)
        self.assertNotEqual(first.identity.group_key, third.identity.group_key)

    def test_request_id_ignores_editable_content(self) -> None:
        soup = fixture("individual.html")
        first = detail(soup)
        tag(soup, "N_EXSP_MOD_DT_N_MOD_COMMENTS$0").string = "Reviewer added a comment"
        tag(soup, "N_EXSP_MOD_DT_N_URL$0").string = "https://example.org/new-syllabus"
        second = detail(soup)
        self.assertEqual(request_id(b"k", first.identity), request_id(b"k", second.identity))
