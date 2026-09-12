"""Pure parsers from Banner 9 JSON into models."""

import html
import json

from .models import CatalogCourse, CodeRef, InstructorRef, Meeting, Section, Semester
from .session import verify_term


def _txt(value) -> str:
    """Banner 9 HTML-escapes text inside the JSON, so 'Qur&#39;an' arrives escaped."""
    return html.unescape(value) if value else ""


def parse_terms(raw: str | bytes) -> list[Semester]:
    return [
        Semester(term_id=t["code"],
                 term_name=_txt(t["description"]).replace("(View Only)", "").strip())
        for t in json.loads(raw)
    ]


def parse_code_list(raw: str | bytes) -> list[CodeRef]:
    return [CodeRef(code=r["code"], description=_txt(r["description"]))
            for r in json.loads(raw)]


def _meeting(raw: dict, crn: str, term_id: str, index: int) -> Meeting:
    mt = raw.get("meetingTime") or {}
    return Meeting(
        crn=crn, term_id=term_id, meeting_index=index,
        meeting_type=mt.get("meetingType") or "",
        meeting_type_desc=_txt(mt.get("meetingTypeDescription")),
        begin_time=mt.get("beginTime") or "",
        end_time=mt.get("endTime") or "",
        monday=bool(mt.get("monday")), tuesday=bool(mt.get("tuesday")),
        wednesday=bool(mt.get("wednesday")), thursday=bool(mt.get("thursday")),
        friday=bool(mt.get("friday")), saturday=bool(mt.get("saturday")),
        sunday=bool(mt.get("sunday")),
        building=mt.get("building") or "",
        building_name=_txt(mt.get("buildingDescription")),
        room=mt.get("room") or "",
        campus=mt.get("campus") or "",
        campus_desc=_txt(mt.get("campusDescription")),
        start_date=mt.get("startDate") or "",
        end_date=mt.get("endDate") or "",
        hours_week=mt.get("hoursWeek"),
        credit_hour_session=mt.get("creditHourSession"),
        schedule_type=mt.get("meetingScheduleType") or "",
    )


def parse_sections(raw: str | bytes, expected_term: str) -> tuple[int, list[Section]]:
    payload = json.loads(raw)
    verify_term(payload, expected_term)
    out: list[Section] = []
    for r in payload.get("data") or []:
        crn = r["courseReferenceNumber"]
        attrs = [CodeRef(code=a.get("code") or "",
                         description=_txt(a.get("description")))
                 for a in (r.get("sectionAttributes") or [])]
        out.append(Section(
            crn=crn,
            term_id=r["term"],
            subject=r["subject"],
            course_number=r["courseNumber"],
            title=_txt(r.get("courseTitle")),
            section=r.get("sequenceNumber") or "",
            credits=r.get("creditHourLow"),
            schedule_type=_txt(r.get("scheduleTypeDescription")),
            instructional_method=_txt(r.get("instructionalMethodDescription")),
            campus=_txt(r.get("campusDescription")),
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
                name=_txt(f.get("displayName")),
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
    payload = json.loads(raw)
    out: list[CatalogCourse] = []
    for r in payload.get("data") or []:
        out.append(CatalogCourse(
            subject=r["subject"],
            course_number=r["courseNumber"],
            title=_txt(r.get("courseTitle")),
            term_effective=r.get("termEffective") or "",
            description=_txt(r.get("courseDescription")).strip(),
            term_start=r.get("termStart") or "",
            term_end=r.get("termEnd") or "",
            college=_txt(r.get("college")),
            college_code=r.get("collegeCode") or "",
            department=_txt(r.get("department")),
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
