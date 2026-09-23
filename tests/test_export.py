import copy
import tempfile
import unittest
from collections.abc import Callable
from dataclasses import astuple, replace
from pathlib import Path
from unittest import mock

import yaml
from playwright.sync_api import sync_playwright

from edurec_mappings.browser import EduRec, subdivide
from edurec_mappings.cli import configured_terms
from edurec_mappings.documents import MAX_TEXT_BYTES, scrape
from edurec_mappings.export import export, restore_list
from edurec_mappings.models import Export, Fetched, Listing, ListRow, Partition, Request
from edurec_mappings.parse import detail
from edurec_mappings.store import Store
from tests.test_parse import fixture, tag


def run(
    site: "FakeSite", reassign_id: str = "", rows: int | None = None, terms: list[str] | None = None
) -> Export:
    data = Export()
    export(site, data, reassign_id=reassign_id, rows=rows, terms=terms)
    return data


def keys(requests: list[Request]) -> set[tuple[str, ...]]:
    return {astuple(r.identity) for r in requests}


def records() -> list[tuple[ListRow, Request]]:
    template = detail(fixture("individual.html"))
    result: list[tuple[ListRow, Request]] = []
    # A capped term, a second term, repeated student IDs, mixed reassignees,
    # and more than one page of matching rows exercise subdivision and limits.
    for i, term in enumerate(["2600"] * 7 + ["2610"] * 3):
        student = f"A{i // 2:03d}"
        row = ListRow(
            student_id=student,
            term_code=term,
            reassigned_to="OWNER" if i % 2 == 0 else "OTHER",
            partner_subject="CS",
            partner_number=str(i),
            nus_subject="CS",
            nus_number="3243",
            action=f"#ICRow{i}",
        )
        request = copy.deepcopy(template)
        request.identity = replace(
            request.identity, student_id=student, term=term, mapping_number=str(i), sequence="1"
        )
        request.term_code = row.term_code
        result.append((row, request))
    return result


def within(partition: Partition, r: ListRow, d: Request) -> bool:
    assert r.term_code is not None and r.student_id is not None
    return (
        partition.term_low <= int(r.term_code) <= partition.term_high
        and (partition.student_low is None or r.student_id >= partition.student_low)
        and (partition.student_high is None or r.student_id <= partition.student_high)
        and partition.group_low <= int(d.identity.mapping_number) <= partition.group_high
        and partition.sequence_low <= int(d.identity.sequence) <= partition.sequence_high
    )


class FakeSite:
    def __init__(
        self,
        entries: list[tuple[ListRow, Request]] | None = None,
        cap: int = 4,
        page_size: int = 2,
        fail_after: int | None = None,
    ) -> None:
        self.entries = entries if entries is not None else records()
        self.cap = cap
        self.page_size = page_size
        self.searches: list[Partition] = []
        self.opened = 0
        self.fail_after = fail_after

    def search(self, partition: Partition) -> Listing:
        self.searches.append(partition)
        self.matches = [(r, d) for r, d in self.entries if within(partition, r, d)]
        self.capped = len(self.matches) >= self.cap
        self.matches = self.matches[: self.cap]
        self.offset = 0
        return self.read_list()

    def read_list(self) -> Listing:
        page = self.matches[self.offset : self.offset + self.page_size]
        return Listing(
            rows=[r for r, _ in page],
            range=(self.offset + 1, self.offset + len(page), len(self.matches))
            if page
            else (0, 0, 0),
            capped=self.capped,
            has_next=self.offset + len(page) < len(self.matches),
        )

    def next_page(self) -> Listing:
        self.offset += self.page_size
        return self.read_list()

    def request(self, row: ListRow) -> Request:
        if self.fail_after is not None and self.opened >= self.fail_after:
            raise RuntimeError("Simulated lost session")
        self.opened += 1
        return copy.deepcopy(next(d for r, d in self.matches if r.action == row.action))

    def back(self) -> Listing:
        return self.read_list()


class ExportTests(unittest.TestCase):
    def test_return_to_first_page_recovers_pagination(self) -> None:
        class ResetOnBack(FakeSite):
            def back(self) -> Listing:
                self.offset = 0
                return self.read_list()

        data = run(ResetOnBack(cap=100))
        self.assertEqual(len(data.requests), 10)
        self.assertEqual(data.status, "complete")

    def test_blank_term_uses_configured_terms_and_explicit_term_overrides(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "terms.yaml"
            path.write_text("terms: [2610, 2620]\n")
            terms = configured_terms(path)
            self.assertEqual(terms, ["2610", "2620"])
            site = FakeSite()
            data = run(site, terms=terms)
            self.assertEqual(len(data.requests), 3)
            self.assertEqual({p.term_low for p in site.searches}, {2610, 2620})
            override = run(FakeSite(cap=100), terms=["2600"])
            self.assertEqual(len(override.requests), 7)

    def test_reusing_between_operator_does_not_trigger_form_rebuild(self) -> None:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            context = browser.new_context()
            page = context.new_page()
            page.set_content("""<form name="win0" id="N_EXSP_MOD_APPR">
                <select id="N_EXSP_MOD_VW2_STRM$op" onchange="window.rebuilds++">
                  <option value="2">=</option><option value="9" selected>between</option>
                </select>
                <input id="N_EXSP_MOD_VW2_STRM"><input id="N_EXSP_MOD_VW2_STRM$to">
                </form><script>window.rebuilds = 0;</script>""")
            site = EduRec(context)
            site.criterion("STRM", "0000", "9999")
            site.criterion("STRM", "0000", "2619")
            self.assertEqual(page.evaluate("window.rebuilds"), 0)
            self.assertEqual(site.control("STRM").input_value(), "0000")
            self.assertEqual(site.control("STRM$to").input_value(), "2619")
            browser.close()

    def test_leaving_between_operator_waits_for_form_rebuild(self) -> None:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            context = browser.new_context()
            page = context.new_page()
            page.set_content("""<form name="win0" id="N_EXSP_MOD_APPR">
                <select id="N_EXSP_MOD_VW2_EMPLID$op">
                  <option value="2">=</option><option value="5">&gt;=</option>
                  <option value="9" selected>between</option>
                </select>
                <input id="N_EXSP_MOD_VW2_EMPLID"><input id="N_EXSP_MOD_VW2_EMPLID$to">
                </form>""")
            site = EduRec(context)
            transitions = []

            def fake_transition(
                action: str,
                trigger: Callable[[], object] | None = None,
                target: str | None = None,
            ) -> None:
                transitions.append((action, target))
                if trigger:
                    trigger()

            with mock.patch.object(site, "transition", side_effect=fake_transition):
                site.criterion("EMPLID", "A0000500X", None)
            self.assertEqual(transitions, [("N_EXSP_MOD_VW2_EMPLID$op", None)])
            self.assertEqual(site.control("EMPLID").input_value(), "A0000500X")
            browser.close()

    def test_search_switches_grid_to_view_100(self) -> None:
        listing = fixture("main.html")
        tag(listing, "PTS_CFG_CL_STD_RSL$hviewall$0", "a").string = "View 100"
        for script in listing.find_all("script"):
            script.decompose()
        with sync_playwright() as p:
            browser = p.chromium.launch()
            context = browser.new_context()
            page = context.new_page()
            page.route(
                "**/*",
                lambda route: route.fulfill(
                    body="<html><body>"
                    + str(listing.find("form"))
                    + """
                <script>
                window.isLoaderInProcess = () => false;
                window.toggles = 0;
                window.submitAction_win0 = (_, action) => {
                    if (action !== 'PTS_CFG_CL_STD_RSL$hviewall$0') throw new Error(action);
                    const body = 'ICAction=' + encodeURIComponent(action);
                    fetch('/toggle', {method: 'POST', body}).then(() => {
                        window.toggles++;
                        const link = document.getElementById('PTS_CFG_CL_STD_RSL$hviewall$0');
                        link.textContent = 'View 10';
                        document.getElementById('ICStateNum').value = 'next';
                    });
                };
                </script></body></html>""",
                    content_type="text/html",
                ),
            )
            page.goto("https://local.test/approval")
            site = EduRec(context)
            first = site.read_list(expand=True)
            self.assertEqual(page.evaluate("window.toggles"), 1)
            self.assertEqual(len(first.rows), 100)
            site.read_list(expand=True)
            self.assertEqual(
                page.evaluate("window.toggles"), 1, "No toggle once View 10 is offered"
            )
            browser.close()

    def test_stale_search_results_are_rejected(self) -> None:
        class StaleSite(FakeSite):
            def search(self, partition: Partition) -> Listing:
                return super().search(Partition())

        data = Export()
        with self.assertRaisesRegex(RuntimeError, "outside the requested partition"):
            export(StaleSite(cap=100), data, terms=["2610"])
        self.assertEqual(data.status, "interrupted")

    def test_all_terms_and_all_pages_beyond_cap_without_duplicates(self) -> None:
        site = FakeSite()
        data = run(site)
        self.assertEqual(len(data.requests), 10)
        self.assertEqual(len(keys(data.requests)), 10)
        self.assertEqual({r.identity.term for r in data.requests}, {"2600", "2610"})
        self.assertEqual(data.status, "complete")
        self.assertGreater(site.opened, len(data.requests), "Overlapping splits reopen details")
        self.assertTrue(any(p.student_low or p.student_high for p in site.searches))

    def test_reassign_exact_case_insensitive_and_global_limit(self) -> None:
        data = run(FakeSite(cap=100), reassign_id="owner", rows=3)
        self.assertEqual(len(data.requests), 3)
        owned = keys([d for r, d in records() if r.reassigned_to == "OWNER"])
        self.assertLessEqual(keys(data.requests), owned)
        self.assertEqual(data.status, "row_limit_reached")

    def test_term_filter_leaves_documents_unset(self) -> None:
        data = run(FakeSite(), terms=["2610"])
        self.assertEqual(len(data.requests), 3)
        self.assertTrue(all(r.identity.term == "2610" for r in data.requests))
        self.assertTrue(all(r.linked_documents is None for r in data.requests))
        self.assertTrue(all(r.request_id == "" for r in data.requests), "keyed by the store")

    def test_scrape_stage_stores_documents_once_and_caps_their_size(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "store")
            data = run(FakeSite(), terms=["2610"])
            data.requests[0].comments = "see https://example.org/textbook.pdf"
            calls = []

            def fetch(url: str) -> Fetched:
                calls.append(url)
                if url.endswith("textbook.pdf"):
                    return Fetched(200, "text/plain", b"x" * (MAX_TEXT_BYTES + 1))
                return Fetched(200, "text/plain", b"Week 1: search")

            scrape(data.requests, fetch)
            self.assertEqual(len(calls), 2, "The shared supporting URL is fetched once")
            added = store.save(data.requests)
            saved = {
                v.request_id: yaml.safe_load((store.root / v.path()).read_text()) for v in added
            }
            (text_file,) = (store.root / "documents").iterdir()
            self.assertEqual(text_file.read_text(), "Week 1: search")
            for request in saved.values():
                document = request["linked_documents"][0]
                self.assertNotIn("text", document, "Text lives in the documents folder")
                self.assertEqual(document["path"], f"documents/{text_file.name}")
                self.assertEqual(
                    (document["status"], document["kind"], document["bytes"]),
                    ("fetched", "text", 14),
                )
            textbook = saved[added[0].request_id]["linked_documents"][1]
            self.assertEqual((textbook["status"], textbook["path"]), ("too_large", None))
            self.assertEqual(textbook["bytes"], MAX_TEXT_BYTES + 1)
            self.assertIn("exceeds", textbook["error"])

    def test_single_student_capped_search_splits_mapping_groups(self) -> None:
        entries = records()[:7]
        for row, request in entries:
            row.student_id = "ONE_STUDENT"
            request.identity.student_id = "ONE_STUDENT"
        site = FakeSite(entries)
        data = run(site, terms=["2600"])
        self.assertEqual(len(data.requests), 7)
        self.assertTrue(any(p.group_low != 0 or p.group_high != 999 for p in site.searches))

    def test_empty_and_no_matching_reassignee_complete(self) -> None:
        for site in (FakeSite([]), FakeSite(cap=100)):
            data = run(site, reassign_id="MISSING")
            self.assertEqual(data.requests, [])
            self.assertEqual(data.status, "complete")

    def test_interruption_retains_details_and_incomplete_status(self) -> None:
        data = Export()
        with self.assertRaisesRegex(RuntimeError, "lost session"):
            export(FakeSite(cap=100, fail_after=1), data)
        self.assertEqual(len(data.requests), 1)
        self.assertEqual((data.status, data.error), ("interrupted", "Simulated lost session"))

    def test_indivisible_cap_fails_instead_of_claiming_complete(self) -> None:
        p = Partition(2600, 2600, "A", "A", 1, 1, 1, 1)
        with self.assertRaisesRegex(RuntimeError, "indivisible"):
            subdivide(p, [ListRow(term_code="2600", student_id="A", action="#ICRow0")])


def page(start: int, end: int, total: int, has_next: bool = True, name: str = "row") -> Listing:
    rows = [ListRow(action=f"#ICRow{i}", student_id=f"{name}{i}") for i in range(start, end + 1)]
    return Listing(rows=rows, range=(start, end, total), has_next=has_next)


class RestoreListTests(unittest.TestCase):
    def test_pages_forward_after_a_reset_to_page_one(self) -> None:
        site = mock.Mock()
        site.back.return_value = page(1, 2, 4)
        site.next_page.return_value = page(3, 4, 4, has_next=False)
        restored = restore_list(site, page(3, 4, 4, has_next=False), 4)
        self.assertEqual(restored.range, (3, 4, 4))

    def test_pagination_that_does_not_advance_is_an_error(self) -> None:
        site = mock.Mock()
        site.back.return_value = page(1, 2, 4)
        site.next_page.return_value = page(1, 2, 4)
        with self.assertRaisesRegex(RuntimeError, "did not advance"):
            restore_list(site, page(3, 4, 4, has_next=False), 4)

    def test_changed_results_are_an_error(self) -> None:
        site = mock.Mock()
        site.back.return_value = page(1, 2, 4, name="other")
        with self.assertRaisesRegex(RuntimeError, "Results changed"):
            restore_list(site, page(1, 2, 4), 4)
