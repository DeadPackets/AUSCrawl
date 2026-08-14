"""Dataclasses mirroring the Banner 9 payloads."""

from dataclasses import dataclass, field


@dataclass(slots=True)
class Semester:
    term_id: str
    term_name: str


@dataclass(slots=True)
class CodeRef:
    code: str
    description: str


@dataclass(slots=True)
class InstructorRef:
    name: str
    email: str = ""
    banner_id: str = ""
    is_primary: bool = False


@dataclass(slots=True)
class Meeting:
    crn: str
    term_id: str
    meeting_index: int
    meeting_type: str = ""
    meeting_type_desc: str = ""
    begin_time: str = ""
    end_time: str = ""
    monday: bool = False
    tuesday: bool = False
    wednesday: bool = False
    thursday: bool = False
    friday: bool = False
    saturday: bool = False
    sunday: bool = False
    building: str = ""
    building_name: str = ""
    room: str = ""
    campus: str = ""
    campus_desc: str = ""
    start_date: str = ""
    end_date: str = ""
    hours_week: float | None = None
    credit_hour_session: float | None = None
    schedule_type: str = ""


@dataclass(slots=True)
class Section:
    crn: str
    term_id: str
    subject: str
    course_number: str
    title: str
    section: str = ""
    credits: float | None = None
    schedule_type: str = ""
    instructional_method: str = ""
    campus: str = ""
    levels: str = ""
    attributes_text: str = ""
    registration_dates: str = ""
    part_of_term: str = ""
    section_id: int | None = None
    enrollment: int | None = None
    max_enrollment: int | None = None
    seats_available_count: int | None = None
    waitlist_capacity: int | None = None
    waitlist_count: int | None = None
    waitlist_available: int | None = None
    cross_list: str = ""
    cross_list_capacity: int | None = None
    cross_list_count: int | None = None
    cross_list_available: int | None = None
    open_section: bool = False
    meetings: list[Meeting] = field(default_factory=list)
    instructors: list[InstructorRef] = field(default_factory=list)
    attributes: list[CodeRef] = field(default_factory=list)


@dataclass(slots=True)
class CatalogCourse:
    subject: str
    course_number: str
    title: str
    term_effective: str
    description: str = ""
    term_start: str = ""
    term_end: str = ""
    college: str = ""
    college_code: str = ""
    department: str = ""
    department_code: str = ""
    credit_hours_low: float | None = None
    credit_hours_high: float | None = None
    lecture_hours_low: float | None = None
    lecture_hours_high: float | None = None
    lab_hours_low: float | None = None
    lab_hours_high: float | None = None
    other_hours_low: float | None = None
    other_hours_high: float | None = None
    bill_hours_low: float | None = None
    bill_hours_high: float | None = None
    prereq_check_method: str = ""


@dataclass(slots=True)
class PrereqRule:
    seq: int
    connector: str = ""       # 'And' | 'Or' | '' on the first row
    open_paren: bool = False
    close_paren: bool = False
    test_code: str = ""
    test_score: str = ""
    req_subject: str = ""
    req_course_number: str = ""
    req_level: str = ""
    min_grade: str = ""


@dataclass(slots=True)
class CourseDetail:
    subject: str
    course_number: str
    term_effective: str
    prerequisites: str = ""
    corequisites: str = ""
    restrictions: str = ""
    course_attributes: str = ""
    levels: str = ""
    grade_modes: str = ""
    schedule_types: str = ""
    prerequisites_json: str = ""
    restrictions_json: str = ""
    rules: list[PrereqRule] = field(default_factory=list)
