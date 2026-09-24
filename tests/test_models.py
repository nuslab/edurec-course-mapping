import unittest
from datetime import UTC, datetime

import yaml

from edurec_course_mapping.models import LIST_COLUMNS, Proposal, Request, hydrate, plain
from edurec_course_mapping.store import dump
from tests.test_export import records
from tests.test_review import FALLBACK, proposal


class RecordTests(unittest.TestCase):
    def test_hydrate_is_the_inverse_of_plain(self) -> None:
        _, request = records()[0]
        loaded = hydrate(Request, yaml.safe_load(dump(plain(request))))
        self.assertEqual(loaded, request)
        advice = proposal(request, fallback=FALLBACK)
        self.assertEqual(hydrate(Proposal, plain(advice)), advice)
        with self.assertRaises(ValueError):
            hydrate(Proposal, ["not", "a", "record"])

    def test_malformed_proposals_are_refused(self) -> None:
        _, request = records()[0]
        valid = plain(proposal(request))
        for field, value in [
            ("verdict", "Approve"),
            ("verdict", "request remapping"),
            ("confidence", "High"),
            ("overlap_percent", "80"),
            ("overlap_percent", 80.5),
            ("concerns", "one concern"),
            ("fallback", {"verdict": "reject"}),
            ("fallback_verdict", "reject"),
            ("remap", {"analysis": "no target"}),
            # An unquoted YAML timestamp loads as a datetime, which JSON cannot hold.
            ("comment", datetime(2026, 9, 22, tzinfo=UTC)),
        ]:
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                hydrate(Proposal, {**valid, field: value})

    def test_course_is_derived_from_the_request(self) -> None:
        _, request = records()[0]
        self.assertEqual(request.course, "EXU 1001 (Example College) -> CS3243")

    def test_list_columns_follow_the_grid_order(self) -> None:
        # `parse_listing` zips cells with LIST_COLUMNS, so ListRow's field order is the grid's.
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
                "exchange_program",
                "partner_university",
                "partner_subject",
                "partner_number",
                "nus_subject",
                "nus_number",
                "reassigned_to",
            ),
        )
