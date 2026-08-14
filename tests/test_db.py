import sqlite3

from auscrawl import db

LEGACY_COURSES = """
CREATE TABLE courses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    crn TEXT NOT NULL, term_id TEXT NOT NULL, subject TEXT NOT NULL,
    course_number TEXT NOT NULL, title TEXT NOT NULL, section TEXT,
    credits REAL, schedule_type TEXT, instructional_method TEXT,
    campus TEXT, levels TEXT, attributes TEXT, registration_dates TEXT,
    class_type TEXT, start_time TEXT, end_time TEXT, days TEXT,
    seats_available BOOLEAN, classroom TEXT, date_range TEXT,
    instructor_name TEXT, instructor_email TEXT, is_lab BOOLEAN DEFAULT 0,
    UNIQUE(crn, term_id, class_type, days, start_time)
);
"""


def cols(conn, table):
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def make_legacy(path, extra=""):
    conn = sqlite3.connect(path)
    conn.executescript(LEGACY_COURSES + extra)
    conn.executescript("""
        INSERT INTO courses (crn, term_id, subject, course_number, title,
                             registration_dates, days, start_time, class_type)
        VALUES ('10394','202710','ACC','201','Fund of Financial Accounting',
                'Apr 13, 2026 to Aug 31, 2026','MW','11:00 am','Class');
    """)
    conn.commit()
    conn.close()


def test_fresh_database_has_every_table(tmp_path):
    conn = db.init_db(str(tmp_path / "new.db"))
    names = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    for t in ("semesters", "subjects", "instructors", "levels", "attributes",
              "courses", "catalog", "section_details", "course_dependencies",
              "section_instructors", "catalog_detail",
              "meetings", "catalog_versions", "prereq_rules"):
        assert t in names, t


def test_migration_adds_new_columns_to_a_legacy_database(tmp_path):
    p = str(tmp_path / "legacy.db")
    make_legacy(p, """
        CREATE TABLE instructors (
            id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
            email TEXT, first_seen TEXT, UNIQUE(name, email)
        );
        CREATE TABLE section_instructors (
            id INTEGER PRIMARY KEY AUTOINCREMENT, crn TEXT NOT NULL,
            term_id TEXT NOT NULL, name TEXT NOT NULL, email TEXT,
            is_primary BOOLEAN DEFAULT 0, UNIQUE(crn, term_id, name)
        );
        CREATE TABLE catalog (
            id INTEGER PRIMARY KEY AUTOINCREMENT, subject TEXT NOT NULL,
            course_number TEXT NOT NULL, description TEXT DEFAULT '',
            credit_hours REAL, lecture_hours REAL, lab_hours REAL,
            department TEXT DEFAULT '', UNIQUE(subject, course_number)
        );
    """)
    conn = db.init_db(p)
    c = cols(conn, "courses")
    for new in ("part_of_term", "building", "room", "enrollment", "max_enrollment",
                "seats_available_count", "cross_list", "section_id", "open_section"):
        assert new in c, new
    assert "banner_id" in cols(conn, "instructors")
    assert "banner_id" in cols(conn, "section_instructors")
    assert "term_effective" in cols(conn, "catalog")


def test_migration_preserves_existing_rows_and_registration_dates(tmp_path):
    p = str(tmp_path / "legacy2.db")
    make_legacy(p)
    conn = db.init_db(p)
    row = conn.execute(
        "SELECT registration_dates, title FROM courses WHERE crn='10394'").fetchone()
    assert row[0] == "Apr 13, 2026 to Aug 31, 2026"
    assert row[1] == "Fund of Financial Accounting"


def test_migration_is_idempotent(tmp_path):
    p = str(tmp_path / "twice.db")
    db.init_db(p).close()
    conn = db.init_db(p)          # must not raise "duplicate column name"
    assert "building" in cols(conn, "courses")


def test_force_recreates_from_scratch(tmp_path):
    p = str(tmp_path / "forced.db")
    conn = db.init_db(p)
    conn.execute("INSERT INTO semesters (term_id, term_name) VALUES ('202710','Fall')")
    conn.commit()
    conn.close()
    conn = db.init_db(p, force=True)
    assert conn.execute("SELECT COUNT(*) FROM semesters").fetchone()[0] == 0


def test_write_pragmas_are_set(tmp_path):
    conn = db.init_db(str(tmp_path / "p.db"))
    assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
