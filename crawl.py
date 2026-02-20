#!/usr/bin/env python3
"""AUSCrawl - Fast AUS Banner course data scraper.

Crawls the AUS Banner system for course data across all semesters since 2005
and stores it in an SQLite database for analysis.
"""

import argparse
import asyncio
import concurrent.futures
import json
import logging
import re
import sqlite3
import time
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlencode

import httpx
from lxml import etree
from lxml import html as lxml_html
from rich.console import Console
from rich.logging import RichHandler
from rich.progress import (
    Progress,
    SpinnerColumn,
    BarColumn,
    TextColumn,
    TimeElapsedColumn,
    MofNCompleteColumn,
    TimeRemainingColumn,
)

# ── Constants ────────────────────────────────────────────────────────────────

BASE_URL = "https://banner.aus.edu/axp3b21h/owa"
ENDPOINTS = {
    "semesters": f"{BASE_URL}/bwckschd.p_disp_dyn_sched",
    "subjects": f"{BASE_URL}/bwckgens.p_proc_term_date",
    "courses": f"{BASE_URL}/bwckschd.p_get_crse_unsec",
    "catalog": f"{BASE_URL}/bwckctlg.p_display_courses",
    "detail": f"{BASE_URL}/bwckschd.p_disp_detail_sched",
}
DEFAULT_WORKERS = 50
DEFAULT_DELAY = 0.0
MAX_RETRIES = 5
RETRY_BASE = 2.0            # backoff base for all retries
DETAIL_BATCH_SIZE = 5000   # save details every N for resilience
CATALOG_SAMPLE_COUNT = 6   # number of evenly-spaced terms to sample for catalog

console = Console()
log = logging.getLogger("auscrawl")

# ── Pre-compiled regexes ─────────────────────────────────────────────────────

RE_CREDITS = re.compile(r"([\d.]+)\s+Credits")
RE_CREDIT_HOURS = re.compile(r"([\d.]+)\s+Credit hours")
RE_LECTURE_HOURS = re.compile(r"([\d.]+)\s+Lecture hours")
RE_LAB_HOURS = re.compile(r"([\d.]+)\s+Lab hours")
RE_INSTRUCTOR_P = re.compile(r"(.+?)\s*\(P\)")
RE_WHITESPACE = re.compile(r"\s+")
RE_CF_EMAIL = re.compile(r"/cdn-cgi/l/email-protection#([a-fA-F0-9]+)")
RE_OPTION = re.compile(r'OPTION VALUE="([^"]+)"[^>]*>([^<]+)')
RE_MIN_GRADE = re.compile(r"Minimum Grade of\s+([A-Z][+-]?)")
RE_DETAIL_SECTION = re.compile(
    r'<span[^>]*class="fieldlabeltext"[^>]*>\s*'
    r"(Prerequisites|Corequisites|Restrictions)"
    r"[^<]*</span>(.*?)(?=<span[^>]*class="
    r'"fieldlabeltext"|<table|</td>)',
    re.DOTALL | re.IGNORECASE,
)

# ── Data Classes ─────────────────────────────────────────────────────────────


@dataclass(slots=True)
class Semester:
    term_id: str
    term_name: str


@dataclass(slots=True)
class Subject:
    short_name: str
    long_name: str


@dataclass(slots=True)
class Course:
    crn: str
    term_id: str
    subject: str
    course_number: str
    title: str
    section: str
    credits: Optional[float] = None
    schedule_type: str = ""
    instructional_method: str = ""
    campus: str = ""
    levels: str = ""
    attributes: str = ""
    registration_dates: str = ""
    class_type: str = ""
    start_time: str = ""
    end_time: str = ""
    days: str = ""
    seats_available: Optional[bool] = None
    classroom: str = ""
    date_range: str = ""
    instructor_name: str = ""
    instructor_email: str = ""
    is_lab: bool = False


@dataclass(slots=True)
class CatalogEntry:
    subject: str
    course_number: str
    description: str = ""
    credit_hours: Optional[float] = None
    lecture_hours: Optional[float] = None
    lab_hours: Optional[float] = None
    department: str = ""


@dataclass(slots=True)
class SectionDetail:
    crn: str
    term_id: str
    prerequisites: str = ""
    corequisites: str = ""
    restrictions: str = ""
    waitlist_capacity: int = 0
    waitlist_actual: int = 0
    waitlist_remaining: int = 0
    fees: str = ""  # JSON array of {description, amount}


@dataclass(slots=True)
class CourseDependency:
    crn: str
    term_id: str
    dep_type: str  # 'prerequisite' or 'corequisite'
    subject: str
    course_number: str
    minimum_grade: str = ""


# ── Utilities ────────────────────────────────────────────────────────────────


def decode_cf_email(encoded: str) -> str:
    """Decode Cloudflare email-protection XOR obfuscation."""
    try:
        key = int(encoded[:2], 16)
        return "".join(
            chr(int(encoded[i : i + 2], 16) ^ key)
            for i in range(2, len(encoded), 2)
        )
    except (ValueError, IndexError):
        return ""


def text_of(el) -> str:
    """Fast text_content() for an lxml element."""
    return el.text_content().strip()


# ── Database ─────────────────────────────────────────────────────────────────

SCHEMA = """
CREATE TABLE IF NOT EXISTS semesters (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    term_id TEXT UNIQUE NOT NULL,
    term_name TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS subjects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    short_name TEXT NOT NULL,
    long_name TEXT NOT NULL,
    first_seen TEXT,
    UNIQUE(short_name)
);

CREATE TABLE IF NOT EXISTS instructors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    email TEXT,
    first_seen TEXT,
    UNIQUE(name, email)
);

CREATE TABLE IF NOT EXISTS levels (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    level TEXT UNIQUE NOT NULL,
    first_seen TEXT
);

CREATE TABLE IF NOT EXISTS attributes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    attribute TEXT UNIQUE NOT NULL,
    first_seen TEXT
);

CREATE TABLE IF NOT EXISTS courses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    crn TEXT NOT NULL,
    term_id TEXT NOT NULL,
    subject TEXT NOT NULL,
    course_number TEXT NOT NULL,
    title TEXT NOT NULL,
    section TEXT,
    credits REAL,
    schedule_type TEXT,
    instructional_method TEXT,
    campus TEXT,
    levels TEXT,
    attributes TEXT,
    registration_dates TEXT,
    class_type TEXT,
    start_time TEXT,
    end_time TEXT,
    days TEXT,
    seats_available BOOLEAN,
    classroom TEXT,
    date_range TEXT,
    instructor_name TEXT,
    instructor_email TEXT,
    is_lab BOOLEAN DEFAULT 0,
    UNIQUE(crn, term_id, class_type, days, start_time)
);

CREATE INDEX IF NOT EXISTS idx_courses_term ON courses(term_id);
CREATE INDEX IF NOT EXISTS idx_courses_subject ON courses(subject);
CREATE INDEX IF NOT EXISTS idx_courses_crn ON courses(crn);
CREATE INDEX IF NOT EXISTS idx_courses_instructor ON courses(instructor_name);

CREATE TABLE IF NOT EXISTS catalog (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    subject TEXT NOT NULL,
    course_number TEXT NOT NULL,
    description TEXT DEFAULT '',
    credit_hours REAL,
    lecture_hours REAL,
    lab_hours REAL,
    department TEXT DEFAULT '',
    UNIQUE(subject, course_number)
);

CREATE TABLE IF NOT EXISTS section_details (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    crn TEXT NOT NULL,
    term_id TEXT NOT NULL,
    prerequisites TEXT DEFAULT '',
    corequisites TEXT DEFAULT '',
    restrictions TEXT DEFAULT '',
    waitlist_capacity INTEGER DEFAULT 0,
    waitlist_actual INTEGER DEFAULT 0,
    waitlist_remaining INTEGER DEFAULT 0,
    fees TEXT DEFAULT '',
    UNIQUE(crn, term_id)
);

CREATE TABLE IF NOT EXISTS course_dependencies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    crn TEXT NOT NULL,
    term_id TEXT NOT NULL,
    dep_type TEXT NOT NULL,
    subject TEXT NOT NULL,
    course_number TEXT NOT NULL,
    minimum_grade TEXT DEFAULT '',
    UNIQUE(crn, term_id, dep_type, subject, course_number)
);

CREATE INDEX IF NOT EXISTS idx_catalog_subject ON catalog(subject);
CREATE INDEX IF NOT EXISTS idx_section_details_crn ON section_details(crn, term_id);
CREATE INDEX IF NOT EXISTS idx_deps_crn ON course_dependencies(crn, term_id);
CREATE INDEX IF NOT EXISTS idx_deps_target ON course_dependencies(subject, course_number);
"""


def init_db(db_path: str, force: bool = False) -> sqlite3.Connection:
    """Initialize SQLite database with schema."""
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=OFF")
    conn.execute("PRAGMA cache_size=-64000")
    conn.execute("PRAGMA temp_store=MEMORY")

    if force:
        for table in (
            "course_dependencies", "section_details", "catalog",
            "courses", "instructors", "subjects", "levels", "attributes", "semesters",
        ):
            conn.execute(f"DROP TABLE IF EXISTS {table}")
        conn.commit()

    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def bulk_save(
    conn: sqlite3.Connection,
    semesters: list[Semester],
    subjects: list[Subject],
    all_courses: list[tuple[Semester, list[Course]]],
):
    """Bulk-write all crawled data to DB, sorted chronologically for correct first_seen."""
    cur = conn.cursor()

    # Semesters
    cur.executemany(
        "INSERT OR IGNORE INTO semesters (term_id, term_name) VALUES (?, ?)",
        [(s.term_id, s.term_name) for s in semesters],
    )

    # Subjects (first_seen will be fixed in post-processing)
    cur.executemany(
        "INSERT OR IGNORE INTO subjects (short_name, long_name, first_seen) VALUES (?, ?, ?)",
        [(s.short_name, s.long_name, "") for s in subjects],
    )

    # Sort by term_id for chronological insert order
    all_courses.sort(key=lambda t: t[0].term_id)

    instructors_seen: set[tuple[str, str]] = set()
    levels_seen: set[str] = set()
    attrs_seen: set[str] = set()

    course_rows = []
    instructor_rows = []
    level_rows = []
    attr_rows = []

    for semester, courses in all_courses:
        for c in courses:
            course_rows.append((
                c.crn, c.term_id, c.subject, c.course_number, c.title, c.section,
                c.credits, c.schedule_type, c.instructional_method, c.campus,
                c.levels, c.attributes, c.registration_dates, c.class_type,
                c.start_time, c.end_time, c.days, c.seats_available,
                c.classroom, c.date_range, c.instructor_name, c.instructor_email,
                c.is_lab,
            ))

            if c.instructor_name and c.instructor_name != "TBA":
                key = (c.instructor_name, c.instructor_email or "")
                if key not in instructors_seen:
                    instructors_seen.add(key)
                    instructor_rows.append((c.instructor_name, c.instructor_email or None, semester.term_id))

            if c.levels:
                for level in c.levels.split(", "):
                    level = level.strip()
                    if level and level not in levels_seen:
                        levels_seen.add(level)
                        level_rows.append((level, semester.term_id))

            if c.attributes:
                for attr in c.attributes.split(", "):
                    attr = attr.strip()
                    if attr and attr not in attrs_seen:
                        attrs_seen.add(attr)
                        attr_rows.append((attr, semester.term_id))

    cur.executemany(
        "INSERT OR IGNORE INTO courses "
        "(crn,term_id,subject,course_number,title,section,"
        "credits,schedule_type,instructional_method,campus,"
        "levels,attributes,registration_dates,class_type,"
        "start_time,end_time,days,seats_available,"
        "classroom,date_range,instructor_name,instructor_email,is_lab) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        course_rows,
    )
    cur.executemany(
        "INSERT OR IGNORE INTO instructors (name, email, first_seen) VALUES (?, ?, ?)",
        instructor_rows,
    )
    cur.executemany(
        "INSERT OR IGNORE INTO levels (level, first_seen) VALUES (?, ?)",
        level_rows,
    )
    cur.executemany(
        "INSERT OR IGNORE INTO attributes (attribute, first_seen) VALUES (?, ?)",
        attr_rows,
    )

    conn.commit()


def fix_first_seen(conn: sqlite3.Connection):
    """Fix first_seen for subjects (the only table that needs post-processing).

    Instructors, levels, and attributes already get correct first_seen during
    bulk_save because data is sorted chronologically and INSERT OR IGNORE
    keeps the earliest row.
    """
    conn.execute("""
        UPDATE subjects SET first_seen = (
            SELECT MIN(c.term_id) FROM courses c WHERE c.subject = subjects.short_name
        ) WHERE EXISTS (SELECT 1 FROM courses c WHERE c.subject = subjects.short_name)
    """)
    conn.commit()


def save_catalog(conn: sqlite3.Connection, entries: list[CatalogEntry]):
    """Bulk-write catalog entries. Keeps entry with longest description per course."""
    # Deduplicate: keep entry with longest description
    best: dict[tuple[str, str], CatalogEntry] = {}
    for e in entries:
        key = (e.subject, e.course_number)
        if key not in best or len(e.description) > len(best[key].description):
            best[key] = e

    conn.executemany(
        "INSERT OR REPLACE INTO catalog "
        "(subject, course_number, description, credit_hours, lecture_hours, lab_hours, department) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            (e.subject, e.course_number, e.description, e.credit_hours,
             e.lecture_hours, e.lab_hours, e.department)
            for e in best.values()
        ],
    )
    conn.commit()


def save_details(
    conn: sqlite3.Connection,
    details: list[SectionDetail],
    deps: list[CourseDependency],
):
    """Bulk-write section details and course dependencies."""
    if details:
        conn.executemany(
            "INSERT OR IGNORE INTO section_details "
            "(crn, term_id, prerequisites, corequisites, restrictions, "
            "waitlist_capacity, waitlist_actual, waitlist_remaining, fees) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (d.crn, d.term_id, d.prerequisites, d.corequisites, d.restrictions,
                 d.waitlist_capacity, d.waitlist_actual, d.waitlist_remaining, d.fees)
                for d in details
            ],
        )
    if deps:
        conn.executemany(
            "INSERT OR IGNORE INTO course_dependencies "
            "(crn, term_id, dep_type, subject, course_number, minimum_grade) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [
                (d.crn, d.term_id, d.dep_type, d.subject, d.course_number, d.minimum_grade)
                for d in deps
            ],
        )
    conn.commit()


# ── HTTP Layer ───────────────────────────────────────────────────────────────

FORM_CONTENT_TYPE = {"content-type": "application/x-www-form-urlencoded"}


async def request_with_retry(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    form: list[tuple[str, str]] | dict[str, str] | None = None,
    params: dict[str, str] | None = None,
) -> httpx.Response:
    """HTTP request with retry and WAF detection."""
    kwargs: dict = {}
    if form is not None:
        kwargs["content"] = urlencode(form)
        kwargs["headers"] = FORM_CONTENT_TYPE
    if params is not None:
        kwargs["params"] = params

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = await client.request(method, url, **kwargs)
            resp.raise_for_status()

            if "support ticket" in resp.text.lower():
                wait = RETRY_BASE * (2 ** attempt)
                log.warning(f"WAF block (attempt {attempt}), retrying in {wait:.0f}s")
                await asyncio.sleep(wait)
                continue

            return resp
        except httpx.HTTPStatusError as e:
            code = e.response.status_code
            wait = RETRY_BASE * (2 ** attempt)
            if code in (403, 429, 500, 502, 503) or code >= 400:
                log.warning(f"HTTP {code} (attempt {attempt}), retrying in {wait:.1f}s")
                await asyncio.sleep(wait)
                continue
            raise
        except httpx.RequestError as e:
            if attempt == MAX_RETRIES:
                raise
            wait = RETRY_BASE * attempt
            log.warning(f"Network error (attempt {attempt}): {e}, retrying in {wait:.0f}s")
            await asyncio.sleep(wait)

    raise RuntimeError(f"Failed after {MAX_RETRIES} retries: {method} {url}")


# ── Fetchers ─────────────────────────────────────────────────────────────────


async def fetch_semesters(client: httpx.AsyncClient) -> list[Semester]:
    """Fetch all available semesters from Banner."""
    resp = await request_with_retry(client, "GET", ENDPOINTS["semesters"])
    semesters = []

    for m in RE_OPTION.finditer(resp.text):
        value, text = m.group(1).strip(), m.group(2).strip()
        if value and text != "None":
            semesters.append(Semester(
                term_id=value,
                term_name=text.replace(" (View only)", ""),
            ))

    semesters.sort(key=lambda s: s.term_id)
    return semesters


async def fetch_subjects(client: httpx.AsyncClient, term_id: str) -> list[Subject]:
    """Fetch subjects for a semester — done ONCE and reused for all terms.

    Parses only the sel_subj <select> via lxml to avoid matching other dropdowns.
    """
    resp = await request_with_retry(
        client, "POST", ENDPOINTS["subjects"],
        form={"p_calling_proc": "bwckschd.p_disp_dyn_sched", "p_term": term_id},
    )
    tree = lxml_html.fromstring(resp.content)
    subjects = []

    for select in tree.xpath('//select[@name="sel_subj"]'):
        for option in select.findall("option"):
            value = (option.get("value") or "").strip()
            text = (option.text or "").strip()
            if value:
                subjects.append(Subject(short_name=value, long_name=text))

    return subjects


def build_course_params(term_id: str, subject_codes: list[str]) -> list[tuple[str, str]]:
    """Build form data for a course search POST."""
    params: list[tuple[str, str]] = [
        ("term_in", term_id),
        ("sel_subj", "dummy"), ("sel_day", "dummy"), ("sel_schd", "dummy"),
        ("sel_insm", "dummy"), ("sel_camp", "dummy"), ("sel_levl", "dummy"),
        ("sel_sess", "dummy"), ("sel_instr", "dummy"), ("sel_ptrm", "dummy"),
        ("sel_attr", "dummy"),
    ]
    for code in subject_codes:
        params.append(("sel_subj", code))
    params.extend([
        ("sel_crse", ""), ("sel_title", ""),
        ("sel_from_cred", ""), ("sel_to_cred", ""),
        ("sel_levl", "%"), ("sel_schd", "%"), ("sel_camp", "%"),
        ("sel_insm", "%"), ("sel_ptrm", "%"), ("sel_instr", "%"),
        ("sel_attr", "%"),
        ("begin_hh", "0"), ("begin_mi", "0"), ("begin_ap", "a"),
        ("end_hh", "0"), ("end_mi", "0"), ("end_ap", "a"),
    ])
    return params


# ── HTML Parsing — Course Schedule ────────────────────────────────────────────


def parse_title(title_text: str) -> tuple[str, str, str, str, str]:
    """Parse 'Title - CRN - SUBJ NUM - Section' handling dashes in titles."""
    parts = title_text.split(" - ")
    if len(parts) < 4:
        return title_text, "", "", "", ""

    section = parts[-1].strip()
    subj_num = parts[-2].strip()
    crn = parts[-3].strip()
    title = " - ".join(parts[:-3]).strip()

    sp = subj_num.split()
    return title, crn, sp[0] if sp else subj_num, sp[1] if len(sp) > 1 else "", section


def _extract_meta(detail_td) -> dict:
    """Extract metadata from a detail cell using SPAN labels and text nodes.

    Avoids the expensive full text_content() call by reading SPAN.tail
    and iterating only the direct-child text nodes.
    """
    levels = attributes = registration_dates = ""
    for span in detail_td.iterdescendants("span"):
        if span.get("class") != "fieldlabeltext":
            continue
        label = (span.text or "").strip()
        value = (span.tail or "").strip()
        if "Levels:" in label:
            levels = value
        elif "Attributes:" in label:
            attributes = value
        elif "Registration Dates:" in label:
            registration_dates = value

    # Credits, schedule type, method, campus are bare text nodes
    credits: Optional[float] = None
    schedule_type = instructional_method = campus = ""

    for text in detail_td.itertext():
        t = text.strip()
        if not t:
            continue
        if t.endswith("Credits"):
            m = RE_CREDITS.match(t)
            if m:
                try:
                    credits = float(m.group(1))
                except ValueError:
                    pass
        elif t.endswith("Schedule Type"):
            schedule_type = t.rsplit(" Schedule Type", 1)[0].strip()
        elif t.endswith("Instructional Method"):
            instructional_method = t.rsplit(" Instructional Method", 1)[0].strip()
        elif t.endswith("Campus"):
            campus = t.rsplit(" Campus", 1)[0].strip()

    return dict(
        levels=levels, attributes=attributes,
        registration_dates=registration_dates,
        credits=credits, schedule_type=schedule_type,
        instructional_method=instructional_method, campus=campus,
    )


def parse_courses(raw_html: str, term_id: str) -> list[Course]:
    """Parse courses from Banner HTML using lxml directly."""
    tree = lxml_html.fromstring(raw_html)
    courses: list[Course] = []

    for title_th in tree.xpath('//th[@class="ddtitle"]'):
        links = title_th.findall(".//a")
        if not links:
            continue

        class_title, crn, subject, course_number, section = parse_title(
            links[0].text_content().strip()
        )
        if not crn:
            continue

        title_tr = title_th.getparent()
        if title_tr is None:
            continue
        detail_tr = title_tr.getnext()
        if detail_tr is None:
            continue
        detail_tds = detail_tr.xpath('.//td[@class="dddefault"]')
        if not detail_tds:
            continue
        detail_td = detail_tds[0]

        meta = _extract_meta(detail_td)

        base = dict(
            crn=crn, term_id=term_id, subject=subject,
            course_number=course_number, title=class_title, section=section,
            **meta,
        )

        # Parse schedule table
        sched_tables = detail_td.xpath('.//table[@class="datadisplaytable"]')

        if sched_tables:
            rows = sched_tables[0].findall(".//tr")[1:]  # skip header
            for row in rows:
                cells = row.findall(".//td")
                if len(cells) < 8:
                    continue

                class_type = text_of(cells[0])
                time_text = text_of(cells[1])
                days_text = text_of(cells[2])
                seats_text = text_of(cells[3])
                classroom = text_of(cells[4])
                date_range = text_of(cells[5])
                _sched_type = text_of(cells[6])

                start_time = ""
                end_time = ""
                if " - " in time_text and time_text != "TBA":
                    tp = time_text.split(" - ", 1)
                    start_time = tp[0].strip()
                    end_time = tp[1].strip() if len(tp) > 1 else ""

                inst_text = text_of(cells[7])
                pm = RE_INSTRUCTOR_P.match(inst_text)
                instructor_name = RE_WHITESPACE.sub(" ", pm.group(1).strip()) if pm else RE_WHITESPACE.sub(" ", inst_text)

                instructor_email = ""
                for a in cells[7].xpath(".//a[@href]"):
                    href = a.get("href", "")
                    cf = RE_CF_EMAIL.search(href)
                    if cf:
                        instructor_email = decode_cf_email(cf.group(1))
                        break

                courses.append(Course(
                    **base,
                    class_type=class_type,
                    start_time=start_time, end_time=end_time,
                    days=days_text,
                    seats_available=(seats_text == "Y") if seats_text in ("Y", "N") else None,
                    classroom=classroom, date_range=date_range,
                    instructor_name=instructor_name,
                    instructor_email=instructor_email,
                    is_lab=(class_type == "Lab" or _sched_type == "Lab"),
                ))
        else:
            courses.append(Course(**base, is_lab="lab" in class_title.lower()))

    return courses


# ── HTML Parsing — Catalog ────────────────────────────────────────────────────


def parse_catalog_page(raw_html: str) -> list[CatalogEntry]:
    """Parse catalog page for all courses of a subject."""
    tree = lxml_html.fromstring(raw_html)
    entries: list[CatalogEntry] = []

    for title_td in tree.xpath('//td[@class="nttitle"]'):
        link = title_td.find(".//a")
        if link is None:
            continue

        # Parse "COE 221 - Digital Systems"
        title_text = link.text_content().strip()
        parts = title_text.split(" - ", 1)
        if len(parts) < 2:
            continue
        subj_num_parts = parts[0].strip().split()
        if len(subj_num_parts) < 2:
            continue
        subject = subj_num_parts[0]
        course_number = " ".join(subj_num_parts[1:])

        # Content cell is in the next row
        title_tr = title_td.getparent()
        if title_tr is None:
            continue
        content_tr = title_tr.getnext()
        if content_tr is None:
            continue
        content_td = content_tr.find('.//td[@class="ntdefault"]')
        if content_td is None:
            continue

        # Description = first direct text of the td (before any <br/> or child element)
        description = (content_td.text or "").strip()

        # Parse hours from full text content
        full_text = content_td.text_content()

        credit_hours = lecture_hours = lab_hours = None
        m = RE_CREDIT_HOURS.search(full_text)
        if m:
            try:
                credit_hours = float(m.group(1))
            except ValueError:
                pass
        m = RE_LECTURE_HOURS.search(full_text)
        if m:
            try:
                lecture_hours = float(m.group(1))
            except ValueError:
                pass
        m = RE_LAB_HOURS.search(full_text)
        if m:
            try:
                lab_hours = float(m.group(1))
            except ValueError:
                pass

        # Department: text node ending with "Department"
        department = ""
        for text in content_td.itertext():
            t = text.strip()
            if t.endswith("Department"):
                department = t
                break

        entries.append(CatalogEntry(
            subject=subject,
            course_number=course_number,
            description=description,
            credit_hours=credit_hours,
            lecture_hours=lecture_hours,
            lab_hours=lab_hours,
            department=department,
        ))

    return entries


# ── HTML Parsing — Section Detail ─────────────────────────────────────────────


def parse_detail_page(
    raw_html: str, crn: str, term_id: str,
) -> tuple[SectionDetail, list[CourseDependency]]:
    """Parse section detail page for prerequisites, coreqs, restrictions, fees."""
    tree = lxml_html.fromstring(raw_html)

    # Find the main detail cell (the one with tables inside)
    detail_tds = tree.xpath('//td[@class="dddefault"]')
    main_td = None
    for td in detail_tds:
        if td.find(".//table") is not None:
            main_td = td
            break
    if main_td is None:
        # Fallback: pick the largest dddefault cell
        if detail_tds:
            main_td = max(detail_tds, key=lambda td: len(etree.tostring(td)))
        else:
            return SectionDetail(crn=crn, term_id=term_id), []

    # ── Parse tables (waitlist, fees) ──
    waitlist_cap = waitlist_act = waitlist_rem = 0
    fees_list: list[dict[str, str]] = []

    for table in main_td.xpath(".//table"):
        caption = table.find(".//caption")
        cap_text = caption.text_content().strip() if caption is not None else ""

        if "Registration Availability" in cap_text:
            for row in table.findall(".//tr"):
                th = row.find(".//th")
                if th is not None and "Waitlist" in th.text_content():
                    cells = row.findall(".//td")
                    if len(cells) >= 3:
                        try:
                            waitlist_cap = int(cells[0].text_content().strip())
                            waitlist_act = int(cells[1].text_content().strip())
                            waitlist_rem = int(cells[2].text_content().strip())
                        except ValueError:
                            pass

        elif "fee" in cap_text.lower():
            for row in table.findall(".//tr"):
                cells = row.findall(".//td")
                if len(cells) >= 2:
                    desc = cells[-2].text_content().strip()
                    amt = cells[-1].text_content().strip()
                    if desc:
                        fees_list.append({"description": desc, "amount": amt})

    # ── Parse text sections (prereqs, coreqs, restrictions) from HTML ──
    raw_html_str = etree.tostring(main_td, encoding="unicode")

    sections: dict[str, str] = {}
    for m in RE_DETAIL_SECTION.finditer(raw_html_str):
        label = m.group(1)
        fragment_html = m.group(2)
        try:
            frag_tree = lxml_html.fromstring(f"<div>{fragment_html}</div>")
            text = RE_WHITESPACE.sub(" ", frag_tree.text_content()).strip()
            sections[label] = text
        except Exception:
            pass

    prerequisites = sections.get("Prerequisites", "")
    corequisites = sections.get("Corequisites", "")
    restrictions = sections.get("Restrictions", "")

    # ── Parse structured dependency links ──
    deps: list[CourseDependency] = []

    def extract_deps(label: str, dep_type: str):
        pattern = re.compile(
            rf'<span[^>]*class="fieldlabeltext"[^>]*>[^<]*{re.escape(label)}[^<]*</span>'
            rf"(.*?)(?=<span[^>]*class="
            rf'"fieldlabeltext"|<table|</td>)',
            re.DOTALL | re.IGNORECASE,
        )
        m = pattern.search(raw_html_str)
        if not m:
            return
        try:
            frag_tree = lxml_html.fromstring(f"<div>{m.group(1)}</div>")
        except Exception:
            return
        for a in frag_tree.xpath(".//a[@href]"):
            link_text = a.text_content().strip()
            parts = link_text.split()
            if len(parts) < 2:
                continue
            subj = parts[0]
            crse = parts[1]
            # Minimum grade from tail text
            min_grade = ""
            tail = (a.tail or "").strip()
            grade_m = RE_MIN_GRADE.search(tail)
            if grade_m:
                min_grade = grade_m.group(1)
            deps.append(CourseDependency(
                crn=crn, term_id=term_id, dep_type=dep_type,
                subject=subj, course_number=crse, minimum_grade=min_grade,
            ))

    extract_deps("Prerequisites", "prerequisite")
    extract_deps("Corequisites", "corequisite")

    detail = SectionDetail(
        crn=crn, term_id=term_id,
        prerequisites=prerequisites,
        corequisites=corequisites,
        restrictions=restrictions,
        waitlist_capacity=waitlist_cap,
        waitlist_actual=waitlist_act,
        waitlist_remaining=waitlist_rem,
        fees=json.dumps(fees_list) if fees_list else "",
    )

    return detail, deps


# ── Crawl Orchestration ──────────────────────────────────────────────────────


async def run(args: argparse.Namespace):
    """Main crawl pipeline."""
    conn = init_db(args.output, force=args.force)

    # Thread pool for CPU-bound HTML parsing (avoids blocking the event loop)
    parse_pool = concurrent.futures.ThreadPoolExecutor(max_workers=4)
    loop = asyncio.get_running_loop()

    async with httpx.AsyncClient(
        timeout=httpx.Timeout(120.0),
        follow_redirects=True,
        http2=True,
        headers={"User-Agent": "AUSCrawl/2.0 (academic-data-project)"},
        limits=httpx.Limits(
            max_connections=args.workers + 5,
            max_keepalive_connections=args.workers + 5,
        ),
    ) as client:
        t0 = time.monotonic()

        # ── Phase 1: Fetch semester list ──
        console.print("[bold]Phase 1:[/] Fetching semester list...")
        semesters = await fetch_semesters(client)
        console.print(f"  Found [cyan]{len(semesters)}[/] semesters")

        # ── Filter ──
        if args.terms:
            term_set = set(args.terms)
            semesters = [s for s in semesters if s.term_id in term_set]
            console.print(f"  Filtered to [cyan]{len(semesters)}[/] requested terms")

        if args.latest:
            semesters = semesters[-1:] if semesters else []
            if semesters:
                console.print(f"  Latest only: [cyan]{semesters[0].term_name}[/]")

        if args.resume:
            existing = {r[0] for r in conn.execute("SELECT term_id FROM semesters").fetchall()}
            before = len(semesters)
            semesters = [s for s in semesters if s.term_id not in existing]
            skipped = before - len(semesters)
            console.print(f"  Resume: skipping [yellow]{skipped}[/] done, [cyan]{len(semesters)}[/] remaining")

        if not semesters:
            console.print("[yellow]Nothing to crawl.[/]")
            conn.close()
            return

        # ── Phase 2: Build complete subject catalog ──
        console.print("[bold]Phase 2:[/] Fetching subject catalog...")

        # Use known subjects from existing DB if available (fast path)
        known = [r[0] for r in conn.execute("SELECT short_name FROM subjects").fetchall()]
        if known and not args.force:
            subject_codes = known
            subjects = [Subject(s, "") for s in known]
            console.print(f"  [cyan]{len(subject_codes)}[/] known subjects from DB")
        else:
            # Fresh crawl: fetch from ALL semesters concurrently to discover
            # every subject that ever existed (dropdown varies per term).
            subj_sem = asyncio.Semaphore(args.workers)

            async def fetch_subj(term_id: str) -> list[Subject]:
                async with subj_sem:
                    return await fetch_subjects(client, term_id)

            all_subj_lists = await asyncio.gather(
                *(fetch_subj(s.term_id) for s in semesters)
            )

            # Deduplicate: keep the first (longest) long_name per code
            seen: dict[str, Subject] = {}
            for subj_list in all_subj_lists:
                for s in subj_list:
                    if s.short_name not in seen or len(s.long_name) > len(seen[s.short_name].long_name):
                        seen[s.short_name] = s

            subjects = list(seen.values())
            subject_codes = [s.short_name for s in subjects]
            console.print(f"  [cyan]{len(subject_codes)}[/] unique subjects across all semesters")

        # Split subjects into batches that stay under the ~4500-byte WAF limit
        batch_size = 250
        subject_batches = [
            subject_codes[i : i + batch_size]
            for i in range(0, len(subject_codes), batch_size)
        ]
        n_batches = len(subject_batches)
        total_requests = len(semesters) * n_batches
        console.print(f"  {n_batches} batch(es)/semester → [cyan]{total_requests}[/] total requests")

        # Pre-build form params per batch (shared across semesters)
        batch_params = [build_course_params("PLACEHOLDER", batch) for batch in subject_batches]

        # ── Phase 3: Fire all course requests concurrently ──
        console.print(f"[bold]Phase 3:[/] Crawling {len(semesters)} semesters ({args.workers} workers)...")
        semaphore = asyncio.Semaphore(args.workers)
        results: list[tuple[Semester, list[Course]]] = []
        errors: list[str] = []

        with Progress(
            SpinnerColumn(),
            TextColumn("[bold blue]{task.description}"),
            BarColumn(),
            MofNCompleteColumn(),
            TextColumn("•"),
            TimeElapsedColumn(),
            TextColumn("•"),
            TimeRemainingColumn(),
            console=console,
        ) as progress:
            task = progress.add_task("Crawling", total=len(semesters))

            async def fetch_batch(semester: Semester, params_template: list) -> list[Course]:
                """Fetch one batch of courses for a semester."""
                async with semaphore:
                    params = [("term_in", semester.term_id)] + params_template[1:]
                    resp = await request_with_retry(
                        client, "POST", ENDPOINTS["courses"], form=params
                    )
                    # Parse in thread pool so HTTP I/O isn't blocked
                    return await loop.run_in_executor(
                        parse_pool, parse_courses, resp.text, semester.term_id
                    )

            async def crawl_one(semester: Semester):
                try:
                    # Fire all batches for this semester concurrently
                    batch_coros = [
                        fetch_batch(semester, bp) for bp in batch_params
                    ]
                    batch_results = await asyncio.gather(*batch_coros)
                    courses = [c for batch in batch_results for c in batch]
                    results.append((semester, courses))
                    progress.update(
                        task, advance=1,
                        description=f"[bold blue]{semester.term_name} → {len(courses)}",
                    )
                except Exception as e:
                    errors.append(f"{semester.term_name}: {e}")
                    results.append((semester, []))
                    progress.update(task, advance=1)

                if args.delay > 0:
                    await asyncio.sleep(args.delay)

            await asyncio.gather(*(crawl_one(s) for s in semesters))

        t_courses = time.monotonic() - t0

        if errors:
            for e in errors:
                log.error(e)

        # ── Save courses immediately (crash-safe checkpoint) ──
        t_db = time.monotonic()
        console.print("[bold]Saving:[/] Writing course data to database...")
        bulk_save(conn, semesters, subjects, results)
        fix_first_seen(conn)
        t_db_courses = time.monotonic() - t_db

        total_courses = sum(len(cs) for _, cs in results)
        console.print(
            f"  [cyan]{total_courses:,}[/] courses saved "
            f"(crawl: {t_courses:.1f}s, DB: {t_db_courses:.1f}s)"
        )

        # ── Phase 4+5: Catalog + Details (sequential, shared semaphore) ──
        t_extra = time.monotonic()
        run_catalog = not args.no_catalog
        run_details = not args.no_details

        # Shared state for both phases
        catalog_entries: list[CatalogEntry] = []
        cat_errors = 0
        all_details: list[SectionDetail] = []
        all_deps: list[CourseDependency] = []
        det_errors = 0

        # ── Build work lists ──

        # Catalog: sample evenly-spaced terms per subject (not all 4k+ combos)
        subj_term_list: list[tuple[str, str]] = []
        if run_catalog:
            all_term_ids = sorted({s.term_id for s in semesters})
            # Also pull from DB for resume
            for row in conn.execute("SELECT DISTINCT term_id FROM courses"):
                all_term_ids.append(row[0])
            all_term_ids = sorted(set(all_term_ids))
            n = len(all_term_ids)
            step = max(1, n // CATALOG_SAMPLE_COUNT)
            sample_terms = [all_term_ids[i] for i in range(0, n, step)]
            if all_term_ids[-1] not in sample_terms:
                sample_terms.append(all_term_ids[-1])

            all_subjects = sorted({
                c.subject for _, courses in results for c in courses
            } | {r[0] for r in conn.execute("SELECT DISTINCT subject FROM courses")})

            for term in sample_terms:
                for subj in all_subjects:
                    subj_term_list.append((subj, term))

        # Details: unique (crn, term) minus already-fetched
        crn_term_list: list[tuple[str, str]] = []
        if run_details:
            crn_terms: set[tuple[str, str]] = set()
            for semester, courses in results:
                for c in courses:
                    crn_terms.add((c.crn, semester.term_id))
            for row in conn.execute("SELECT DISTINCT crn, term_id FROM courses"):
                crn_terms.add(tuple(row))
            existing_details = set(
                conn.execute("SELECT crn, term_id FROM section_details").fetchall()
            )
            crn_term_list = sorted(crn_terms - existing_details)

        if not subj_term_list and not crn_term_list:
            console.print("[bold]Phase 4+5:[/] Catalog and details already complete.")
        else:
            # 10 workers for GET endpoints (higher causes excessive 429s after Phase 3)
            detail_workers = min(args.workers, 10)
            get_sem = asyncio.Semaphore(detail_workers)

            # ── Phase 4: Catalog ──
            if subj_term_list:
                console.print(f"[bold]Phase 4:[/] Fetching {len(subj_term_list):,} catalog entries ({detail_workers}w)...")

                with Progress(
                    SpinnerColumn(),
                    TextColumn("[bold blue]{task.description}"),
                    BarColumn(),
                    MofNCompleteColumn(),
                    TextColumn("•"),
                    TimeElapsedColumn(),
                    TextColumn("•"),
                    TimeRemainingColumn(),
                    console=console,
                ) as progress:
                    cat_task = progress.add_task("Catalog", total=len(subj_term_list))

                    async def fetch_cat(subj: str, term_id: str):
                        nonlocal cat_errors
                        async with get_sem:
                            try:
                                resp = await request_with_retry(
                                    client, "GET", ENDPOINTS["catalog"],
                                    params={
                                        "term_in": term_id, "one_subj": subj,
                                        "sel_crse_strt": "0", "sel_crse_end": "9999",
                                        "sel_subj": "", "sel_levl": "",
                                        "sel_schd": "", "sel_coll": "",
                                        "sel_divs": "", "sel_dept": "",
                                        "sel_attr": "",
                                    },
                                )
                                entries = await loop.run_in_executor(
                                    parse_pool, parse_catalog_page, resp.text
                                )
                                catalog_entries.extend(entries)
                            except Exception:
                                cat_errors += 1
                            progress.update(cat_task, advance=1)

                    await asyncio.gather(*(fetch_cat(s, t) for s, t in subj_term_list))

                save_catalog(conn, catalog_entries)
                cat_count = conn.execute("SELECT COUNT(*) FROM catalog").fetchone()[0]
                console.print(
                    f"  Catalog: [cyan]{cat_count:,}[/] entries"
                    + (f", [yellow]{cat_errors}[/] errors" if cat_errors else "")
                )

            # ── Phase 5: Section Details ──
            if crn_term_list:
                console.print(f"[bold]Phase 5:[/] Fetching {len(crn_term_list):,} section details ({detail_workers}w)...")

                with Progress(
                    SpinnerColumn(),
                    TextColumn("[bold blue]{task.description}"),
                    BarColumn(),
                    MofNCompleteColumn(),
                    TextColumn("•"),
                    TimeElapsedColumn(),
                    TextColumn("•"),
                    TimeRemainingColumn(),
                    console=console,
                ) as progress:
                    det_task = progress.add_task("Details", total=len(crn_term_list))

                    async def fetch_det(c: str, t: str):
                        nonlocal det_errors
                        async with get_sem:
                            try:
                                resp = await request_with_retry(
                                    client, "GET", ENDPOINTS["detail"],
                                    params={"term_in": t, "crn_in": c},
                                )
                                detail, deps = await loop.run_in_executor(
                                    parse_pool, parse_detail_page, resp.text, c, t,
                                )
                                all_details.append(detail)
                                all_deps.extend(deps)
                            except Exception as ex:
                                det_errors += 1
                                log.warning(f"Detail {c}/{t}: {type(ex).__name__}: {ex}")
                            progress.update(det_task, advance=1)

                            # Periodic batch save for resilience
                            if len(all_details) >= DETAIL_BATCH_SIZE:
                                batch_d = all_details[:]
                                batch_dep = all_deps[:]
                                all_details.clear()
                                all_deps.clear()
                                save_details(conn, batch_d, batch_dep)

                    await asyncio.gather(*(fetch_det(c, t) for c, t in crn_term_list))

            # Save remaining details
            if all_details:
                save_details(conn, all_details, all_deps)
            if run_details:
                det_count = conn.execute("SELECT COUNT(*) FROM section_details").fetchone()[0]
                dep_count = conn.execute("SELECT COUNT(*) FROM course_dependencies").fetchone()[0]
                console.print(
                    f"  Details: [cyan]{det_count:,}[/] sections, "
                    f"[cyan]{dep_count:,}[/] dependencies"
                    + (f", [yellow]{det_errors}[/] errors" if det_errors else "")
                )

            t_extra = time.monotonic() - t_extra
            console.print(f"  Phase 4+5 time: {t_extra:.1f}s")

        elapsed = time.monotonic() - t0

        # ── Summary ──
        stats = {
            "semesters": conn.execute("SELECT COUNT(*) FROM semesters").fetchone()[0],
            "courses": conn.execute("SELECT COUNT(*) FROM courses").fetchone()[0],
            "instructors": conn.execute("SELECT COUNT(*) FROM instructors").fetchone()[0],
            "subjects": conn.execute("SELECT COUNT(*) FROM subjects").fetchone()[0],
            "levels": conn.execute("SELECT COUNT(*) FROM levels").fetchone()[0],
            "attributes": conn.execute("SELECT COUNT(*) FROM attributes").fetchone()[0],
            "catalog": conn.execute("SELECT COUNT(*) FROM catalog").fetchone()[0],
            "details": conn.execute("SELECT COUNT(*) FROM section_details").fetchone()[0],
            "dependencies": conn.execute("SELECT COUNT(*) FROM course_dependencies").fetchone()[0],
        }

        console.print()
        console.print("[bold green]Crawl complete![/]")
        console.print(f"  Total time:  [bold]{elapsed:.1f}s[/]")
        for label, count in stats.items():
            console.print(f"  {label.capitalize():14s} {count:,}")
        console.print(f"  Database:    {args.output}")

    parse_pool.shutdown(wait=False)
    conn.close()


# ── CLI ──────────────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(
        description="AUSCrawl — Fast AUS Banner course data scraper",
    )
    parser.add_argument(
        "-o", "--output", default="aus_data.db",
        help="SQLite output path (default: aus_data.db)",
    )
    parser.add_argument(
        "-t", "--terms", nargs="*", metavar="TERM_ID",
        help="Only crawl specific term IDs (e.g. 202620 202510)",
    )
    parser.add_argument(
        "-w", "--workers", type=int, default=DEFAULT_WORKERS,
        help=f"Max concurrent requests (default: {DEFAULT_WORKERS})",
    )
    parser.add_argument(
        "--delay", type=float, default=DEFAULT_DELAY,
        help=f"Seconds between requests (default: {DEFAULT_DELAY})",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true",
        help="Debug-level logging",
    )
    parser.add_argument(
        "--resume", action="store_true",
        help="Skip semesters already in the database",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Drop and recreate all tables",
    )
    parser.add_argument(
        "--latest", action="store_true",
        help="Only crawl the most recent semester",
    )
    parser.add_argument(
        "--no-catalog", action="store_true",
        help="Skip catalog description scraping (Phase 4)",
    )
    parser.add_argument(
        "--no-details", action="store_true",
        help="Skip section detail scraping (Phase 5)",
    )

    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(message)s",
        handlers=[RichHandler(console=console, rich_tracebacks=True, show_path=False)],
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    asyncio.run(run(args))


if __name__ == "__main__":
    main()
