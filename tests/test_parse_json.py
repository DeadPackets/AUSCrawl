import pytest

from auscrawl import models, parse_json
from auscrawl.session import TermMismatch
from tests.conftest import read_b9


def test_parse_terms_reads_code_and_description():
    terms = parse_json.parse_terms(read_b9("terms.json"))
    assert len(terms) >= 100
    assert terms[0].term_id.isdigit() and len(terms[0].term_id) == 6
    assert any(t.term_id == "200520" for t in terms)
    assert all("(View Only)" not in t.term_name for t in terms)


def test_parse_sections_returns_total_and_rows():
    total, sections = parse_json.parse_sections(
        read_b9("sections_202710_p0.json"), "202710")
    assert total > 1000
    assert len(sections) == 500
    s = sections[0]
    assert s.term_id == "202710"
    assert s.crn.isdigit()
    assert s.subject and s.course_number and s.title


def test_parse_sections_rejects_a_wrong_term_payload():
    raw = read_b9("sections_202710_p0.json")
    with pytest.raises(TermMismatch):
        parse_json.parse_sections(raw, "201510")


def test_seat_counts_are_carried_through():
    _, sections = parse_json.parse_sections(
        read_b9("sections_202710_p0.json"), "202710")
    seated = [s for s in sections if s.max_enrollment]
    assert seated, "expected at least one section with a capacity"
    s = seated[0]
    assert s.enrollment is not None
    assert s.seats_available_count is not None


def test_instructors_carry_banner_id_and_primary_flag():
    _, sections = parse_json.parse_sections(
        read_b9("sections_202710_p0.json"), "202710")
    withfac = [s for s in sections if s.instructors]
    assert withfac
    ins = withfac[0].instructors[0]
    assert ins.name
    assert ins.banner_id.isdigit()
    assert isinstance(ins.is_primary, bool)


def test_meetings_are_structured_not_concatenated():
    _, sections = parse_json.parse_sections(
        read_b9("sections_202710_p0.json"), "202710")
    withmt = [s for s in sections if s.meetings and s.meetings[0].room]
    assert withmt
    m = withmt[0].meetings[0]
    assert m.building and m.room
    assert m.building_name != m.building
    assert m.meeting_index == 0


# --- legacy format compatibility --------------------------------------------

def test_days_string_uses_the_legacy_letters():
    m = models.Meeting(crn="1", term_id="t", meeting_index=0,
                       monday=True, wednesday=True)
    assert parse_json.days_string(m) == "MW"
    m2 = models.Meeting(crn="1", term_id="t", meeting_index=0,
                        tuesday=True, thursday=True)
    assert parse_json.days_string(m2) == "TR"
    m3 = models.Meeting(crn="1", term_id="t", meeting_index=0,
                        saturday=True, sunday=True)
    assert parse_json.days_string(m3) == "SU"
    assert parse_json.days_string(models.Meeting(crn="1", term_id="t",
                                                 meeting_index=0)) == ""


def test_to_12h_matches_the_shipped_database_format():
    assert parse_json.to_12h("1100") == "11:00 am"
    assert parse_json.to_12h("1215") == "12:15 pm"
    assert parse_json.to_12h("1345") == "1:45 pm"
    assert parse_json.to_12h("0800") == "8:00 am"
    assert parse_json.to_12h("0000") == "12:00 am"
    assert parse_json.to_12h("1200") == "12:00 pm"
    assert parse_json.to_12h("") == ""
    assert parse_json.to_12h(None) == ""


def test_format_date_range_matches_the_shipped_database_format():
    assert parse_json.format_date_range("08/24/2026", "12/10/2026") == \
        "Aug 24, 2026 - Dec 10, 2026"
    assert parse_json.format_date_range("", "") == ""


def test_classroom_string_matches_the_shipped_database_format():
    m = models.Meeting(crn="1", term_id="t", meeting_index=0,
                       building_name="School of Business Administrtn", room="1104")
    assert parse_json.classroom_string(m) == "School of Business Administrtn 1104"


def test_code_list_parses_reference_endpoints():
    subjects = parse_json.parse_code_list(read_b9("ref_subjects_202710.json"))
    assert len(subjects) > 50
    assert all(s.code and s.description for s in subjects)
    instructors = parse_json.parse_code_list(read_b9("ref_instructors_202710.json"))
    assert len(instructors) > 100
    assert instructors[0].code.isdigit()


# --- catalog -----------------------------------------------------------------

def test_parse_catalog_returns_total_and_courses():
    total, courses = parse_json.parse_catalog(
        read_b9("catalog_202710_p0.json"), "202710")
    assert total > 1000
    assert len(courses) == 500
    c = courses[0]
    assert c.subject and c.course_number and c.title
    assert c.term_effective.isdigit() and len(c.term_effective) == 6


def test_catalog_carries_description_inline_so_no_extra_request_is_needed():
    _, courses = parse_json.parse_catalog(
        read_b9("catalog_202710_p0.json"), "202710")
    described = [c for c in courses if c.description]
    assert len(described) > len(courses) * 0.5


def test_catalog_splits_hour_types():
    _, courses = parse_json.parse_catalog(
        read_b9("catalog_202710_p0.json"), "202710")
    assert any(c.lecture_hours_low is not None for c in courses)
    assert any(c.lab_hours_low is not None for c in courses)


def test_catalog_carries_college_and_department_codes():
    _, courses = parse_json.parse_catalog(
        read_b9("catalog_202710_p0.json"), "202710")
    assert all(c.college_code for c in courses)
    assert any(c.department_code for c in courses)


def test_html_entities_in_the_json_payload_are_decoded():
    """Banner 9 double-encodes text: courseTitle arrives as 'Qur&#39;an'."""
    raw = (b'{"totalCount":1,"data":[{"term":"201510","courseReferenceNumber":"1",'
           b'"subject":"ARA","courseNumber":"205",'
           b'"courseTitle":"The Language of the Qur&#39;an",'
           b'"subjectDescription":"Arabic &amp; Translation",'
           b'"faculty":[{"displayName":"A &amp; B","bannerId":"1"}],'
           b'"sectionAttributes":[{"code":"X","description":"Maths &amp; Stats"}],'
           b'"meetingsFaculty":[{"meetingTime":{"buildingDescription":"Arts &amp; Sci",'
           b'"room":"1"}}]}]}')
    _, sections = parse_json.parse_sections(raw, "201510")
    s = sections[0]
    assert s.title == "The Language of the Qur'an"
    assert s.instructors[0].name == "A & B"
    assert s.attributes[0].description == "Maths & Stats"
    assert s.meetings[0].building_name == "Arts & Sci"


def test_html_entities_in_catalog_text_are_decoded():
    raw = (b'{"totalCount":1,"data":[{"subject":"ARA","courseNumber":"205",'
           b'"courseTitle":"Qur&#39;an","termEffective":"201510",'
           b'"courseDescription":"Study of &quot;classical&quot; texts",'
           b'"college":"Arts &amp; Sciences"}]}')
    _, courses = parse_json.parse_catalog(raw, "201510")
    c = courses[0]
    assert c.title == "Qur'an"
    assert c.description == 'Study of "classical" texts'
    assert c.college == "Arts & Sciences"


def test_classroom_is_tba_when_a_meeting_has_no_room():
    """The shipped database stores 'TBA' for an unassigned room, not ''."""
    m = models.Meeting(crn="1", term_id="t", meeting_index=0)
    assert parse_json.classroom_string(m) == "TBA"
    m2 = models.Meeting(crn="1", term_id="t", meeting_index=0,
                        building_name="SBA", room="1104")
    assert parse_json.classroom_string(m2) == "SBA 1104"
