"""Pure parsers from Banner 9 JSON into models."""

import json

from .models import CatalogCourse, CodeRef, InstructorRef, Meeting, Section, Semester
from .session import verify_term

_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")

# Legacy day letters: R is Thursday, U is Sunday.
_DAY_LETTERS = (
    ("monday", "M"), ("tuesday", "T"), ("wednesday", "W"), ("thursday", "R"),
    ("friday", "F"), ("saturday", "S"), ("sunday", "U"),
)


def _load(raw: str | bytes):
    return json.loads(raw)


def parse_terms(raw: str | bytes) -> list[Semester]:
    return [
        Semester(term_id=t["code"],
                 term_name=t["description"].replace("(View Only)", "").strip())
        for t in _load(raw)
    ]


def parse_code_list(raw: str | bytes) -> list[CodeRef]:
    return [CodeRef(code=r["code"], description=r["description"]) for r in _load(raw)]


def to_12h(hhmm: str | None) -> str:
    """'1345' -> '1:45 pm', matching the format already in the shipped database."""
    if not hhmm or len(hhmm) != 4 or not hhmm.isdigit():
        return ""
    h, m = int(hhmm[:2]), hhmm[2:]
    suffix = "am" if h < 12 else "pm"
    h12 = h % 12 or 12
    return f"{h12}:{m} {suffix}"


def format_date_range(start: str | None, end: str | None) -> str:
    """'08/24/2026','12/10/2026' -> 'Aug 24, 2026 - Dec 10, 2026'."""
    def one(d):
        if not d or d.count("/") != 2:
            return ""
        mm, dd, yy = d.split("/")
        return f"{_MONTHS[int(mm) - 1]} {int(dd)}, {yy}"

    a, b = one(start), one(end)
    return f"{a} - {b}" if a and b else ""


def days_string(m: Meeting) -> str:
    return "".join(letter for attr, letter in _DAY_LETTERS if getattr(m, attr))


def classroom_string(m: Meeting) -> str:
    parts = [p for p in (m.building_name or m.building, m.room) if p]
    return " ".join(parts)


def _meeting(raw: dict, crn: str, term_id: str, index: int) -> Meeting:
    mt = raw.get("meetingTime") or {}
    return Meeting(
        crn=crn, term_id=term_id, meeting_index=index,
        meeting_type=mt.get("meetingType") or "",
        meeting_type_desc=mt.get("meetingTypeDescription") or "",
        begin_time=mt.get("beginTime") or "",
        end_time=mt.get("endTime") or "",
        monday=bool(mt.get("monday")), tuesday=bool(mt.get("tuesday")),
        wednesday=bool(mt.get("wednesday")), thursday=bool(mt.get("thursday")),
        friday=bool(mt.get("friday")), saturday=bool(mt.get("saturday")),
        sunday=bool(mt.get("sunday")),
        building=mt.get("building") or "",
        building_name=mt.get("buildingDescription") or "",
        room=mt.get("room") or "",
        campus=mt.get("campus") or "",
        campus_desc=mt.get("campusDescription") or "",
        start_date=mt.get("startDate") or "",
        end_date=mt.get("endDate") or "",
        hours_week=mt.get("hoursWeek"),
        credit_hour_session=mt.get("creditHourSession"),
        schedule_type=mt.get("meetingScheduleType") or "",
    )


def parse_sections(raw: str | bytes, expected_term: str) -> tuple[int, list[Section]]:
    payload = _load(raw)
    verify_term(payload, expected_term)
    out: list[Section] = []
    for r in payload.get("data") or []:
        crn = r["courseReferenceNumber"]
        attrs = [CodeRef(code=a.get("code") or "",
                         description=a.get("description") or "")
                 for a in (r.get("sectionAttributes") or [])]
        out.append(Section(
            crn=crn,
            term_id=r["term"],
            subject=r["subject"],
            course_number=r["courseNumber"],
            title=r.get("courseTitle") or "",
            section=r.get("sequenceNumber") or "",
            credits=r.get("creditHourLow"),
            schedule_type=r.get("scheduleTypeDescription") or "",
            instructional_method=r.get("instructionalMethodDescription") or "",
            campus=r.get("campusDescription") or "",
            attributes_text=", ".join(a.description for a in attrs),
            part_of_term=r.get("partOfTerm") or "",
            section_id=r.get("id"),
            enrollment=r.get("enrollment"),
            max_enrollment=r.get("maximumEnrollment"),
            seats_available_count=r.get("seatsAvailable"),
            waitlist_capacity=r.get("waitCapacity"),
            waitlist_count=r.get("waitCount"),
            waitlist_available=r.get("waitAvailable"),
            cross_list=r.get("crossList") or "",
            cross_list_capacity=r.get("crossListCapacity"),
            cross_list_count=r.get("crossListCount"),
            cross_list_available=r.get("crossListAvailable"),
            open_section=bool(r.get("openSection")),
            meetings=[_meeting(m, crn, r["term"], i)
                      for i, m in enumerate(r.get("meetingsFaculty") or [])],
            instructors=[InstructorRef(
                name=f.get("displayName") or "",
                email=f.get("emailAddress") or "",
                banner_id=str(f.get("bannerId") or ""),
                is_primary=bool(f.get("primaryIndicator")),
            ) for f in (r.get("faculty") or [])],
            attributes=attrs,
        ))
    return payload.get("totalCount") or 0, out


def parse_catalog(raw: str | bytes,
                  expected_term: str) -> tuple[int, list[CatalogCourse]]:
    """Catalog rows carry termEffective, not term, so there is nothing to verify
    against the bound term — that guard belongs on the section path."""
    payload = _load(raw)
    out: list[CatalogCourse] = []
    for r in payload.get("data") or []:
        out.append(CatalogCourse(
            subject=r["subject"],
            course_number=r["courseNumber"],
            title=r.get("courseTitle") or "",
            term_effective=r.get("termEffective") or "",
            description=(r.get("courseDescription") or "").strip(),
            term_start=r.get("termStart") or "",
            term_end=r.get("termEnd") or "",
            college=r.get("college") or "",
            college_code=r.get("collegeCode") or "",
            department=r.get("department") or "",
            department_code=r.get("departmentCode") or "",
            credit_hours_low=r.get("creditHourLow"),
            credit_hours_high=r.get("creditHourHigh"),
            lecture_hours_low=r.get("lectureHourLow"),
            lecture_hours_high=r.get("lectureHourHigh"),
            lab_hours_low=r.get("labHourLow"),
            lab_hours_high=r.get("labHourHigh"),
            other_hours_low=r.get("otherHourLow"),
            other_hours_high=r.get("otherHourHigh"),
            bill_hours_low=r.get("billHourLow"),
            bill_hours_high=r.get("billHourHigh"),
            prereq_check_method=r.get("preRequisiteCheckMethodCde") or "",
        ))
    return payload.get("totalCount") or 0, out
