"""SQLite schema, bulk writes, and the compatibility views.

The tables are normalized around what Banner 9 actually serves: a course version, a
section, its meetings, its people. The table names the Banner 8 database used are
re-exposed as views over those tables, so queries written against older releases keep
working *and* keep returning fresh data — the previous design kept them as real tables,
which meant three of them silently froze at the last Banner 8 crawl.
"""

import logging
import os
import sqlite3

log = logging.getLogger("auscrawl")


# ── Legacy value formats, expressed in SQL for the compat views ──────────────

def _time_12h(col: str) -> str:
    """'1345' -> '1:45 pm', the format the published database has always used."""
    return (
        f"CASE WHEN length(COALESCE({col},'')) <> 4 THEN '' ELSE "
        f"CAST((CAST(substr({col},1,2) AS INTEGER)+11)%12+1 AS TEXT) || ':' || "
        f"substr({col},3,2) || "
        f"CASE WHEN CAST(substr({col},1,2) AS INTEGER) < 12 THEN ' am' ELSE ' pm' END "
        f"END"
    )


_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def _date_long(col: str) -> str:
    """'08/24/2026' -> 'Aug 24, 2026'."""
    months = " ".join(f"WHEN '{i + 1:02d}' THEN '{m}'" for i, m in enumerate(_MONTHS))
    return (
        f"CASE WHEN length(COALESCE({col},'')) <> 10 THEN '' ELSE "
        f"(CASE substr({col},1,2) {months} ELSE '' END) || ' ' || "
        f"CAST(CAST(substr({col},4,2) AS INTEGER) AS TEXT) || ', ' || "
        f"substr({col},7,4) END"
    )


# R is Thursday and U is Sunday, as in the published database.
_DAYS = " || ".join(
    f"CASE WHEN m.{col} THEN '{letter}' ELSE '' END"
    for col, letter in (("monday", "M"), ("tuesday", "T"), ("wednesday", "W"),
                        ("thursday", "R"), ("friday", "F"), ("saturday", "S"),
                        ("sunday", "U"))
)

_CLASSROOM = (
    "CASE WHEN COALESCE(m.building_name, m.building, '') <> '' "
    "       OR COALESCE(m.room,'') <> '' "
    "THEN trim(COALESCE(m.building_name, m.building, '') || ' ' || "
    "          COALESCE(m.room,'')) "
    "ELSE 'TBA' END"
)

# The course version in effect for a section's term: the newest one that had taken
# effect by then, falling back to the oldest on record for pre-catalog terms.
def _version_column(col: str) -> str:
    return (f"COALESCE((SELECT w.{col} FROM course_versions w "
            f"WHERE w.subject = s.subject AND w.course_number = s.course_number "
            f"ORDER BY (w.term_effective <= s.term_id) DESC, "
            f"w.term_effective DESC LIMIT 1), '')")


SCHEMA = f"""
-- ── Reference ───────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS terms (
    term_id TEXT PRIMARY KEY,
    name TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS subjects (
    code TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    first_seen TEXT
);
CREATE TABLE IF NOT EXISTS instructors (
    banner_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    email TEXT,
    first_seen TEXT
);
CREATE TABLE IF NOT EXISTS attributes (
    code TEXT PRIMARY KEY,
    description TEXT NOT NULL,
    first_seen TEXT
);

-- ── Catalog: one row per course version ─────────────────────────────────────
CREATE TABLE IF NOT EXISTS course_versions (
    subject TEXT NOT NULL,
    course_number TEXT NOT NULL,
    term_effective TEXT NOT NULL,
    title TEXT DEFAULT '',
    description TEXT DEFAULT '',
    college TEXT DEFAULT '',
    college_code TEXT DEFAULT '',
    department TEXT DEFAULT '',
    department_code TEXT DEFAULT '',
    credit_hours_low REAL, credit_hours_high REAL,
    lecture_hours_low REAL, lecture_hours_high REAL,
    lab_hours_low REAL, lab_hours_high REAL,
    other_hours_low REAL, other_hours_high REAL,
    bill_hours_low REAL, bill_hours_high REAL,
    prereq_check_method TEXT DEFAULT '',
    term_start TEXT DEFAULT '',
    term_end TEXT DEFAULT '',
    levels TEXT DEFAULT '',
    grade_modes TEXT DEFAULT '',
    schedule_types TEXT DEFAULT '',
    course_attributes TEXT DEFAULT '',
    prerequisites TEXT DEFAULT '',
    corequisites TEXT DEFAULT '',
    restrictions TEXT DEFAULT '',
    prerequisites_json TEXT DEFAULT '',
    restrictions_json TEXT DEFAULT '',
    PRIMARY KEY (subject, course_number, term_effective)
);
CREATE TABLE IF NOT EXISTS prereq_rules (
    subject TEXT NOT NULL,
    course_number TEXT NOT NULL,
    term_effective TEXT NOT NULL,
    seq INTEGER NOT NULL,
    connector TEXT DEFAULT '',
    open_paren BOOLEAN DEFAULT 0,
    close_paren BOOLEAN DEFAULT 0,
    test_code TEXT DEFAULT '',
    test_score TEXT DEFAULT '',
    req_subject TEXT DEFAULT '',
    req_course_number TEXT DEFAULT '',
    req_level TEXT DEFAULT '',
    min_grade TEXT DEFAULT '',
    PRIMARY KEY (subject, course_number, term_effective, seq)
);

-- ── Sections ────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS sections (
    crn TEXT NOT NULL,
    term_id TEXT NOT NULL,
    subject TEXT NOT NULL,
    course_number TEXT NOT NULL,
    sequence TEXT DEFAULT '',
    title TEXT DEFAULT '',
    credits REAL,
    schedule_type TEXT DEFAULT '',
    instructional_method TEXT DEFAULT '',
    campus TEXT DEFAULT '',
    part_of_term TEXT DEFAULT '',
    enrollment INTEGER,
    max_enrollment INTEGER,
    seats_available INTEGER,
    waitlist_capacity INTEGER,
    waitlist_count INTEGER,
    waitlist_available INTEGER,
    cross_list TEXT DEFAULT '',
    cross_list_capacity INTEGER,
    cross_list_count INTEGER,
    cross_list_available INTEGER,
    open_section BOOLEAN DEFAULT 0,
    banner_section_id INTEGER,
    PRIMARY KEY (crn, term_id)
);
CREATE TABLE IF NOT EXISTS meetings (
    crn TEXT NOT NULL,
    term_id TEXT NOT NULL,
    meeting_index INTEGER NOT NULL,
    meeting_type TEXT DEFAULT '',
    meeting_type_desc TEXT DEFAULT '',
    begin_time TEXT DEFAULT '',
    end_time TEXT DEFAULT '',
    monday BOOLEAN DEFAULT 0, tuesday BOOLEAN DEFAULT 0,
    wednesday BOOLEAN DEFAULT 0, thursday BOOLEAN DEFAULT 0,
    friday BOOLEAN DEFAULT 0, saturday BOOLEAN DEFAULT 0, sunday BOOLEAN DEFAULT 0,
    building TEXT DEFAULT '',
    building_name TEXT DEFAULT '',
    room TEXT DEFAULT '',
    campus TEXT DEFAULT '',
    campus_desc TEXT DEFAULT '',
    start_date TEXT DEFAULT '',
    end_date TEXT DEFAULT '',
    hours_week REAL,
    credit_hour_session REAL,
    schedule_type TEXT DEFAULT '',
    PRIMARY KEY (crn, term_id, meeting_index)
);
CREATE TABLE IF NOT EXISTS section_instructors (
    crn TEXT NOT NULL,
    term_id TEXT NOT NULL,
    banner_id TEXT NOT NULL,
    name TEXT DEFAULT '',
    email TEXT DEFAULT '',
    is_primary BOOLEAN DEFAULT 0,
    PRIMARY KEY (crn, term_id, banner_id)
);
CREATE TABLE IF NOT EXISTS section_attributes (
    crn TEXT NOT NULL,
    term_id TEXT NOT NULL,
    code TEXT NOT NULL,
    description TEXT DEFAULT '',
    PRIMARY KEY (crn, term_id, code)
);

-- ── Banner 8 leftovers ──────────────────────────────────────────────────────
-- Registration windows and section-specific title suffixes existed only in the old
-- portal and no Banner 9 endpoint serves them. They are imported once from an old
-- snapshot and kept here rather than diluting the tables above.
CREATE TABLE IF NOT EXISTS legacy_section_extras (
    crn TEXT NOT NULL,
    term_id TEXT NOT NULL,
    registration_dates TEXT DEFAULT '',
    title TEXT DEFAULT '',
    PRIMARY KEY (crn, term_id)
);

CREATE INDEX IF NOT EXISTS idx_sections_term ON sections(term_id);
CREATE INDEX IF NOT EXISTS idx_sections_subject ON sections(subject, course_number);
CREATE INDEX IF NOT EXISTS idx_meetings_section ON meetings(crn, term_id);
CREATE INDEX IF NOT EXISTS idx_section_instructors_person
    ON section_instructors(banner_id);
CREATE INDEX IF NOT EXISTS idx_section_instructors_name ON section_instructors(name);
CREATE INDEX IF NOT EXISTS idx_course_versions_course
    ON course_versions(subject, course_number);
CREATE INDEX IF NOT EXISTS idx_prereq_rules_target
    ON prereq_rules(req_subject, req_course_number);

-- ── Compatibility views ─────────────────────────────────────────────────────
-- These carry the table names and column shapes of the Banner 8 database. They are
-- views, not tables, so they cannot drift out of date.

CREATE VIEW IF NOT EXISTS semesters AS
    SELECT term_id, name AS term_name FROM terms;

CREATE VIEW IF NOT EXISTS levels AS
    SELECT DISTINCT trim(value) AS level
    FROM course_versions, json_each('["' || replace(levels, ', ', '","') || '"]')
    WHERE levels <> '';

CREATE VIEW IF NOT EXISTS courses AS
SELECT
    s.crn,
    s.term_id,
    s.subject,
    s.course_number,
    COALESCE(NULLIF(lx.title, ''), s.title) AS title,
    s.sequence AS section,
    s.credits,
    s.schedule_type,
    s.instructional_method,
    s.campus,
    {_version_column('levels')} AS levels,
    COALESCE((SELECT group_concat(a.description, ', ') FROM section_attributes a
              WHERE a.crn = s.crn AND a.term_id = s.term_id), '') AS attributes,
    COALESCE(lx.registration_dates, '') AS registration_dates,
    COALESCE(m.meeting_type_desc, '') AS class_type,
    {_time_12h('m.begin_time')} AS start_time,
    {_time_12h('m.end_time')} AS end_time,
    COALESCE({_DAYS}, '') AS days,
    CASE WHEN s.seats_available IS NULL THEN NULL
         WHEN s.seats_available > 0 THEN 1 ELSE 0 END AS seats_available,
    COALESCE({_CLASSROOM}, 'TBA') AS classroom,
    CASE WHEN m.start_date IS NULL OR m.end_date IS NULL
              OR m.start_date = '' OR m.end_date = '' THEN ''
         ELSE {_date_long('m.start_date')} || ' - ' || {_date_long('m.end_date')}
    END AS date_range,
    -- 'TBA' is the sentinel the published database has always used for an
    -- unassigned instructor, matching the unassigned-room convention.
    CASE WHEN COALESCE(p.name,'') = '' THEN 'TBA' ELSE p.name END AS instructor_name,
    COALESCE(p.email, '') AS instructor_email,
    -- Exact equality, matching the Banner 8 parser: a 'Lecture/Lab' section was
    -- never flagged, and a compatibility view must not quietly widen that.
    CASE WHEN s.schedule_type = 'Lab' OR COALESCE(m.meeting_type_desc,'') = 'Lab'
         THEN 1 ELSE 0 END AS is_lab,
    s.part_of_term,
    m.building, m.building_name, m.room,
    s.enrollment, s.max_enrollment,
    s.seats_available AS seats_available_count,
    s.waitlist_capacity, s.waitlist_count, s.waitlist_available,
    s.cross_list, s.cross_list_capacity, s.cross_list_count, s.cross_list_available,
    s.open_section, s.banner_section_id AS section_id
FROM sections s
LEFT JOIN meetings m ON m.crn = s.crn AND m.term_id = s.term_id
LEFT JOIN legacy_section_extras lx ON lx.crn = s.crn AND lx.term_id = s.term_id
LEFT JOIN section_instructors p
       ON p.crn = s.crn AND p.term_id = s.term_id AND p.is_primary = 1;

CREATE VIEW IF NOT EXISTS catalog AS
SELECT subject, course_number, description,
       credit_hours_low AS credit_hours,
       lecture_hours_low AS lecture_hours,
       lab_hours_low AS lab_hours,
       department, title, college, college_code, department_code,
       term_effective, term_start, term_end, prereq_check_method,
       credit_hours_high, lecture_hours_high, lab_hours_high,
       other_hours_low, other_hours_high, bill_hours_low, bill_hours_high
FROM course_versions v
WHERE v.term_effective = (SELECT MAX(w.term_effective) FROM course_versions w
                          WHERE w.subject = v.subject
                            AND w.course_number = v.course_number);

CREATE VIEW IF NOT EXISTS catalog_detail AS
SELECT subject, course_number, term_effective AS term_id, levels, schedule_types,
       course_attributes, prerequisites, corequisites, restrictions, grade_modes
FROM course_versions v
WHERE v.term_effective = (SELECT MAX(w.term_effective) FROM course_versions w
                          WHERE w.subject = v.subject
                            AND w.course_number = v.course_number);

CREATE VIEW IF NOT EXISTS section_details AS
SELECT s.crn, s.term_id,
       COALESCE(v.prerequisites, '') AS prerequisites,
       COALESCE(v.corequisites, '') AS corequisites,
       COALESCE(v.restrictions, '') AS restrictions,
       COALESCE(s.waitlist_capacity, 0) AS waitlist_capacity,
       COALESCE(s.waitlist_count, 0) AS waitlist_actual,
       COALESCE(s.waitlist_available, 0) AS waitlist_remaining,
       '' AS fees,
       COALESCE(v.prerequisites_json, '') AS prerequisites_json,
       '' AS corequisites_json,
       COALESCE(v.restrictions_json, '') AS restrictions_json
FROM sections s
LEFT JOIN course_versions v
       ON v.subject = s.subject AND v.course_number = s.course_number
      AND v.term_effective = (SELECT w.term_effective FROM course_versions w
                              WHERE w.subject = s.subject
                                AND w.course_number = s.course_number
                              ORDER BY (w.term_effective <= s.term_id) DESC,
                                       w.term_effective DESC LIMIT 1);

CREATE VIEW IF NOT EXISTS course_dependencies AS
SELECT s.crn, s.term_id, 'prerequisite' AS dep_type,
       r.req_subject AS subject, r.req_course_number AS course_number,
       r.min_grade AS minimum_grade
FROM sections s
JOIN prereq_rules r
  ON r.subject = s.subject AND r.course_number = s.course_number
 AND r.term_effective = (SELECT w.term_effective FROM course_versions w
                         WHERE w.subject = s.subject
                           AND w.course_number = s.course_number
                         ORDER BY (w.term_effective <= s.term_id) DESC,
                                  w.term_effective DESC LIMIT 1)
WHERE r.req_subject <> '';
"""


def init_db(db_path: str, force: bool = False) -> sqlite3.Connection:
    if force and os.path.exists(db_path):
        os.remove(db_path)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


# ── Saves ────────────────────────────────────────────────────────────────────

def save_semesters(conn: sqlite3.Connection, semesters) -> None:
    conn.executemany(
        "INSERT INTO terms (term_id, name) VALUES (?,?) "
        "ON CONFLICT(term_id) DO UPDATE SET name = excluded.name",
        [(s.term_id, s.term_name) for s in semesters],
    )
    conn.commit()


def save_subjects(conn: sqlite3.Connection, refs, term_id: str) -> None:
    # first_seen is never updated; the term-ordered crawl makes the first write the
    # earliest occurrence.
    conn.executemany(
        "INSERT INTO subjects (code, name, first_seen) VALUES (?,?,?) "
        "ON CONFLICT(code) DO UPDATE SET name = excluded.name",
        [(r.code, r.description, term_id) for r in refs],
    )
    conn.commit()


def save_attributes(conn: sqlite3.Connection, refs, term_id: str) -> None:
    conn.executemany(
        "INSERT INTO attributes (code, description, first_seen) VALUES (?,?,?) "
        "ON CONFLICT(code) DO UPDATE SET description = excluded.description",
        [(r.code, r.description, term_id) for r in refs],
    )
    conn.commit()


_SECTION_COLS = (
    "crn, term_id, subject, course_number, sequence, title, credits, schedule_type, "
    "instructional_method, campus, part_of_term, enrollment, max_enrollment, "
    "seats_available, waitlist_capacity, waitlist_count, waitlist_available, "
    "cross_list, cross_list_capacity, cross_list_count, cross_list_available, "
    "open_section, banner_section_id"
)
_SECTION_UPSERT = (
    f"INSERT INTO sections ({_SECTION_COLS}) "
    f"VALUES ({', '.join('?' * len(_SECTION_COLS.split(', ')))}) "
    "ON CONFLICT(crn, term_id) DO UPDATE SET "
    + ", ".join(f"{c} = excluded.{c}" for c in _SECTION_COLS.split(", ")
                if c not in ("crn", "term_id"))
)

_MEETING_COLS = (
    "crn, term_id, meeting_index, meeting_type, meeting_type_desc, begin_time, "
    "end_time, monday, tuesday, wednesday, thursday, friday, saturday, sunday, "
    "building, building_name, room, campus, campus_desc, start_date, end_date, "
    "hours_week, credit_hour_session, schedule_type"
)
_MEETING_UPSERT = (
    f"INSERT INTO meetings ({_MEETING_COLS}) "
    f"VALUES ({', '.join('?' * len(_MEETING_COLS.split(', ')))}) "
    "ON CONFLICT(crn, term_id, meeting_index) DO UPDATE SET "
    + ", ".join(f"{c} = excluded.{c}" for c in _MEETING_COLS.split(", ")
                if c not in ("crn", "term_id", "meeting_index"))
)


def save_sections(conn: sqlite3.Connection, sections) -> None:
    """Insert or refresh sections, meetings, instructors and section attributes.

    Sorted by term so the first write of an instructor or subject is its earliest
    occurrence, which is what makes first_seen correct without a second pass.
    """
    ordered = sorted(sections, key=lambda s: s.term_id)

    conn.executemany(_SECTION_UPSERT, [
        (s.crn, s.term_id, s.subject, s.course_number, s.section, s.title, s.credits,
         s.schedule_type, s.instructional_method, s.campus, s.part_of_term,
         s.enrollment, s.max_enrollment, s.seats_available_count,
         s.waitlist_capacity, s.waitlist_count, s.waitlist_available,
         s.cross_list, s.cross_list_capacity, s.cross_list_count,
         s.cross_list_available, int(s.open_section), s.section_id)
        for s in ordered])

    conn.executemany(_MEETING_UPSERT, [
        (m.crn, m.term_id, m.meeting_index, m.meeting_type, m.meeting_type_desc,
         m.begin_time, m.end_time, int(m.monday), int(m.tuesday), int(m.wednesday),
         int(m.thursday), int(m.friday), int(m.saturday), int(m.sunday),
         m.building, m.building_name, m.room, m.campus, m.campus_desc,
         m.start_date, m.end_date, m.hours_week, m.credit_hour_session,
         m.schedule_type)
        for s in ordered for m in s.meetings])

    # A section whose meeting blocks shrank must not keep the vanished ones.
    conn.executemany(
        "DELETE FROM meetings WHERE crn=? AND term_id=? AND meeting_index >= ?",
        [(s.crn, s.term_id, len(s.meetings)) for s in ordered])

    conn.executemany(
        "INSERT INTO instructors (banner_id, name, email, first_seen) "
        "VALUES (?,?,?,?) "
        "ON CONFLICT(banner_id) DO UPDATE SET name = excluded.name, "
        "email = excluded.email",
        [(i.banner_id, i.name, i.email, s.term_id)
         for s in ordered for i in s.instructors if i.banner_id])

    conn.executemany(
        "INSERT INTO section_instructors (crn, term_id, banner_id, name, email, "
        "is_primary) VALUES (?,?,?,?,?,?) "
        "ON CONFLICT(crn, term_id, banner_id) DO UPDATE SET name = excluded.name, "
        "email = excluded.email, is_primary = excluded.is_primary",
        [(s.crn, s.term_id, i.banner_id, i.name, i.email, int(i.is_primary))
         for s in ordered for i in s.instructors if i.banner_id])

    conn.executemany(
        "INSERT INTO section_attributes (crn, term_id, code, description) "
        "VALUES (?,?,?,?) "
        "ON CONFLICT(crn, term_id, code) DO UPDATE SET "
        "description = excluded.description",
        [(s.crn, s.term_id, a.code, a.description)
         for s in ordered for a in s.attributes if a.code])

    conn.executemany(
        "INSERT INTO attributes (code, description, first_seen) VALUES (?,?,?) "
        "ON CONFLICT(code) DO UPDATE SET description = excluded.description",
        [(a.code, a.description, s.term_id)
         for s in ordered for a in s.attributes if a.code])

    conn.commit()


_CATALOG_COLS = (
    "subject, course_number, term_effective, title, description, college, "
    "college_code, department, department_code, credit_hours_low, credit_hours_high, "
    "lecture_hours_low, lecture_hours_high, lab_hours_low, lab_hours_high, "
    "other_hours_low, other_hours_high, bill_hours_low, bill_hours_high, "
    "prereq_check_method, term_start, term_end"
)
# Only the columns the catalog search owns; the detail columns belong to
# save_course_details and must not be reset when another term revisits this version.
_CATALOG_UPSERT = (
    f"INSERT INTO course_versions ({_CATALOG_COLS}) "
    f"VALUES ({', '.join('?' * len(_CATALOG_COLS.split(', ')))}) "
    "ON CONFLICT(subject, course_number, term_effective) DO UPDATE SET "
    + ", ".join(f"{c} = excluded.{c}" for c in _CATALOG_COLS.split(", ")
                if c not in ("subject", "course_number", "term_effective"))
)


def save_catalog(conn: sqlite3.Connection, courses) -> None:
    conn.executemany(_CATALOG_UPSERT, [
        (c.subject, c.course_number, c.term_effective, c.title, c.description,
         c.college, c.college_code, c.department, c.department_code,
         c.credit_hours_low, c.credit_hours_high, c.lecture_hours_low,
         c.lecture_hours_high, c.lab_hours_low, c.lab_hours_high,
         c.other_hours_low, c.other_hours_high, c.bill_hours_low, c.bill_hours_high,
         c.prereq_check_method, c.term_start, c.term_end)
        for c in courses if c.term_effective])
    conn.commit()


def save_course_details(conn: sqlite3.Connection, details) -> None:
    conn.executemany(
        """INSERT INTO course_versions (subject, course_number, term_effective,
               prerequisites, corequisites, restrictions, course_attributes,
               levels, grade_modes, schedule_types, prerequisites_json,
               restrictions_json)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(subject, course_number, term_effective) DO UPDATE SET
               prerequisites = excluded.prerequisites,
               corequisites = excluded.corequisites,
               restrictions = excluded.restrictions,
               course_attributes = excluded.course_attributes,
               levels = excluded.levels,
               grade_modes = excluded.grade_modes,
               schedule_types = excluded.schedule_types,
               prerequisites_json = excluded.prerequisites_json,
               restrictions_json = excluded.restrictions_json""",
        [(d.subject, d.course_number, d.term_effective, d.prerequisites,
          d.corequisites, d.restrictions, d.course_attributes, d.levels,
          d.grade_modes, d.schedule_types, d.prerequisites_json, d.restrictions_json)
         for d in details])

    conn.executemany(
        "DELETE FROM prereq_rules WHERE subject=? AND course_number=? "
        "AND term_effective=?",
        [(d.subject, d.course_number, d.term_effective) for d in details])
    conn.executemany(
        """INSERT INTO prereq_rules (subject, course_number, term_effective, seq,
               connector, open_paren, close_paren, test_code, test_score,
               req_subject, req_course_number, req_level, min_grade)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        [(d.subject, d.course_number, d.term_effective, r.seq, r.connector,
          int(r.open_paren), int(r.close_paren), r.test_code, r.test_score,
          r.req_subject, r.req_course_number, r.req_level, r.min_grade)
         for d in details for r in d.rules])
    conn.commit()


def import_legacy_extras(conn: sqlite3.Connection, old_db_path: str) -> int:
    """Copy the Banner 8 columns no endpoint can regenerate out of an old snapshot.

    Registration windows and section-specific title suffixes were only ever in the
    old portal. Returns the number of sections imported.
    """
    src = sqlite3.connect(f"file:{old_db_path}?mode=ro", uri=True)
    try:
        rows = src.execute("""
            SELECT crn, term_id,
                   MAX(COALESCE(registration_dates, '')),
                   MAX(COALESCE(title, ''))
            FROM courses GROUP BY crn, term_id
        """).fetchall()
    finally:
        src.close()

    rows = [r for r in rows if r[2] or r[3]]
    conn.executemany(
        "INSERT INTO legacy_section_extras (crn, term_id, registration_dates, title) "
        "VALUES (?,?,?,?) "
        "ON CONFLICT(crn, term_id) DO UPDATE SET "
        "registration_dates = excluded.registration_dates, title = excluded.title",
        rows)
    conn.commit()
    log.info("imported legacy extras for %d sections", len(rows))
    return len(rows)


def fix_first_seen(conn: sqlite3.Connection) -> None:
    """Backfill first_seen from the earliest term each entity actually appears in."""
    conn.execute("""
        UPDATE subjects SET first_seen = (
            SELECT MIN(term_id) FROM sections WHERE sections.subject = subjects.code
        ) WHERE EXISTS (
            SELECT 1 FROM sections WHERE sections.subject = subjects.code)
    """)
    conn.execute("""
        UPDATE instructors SET first_seen = (
            SELECT MIN(term_id) FROM section_instructors si
            WHERE si.banner_id = instructors.banner_id
        ) WHERE EXISTS (
            SELECT 1 FROM section_instructors si
            WHERE si.banner_id = instructors.banner_id)
    """)
    conn.commit()


def done_terms(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute("SELECT DISTINCT term_id FROM sections")}


def done_course_versions(conn: sqlite3.Connection) -> set[tuple[str, str, str]]:
    return {(r[0], r[1], r[2]) for r in conn.execute(
        "SELECT subject, course_number, term_effective FROM course_versions "
        "WHERE levels != '' OR prerequisites != '' OR restrictions != ''")}
