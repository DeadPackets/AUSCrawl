from auscrawl import db, models


def _section(crn="10394", term="202710", **kw):
    s = models.Section(crn=crn, term_id=term, subject="ACC", course_number="201",
                       title="Fund of Financial Accounting", section="01",
                       schedule_type="Lecture", campus="Main Campus",
                       enrollment=18, max_enrollment=18, seats_available_count=0,
                       **kw)
    s.meetings = [models.Meeting(
        crn=crn, term_id=term, meeting_index=0, meeting_type_desc="Class",
        begin_time="1100", end_time="1215", monday=True, wednesday=True,
        building="SBA", building_name="School of Business Administrtn", room="1104",
        start_date="08/24/2026", end_date="12/10/2026")]
    s.instructors = [models.InstructorRef(name="Karen Hawa", email="khawa@aus.edu",
                                          banner_id="220388", is_primary=True)]
    s.attributes = [models.CodeRef(code="AMTN", description="Actuarial Math Minor")]
    return s


def test_saving_a_section_fills_the_legacy_columns_in_legacy_formats(tmp_path):
    conn = db.init_db(str(tmp_path / "a.db"))
    db.save_sections(conn, [_section()])
    row = conn.execute("""SELECT days, start_time, end_time, classroom, date_range,
                                 class_type, seats_available, schedule_type
                          FROM courses WHERE crn='10394'""").fetchone()
    assert row == ("MW", "11:00 am", "12:15 pm",
                   "School of Business Administrtn 1104",
                   "Aug 24, 2026 - Dec 10, 2026", "Class", 0, "Lecture")


def test_saving_a_section_fills_the_new_columns(tmp_path):
    conn = db.init_db(str(tmp_path / "b.db"))
    db.save_sections(conn, [_section()])
    row = conn.execute("""SELECT building, building_name, room, enrollment,
                                 max_enrollment, seats_available_count
                          FROM courses WHERE crn='10394'""").fetchone()
    assert row == ("SBA", "School of Business Administrtn", "1104", 18, 18, 0)


def test_a_section_with_no_meetings_still_produces_one_row(tmp_path):
    conn = db.init_db(str(tmp_path / "nm.db"))
    s = _section(crn="55")
    s.meetings = []
    db.save_sections(conn, [s])
    row = conn.execute(
        "SELECT days, classroom, enrollment FROM courses WHERE crn='55'").fetchone()
    assert row == ("", "", 18)


def test_meetings_are_written_as_their_own_rows(tmp_path):
    conn = db.init_db(str(tmp_path / "c.db"))
    db.save_sections(conn, [_section()])
    row = conn.execute("""SELECT meeting_index, building, room, monday, friday
                          FROM meetings WHERE crn='10394'""").fetchone()
    assert row == (0, "SBA", "1104", 1, 0)


def test_instructors_carry_banner_id_into_both_tables(tmp_path):
    conn = db.init_db(str(tmp_path / "d.db"))
    db.save_sections(conn, [_section()])
    assert conn.execute(
        "SELECT banner_id FROM instructors WHERE name='Karen Hawa'"
    ).fetchone()[0] == "220388"
    assert conn.execute(
        "SELECT banner_id, is_primary FROM section_instructors WHERE crn='10394'"
    ).fetchone() == ("220388", 1)


def test_registration_dates_is_never_overwritten_with_empty(tmp_path):
    conn = db.init_db(str(tmp_path / "e.db"))
    db.save_sections(conn, [_section()])
    conn.execute("UPDATE courses SET registration_dates='Apr 13, 2026 to Aug 31, 2026'")
    conn.commit()
    db.save_sections(conn, [_section()])          # a re-crawl; no reg dates available
    assert conn.execute(
        "SELECT registration_dates FROM courses WHERE crn='10394'"
    ).fetchone()[0] == "Apr 13, 2026 to Aug 31, 2026"


def test_first_seen_is_the_earliest_term(tmp_path):
    conn = db.init_db(str(tmp_path / "f.db"))
    db.save_sections(conn, [_section(term="202710"), _section(crn="99", term="200520")])
    db.save_subjects(conn, [models.CodeRef(code="ACC", description="Accounting")],
                     "202710")
    db.fix_first_seen(conn)
    assert conn.execute(
        "SELECT first_seen FROM instructors WHERE name='Karen Hawa'"
    ).fetchone()[0] == "200520"
    assert conn.execute(
        "SELECT first_seen FROM subjects WHERE short_name='ACC'"
    ).fetchone()[0] == "200520"


def test_catalog_versions_and_flat_catalog_both_written(tmp_path):
    conn = db.init_db(str(tmp_path / "g.db"))
    old = models.CatalogCourse(subject="ACC", course_number="201", title="T",
                               term_effective="201510", description="old text",
                               credit_hours_low=3.0)
    new = models.CatalogCourse(subject="ACC", course_number="201", title="T",
                               term_effective="202610", description="new text",
                               credit_hours_low=3.0)
    db.save_catalog(conn, [old, new])
    assert conn.execute("SELECT COUNT(*) FROM catalog_versions").fetchone()[0] == 2
    # the flat table holds the newest version
    assert conn.execute(
        "SELECT description, term_effective FROM catalog WHERE subject='ACC'"
    ).fetchone() == ("new text", "202610")


def test_a_later_crawl_can_only_advance_the_flat_catalog(tmp_path):
    conn = db.init_db(str(tmp_path / "g2.db"))
    db.save_catalog(conn, [models.CatalogCourse(
        subject="ACC", course_number="201", title="T", term_effective="202610",
        description="new text")])
    db.save_catalog(conn, [models.CatalogCourse(
        subject="ACC", course_number="201", title="T", term_effective="200510",
        description="ancient text")])
    assert conn.execute(
        "SELECT description FROM catalog WHERE subject='ACC'"
    ).fetchone()[0] == "new text"


def test_course_details_write_rules_and_json(tmp_path):
    conn = db.init_db(str(tmp_path / "h.db"))
    db.save_catalog(conn, [models.CatalogCourse(
        subject="CMP", course_number="305", title="T", term_effective="202610")])
    d = models.CourseDetail(
        subject="CMP", course_number="305", term_effective="202610",
        prerequisites="CMP 220 and (CMP 213 or MTH 213)",
        levels="Undergraduate", grade_modes="Standard Letter",
        prerequisites_json='{"type":"and","operands":[]}',
        rules=[models.PrereqRule(seq=0, req_subject="Computer Science",
                                 req_course_number="220", min_grade="C-")])
    db.save_course_details(conn, [d])
    assert conn.execute("SELECT COUNT(*) FROM prereq_rules").fetchone()[0] == 1
    assert conn.execute(
        "SELECT levels, grade_modes FROM catalog_versions WHERE subject='CMP'"
    ).fetchone() == ("Undergraduate", "Standard Letter")
    assert conn.execute(
        "SELECT levels FROM catalog_detail WHERE subject='CMP'"
    ).fetchone()[0] == "Undergraduate"


def test_resume_helpers_report_what_is_already_stored(tmp_path):
    conn = db.init_db(str(tmp_path / "i.db"))
    db.save_sections(conn, [_section()])
    db.save_catalog(conn, [models.CatalogCourse(
        subject="ACC", course_number="201", title="T", term_effective="202610")])
    assert db.done_terms(conn) == {"202710"}
    assert db.done_course_versions(conn) == set()      # no details fetched yet
    db.save_course_details(conn, [models.CourseDetail(
        subject="ACC", course_number="201", term_effective="202610",
        levels="Undergraduate")])
    assert ("ACC", "201", "202610") in db.done_course_versions(conn)
