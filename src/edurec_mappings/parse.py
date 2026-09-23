"""Parse EduRec Course Mapping Approval pages into records (no scripts executed)."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from typing import TypeVar

from bs4 import BeautifulSoup, Tag
from bs4.element import PageElement

from .models import (
    LIST_COLUMNS,
    Assessment,
    ContactHours,
    Identity,
    Listing,
    ListRow,
    NusCourse,
    PartnerCourse,
    Request,
    Student,
    as_dict,
    plain,
)

GRID = "tdgbrPTS_CFG_CL_STD_RSL$0"
NEXT = "PTS_CFG_CL_STD_RSL$hdown$0"
VIEW_ALL = "PTS_CFG_CL_STD_RSL$hviewall$0"
DETAIL = "N_EXSP_MOD_DT_TRNSFR_EQVLNCY_GRP$0"

T = TypeVar("T")


def clean(value: str) -> str | None:
    value = value.replace("\xa0", " ").strip()
    return value if value and value != "-" else None


def text(node: PageElement | None) -> str | None:
    return clean(node.get_text()) if node else None


def digest(value: object) -> str:
    return hashlib.sha256(json.dumps(plain(value), sort_keys=True).encode()).hexdigest()[:24]


def expand_action(soup: BeautifulSoup) -> str | None:
    """Return the grid's view-toggle action while it still offers a larger page size."""
    link = soup.find("a", id=VIEW_ALL)
    if link is not None and re.fullmatch(r"View\s+(100|All)", text(link) or "", re.I):
        return VIEW_ALL
    return None


def listing(soup: BeautifulSoup) -> Listing:
    grid = soup.find(id=GRID)
    if not isinstance(grid, Tag):
        raise ValueError("Request list not found")
    rows: list[ListRow] = []
    for tr in grid.find_all("tr"):
        action = re.search(r"#ICRow\d+", str(tr.get("onclick", "")))
        if not action:
            continue
        cells = tr.find_all("td", recursive=False)
        if len(cells) < len(LIST_COLUMNS):
            raise ValueError("Unexpected list columns; extraction stopped")
        values = dict(zip(LIST_COLUMNS, (text(c) for c in cells), strict=False))
        rows.append(ListRow(**values, action=action[0]))
    counter = soup.find(id="win0divPTS_CFG_CL_STD_RSLGP$0")
    counter = counter.select_one(".PSGRIDCOUNTER") if isinstance(counter, Tag) else None
    # A single row is counted "1 of 1"; longer pages "1-100 of 300".
    match = re.search(r"(\d+)(?:\s*-\s*(\d+))?\s+of\s+(\d+)", text(counter) or "")
    return Listing(
        rows=rows,
        range=(int(match[1]), int(match[2] or match[1]), int(match[3])) if match else None,
        has_next=soup.find("a", id=NEXT) is not None,
    )


def required(name: str, value: str | None) -> str:
    if not value:
        raise ValueError(f"Mapping detail is missing its {name}")
    return value


def detail(soup: BeautifulSoup) -> Request:
    if soup.find(id=DETAIL) is None:
        raise ValueError("Mapping detail not found")

    def get(field: str) -> str | None:
        return text(soup.find(id=field))

    def field(suffix: str) -> str | None:
        return get("N_EXSP_MOD_DT_" + suffix + "$0")

    def table(prefix: str, record: Callable[..., T], width: int) -> list[T]:
        pattern = re.compile("^tr" + re.escape(prefix) + r"\$0_row\d+$")
        result: list[T] = []
        for tr in soup.find_all("tr", id=pattern):
            cells = [text(c) for c in tr.find_all("td", recursive=False)][:width]
            result.append(record(*cells))
        return result

    identity = Identity(
        student_id=required("student ID", get("N_EXSP_WKST_HDR_EMPLID")),
        academic_career=required("academic career", get("ACAD_CAR_TBL_DESCR")),
        partner_university=required("partner university", get("EXT_ORG_TBL_N_FORMAL_DESCR")),
        study_program=required("study program", get("N_EXT_PRG_VW_DESCRFORMAL")),
        term=required("term", get("TERM_TBL_DESCR")),
        mapping_number=required("mapping number", field("TRNSFR_EQVLNCY_GRP")),
        sequence=required("sequence", field("TRNSFR_EQVLNCY_SEQ")),
    )
    key = as_dict(identity)
    group_key = {k: v for k, v in key.items() if k != "sequence"}
    supporting_url = field("N_URL")
    return Request(
        request_id=digest(key),
        group_id=digest(group_key),
        identity=identity,
        mapping_type=field("N_PU_MAP"),
        student=Student(
            academic_program=get("N_ACAD_PROG_VW_DESCR"),
            academic_plan=get("N_EXSP_PLAN_VW_DESCRLONG"),
            requirement_term=get("TERM_VAL_TBL_DESCR"),
            admit_term=get("TERM_VAL_TBL_DESCR$132$"),
        ),
        partner_course=PartnerCourse(
            subject=field("SCHOOL_SUBJECT"),
            number=field("SCHOOL_CRSE_NBR"),
            title=field("DESCR100"),
            credits=field("UNT_TAKEN"),
            syllabus=field("N_MOD_SYNOPSIS"),
            instruction_weeks=field("WEEKS_OF_INSTRUCT"),
            contact_hours=table("N_EXSP_MOD", ContactHours, 3),
            assessments=table("N_EXSP_ASGNMT", Assessment, 3),
            supporting_url=supporting_url,
            supporting_document_status="not_fetched" if supporting_url else "not_provided",
            other_information=field("N_MISC_DETAILS"),
        ),
        nus_course=NusCourse(
            subject=field("SUBJECT"),
            number=field("CATALOG_NBR"),
            title=get("N_CRSE_CTLG1_VW_COURSE_TITLE_LONG$0"),
            units=field("UNT_TRNSFR"),
        ),
        prerequisites=field("N_PREREQUISITE_DTL"),
        status=field("N_MOD_APPR_STATUS"),
        comments=field("N_MOD_COMMENTS"),
    )
