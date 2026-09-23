"""Parse EduRec Course Mapping Approval pages into records (no scripts executed)."""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import TypeVar

from bs4 import BeautifulSoup, Tag
from bs4.element import PageElement

from .models import (
    LIST_COLUMNS,
    Assessment,
    ContactHours,
    GridCounter,
    Identity,
    Listing,
    ListRow,
    NusCourse,
    PartnerCourse,
    Request,
    Student,
)

GRID = "tdgbrPTS_CFG_CL_STD_RSL$0"
COUNTER = "win0divPTS_CFG_CL_STD_RSLGP$0"
NEXT = "PTS_CFG_CL_STD_RSL$hdown$0"
VIEW_ALL = "PTS_CFG_CL_STD_RSL$hviewall$0"
DETAIL = "N_EXSP_MOD_DT_TRNSFR_EQVLNCY_GRP$0"

T = TypeVar("T")


def text(node: PageElement | None) -> str | None:
    """The node's text without padding; None when it is absent, blank or EduRec's "-"."""
    value = node.get_text().replace("\xa0", " ").strip() if node else ""
    return value if value and value != "-" else None


def can_expand(soup: BeautifulSoup) -> bool:
    """Whether the grid's view toggle still offers a larger page size."""
    link = soup.find("a", id=VIEW_ALL)
    return link is not None and bool(re.fullmatch(r"View\s+(100|All)", text(link) or "", re.I))


def parse_listing(soup: BeautifulSoup) -> Listing:
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
        rows.append(ListRow(**values, row_action=action[0]))
    counter = soup.find(id=COUNTER)
    counter = counter.select_one(".PSGRIDCOUNTER") if isinstance(counter, Tag) else None
    # A single row is counted "1 of 1"; longer pages "1-100 of 300".
    match = re.search(r"(\d+)(?:\s*-\s*(\d+))?\s+of\s+(\d+)", text(counter) or "")
    return Listing(
        rows=rows,
        counter=GridCounter(int(match[1]), int(match[2] or match[1]), int(match[3]))
        if match
        else None,
        has_next=soup.find("a", id=NEXT) is not None,
    )


def required(name: str, value: str | None) -> str:
    if not value:
        raise ValueError(f"Mapping detail is missing its {name}")
    return value


def detail_field(soup: BeautifulSoup, suffix: str) -> str | None:
    return text(soup.find(id=f"N_EXSP_MOD_DT_{suffix}$0"))


def approval_status(soup: BeautifulSoup) -> str | None:
    """The status shown on a detail page; `ValueError` on any other page."""
    if soup.find(id=DETAIL) is None:
        raise ValueError("Mapping detail not found")
    return detail_field(soup, "N_MOD_APPR_STATUS")


def parse_detail(soup: BeautifulSoup, term_code: str | None) -> Request:
    """The detail page as a request; `term_code` comes from the results row it was opened from."""
    status = approval_status(soup)

    def get(element_id: str) -> str | None:
        return text(soup.find(id=element_id))

    def field(suffix: str) -> str | None:
        return detail_field(soup, suffix)

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
        exchange_program=required("exchange program", get("N_EXT_PRG_VW_DESCRFORMAL")),
        term=required("term", get("TERM_TBL_DESCR")),
        term_code=required("term code", term_code),
        group=required("group", field("TRNSFR_EQVLNCY_GRP")),
        sequence=required("sequence", field("TRNSFR_EQVLNCY_SEQ")),
    )
    return Request(
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
            synopsis=field("N_MOD_SYNOPSIS"),
            instruction_weeks=field("WEEKS_OF_INSTRUCT"),
            contact_hours=table("N_EXSP_MOD", ContactHours, 3),
            assessments=table("N_EXSP_ASGNMT", Assessment, 3),
            supporting_url=field("N_URL"),
            other_information=field("N_MISC_DETAILS"),
        ),
        nus_course=NusCourse(
            subject=field("SUBJECT"),
            number=field("CATALOG_NBR"),
            title=get("N_CRSE_CTLG1_VW_COURSE_TITLE_LONG$0"),
            units=field("UNT_TRNSFR"),
        ),
        prerequisites=field("N_PREREQUISITE_DTL"),
        approval_status=status,
        review_comments=field("N_MOD_COMMENTS"),
    )
