"""The redesigned schema: normalized tables with the old names re-exposed as views."""

import sqlite3

from auscrawl import db, models

REAL_TABLES = {
    "terms", "subjects", "instructors", "attributes",
    "course_versions", "prereq_rules",
    "sections", "meetings", "section_instructors", "section_attributes",
    "legacy_section_extras",
}
COMPAT_VIEWS = {
    "courses", "catalog", "catalog_detail", "section_details",
    "course_dependencies", "semesters", "levels",
}


def objects(conn, kind):
    return {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type=? AND name NOT LIKE 'sqlite_%'",
        (kind,))}


def _section(crn="10394", term="202710", **kw):
    fields = dict(subject="ACC", course_number="201",
                  title="Fund of Financial Accounting", section="01",
                  credits=3.0, schedule_type="Lecture", campus="Main Campus",
                  instructional_method="Traditional", part_of_term="1",
                  enrollment=18, max_enrollment=18, seats_available_count=2)
    fields.update(kw)
    s = models.Section(crn=crn, term_id=term, **fields)
    s.meetings = [models.Meeting(
        crn=crn, term_id=term, meeting_index=0, meeting_type_desc="Class",
        begin_time="1100", end_time="1215", monday=True, wednesday=True,
        building="SBA", building_name="School of Business Administrtn", room="1104",
        start_date="08/24/2026", end_date="12/10/2026")]
    s.instructors = [models.InstructorRef(name="Karen Hawa", email="khawa@aus.edu",
                                          banner_id="220388", is_primary=True)]
    s.attributes = [models.CodeRef(code="AMTN", description="Actuarial Math Minor")]
    return s


def _version(subject="ACC", number="201", term_effective="202610", **kw):
    return models.CatalogCourse(subject=subject, course_number=number,
                                title="Fund of Financial Accounting",
                                term_effective=term_effective, **kw)


def seeded(tmp_path, name="s.db"):
    conn = db.init_db(str(tmp_path / name))
    db.save_semesters(conn, [models.Semester(term_id="202710", term_name="Fall 2026")])
    db.save_catalog(conn, [_version(description="Intro to accounting.",
                                    credit_hours_low=3.0, college="Business",
                                    department="Accounting")])
    db.save_course_details(conn, [models.CourseDetail(
        subject="ACC", course_number="201", term_effective="202610",
        levels="Undergraduate", grade_modes="Standard Letter",
        schedule_types="Lecture", prerequisites="MTH 104",
        restrictions="Must be Undergraduate",
        rules=[models.PrereqRule(seq=0, req_subject="Math",
                                 req_course_number="104", req_level="Undergraduate",
                                 min_grade="C-")])])
    db.save_sections(conn, [_section()])
    return conn


# --- structure ---------------------------------------------------------------

def test_the_normalized_tables_exist(tmp_path):
    conn = db.init_db(str(tmp_path / "a.db"))
    assert REAL_TABLES <= objects(conn, "table")


def test_the_old_table_names_are_views_not_tables(tmp_path):
    conn = db.init_db(str(tmp_path / "b.db"))
    views, tables = objects(conn, "view"), objects(conn, "table")
    assert COMPAT_VIEWS <= views
    assert not (COMPAT_VIEWS & tables), "old names must not also exist as tables"


def test_no_redundant_columns_survive_on_sections(tmp_path):
    conn = db.init_db(str(tmp_path / "c.db"))
    cols = {r[1] for r in conn.execute("PRAGMA table_info(sections)")}
    for gone in ("classroom", "days", "start_time", "class_type", "date_range",
                 "instructor_name", "levels", "attributes", "registration_dates"):
        assert gone not in cols, f"{gone} belongs in meetings/views, not sections"


# --- the courses view reproduces the legacy shape -----------------------------

def test_courses_view_reproduces_the_legacy_row(tmp_path):
    conn = seeded(tmp_path)
    row = conn.execute("""SELECT crn, term_id, subject, course_number, title, section,
                                 credits, schedule_type, campus, class_type,
                                 start_time, end_time, days, classroom, date_range,
                                 seats_available, instructor_name, instructor_email,
                                 is_lab, levels, attributes
                          FROM courses""").fetchone()
    assert row == ("10394", "202710", "ACC", "201", "Fund of Financial Accounting",
                   "01", 3.0, "Lecture", "Main Campus", "Class",
                   "11:00 am", "12:15 pm", "MW",
                   "School of Business Administrtn 1104",
                   "Aug 24, 2026 - Dec 10, 2026", 1, "Karen Hawa", "khawa@aus.edu",
                   0, "Undergraduate", "Actuarial Math Minor")


def test_courses_view_renders_tba_and_blank_times_for_a_meetingless_section(tmp_path):
    conn = seeded(tmp_path)
    s = _section(crn="777")
    s.meetings = []
    db.save_sections(conn, [s])
    row = conn.execute("""SELECT class_type, start_time, days, classroom, date_range
                          FROM courses WHERE crn='777'""").fetchone()
    assert row == ("", "", "", "TBA", "")


def test_courses_view_emits_one_row_per_meeting_block(tmp_path):
    conn = seeded(tmp_path)
    s = _section(crn="888")
    s.meetings.append(models.Meeting(
        crn="888", term_id="202710", meeting_index=1, meeting_type_desc="Lab",
        begin_time="1400", end_time="1650", friday=True,
        building="ENG", building_name="Engineering", room="002",
        start_date="08/24/2026", end_date="12/10/2026"))
    db.save_sections(conn, [s])
    rows = conn.execute("""SELECT class_type, days, start_time, is_lab FROM courses
                           WHERE crn='888' ORDER BY class_type""").fetchall()
    assert rows == [("Class", "MW", "11:00 am", 0), ("Lab", "F", "2:00 pm", 1)]


def test_time_conversion_covers_midnight_and_noon(tmp_path):
    conn = seeded(tmp_path)
    for crn, begin, expected in (("1", "0000", "12:00 am"), ("2", "1200", "12:00 pm"),
                                 ("3", "0800", "8:00 am"), ("4", "2345", "11:45 pm")):
        s = _section(crn=crn)
        s.meetings[0].crn = crn
        s.meetings[0].begin_time = begin
        db.save_sections(conn, [s])
        assert conn.execute(
            "SELECT start_time FROM courses WHERE crn=?", (crn,)
        ).fetchone()[0] == expected


# --- the views that used to go stale -----------------------------------------

def test_course_dependencies_view_is_derived_and_therefore_never_stale(tmp_path):
    conn = seeded(tmp_path)
    rows = conn.execute("""SELECT crn, term_id, dep_type, subject, course_number,
                                  minimum_grade FROM course_dependencies""").fetchall()
    assert rows == [("10394", "202710", "prerequisite", "Math", "104", "C-")]


def test_section_details_view_is_derived_from_the_course_version(tmp_path):
    conn = seeded(tmp_path)
    row = conn.execute("""SELECT crn, term_id, prerequisites, restrictions
                          FROM section_details""").fetchone()
    assert row == ("10394", "202710", "MTH 104", "Must be Undergraduate")


def test_levels_view_lists_the_distinct_levels(tmp_path):
    conn = seeded(tmp_path)
    assert conn.execute("SELECT level FROM levels").fetchall() == [("Undergraduate",)]


def test_catalog_view_shows_the_newest_version(tmp_path):
    conn = seeded(tmp_path)
    db.save_catalog(conn, [_version(term_effective="202710",
                                    description="Revised description.",
                                    credit_hours_low=3.0)])
    row = conn.execute(
        "SELECT description, credit_hours FROM catalog WHERE subject='ACC'").fetchone()
    assert row == ("Revised description.", 3.0)


def test_catalog_detail_view_shows_levels_and_schedule_types(tmp_path):
    conn = seeded(tmp_path)
    row = conn.execute("""SELECT levels, schedule_types, prerequisites
                          FROM catalog_detail WHERE subject='ACC'""").fetchone()
    assert row == ("Undergraduate", "Lecture", "MTH 104")


def test_semesters_view_still_answers(tmp_path):
    conn = seeded(tmp_path)
    assert conn.execute("SELECT term_id, term_name FROM semesters").fetchall() == [
        ("202710", "Fall 2026")]


# --- a section is now updated, not duplicated --------------------------------

def test_a_changed_room_updates_the_section_instead_of_duplicating_it(tmp_path):
    conn = seeded(tmp_path)
    s = _section()
    s.meetings[0].room = "9999"
    s.meetings[0].begin_time = "1300"
    db.save_sections(conn, [s])
    assert conn.execute("SELECT COUNT(*) FROM sections").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM meetings").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM courses").fetchone()[0] == 1
    assert conn.execute(
        "SELECT classroom, start_time FROM courses").fetchone() == (
        "School of Business Administrtn 9999", "1:00 pm")


def test_seat_counts_refresh_on_a_recrawl(tmp_path):
    conn = seeded(tmp_path)
    db.save_sections(conn, [_section(enrollment=20, max_enrollment=20,
                                     seats_available_count=0)])
    assert conn.execute(
        "SELECT enrollment, seats_available FROM sections").fetchone() == (20, 0)
    assert conn.execute("SELECT seats_available FROM courses").fetchone()[0] == 0


# --- Banner 8 only data survives in one clearly named place -------------------

def test_legacy_extras_supply_registration_dates_and_richer_titles(tmp_path):
    conn = seeded(tmp_path)
    conn.execute("""INSERT INTO legacy_section_extras
                        (crn, term_id, registration_dates, title)
                    VALUES ('10394','202710','Apr 13, 2026 to Aug 31, 2026',
                            'Fund of Financial Accounting (special)')""")
    conn.commit()
    row = conn.execute(
        "SELECT registration_dates, title FROM courses WHERE crn='10394'").fetchone()
    assert row == ("Apr 13, 2026 to Aug 31, 2026",
                   "Fund of Financial Accounting (special)")


def test_a_section_without_legacy_extras_uses_the_banner9_title(tmp_path):
    conn = seeded(tmp_path)
    row = conn.execute(
        "SELECT registration_dates, title FROM courses WHERE crn='10394'").fetchone()
    assert row == ("", "Fund of Financial Accounting")


def test_importing_legacy_extras_from_an_old_database(tmp_path):
    """The Banner 8 snapshot holds data no endpoint can regenerate. It is imported
    once into a table named for exactly what it is."""
    old = str(tmp_path / "old.db")
    src = sqlite3.connect(old)
    src.executescript("""
        CREATE TABLE courses (crn TEXT, term_id TEXT, title TEXT,
                              registration_dates TEXT);
        INSERT INTO courses VALUES
            ('10394','202710','Calculus III (Take it with MTH 203R Sec.1)',
             'Apr 13, 2026 to Aug 31, 2026'),
            ('10394','202710','Calculus III (Take it with MTH 203R Sec.1)',
             'Apr 13, 2026 to Aug 31, 2026');
    """)
    src.commit()
    src.close()

    conn = seeded(tmp_path)
    n = db.import_legacy_extras(conn, old)
    assert n == 1, "duplicate legacy rows collapse to one per section"
    assert conn.execute(
        "SELECT title FROM courses WHERE crn='10394'"
    ).fetchone()[0] == "Calculus III (Take it with MTH 203R Sec.1)"


def test_is_lab_matches_the_legacy_definition_exactly(tmp_path):
    """The Banner 8 parser tested schedule type == 'Lab', so a 'Lecture/Lab' section
    was not flagged. A compatibility view must not quietly widen that."""
    conn = seeded(tmp_path)
    for crn, sched, expected in (("a", "Lab", 1), ("b", "Lecture/Lab", 0),
                                 ("c", "Lecture", 0)):
        s = _section(crn=crn, schedule_type=sched)
        s.meetings[0].crn = crn
        db.save_sections(conn, [s])
        assert conn.execute(
            "SELECT is_lab FROM courses WHERE crn=?", (crn,)
        ).fetchone()[0] == expected, sched


def test_is_lab_is_set_by_a_lab_meeting_block(tmp_path):
    conn = seeded(tmp_path)
    s = _section(crn="d", schedule_type="Lecture")
    s.meetings[0].crn = "d"
    s.meetings[0].meeting_type_desc = "Lab"
    db.save_sections(conn, [s])
    assert conn.execute("SELECT is_lab FROM courses WHERE crn='d'").fetchone()[0] == 1


def test_a_section_with_no_instructor_reads_tba(tmp_path):
    """The published database records an unassigned instructor as 'TBA' with an
    empty email, the same sentinel it uses for an unassigned room."""
    conn = seeded(tmp_path)
    s = _section(crn="noinstr")
    s.meetings[0].crn = "noinstr"
    s.instructors = []
    db.save_sections(conn, [s])
    assert conn.execute(
        "SELECT instructor_name, instructor_email FROM courses WHERE crn='noinstr'"
    ).fetchone() == ("TBA", "")
