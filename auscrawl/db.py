"""SQLite schema, additive migration, and bulk writes."""

import logging
import os
import sqlite3

from .parse_json import classroom_string, days_string, format_date_range, to_12h

log = logging.getLogger("auscrawl")

SCHEMA = """
CREATE TABLE IF NOT EXISTS semesters (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    term_id TEXT UNIQUE NOT NULL,
    term_name TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS subjects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    short_name TEXT NOT NULL, long_name TEXT NOT NULL, first_seen TEXT,
    UNIQUE(short_name)
);
CREATE TABLE IF NOT EXISTS instructors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL, email TEXT, first_seen TEXT, banner_id TEXT,
    UNIQUE(name, email)
);
CREATE TABLE IF NOT EXISTS levels (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    level TEXT UNIQUE NOT NULL, first_seen TEXT
);
CREATE TABLE IF NOT EXISTS attributes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    attribute TEXT UNIQUE NOT NULL, first_seen TEXT
);
CREATE TABLE IF NOT EXISTS courses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    crn TEXT NOT NULL, term_id TEXT NOT NULL, subject TEXT NOT NULL,
    course_number TEXT NOT NULL, title TEXT NOT NULL, section TEXT,
    credits REAL, schedule_type TEXT, instructional_method TEXT, campus TEXT,
    levels TEXT, attributes TEXT, registration_dates TEXT,
    class_type TEXT, start_time TEXT, end_time TEXT, days TEXT,
    seats_available BOOLEAN, classroom TEXT, date_range TEXT,
    instructor_name TEXT, instructor_email TEXT, is_lab BOOLEAN DEFAULT 0,
    part_of_term TEXT, building TEXT, building_name TEXT, room TEXT,
    campus_code TEXT, enrollment INTEGER, max_enrollment INTEGER,
    seats_available_count INTEGER, waitlist_capacity INTEGER,
    waitlist_count INTEGER, waitlist_available INTEGER,
    cross_list TEXT, cross_list_capacity INTEGER, cross_list_count INTEGER,
    cross_list_available INTEGER, open_section BOOLEAN, section_id INTEGER,
    UNIQUE(crn, term_id, class_type, days, start_time)
);
CREATE TABLE IF NOT EXISTS catalog (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    subject TEXT NOT NULL, course_number TEXT NOT NULL,
    description TEXT DEFAULT '', credit_hours REAL, lecture_hours REAL,
    lab_hours REAL, department TEXT DEFAULT '',
    lecture_hours_high REAL, lab_hours_high REAL,
    other_hours_low REAL, other_hours_high REAL,
    bill_hours_low REAL, bill_hours_high REAL, credit_hours_high REAL,
    college TEXT DEFAULT '', college_code TEXT DEFAULT '',
    department_code TEXT DEFAULT '', term_effective TEXT DEFAULT '',
    term_start TEXT DEFAULT '', term_end TEXT DEFAULT '',
    prereq_check_method TEXT DEFAULT '', title TEXT DEFAULT '',
    UNIQUE(subject, course_number)
);
CREATE TABLE IF NOT EXISTS section_details (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    crn TEXT NOT NULL, term_id TEXT NOT NULL,
    prerequisites TEXT DEFAULT '', corequisites TEXT DEFAULT '',
    restrictions TEXT DEFAULT '', waitlist_capacity INTEGER DEFAULT 0,
    waitlist_actual INTEGER DEFAULT 0, waitlist_remaining INTEGER DEFAULT 0,
    fees TEXT DEFAULT '', prerequisites_json TEXT DEFAULT '',
    corequisites_json TEXT DEFAULT '', restrictions_json TEXT DEFAULT '',
    UNIQUE(crn, term_id)
);
CREATE TABLE IF NOT EXISTS course_dependencies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    crn TEXT NOT NULL, term_id TEXT NOT NULL, dep_type TEXT NOT NULL,
    subject TEXT NOT NULL, course_number TEXT NOT NULL,
    minimum_grade TEXT DEFAULT '',
    UNIQUE(crn, term_id, dep_type, subject, course_number)
);
CREATE TABLE IF NOT EXISTS section_instructors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    crn TEXT NOT NULL, term_id TEXT NOT NULL, name TEXT NOT NULL,
    email TEXT, is_primary BOOLEAN DEFAULT 0, banner_id TEXT,
    UNIQUE(crn, term_id, name)
);
CREATE TABLE IF NOT EXISTS catalog_detail (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    subject TEXT NOT NULL, course_number TEXT NOT NULL, term_id TEXT DEFAULT '',
    levels TEXT DEFAULT '', schedule_types TEXT DEFAULT '',
    course_attributes TEXT DEFAULT '', prerequisites TEXT DEFAULT '',
    corequisites TEXT DEFAULT '', restrictions TEXT DEFAULT '',
    grade_modes TEXT DEFAULT '',
    UNIQUE(subject, course_number)
);
CREATE TABLE IF NOT EXISTS meetings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    crn TEXT NOT NULL, term_id TEXT NOT NULL, meeting_index INTEGER NOT NULL,
    meeting_type TEXT DEFAULT '', meeting_type_desc TEXT DEFAULT '',
    begin_time TEXT DEFAULT '', end_time TEXT DEFAULT '',
    monday BOOLEAN DEFAULT 0, tuesday BOOLEAN DEFAULT 0,
    wednesday BOOLEAN DEFAULT 0, thursday BOOLEAN DEFAULT 0,
    friday BOOLEAN DEFAULT 0, saturday BOOLEAN DEFAULT 0, sunday BOOLEAN DEFAULT 0,
    building TEXT DEFAULT '', building_name TEXT DEFAULT '', room TEXT DEFAULT '',
    campus TEXT DEFAULT '', campus_desc TEXT DEFAULT '',
    start_date TEXT DEFAULT '', end_date TEXT DEFAULT '',
    hours_week REAL, credit_hour_session REAL, schedule_type TEXT DEFAULT '',
    UNIQUE(crn, term_id, meeting_index)
);
CREATE TABLE IF NOT EXISTS catalog_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    subject TEXT NOT NULL, course_number TEXT NOT NULL,
    term_effective TEXT NOT NULL, term_start TEXT DEFAULT '',
    term_end TEXT DEFAULT '', title TEXT DEFAULT '', description TEXT DEFAULT '',
    college TEXT DEFAULT '', college_code TEXT DEFAULT '',
    department TEXT DEFAULT '', department_code TEXT DEFAULT '',
    credit_hours_low REAL, credit_hours_high REAL,
    lecture_hours_low REAL, lecture_hours_high REAL,
    lab_hours_low REAL, lab_hours_high REAL,
    other_hours_low REAL, other_hours_high REAL,
    bill_hours_low REAL, bill_hours_high REAL,
    prereq_check_method TEXT DEFAULT '',
    prerequisites TEXT DEFAULT '', corequisites TEXT DEFAULT '',
    restrictions TEXT DEFAULT '', course_attributes TEXT DEFAULT '',
    levels TEXT DEFAULT '', grade_modes TEXT DEFAULT '',
    schedule_types TEXT DEFAULT '',
    prerequisites_json TEXT DEFAULT '', restrictions_json TEXT DEFAULT '',
    UNIQUE(subject, course_number, term_effective)
);
CREATE TABLE IF NOT EXISTS prereq_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    subject TEXT NOT NULL, course_number TEXT NOT NULL,
    term_effective TEXT NOT NULL, seq INTEGER NOT NULL,
    connector TEXT DEFAULT '', open_paren BOOLEAN DEFAULT 0,
    close_paren BOOLEAN DEFAULT 0, test_code TEXT DEFAULT '',
    test_score TEXT DEFAULT '', req_subject TEXT DEFAULT '',
    req_course_number TEXT DEFAULT '', req_level TEXT DEFAULT '',
    min_grade TEXT DEFAULT '',
    UNIQUE(subject, course_number, term_effective, seq)
);
CREATE INDEX IF NOT EXISTS idx_courses_term ON courses(term_id);
CREATE INDEX IF NOT EXISTS idx_courses_subject ON courses(subject);
CREATE INDEX IF NOT EXISTS idx_courses_crn ON courses(crn);
CREATE INDEX IF NOT EXISTS idx_courses_instructor ON courses(instructor_name);
CREATE INDEX IF NOT EXISTS idx_courses_crn_term ON courses(crn, term_id);
CREATE INDEX IF NOT EXISTS idx_catalog_subject ON catalog(subject);
CREATE INDEX IF NOT EXISTS idx_section_details_crn ON section_details(crn, term_id);
CREATE INDEX IF NOT EXISTS idx_deps_crn ON course_dependencies(crn, term_id);
CREATE INDEX IF NOT EXISTS idx_deps_target
    ON course_dependencies(subject, course_number);
CREATE INDEX IF NOT EXISTS idx_section_instructors
    ON section_instructors(crn, term_id);
CREATE INDEX IF NOT EXISTS idx_section_instructors_name ON section_instructors(name);
CREATE INDEX IF NOT EXISTS idx_catalog_detail_subject ON catalog_detail(subject);
CREATE INDEX IF NOT EXISTS idx_meetings_crn_term ON meetings(crn, term_id);
CREATE INDEX IF NOT EXISTS idx_catalog_versions_course
    ON catalog_versions(subject, course_number);
CREATE INDEX IF NOT EXISTS idx_prereq_rules_course
    ON prereq_rules(subject, course_number, term_effective);
CREATE INDEX IF NOT EXISTS idx_prereq_rules_target
    ON prereq_rules(req_subject, req_course_number);
"""

# Columns added to tables that already exist in the shipped database. Migration is
# additive so that pointing the crawler at aus_courses.db upgrades it in place.
NEW_COLUMNS: dict[str, list[tuple[str, str]]] = {
    "courses": [
        ("part_of_term", "TEXT"), ("building", "TEXT"), ("building_name", "TEXT"),
        ("room", "TEXT"), ("campus_code", "TEXT"), ("enrollment", "INTEGER"),
        ("max_enrollment", "INTEGER"), ("seats_available_count", "INTEGER"),
        ("waitlist_capacity", "INTEGER"), ("waitlist_count", "INTEGER"),
        ("waitlist_available", "INTEGER"), ("cross_list", "TEXT"),
        ("cross_list_capacity", "INTEGER"), ("cross_list_count", "INTEGER"),
        ("cross_list_available", "INTEGER"), ("open_section", "BOOLEAN"),
        ("section_id", "INTEGER"),
    ],
    "instructors": [("banner_id", "TEXT")],
    "section_instructors": [("banner_id", "TEXT")],
    "catalog": [
        ("lecture_hours_high", "REAL"), ("lab_hours_high", "REAL"),
        ("other_hours_low", "REAL"), ("other_hours_high", "REAL"),
        ("bill_hours_low", "REAL"), ("bill_hours_high", "REAL"),
        ("credit_hours_high", "REAL"), ("college", "TEXT DEFAULT ''"),
        ("college_code", "TEXT DEFAULT ''"), ("department_code", "TEXT DEFAULT ''"),
        ("term_effective", "TEXT DEFAULT ''"), ("term_start", "TEXT DEFAULT ''"),
        ("term_end", "TEXT DEFAULT ''"), ("prereq_check_method", "TEXT DEFAULT ''"),
        ("title", "TEXT DEFAULT ''"),
    ],
    "catalog_detail": [("grade_modes", "TEXT DEFAULT ''")],
}


def migrate_schema(conn: sqlite3.Connection) -> list[str]:
    """Add any missing column to a pre-existing table. Idempotent."""
    added: list[str] = []
    existing = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    for table, columns in NEW_COLUMNS.items():
        if table not in existing:
            continue
        have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        for name, ddl in columns:
            if name not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
                added.append(f"{table}.{name}")
    if added:
        conn.commit()
        log.info("migrated %d columns: %s", len(added), ", ".join(added))
    return added


def init_db(db_path: str, force: bool = False) -> sqlite3.Connection:
    if force and os.path.exists(db_path):
        os.remove(db_path)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    migrate_schema(conn)          # widen old tables before CREATE IF NOT EXISTS
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


# ── Saves ────────────────────────────────────────────────────────────────────

def save_semesters(conn: sqlite3.Connection, semesters) -> None:
    conn.executemany(
        "INSERT OR IGNORE INTO semesters (term_id, term_name) VALUES (?, ?)",
        [(s.term_id, s.term_name) for s in semesters],
    )
    conn.commit()


def save_subjects(conn: sqlite3.Connection, refs, term_id: str) -> None:
    conn.executemany(
        "INSERT OR IGNORE INTO subjects (short_name, long_name, first_seen) "
        "VALUES (?, ?, ?)",
        [(r.code, r.description, term_id) for r in refs],
    )
    conn.commit()


def save_attributes(conn: sqlite3.Connection, refs, term_id: str) -> None:
    conn.executemany(
        "INSERT OR IGNORE INTO attributes (attribute, first_seen) VALUES (?, ?)",
        [(r.description, term_id) for r in refs],
    )
    conn.commit()


_COURSE_COLS = (
    "crn, term_id, subject, course_number, title, section, credits, schedule_type, "
    "instructional_method, campus, attributes, class_type, start_time, end_time, "
    "days, seats_available, classroom, date_range, instructor_name, "
    "instructor_email, is_lab, part_of_term, building, building_name, room, "
    "campus_code, enrollment, max_enrollment, seats_available_count, "
    "waitlist_capacity, waitlist_count, waitlist_available, cross_list, "
    "cross_list_capacity, cross_list_count, cross_list_available, open_section, "
    "section_id"
)


def _course_rows(s):
    """One row per meeting block, preserving the legacy row model."""
    primary = next((i for i in s.instructors if i.is_primary),
                   s.instructors[0] if s.instructors else None)
    head = (s.crn, s.term_id, s.subject, s.course_number, s.title, s.section,
            s.credits, s.schedule_type, s.instructional_method, s.campus,
            s.attributes_text)
    who = (primary.name if primary else "", primary.email if primary else "")
    counts = (s.part_of_term,) + (
        s.enrollment, s.max_enrollment, s.seats_available_count,
        s.waitlist_capacity, s.waitlist_count, s.waitlist_available,
        s.cross_list, s.cross_list_capacity, s.cross_list_count,
        s.cross_list_available, int(s.open_section), s.section_id)
    seats = (None if s.seats_available_count is None
             else int(s.seats_available_count > 0))

    for m in (s.meetings or [None]):
        if m is None:
            sched = ("", "", "", "", seats, "", "")
            is_lab = int("lab" in s.title.lower())
            place = ("", "", "", "")
        else:
            sched = (m.meeting_type_desc, to_12h(m.begin_time), to_12h(m.end_time),
                     days_string(m), seats, classroom_string(m),
                     format_date_range(m.start_date, m.end_date))
            is_lab = int("lab" in (s.schedule_type or "").lower()
                         or "lab" in (m.meeting_type_desc or "").lower())
            place = (m.building, m.building_name, m.room, m.campus)
        yield head + sched + who + (is_lab,) + counts[:1] + place + counts[1:]


def save_sections(conn: sqlite3.Connection, sections) -> None:
    """Insert sections, meetings and instructors.

    Sorted by term so INSERT OR IGNORE keeps the earliest occurrence, which is how
    first_seen comes out right for free. registration_dates is deliberately absent
    from the column list: Banner 9 has no source for it, and writing would erase
    values the old crawler collected.
    """
    ordered = sorted(sections, key=lambda s: s.term_id)
    n_cols = len(_COURSE_COLS.split(", "))
    conn.executemany(
        f"INSERT OR IGNORE INTO courses ({_COURSE_COLS}) "
        f"VALUES ({', '.join('?' * n_cols)})",
        [r for s in ordered for r in _course_rows(s)],
    )
    conn.executemany(
        """INSERT OR IGNORE INTO meetings (
               crn, term_id, meeting_index, meeting_type, meeting_type_desc,
               begin_time, end_time, monday, tuesday, wednesday, thursday, friday,
               saturday, sunday, building, building_name, room, campus, campus_desc,
               start_date, end_date, hours_week, credit_hour_session, schedule_type)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        [(m.crn, m.term_id, m.meeting_index, m.meeting_type, m.meeting_type_desc,
          m.begin_time, m.end_time, int(m.monday), int(m.tuesday), int(m.wednesday),
          int(m.thursday), int(m.friday), int(m.saturday), int(m.sunday),
          m.building, m.building_name, m.room, m.campus, m.campus_desc,
          m.start_date, m.end_date, m.hours_week, m.credit_hour_session,
          m.schedule_type)
         for s in ordered for m in s.meetings],
    )
    conn.executemany(
        "INSERT OR IGNORE INTO instructors (name, email, first_seen, banner_id) "
        "VALUES (?,?,?,?)",
        [(i.name, i.email, s.term_id, i.banner_id)
         for s in ordered for i in s.instructors],
    )
    conn.executemany(
        "INSERT OR IGNORE INTO section_instructors "
        "(crn, term_id, name, email, is_primary, banner_id) VALUES (?,?,?,?,?,?)",
        [(s.crn, s.term_id, i.name, i.email, int(i.is_primary), i.banner_id)
         for s in ordered for i in s.instructors],
    )
    conn.executemany(
        "INSERT OR IGNORE INTO attributes (attribute, first_seen) VALUES (?,?)",
        [(a.description, s.term_id) for s in ordered for a in s.attributes],
    )
    conn.commit()


def save_catalog(conn: sqlite3.Connection, courses) -> None:
    """Write every version, then refresh the flat table from the newest one."""
    conn.executemany(
        """INSERT OR IGNORE INTO catalog_versions (
               subject, course_number, term_effective, term_start, term_end, title,
               description, college, college_code, department, department_code,
               credit_hours_low, credit_hours_high, lecture_hours_low,
               lecture_hours_high, lab_hours_low, lab_hours_high, other_hours_low,
               other_hours_high, bill_hours_low, bill_hours_high, prereq_check_method)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        [(c.subject, c.course_number, c.term_effective, c.term_start, c.term_end,
          c.title, c.description, c.college, c.college_code, c.department,
          c.department_code, c.credit_hours_low, c.credit_hours_high,
          c.lecture_hours_low, c.lecture_hours_high, c.lab_hours_low,
          c.lab_hours_high, c.other_hours_low, c.other_hours_high,
          c.bill_hours_low, c.bill_hours_high, c.prereq_check_method)
         for c in courses if c.term_effective],
    )
    conn.commit()
    refresh_flat_catalog(conn)


_NEWEST_VERSION = """
    SELECT MAX(term_effective) FROM catalog_versions w
    WHERE w.subject = v.subject AND w.course_number = v.course_number
"""


def refresh_flat_catalog(conn: sqlite3.Connection) -> None:
    """Project the newest catalog_versions row per course into the flat table."""
    conn.execute(f"""
        INSERT INTO catalog (subject, course_number, description, credit_hours,
                             lecture_hours, lab_hours, department, lecture_hours_high,
                             lab_hours_high, other_hours_low, other_hours_high,
                             bill_hours_low, bill_hours_high, credit_hours_high,
                             college, college_code, department_code, term_effective,
                             term_start, term_end, prereq_check_method, title)
        SELECT subject, course_number, description, credit_hours_low,
               lecture_hours_low, lab_hours_low, department, lecture_hours_high,
               lab_hours_high, other_hours_low, other_hours_high, bill_hours_low,
               bill_hours_high, credit_hours_high, college, college_code,
               department_code, term_effective, term_start, term_end,
               prereq_check_method, title
        FROM catalog_versions v
        WHERE v.term_effective = ({_NEWEST_VERSION})
        ON CONFLICT(subject, course_number) DO UPDATE SET
            description = excluded.description,
            credit_hours = excluded.credit_hours,
            lecture_hours = excluded.lecture_hours,
            lab_hours = excluded.lab_hours,
            department = excluded.department,
            lecture_hours_high = excluded.lecture_hours_high,
            lab_hours_high = excluded.lab_hours_high,
            other_hours_low = excluded.other_hours_low,
            other_hours_high = excluded.other_hours_high,
            bill_hours_low = excluded.bill_hours_low,
            bill_hours_high = excluded.bill_hours_high,
            credit_hours_high = excluded.credit_hours_high,
            college = excluded.college,
            college_code = excluded.college_code,
            department_code = excluded.department_code,
            term_effective = excluded.term_effective,
            term_start = excluded.term_start,
            term_end = excluded.term_end,
            prereq_check_method = excluded.prereq_check_method,
            title = excluded.title
    """)
    conn.commit()


def save_course_details(conn: sqlite3.Connection, details) -> None:
    conn.executemany(
        """UPDATE catalog_versions SET prerequisites=?, corequisites=?, restrictions=?,
               course_attributes=?, levels=?, grade_modes=?, schedule_types=?,
               prerequisites_json=?, restrictions_json=?
           WHERE subject=? AND course_number=? AND term_effective=?""",
        [(d.prerequisites, d.corequisites, d.restrictions, d.course_attributes,
          d.levels, d.grade_modes, d.schedule_types, d.prerequisites_json,
          d.restrictions_json, d.subject, d.course_number, d.term_effective)
         for d in details],
    )
    conn.executemany(
        """INSERT OR REPLACE INTO prereq_rules (
               subject, course_number, term_effective, seq, connector, open_paren,
               close_paren, test_code, test_score, req_subject, req_course_number,
               req_level, min_grade)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        [(d.subject, d.course_number, d.term_effective, r.seq, r.connector,
          int(r.open_paren), int(r.close_paren), r.test_code, r.test_score,
          r.req_subject, r.req_course_number, r.req_level, r.min_grade)
         for d in details for r in d.rules],
    )
    conn.commit()
    refresh_catalog_detail(conn)


def refresh_catalog_detail(conn: sqlite3.Connection) -> None:
    """Keep the legacy catalog_detail table in step with the newest version."""
    conn.execute(f"""
        INSERT INTO catalog_detail (subject, course_number, term_id, levels,
                                    schedule_types, course_attributes, prerequisites,
                                    corequisites, restrictions, grade_modes)
        SELECT subject, course_number, term_effective, levels, schedule_types,
               course_attributes, prerequisites, corequisites, restrictions,
               grade_modes
        FROM catalog_versions v
        WHERE v.term_effective = ({_NEWEST_VERSION})
        ON CONFLICT(subject, course_number) DO UPDATE SET
            term_id = excluded.term_id, levels = excluded.levels,
            schedule_types = excluded.schedule_types,
            course_attributes = excluded.course_attributes,
            prerequisites = excluded.prerequisites,
            corequisites = excluded.corequisites,
            restrictions = excluded.restrictions,
            grade_modes = excluded.grade_modes
    """)
    conn.commit()


def fix_first_seen(conn: sqlite3.Connection) -> None:
    """Backfill first_seen from the earliest term each entity actually appears in."""
    conn.execute("""
        UPDATE subjects SET first_seen = (
            SELECT MIN(term_id) FROM courses WHERE courses.subject = subjects.short_name
        ) WHERE EXISTS (
            SELECT 1 FROM courses WHERE courses.subject = subjects.short_name)
    """)
    conn.execute("""
        UPDATE instructors SET first_seen = (
            SELECT MIN(term_id) FROM section_instructors si
            WHERE si.name = instructors.name
        ) WHERE EXISTS (
            SELECT 1 FROM section_instructors si WHERE si.name = instructors.name)
    """)
    conn.commit()


def done_terms(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute("SELECT DISTINCT term_id FROM courses")}


def done_course_versions(conn: sqlite3.Connection) -> set[tuple[str, str, str]]:
    return {(r[0], r[1], r[2]) for r in conn.execute(
        "SELECT subject, course_number, term_effective FROM catalog_versions "
        "WHERE levels != '' OR prerequisites != '' OR restrictions != ''")}
