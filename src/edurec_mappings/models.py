"""Typed records for the extraction run, the mapping requests it exports and the decisions on them.

Every record serialises to plain dictionaries and lists through `plain`, so the
YAML output keeps the same key names as the model fields.

The records fall into three groups:

- `Request` and its parts are what a course mapping decision needs: the partner
  and NUS course evidence, the student context, prior review comments, linked
  documents, and the identity that reopens the request in EduRec.
- `Collection`, `SearchAudit`, `ListPage` and `MappingGroup` are the extraction
  audit that lets an export say whether it is complete.
- `Decision` is the AI course mapping advisor's verdict on one request, and
  `Reviewed` records what the human reviewer then did with it in EduRec.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, fields, replace
from typing import Annotated, Any, Literal, NamedTuple, TypeVar, cast

from pydantic import Field, TypeAdapter

RangeField = Literal["term", "group", "sequence"]
CollectionStatus = Literal["in_progress", "complete", "row_limit_reached", "interrupted"]
PartitionStatus = Literal["in_progress", "subdivided", "complete", "row_limit_reached"]
FetchStatus = Literal["fetched", "empty", "too_large", "failed"]
DocumentKind = Literal["pdf", "html", "text"]
SupportingDocumentStatus = Literal["not_provided", "not_fetched", FetchStatus]
Completeness = Literal["single_mapping", "unverified"]
Verdict = Literal["approve", "reject", "request remapping", "request for more information"]
Confidence = Literal["high", "medium", "low"]
Action = Literal[Verdict, "skip"]
Tab = Literal["recommended", "fallback"]
"""The panel's comment tabs; the fallback exists only when the decision names one."""
CommentSource = Literal[Tab, "edited"]

TERM_PATTERN = re.compile(r"\d{4}")
"""A four-digit EduRec term code, e.g. 2620."""
MANY_TO_ONE = "Many to One"
PENDING = "Pending Approval"
NOT_IN_QUEUE = "not in approval queue"
"""`Reviewed.status_after` when a submission removed the request from the approval queue."""


GREEN, AMBER, RED = "#2e7d32", "#ef6c00", "#c62828"
VERDICT_COLOURS: dict[Verdict, str] = {
    "approve": GREEN,
    "reject": RED,
    "request remapping": AMBER,
    "request for more information": AMBER,
}
"""Colour of the verdict pill and of the matching EduRec button's outline."""


def display(value: str) -> str:
    """The panel's form of a verdict or confidence: "request remapping" -> "Request Remapping"."""
    return value.title()


T = TypeVar("T")
ADAPTERS: dict[type, TypeAdapter[Any]] = {}
"""One adapter per record type; `Any` because the dict holds every type's adapter."""


def adapter(kind: type[T]) -> TypeAdapter[T]:
    """The validator and serialiser for a record type, built once: building takes milliseconds."""
    if kind not in ADAPTERS:
        ADAPTERS[kind] = TypeAdapter(kind)
    return ADAPTERS[kind]


def plain(record: object, exclude: set[str] | None = None) -> dict[str, object]:
    """A record as the JSON-compatible dictionary that YAML writers accept.

    The record's own type drives serialisation, so `Field(exclude=True)` fields are
    dropped; convert a list of records item by item, since an untyped list keeps them.
    """
    data = adapter(type(record)).dump_python(record, mode="json", exclude=exclude)
    return cast("dict[str, object]", data)


def hydrate(cls: type[T], data: object) -> T:
    """Rebuild a record from its `plain` form, as strictly as JSON: no coercion, no unknown keys.

    Validation runs in JSON mode because strict Python mode accepts only instances
    of a dataclass, never a mapping. Raises `ValueError` (`pydantic.ValidationError`
    is one) for invalid content or a YAML value JSON cannot hold, such as a date.
    """
    try:
        payload = json.dumps(data)
    except TypeError as error:
        raise ValueError(f"{cls.__name__} is not JSON-compatible: {error}") from error
    return adapter(cls).validate_json(payload, strict=True, extra="forbid")


# --- Search and results grid -------------------------------------------------------------


@dataclass(frozen=True)
class Partition:
    """Inclusive search bounds.

    Student splits may overlap at their boundary; details are deduplicated by
    full request identity, never by list-row appearance.
    """

    term_low: int = 0
    term_high: int = 9999
    student_low: str | None = None
    student_high: str | None = None
    group_low: int = 0
    group_high: int = 999
    sequence_low: int = 0
    sequence_high: int = 999

    def bounds(self, field: RangeField) -> tuple[int, int]:
        if field == "term":
            return self.term_low, self.term_high
        if field == "group":
            return self.group_low, self.group_high
        return self.sequence_low, self.sequence_high

    def with_bounds(self, field: RangeField, low: int, high: int) -> Partition:
        if field == "term":
            return replace(self, term_low=low, term_high=high)
        if field == "group":
            return replace(self, group_low=low, group_high=high)
        return replace(self, sequence_low=low, sequence_high=high)

    def contains(self, field: RangeField, value: str | None) -> bool:
        low, high = self.bounds(field)
        return bool(value) and str(value).isdigit() and low <= int(str(value)) <= high

    def split(self, field: RangeField, pivot: int) -> list[Partition]:
        low, high = self.bounds(field)
        ranges = [(low, pivot - 1), (pivot, pivot), (pivot + 1, high)]
        return [self.with_bounds(field, a, b) for a, b in ranges if a <= b]


@dataclass(kw_only=True)
class ListRow:
    """One row of the Course Mapping Approval results grid."""

    user_id: str | None = None
    submitted_at: str | None = None
    student_id: str | None = None
    student_name: str | None = None
    institution: str | None = None
    academic_career: str | None = None
    term_code: str | None = None
    study_program: str | None = None
    partner_university: str | None = None
    partner_subject: str | None = None
    partner_number: str | None = None
    nus_subject: str | None = None
    nus_number: str | None = None
    reassigned_to: str | None = None
    action: str
    """The row's PeopleSoft `#ICRow…` action; it changes on every render."""

    def cells(self) -> dict[str, str | None]:
        """The displayed values, without the render-specific action."""
        return {name: getattr(self, name) for name in LIST_COLUMNS}


LIST_COLUMNS = tuple(f.name for f in fields(ListRow) if f.name != "action")
"""The grid's columns, left to right: `ListRow` declares its fields in display order."""


@dataclass
class Listing:
    """One page of results as displayed, with the grid counter when present."""

    rows: list[ListRow]
    range: tuple[int, int, int] | None
    has_next: bool
    capped: bool = False

    def span(self) -> tuple[int, int, int]:
        """The page's (first row, last row, total rows) counter."""
        if not self.range:
            raise RuntimeError("Result counter is missing")
        return self.range


# --- Mapping request ---------------------------------------------------------------------


@dataclass
class Identity:
    """What identifies a request in EduRec; `request_id` is a digest of these seven values.

    Reopening a request searches by student ID, term code, mapping number and
    sequence; the other fields are context that EduRec shows on the detail page.
    """

    student_id: str
    academic_career: str
    partner_university: str
    study_program: str
    term: str
    """Term as displayed, e.g. "2025/2026 Semester 2"; `Request.term_code` is the search key."""
    mapping_number: str
    sequence: str


@dataclass
class Student:
    """Programme context. Mappings are evaluated programme-agnostically, so this only
    informs concerns and remapping targets."""

    academic_program: str | None
    academic_plan: str | None
    requirement_term: str | None
    admit_term: str | None


@dataclass
class ContactHours:
    component: str | None = None
    hours_per_week: str | None = None
    remark: str | None = None


@dataclass
class Assessment:
    method: str | None = None
    weight_percent: str | None = None
    remark: str | None = None


@dataclass
class PartnerCourse:
    subject: str | None
    number: str | None
    title: str | None
    credits: str | None
    syllabus: str | None
    instruction_weeks: str | None
    contact_hours: list[ContactHours]
    assessments: list[Assessment]
    supporting_url: str | None
    supporting_document_status: SupportingDocumentStatus
    other_information: str | None


@dataclass
class NusCourse:
    subject: str | None
    number: str | None
    title: str | None
    units: str | None


@dataclass
class LinkedDocument:
    """A URL found in the course details and what could be read from it.

    The extracted text is written to `path`, relative to the run directory, so
    request files stay small; `text` is only held until the checkpoint writes it.
    """

    url: str
    status: FetchStatus = "failed"
    error: str | None = None
    """Why the document could not be read, e.g. "HTTP 404" or "Unsupported content type"."""
    kind: DocumentKind | None = None
    title: str | None = None
    pages: int | None = None
    bytes: int | None = None
    """Size of the extracted text, recorded even when it was too large to keep."""
    path: str | None = None
    text: Annotated[str | None, Field(exclude=True)] = None
    """Held in memory only until the checkpoint writes it to `path`; never serialised."""


@dataclass
class Request:
    """A Course Mapping Approval request with everything a mapping decision needs."""

    request_id: str
    """Digest of `identity`; stable across runs and unaffected by edits to the request."""
    group_id: str
    """Digest of `identity` without the sequence; shared by the parts of a many-to-one mapping."""
    identity: Identity
    mapping_type: str | None
    student: Student
    partner_course: PartnerCourse
    nus_course: NusCourse
    prerequisites: str | None
    status: str | None
    comments: str | None
    """Prior administrative review comments."""
    term_code: str | None = None
    """Four-digit EduRec term code from the results row; the STRM search key."""
    submitted_at: str | None = None
    reassigned_to: str | None = None
    related_request_ids: list[str] = field(default_factory=list)
    """Other requests in the same mapping group that this export collected."""
    linked_documents: list[LinkedDocument] | None = None
    """Documents fetched from URLs in the course details; None until the scrape stage ran."""

    @property
    def many_to_one(self) -> bool:
        return self.mapping_type == MANY_TO_ONE


# --- Extraction audit --------------------------------------------------------------------


@dataclass
class Filters:
    """Requested scope; None means no restriction on that criterion."""

    reassign_id: str | None = None
    terms: list[str] | None = None
    rows: int | None = None
    mapping_status: str = "all_available"


@dataclass
class SearchAudit:
    criteria: Partition
    reported_rows: int
    capped: bool
    status: PartitionStatus = "in_progress"


@dataclass
class Collection:
    """Run metadata: what was requested, how far the scan got and whether it is complete."""

    started_at: str
    status: CollectionStatus = "in_progress"
    updated_at: str | None = None
    scope: str = "accessible_course_mapping_approval_requests"
    filters: Filters = field(default_factory=Filters)
    linked_documents: Literal["fetched_when_present", "skipped"] = "skipped"
    """Whether the scrape stage fetched the URLs found in course details."""
    anonymized: bool = False
    """True only in the anonymized copy, where student identity is replaced by pseudonyms."""
    all_request_details_collected: bool = False
    search_partitions: list[SearchAudit] = field(default_factory=list)
    scanned_rows: int = 0
    duplicate_details: int = 0
    unique_requests: int = 0
    term_quirk: str = "EduRec truncates searches at 300 rows; capped searches are subdivided"
    related_mapping_completeness: Completeness | None = None
    error: str | None = None


@dataclass
class ListPage:
    """A results page as scanned, with the search criteria that produced it."""

    partition: Partition
    rows: list[ListRow]
    range: tuple[int, int, int] | None
    has_next: bool
    capped: bool

    @classmethod
    def of(cls, partition: Partition, page: Listing) -> ListPage:
        return cls(partition, page.rows, page.range, page.has_next, page.capped)


@dataclass
class MappingGroup:
    group_id: str
    request_ids: list[str]
    completeness: Completeness
    """Many-to-one groups are `unverified`: their other parts may lie outside the export scope."""


@dataclass(kw_only=True)
class Document:
    """The export: run metadata, scanned pages, unique requests and their groups.

    On disk a run is a directory: `inventory.yaml` holds everything but the
    requests, which are written one file each under `requests/`, and scraped
    document text goes under `documents/`.
    """

    schema_version: int = 4
    collection: Collection
    list_pages: list[ListPage] = field(default_factory=list)
    requests: list[Request] = field(default_factory=list)
    mapping_groups: list[MappingGroup] = field(default_factory=list)

    def inventory(self) -> dict[str, object]:
        """The `inventory.yaml` content: the export without the request records."""
        return plain(self, exclude={"requests"})


# --- Decision ----------------------------------------------------------------------------


@dataclass
class Decision:
    """`decisions/<request_id>.yaml`: the AI course mapping advisor's verdict on one request.

    Each file names the export it was made from, so the approval script can
    detect a decision made on an older export than the one it is applying.
    See .claude/agents/course-mapping.md.
    """

    source_export: str
    """Path of the run directory the decision was made from."""
    source_started_at: str
    """`collection.started_at` of that export."""
    request_id: str
    """The only key used to link the decision back to the export and to EduRec."""
    course: str
    """Display only, e.g. "IN 2346 (Technical University of Munich) -> CS5242"."""
    verdict: Verdict
    comment: str
    """The complete text to enter in EduRec, following the verdict's template."""
    overlap_percentage: int
    decision_confidence: Confidence
    overlap: list[str] = field(default_factory=list)
    missing_from_pu: list[str] = field(default_factory=list)
    extra_in_pu: list[str] = field(default_factory=list)
    concerns: list[str] = field(default_factory=list)
    remap_target: str | None = None
    remap_analysis: str | None = None
    fallback_verdict: Verdict | None = None
    """For the reviewer only, never applied: the verdict a reviewer who disagrees
    with `verdict` would most likely reach. Set only on close calls."""
    fallback_comment: str | None = None
    """Complete EduRec text for `fallback_verdict`, following its template."""
    fallback_rationale: str | None = None
    """Why the fallback is defensible, or why no alternative is (when the verdict is null)."""

    def prefills(self, existing: str | None) -> dict[Tab, str]:
        """The comment box content per panel tab; the fallback only when there is one.

        EduRec replaces the field, so the tab's comment goes on top and any
        existing text is kept below it.
        """
        result: dict[Tab, str] = {"recommended": stack(self.comment, existing)}
        if self.fallback_verdict:
            result["fallback"] = stack(self.fallback_comment or "", existing)
        return result


def stack(comment: str, existing: str | None) -> str:
    """`comment` on top of any `existing` text, separated by a blank line."""
    below = (existing or "").strip()
    return f"{comment.strip()}\n\n{below}" if below else comment.strip()


@dataclass
class Reviewed:
    """`decisions/reviewed.yaml` entry: the outcome of showing one decision to the reviewer.

    `action` is the EduRec button the reviewer pressed, or `skip` when the request
    was passed over (by the reviewer, by a freshness check, or in a dry run);
    `reason` says why. A non-skip entry is never offered again.
    """

    request_id: str
    verdict_recommended: Verdict
    action: Action
    comment_submitted: str | None
    comment_edited: bool
    """True when the submitted comment differs from the pre-filled comment of the active tab."""
    status_before: str | None
    status_after: str | None
    reviewed_at: str
    dry_run: bool
    reason: str | None = None
    comment_source: CommentSource | None = None
    """Which pre-filled comment was submitted, or `edited` when the box differed from it;
    None for skips."""


# --- Reviewer outcomes ---------------------------------------------------------------------


class Clicked(NamedTuple):
    """The reviewer's click on the detail page, as the posted `ICAction` and the comment box."""

    action: str
    """A `browser.BUTTONS` id, or `#ICList` when Cancel returned to the list."""
    comment: str | None
    source: Tab | None = None
    """The panel tab selected at the click, when the hook reported it."""


class Skipped(NamedTuple):
    """The panel's Skip was pressed; the comment box is restored."""

    reason: str = ""
    """What the reviewer typed into the panel's skip reason field."""


class Left(NamedTuple):
    """The detail page went away without a recognised action, e.g. the reviewer navigated off."""


Outcome = Clicked | Skipped | Left


class Fetched(NamedTuple):
    """An HTTP response as the document fetcher reports it."""

    status: int
    content_type: str | None
    body: bytes
