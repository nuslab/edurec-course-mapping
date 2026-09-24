"""Records for requests, proposals and outcomes, and their strict YAML round trip.

`plain` and `hydrate` convert every record to and from plain dictionaries whose
keys are the field names.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, fields, replace
from typing import Annotated, Any, Literal, NamedTuple, cast

from pydantic import Field, TypeAdapter

RangeField = Literal["term", "group", "sequence"]
ExportStatus = Literal["in_progress", "complete", "limit_reached", "interrupted"]
FetchStatus = Literal["fetched", "empty", "too_large", "failed"]
DocumentKind = Literal["pdf", "docx", "html", "text", "zip", "folder"]
Verdict = Literal["approve", "reject", "request_remapping", "request_more_information"]
Confidence = Literal["high", "medium", "low"]
Tab = Literal["recommended", "fallback"]

SCHEMA_VERSION = 8
TERM_PATTERN = re.compile(r"\d{4}")
PENDING_APPROVAL = "Pending Approval"
ADAPTERS: dict[type, TypeAdapter[Any]] = {}


def adapter[T](kind: type[T]) -> TypeAdapter[T]:
    if kind not in ADAPTERS:
        ADAPTERS[kind] = TypeAdapter(kind)
    return ADAPTERS[kind]


def plain(record: object, exclude: set[str] | None = None) -> dict[str, object]:
    """A record as the JSON-compatible dictionary that YAML writers accept.

    `Field(exclude=True)` fields are dropped only when the record's own type drives
    serialisation, so convert a list of records item by item.
    """
    data = adapter(type(record)).dump_python(record, mode="json", exclude=exclude)
    return cast("dict[str, object]", data)


def hydrate[T](cls: type[T], data: object) -> T:
    """Rebuild a record from its `plain` form: no coercion, no unknown keys.

    Strict Python mode accepts only dataclass instances, never a mapping, hence the
    JSON round trip. Raises `ValueError` for invalid content or a YAML value that JSON
    cannot hold, such as a date.
    """
    try:
        payload = json.dumps(data)
    except TypeError as error:
        raise ValueError(f"{cls.__name__} is not JSON-compatible: {error}") from error
    return adapter(cls).validate_json(payload, strict=True, extra="forbid")


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
    exchange_program: str | None = None
    partner_university: str | None = None
    partner_subject: str | None = None
    partner_number: str | None = None
    nus_subject: str | None = None
    nus_number: str | None = None
    reassigned_to: str | None = None
    row_action: str
    """The row's PeopleSoft `#ICRow<n>` action; it changes on every render."""

    def cells(self) -> dict[str, str | None]:
        """The displayed values, without the render-specific action."""
        return {name: getattr(self, name) for name in LIST_COLUMNS}


LIST_COLUMNS = tuple(f.name for f in fields(ListRow) if f.name != "row_action")
"""The grid's columns, left to right: `ListRow` declares its fields in display order."""


class GridCounter(NamedTuple):
    """The grid's "first-last of total" counter."""

    first: int
    last: int
    total: int


@dataclass
class Listing:
    rows: list[ListRow]
    counter: GridCounter | None
    has_next: bool
    capped: bool = False

    def span(self) -> GridCounter:
        if not self.counter:
            raise RuntimeError("Result counter is missing")
        return self.counter


@dataclass
class Identity:
    """What identifies a request in EduRec; `request_id` is a keyed digest of it.

    Reopening a request searches by student ID, term code, group and sequence; the
    other fields are context that EduRec shows on the detail page.
    """

    student_id: str
    academic_career: str
    partner_university: str
    exchange_program: str
    term: str
    """Term as displayed, e.g. "2025/2026 Semester 2"."""
    term_code: str
    """Four-digit EduRec term code from the results row; the STRM search key."""
    group: str
    sequence: str

    @property
    def group_key(self) -> tuple[str, ...]:
        """The identity without the sequence; shared by the parts of a many-to-one mapping."""
        return tuple(getattr(self, f.name) for f in fields(self) if f.name != "sequence")


@dataclass
class Student:
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
    synopsis: str | None
    instruction_weeks: str | None
    contact_hours: list[ContactHours]
    assessments: list[Assessment]
    supporting_url: str | None
    other_information: str | None


@dataclass
class NusCourse:
    subject: str | None
    number: str | None
    title: str | None
    units: str | None


@dataclass
class LinkedDocument:
    """A URL found in the course details and what could be read from it."""

    url: str
    status: FetchStatus = "failed"
    error: str | None = None
    """Why the document could not be read, e.g. "HTTP 404" or "Unsupported content type"."""
    kind: DocumentKind | None = None
    title: str | None = None
    page_count: int | None = None
    text_bytes: int | None = None
    """Size of the extracted text, recorded even when it was too large to keep."""
    text_path: str | None = None
    """Store-relative file holding the extracted text."""
    text: Annotated[str | None, Field(exclude=True)] = None
    """Held in memory until the store writes it to `text_path`; never serialised."""


@dataclass(kw_only=True)
class Request:
    """A Course Mapping Approval request with everything a mapping decision needs.

    As parsed it carries the real student ID and no `request_id`; the store writes it
    pseudonymized, with the keyed `request_id`, a pseudonym and `created_at`.
    """

    schema_version: int = SCHEMA_VERSION
    created_at: str | None = None
    request_id: str = ""
    identity: Identity
    mapping_type: str | None
    student: Student
    partner_course: PartnerCourse
    nus_course: NusCourse
    prerequisites: str | None
    approval_status: str | None
    review_comments: str | None
    """Prior administrative review comments."""
    sibling_request_ids: list[str] = field(default_factory=list)
    """Other requests in the same mapping group that the same export collected."""
    documents: list[LinkedDocument] | None = None
    """None when the export did not fetch documents."""

    @property
    def course(self) -> str:
        """The mapping for display, e.g. "IN 2346 (Technical University of Munich) -> CS5242"."""
        partner, nus = self.partner_course, self.nus_course
        return (
            f"{partner.subject} {partner.number} ({self.identity.partner_university})"
            f" -> {nus.subject}{nus.number}"
        )


@dataclass
class ExportResult:
    """One export in memory: how far the scan got and the unique requests it collected."""

    status: ExportStatus = "in_progress"
    error: str | None = None
    requests: list[Request] = field(default_factory=list)


@dataclass
class Remap:
    target: str
    """A course code, or a levelled dummy course such as "3000-level unrestricted elective"."""
    analysis: str | None = None


@dataclass
class Fallback:
    """On close calls, the verdict to submit instead of `Proposal.verdict`."""

    verdict: Verdict
    comment: str


@dataclass
class Proposal:
    """`proposals/<request_id>/<hash>.yaml`: the proposed verdict on one request version.

    The path is the key: it names the request version the proposal was made from, so
    a changed request is a new version without a proposal.
    """

    verdict: Verdict
    comment: str
    """The complete text to enter in EduRec, following the verdict's template."""
    overlap_percent: int
    confidence: Confidence
    overlap_topics: list[str] = field(default_factory=list)
    missing_from_partner: list[str] = field(default_factory=list)
    extra_in_partner: list[str] = field(default_factory=list)
    concerns: list[str] = field(default_factory=list)
    remap: Remap | None = None
    fallback: Fallback | None = None
    fallback_rationale: str | None = None
    """Why the fallback is defensible, or why no alternative is when there is none."""


@dataclass
class Outcome:
    """`outcomes/<request_id>/<hash>.yaml`: what was submitted.

    `verdict` is None when the request left the approval queue before it was opened.
    `verified` is True once the live status showed the request out of the queue; `note`
    says why it could not be.
    """

    verdict: Verdict | None
    comment: str | None
    verified: bool
    recorded_at: str
    note: str | None = None


class Clicked(NamedTuple):
    """The EduRec button a click posted and the comment box at that moment."""

    verdict: Verdict | None
    """None when Cancel returned to the list."""
    comment: str | None


class Skipped(NamedTuple):
    """The request was passed over: the panel's Skip, or the detail page went away."""

    reason: str


Reaction = Clicked | Skipped
