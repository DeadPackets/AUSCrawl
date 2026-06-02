#!/usr/bin/env python3
"""AUSCrawl - Fast AUS Banner course data scraper.

Crawls the AUS Banner system for course data across all semesters since 2005
and stores it in an SQLite database for analysis.
"""

import argparse
import asyncio
import concurrent.futures
import contextlib
import json
import logging
import os
import random
import re
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Callable, Optional
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
    "catalog_detail": f"{BASE_URL}/bwckctlg.p_disp_course_detail",
    "detail": f"{BASE_URL}/bwckschd.p_disp_detail_sched",
}
DEFAULT_WORKERS = 50
# Per-request, per-worker pause. Paces the GET endpoints so they effectively
# stop throttling (0 × 429 in practice, vs ~48 at no delay), trading ~8 min on a
# full crawl to stay a good citizen. Lower it (e.g. 0.1) or set 0 to go faster
# at the cost of more 429s — those are still retried, just noisier.
DEFAULT_DELAY = 0.25
MAX_RETRIES = 5
RETRY_BASE = 2.0            # backoff base for all retries
DETAIL_BATCH_SIZE = 5000   # save details every N for resilience
CATALOG_SAMPLE_COUNT = 6   # number of evenly-spaced terms to sample for catalog
WAF_SCAN_LIMIT = 65536     # bytes of a response to scan for the WAF marker
GET_WORKER_CAP = 10        # GET endpoints start 429-ing above this

# Status codes worth retrying: rate limits + transient server errors.
# Other 4xx (400/401/404/410/422 …) are permanent — retrying just wastes time.
RETRYABLE_STATUS = frozenset({403, 408, 429}) | frozenset(range(500, 600))
# Codes that mean "you're going too fast" — feed these back to the limiter.
THROTTLE_STATUS = frozenset({429, 503})

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
RE_PAREN_P = re.compile(r"\(\s*P?\s*\)")        # "(P)" primary marker or stray "()"
RE_PRIMARY = re.compile(r"\(\s*P\s*\)")          # detect the primary marker in text

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
class InstructorRef:
    name: str
    email: str = ""
    is_primary: bool = False


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
    instructor_name: str = ""   # primary instructor (kept for backward compat)
    instructor_email: str = ""  # primary instructor's email
    is_lab: bool = False
    # All instructors on this meeting block (primary + secondary). See G1.
    instructors: list[InstructorRef] = field(default_factory=list)


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
    prerequisites_json: str = ""   # G3: boolean expression tree (JSON)
    corequisites_json: str = ""    # G3: boolean expression tree (JSON)
    restrictions_json: str = ""    # G4: typed include/exclude groups (JSON)


@dataclass(slots=True)
class CourseDependency:
    crn: str
    term_id: str
    dep_type: str  # 'prerequisite' or 'corequisite'
    subject: str
    course_number: str
    minimum_grade: str = ""


@dataclass(slots=True)
class CatalogDetail:
    """Course-level data from bwckctlg.p_disp_course_detail (G2)."""
    subject: str
    course_number: str
    term_id: str = ""           # the term whose catalog entry was read
    levels: str = ""
    schedule_types: str = ""
    course_attributes: str = ""  # degree-requirement tags
    prerequisites: str = ""
    corequisites: str = ""
    restrictions: str = ""


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


def should_retry_status(code: int) -> bool:
    """Whether an HTTP status is worth retrying (vs. a permanent failure)."""
    return code in RETRYABLE_STATUS


def backoff_delay(
    attempt: int, base: float = RETRY_BASE, *, rand: Callable[[], float] = random.random
) -> float:
    """Equal-jitter exponential backoff.

    Returns a value in [cap/2, cap) where cap = base * 2**attempt. The jitter
    de-synchronizes the many concurrent workers so they don't all back off by
    the same amount and then retry in lockstep, which would re-trigger the
    rate limit.
    """
    cap = base * (2 ** attempt)
    half = cap / 2
    return half + rand() * half


def is_waf_block(content: bytes, limit: int = WAF_SCAN_LIMIT) -> bool:
    """Detect a Cloudflare/WAF block page by its marker.

    Scans only the first `limit` bytes so we never lowercase a multi-MB course
    page on the event loop just to look for a short marker.
    """
    return b"support ticket" in content[:limit].lower()


def parse_pool_size(cpu: Optional[int] = None) -> int:
    """Thread-pool size for CPU-bound lxml parsing: scale with cores, bounded."""
    if cpu is None:
        cpu = os.cpu_count() or 4
    return max(4, min(16, cpu))


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
    UNIQUE(crn, term_id, class_type, days, start_time, end_time, classroom)
);

CREATE INDEX IF NOT EXISTS idx_courses_term ON courses(term_id);
CREATE INDEX IF NOT EXISTS idx_courses_subject ON courses(subject);
CREATE INDEX IF NOT EXISTS idx_courses_crn ON courses(crn);
CREATE INDEX IF NOT EXISTS idx_courses_crn_term ON courses(crn, term_id);
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
    prerequisites_json TEXT DEFAULT '',
    corequisites_json TEXT DEFAULT '',
    restrictions_json TEXT DEFAULT '',
    UNIQUE(crn, term_id)
);

CREATE TABLE IF NOT EXISTS section_instructors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    crn TEXT NOT NULL,
    term_id TEXT NOT NULL,
    name TEXT NOT NULL,
    email TEXT,
    is_primary BOOLEAN DEFAULT 0,
    UNIQUE(crn, term_id, name)
);

CREATE TABLE IF NOT EXISTS catalog_detail (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    subject TEXT NOT NULL,
    course_number TEXT NOT NULL,
    term_id TEXT DEFAULT '',
    levels TEXT DEFAULT '',
    schedule_types TEXT DEFAULT '',
    course_attributes TEXT DEFAULT '',
    prerequisites TEXT DEFAULT '',
    corequisites TEXT DEFAULT '',
    restrictions TEXT DEFAULT '',
    UNIQUE(subject, course_number)
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
CREATE INDEX IF NOT EXISTS idx_section_instructors ON section_instructors(crn, term_id);
CREATE INDEX IF NOT EXISTS idx_section_instructors_name ON section_instructors(name);
CREATE INDEX IF NOT EXISTS idx_catalog_detail_subject ON catalog_detail(subject);
"""


def init_db(db_path: str, force: bool = False) -> sqlite3.Connection:
    """Initialize SQLite database with schema."""
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    # NORMAL (not OFF) with WAL: fast, and safe against OS crash / power loss.
    # OFF only protects against an app crash and can corrupt the file otherwise.
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA cache_size=-64000")
    conn.execute("PRAGMA temp_store=MEMORY")

    if force:
        for table in (
            "course_dependencies", "section_details", "section_instructors",
            "catalog", "catalog_detail", "courses", "instructors", "subjects",
            "levels", "attributes", "semesters",
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
    sec_instr_seen: set[tuple[str, str, str]] = set()
    levels_seen: set[str] = set()
    attrs_seen: set[str] = set()

    course_rows = []
    instructor_rows = []
    section_instructor_rows = []
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

            # Every instructor (primary + secondary) feeds the global table and
            # the per-section link table. Falls back to the primary fields for
            # the no-schedule-table case where `instructors` is empty.
            refs = c.instructors or (
                [InstructorRef(c.instructor_name, c.instructor_email, True)]
                if c.instructor_name and c.instructor_name != "TBA" else []
            )
            for ref in refs:
                if not ref.name or ref.name == "TBA":
                    continue
                gkey = (ref.name, ref.email or "")
                if gkey not in instructors_seen:
                    instructors_seen.add(gkey)
                    instructor_rows.append((ref.name, ref.email or None, semester.term_id))
                skey = (c.crn, c.term_id, ref.name)
                if skey not in sec_instr_seen:
                    sec_instr_seen.add(skey)
                    section_instructor_rows.append(
                        (c.crn, c.term_id, ref.name, ref.email or None, 1 if ref.is_primary else 0)
                    )

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
        "INSERT OR IGNORE INTO section_instructors (crn, term_id, name, email, is_primary) "
        "VALUES (?, ?, ?, ?, ?)",
        section_instructor_rows,
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


def better_catalog(a: CatalogEntry, b: CatalogEntry) -> CatalogEntry:
    """Merge two catalog entries for the same course without losing information.

    The entry with the longer description is the base; any field it's missing
    (None hours / empty department) is filled from the other. This makes catalog
    writes monotonic — re-running can only improve a row, never degrade it.
    """
    base, other = (a, b) if len(a.description) >= len(b.description) else (b, a)
    return CatalogEntry(
        subject=base.subject,
        course_number=base.course_number,
        description=base.description,
        credit_hours=base.credit_hours if base.credit_hours is not None else other.credit_hours,
        lecture_hours=base.lecture_hours if base.lecture_hours is not None else other.lecture_hours,
        lab_hours=base.lab_hours if base.lab_hours is not None else other.lab_hours,
        department=base.department or other.department,
    )


def save_catalog(conn: sqlite3.Connection, entries: list[CatalogEntry]):
    """Merge catalog entries into the DB, keeping the best of new + existing."""
    if not entries:
        return

    # Collapse this batch first.
    best: dict[tuple[str, str], CatalogEntry] = {}
    for e in entries:
        key = (e.subject, e.course_number)
        best[key] = better_catalog(best[key], e) if key in best else e

    # Merge against whatever is already stored so a shorter description from a
    # later run can't overwrite a fuller one.
    placeholders = ",".join("(?, ?)" for _ in best)
    flat = [v for key in best for v in key]
    existing_rows = conn.execute(
        "SELECT subject, course_number, description, credit_hours, lecture_hours, "
        "lab_hours, department FROM catalog "
        f"WHERE (subject, course_number) IN ({placeholders})",
        flat,
    ).fetchall()
    for r in existing_rows:
        key = (r[0], r[1])
        existing = CatalogEntry(
            subject=r[0], course_number=r[1], description=r[2] or "",
            credit_hours=r[3], lecture_hours=r[4], lab_hours=r[5],
            department=r[6] or "",
        )
        best[key] = better_catalog(best[key], existing)

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


def save_catalog_detail(conn: sqlite3.Connection, entries: list[CatalogDetail]):
    """Bulk-upsert course-level catalog detail rows (G2)."""
    if not entries:
        return
    conn.executemany(
        "INSERT OR REPLACE INTO catalog_detail "
        "(subject, course_number, term_id, levels, schedule_types, course_attributes, "
        "prerequisites, corequisites, restrictions) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (e.subject, e.course_number, e.term_id, e.levels, e.schedule_types,
             e.course_attributes, e.prerequisites, e.corequisites, e.restrictions)
            for e in entries
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
            "waitlist_capacity, waitlist_actual, waitlist_remaining, fees, "
            "prerequisites_json, corequisites_json, restrictions_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (d.crn, d.term_id, d.prerequisites, d.corequisites, d.restrictions,
                 d.waitlist_capacity, d.waitlist_actual, d.waitlist_remaining, d.fees,
                 d.prerequisites_json, d.corequisites_json, d.restrictions_json)
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


class AdaptiveLimiter:
    """AIMD concurrency limiter.

    Gates concurrent work to a dynamic `limit`. On a throttle signal (429/503/
    WAF block) the limit is multiplicatively decreased; on success it is
    additively increased back toward `max_limit`. Unlike a fixed semaphore,
    this backs the whole fleet off when the server starts pushing back instead
    of letting the other workers keep hammering at full rate.
    """

    def __init__(
        self,
        start: float,
        max_limit: float,
        min_limit: float = 1.0,
        increase: float = 1.0,
        decrease: float = 0.5,
    ):
        self.limit = float(start)
        self.max_limit = float(max_limit)
        self.min_limit = float(min_limit)
        self.increase = increase
        self.decrease = decrease
        self.active = 0
        self._cond = asyncio.Condition()

    async def acquire(self):
        async with self._cond:
            while self.active >= self.limit:
                await self._cond.wait()
            self.active += 1

    async def release(self):
        async with self._cond:
            self.active -= 1
            self._cond.notify(1)

    async def record_success(self):
        async with self._cond:
            if self.limit < self.max_limit:
                # Additive increase of one step per *window* of `limit` successes
                # (textbook AIMD). Recovering by a full step on every single
                # success would snap straight back to the ceiling and re-trigger
                # the rate limit; this settles near the sustainable rate instead.
                self.limit = min(self.max_limit, self.limit + self.increase / self.limit)
                self._cond.notify(1)  # a freshly opened slot may be claimable

    async def record_throttle(self):
        async with self._cond:
            self.limit = max(self.min_limit, self.limit * self.decrease)

    @contextlib.asynccontextmanager
    async def slot(self):
        await self.acquire()
        try:
            yield
        finally:
            await self.release()


def make_client(workers: int) -> httpx.AsyncClient:
    """Build the shared HTTP/2 client.

    A short connect timeout frees a worker fast when a connection is dead,
    while keeping a long read timeout for the large course-search pages.
    """
    return httpx.AsyncClient(
        timeout=httpx.Timeout(120.0, connect=10.0),
        follow_redirects=True,
        http2=True,
        headers={"User-Agent": "AUSCrawl/2.0 (academic-data-project)"},
        limits=httpx.Limits(
            max_connections=workers + 5,
            max_keepalive_connections=workers + 5,
        ),
    )


async def request_with_retry(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    form: list[tuple[str, str]] | dict[str, str] | None = None,
    params: dict[str, str] | None = None,
    limiter: Optional[AdaptiveLimiter] = None,
) -> httpx.Response:
    """HTTP request with jittered retry, WAF detection, and adaptive feedback."""
    kwargs: dict = {}
    if form is not None:
        kwargs["content"] = urlencode(form)
        kwargs["headers"] = FORM_CONTENT_TYPE
    if params is not None:
        kwargs["params"] = params

    for attempt in range(1, MAX_RETRIES + 1):
        last = attempt == MAX_RETRIES
        try:
            resp = await client.request(method, url, **kwargs)
            resp.raise_for_status()

            if is_waf_block(resp.content):
                if limiter:
                    await limiter.record_throttle()
                if last:
                    break
                wait = backoff_delay(attempt)
                log.warning(f"WAF block (attempt {attempt}), retrying in {wait:.0f}s")
                await asyncio.sleep(wait)
                continue

            if limiter:
                await limiter.record_success()
            return resp
        except httpx.HTTPStatusError as e:
            code = e.response.status_code
            if limiter and code in THROTTLE_STATUS:
                await limiter.record_throttle()
            if not should_retry_status(code):
                raise
            if last:
                break
            wait = backoff_delay(attempt)
            log.warning(f"HTTP {code} (attempt {attempt}), retrying in {wait:.1f}s")
            await asyncio.sleep(wait)
        except httpx.RequestError as e:
            if last:
                raise
            wait = backoff_delay(attempt)
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


def parse_instructors(cell) -> list[InstructorRef]:
    """Parse every instructor from a schedule 'Instructors' cell (G1).

    Banner lists instructors comma-separated; the primary carries a '(P)' marker
    (an ``<abbr title="Primary">P</abbr>``) and each name is followed by its own
    email link. We split on commas while walking the DOM in document order so a
    given email stays attached to the right name even when some instructors have
    no email link or no '(P)'.
    """
    segments: list[InstructorRef] = []
    cur = {"name": "", "email": "", "primary": False}

    def flush():
        raw = cur["name"]
        primary = cur["primary"] or bool(RE_PRIMARY.search(raw))
        name = RE_WHITESPACE.sub(" ", RE_PAREN_P.sub("", raw)).strip().strip(",").strip()
        if name and name.upper() != "TBA":
            segments.append(InstructorRef(name=name, email=cur["email"], is_primary=primary))

    def feed_text(text):
        if not text:
            return
        parts = text.split(",")
        cur["name"] += parts[0]
        for extra in parts[1:]:           # each comma starts a new instructor
            flush()
            cur["name"], cur["email"], cur["primary"] = extra, "", False

    feed_text(cell.text or "")
    for child in cell:
        tag = child.tag
        if isinstance(tag, str):
            if tag == "abbr" and (child.get("title") == "Primary"
                                  or (child.text or "").strip() == "P"):
                cur["primary"] = True
            elif tag == "a":
                cf = RE_CF_EMAIL.search(child.get("href", ""))
                if cf:
                    cur["email"] = decode_cf_email(cf.group(1))
        feed_text(child.tail or "")
    flush()
    return segments


def parse_courses(raw_html: str | bytes, term_id: str) -> list[Course]:
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
                    instructors=parse_instructors(cells[7]),
                ))
        else:
            courses.append(Course(**base, is_lab="lab" in class_title.lower()))

    return courses


# ── HTML Parsing — Catalog ────────────────────────────────────────────────────


def parse_catalog_page(raw_html: str | bytes) -> list[CatalogEntry]:
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


def extract_label_sections(cell, targets: tuple[str, ...]):
    """Single-pass walk of an OWA content cell.

    Returns (items_by_label, links_by_label). ``items`` is an ordered list of
    ``('t', text)`` / ``('el', element)`` per target ``fieldlabeltext`` section,
    from which collapsed text, line structure (G4), and requirement tokens (G3)
    are all derived. Any fieldlabeltext span or a ``<table>`` ends the current
    section — matching the original parser's boundaries.
    """
    items: dict[str, list] = {t: [] for t in targets}
    links: dict[str, list] = {t: [] for t in targets}
    current: Optional[str] = None

    def add_text(text):
        if current is not None and text:
            items[current].append(("t", text))

    add_text(cell.text)
    for child in cell:
        tag = child.tag
        if not isinstance(tag, str):  # comment / PI: no text_content, keep tail
            add_text(child.tail)
            continue
        if tag == "span" and child.get("class") == "fieldlabeltext":
            label = (child.text or "").strip().rstrip(":").strip()
            current = label if label in targets else None
            add_text(child.tail)
            continue
        if tag == "table":
            current = None
            add_text(child.tail)
            continue
        if current is not None:
            items[current].append(("el", child))
            if tag == "a":
                links[current].append(child)
            else:
                links[current].extend(child.iter("a"))
        add_text(child.tail)
    return items, links


def collapse_items(items: list) -> str:
    """Whitespace-collapsed text of a section's items (matches legacy output)."""
    parts = [v if k == "t" else v.text_content() for k, v in items]
    return RE_WHITESPACE.sub(" ", "".join(parts)).strip()


def section_lines(items: list) -> list[str]:
    """Split a section's items into non-empty lines at ``<br>`` boundaries."""
    buf = []
    for kind, val in items:
        if kind == "t":
            buf.append(val)
        elif val.tag == "br":
            buf.append("\n")
        else:
            buf.append(val.text_content())
    lines = [RE_WHITESPACE.sub(" ", ln).strip() for ln in "".join(buf).split("\n")]
    return [ln for ln in lines if ln]


RE_RESTR_HEADER = re.compile(r"^(must|may not)\s+be\s+enrolled\b.*:\s*$", re.IGNORECASE)
RE_RESTR_TYPE = re.compile(r"following\s+(.+?):\s*$", re.IGNORECASE)


def parse_restrictions(items: list) -> str:
    """G4: parse restriction lines into typed include/exclude groups (JSON).

    Banner emits a header per group ("Must be enrolled in one of the following
    Levels:" / "May not be enrolled as the following Classifications:") followed
    by the member values, one per line.
    """
    groups: list[dict] = []
    cur: Optional[dict] = None
    for ln in section_lines(items):
        if RE_RESTR_HEADER.match(ln):
            m = RE_RESTR_TYPE.search(ln)
            cur = {
                "include": ln[:4].lower() == "must",
                "type": (m.group(1).strip() if m else ln.rsplit(":", 1)[0].strip()),
                "values": [],
            }
            groups.append(cur)
        elif cur is not None:
            cur["values"].append(ln)
    groups = [g for g in groups if g["values"]]
    return json.dumps(groups, ensure_ascii=False) if groups else ""


RE_LEVEL_QUALIFIER = re.compile(r"([A-Za-z][\w-]*)\s+level\b")
RE_REQ_OP = re.compile(r"\(|\)|\b(?:and|or)\b", re.IGNORECASE)


def _requirement_tokens(items: list) -> list[tuple]:
    """Tokenize a prereq/coreq section into LEAF / AND / OR / LP / RP tokens."""
    tokens: list[tuple] = []
    for i, (kind, val) in enumerate(items):
        if kind == "el" and isinstance(val.tag, str) and val.tag == "a":
            parts = val.text_content().split()
            if len(parts) < 2:
                continue
            level = ""
            if i > 0 and items[i - 1][0] == "t":
                lm = RE_LEVEL_QUALIFIER.findall(items[i - 1][1])
                if lm:
                    level = lm[-1]
            tail = val.tail or ""
            gm = RE_MIN_GRADE.search(tail)
            tokens.append(("LEAF", {
                "type": "course",
                "subject": parts[0],
                "course_number": parts[1],
                "min_grade": gm.group(1) if gm else "",
                "level": level,
                "concurrent": "concurrent" in tail.lower(),
            }))
        elif kind == "t":
            for mt in RE_REQ_OP.finditer(val):
                s = mt.group(0)
                tokens.append(({"(": "LP", ")": "RP"}.get(s, s.upper()), None))
    return tokens


def _merge(op: str, *nodes) -> list:
    """Flatten same-operator children so chains become a single n-ary node."""
    out: list = []
    for node in nodes:
        if isinstance(node, dict) and node.get("type") == op:
            out.extend(node["operands"])
        else:
            out.append(node)
    return out


def requirement_tree(items: list):
    """G3: build a boolean expression tree from a prereq/coreq section.

    AND binds tighter than OR; parentheses group. Returns a nested dict
    (``{"type":"and"|"or","operands":[...]}`` or a single course leaf), or None.
    """
    prec = {"AND": 2, "OR": 1}
    rpn: list[tuple] = []
    ops: list[str] = []
    for typ, payload in _requirement_tokens(items):
        if typ == "LEAF":
            rpn.append(("LEAF", payload))
        elif typ in ("AND", "OR"):
            while ops and ops[-1] in prec and prec[ops[-1]] >= prec[typ]:
                rpn.append((ops.pop(), None))
            ops.append(typ)
        elif typ == "LP":
            ops.append("LP")
        elif typ == "RP":
            while ops and ops[-1] != "LP":
                rpn.append((ops.pop(), None))
            if ops and ops[-1] == "LP":
                ops.pop()
    while ops:
        if ops[-1] in prec:
            rpn.append((ops.pop(), None))
        else:
            ops.pop()

    stack: list = []
    for typ, payload in rpn:
        if typ == "LEAF":
            stack.append(payload)
        elif len(stack) >= 2:
            b, a = stack.pop(), stack.pop()
            stack.append({"type": typ.lower(), "operands": _merge(typ.lower(), a, b)})
    if not stack:
        return None
    if len(stack) == 1:
        return stack[0]
    return {"type": "and", "operands": stack}  # bare leaves with no operator


def requirement_json(items: list) -> str:
    tree = requirement_tree(items)
    return json.dumps(tree, ensure_ascii=False) if tree else ""


RE_CD_LEVELS = re.compile(
    r"Levels:\s*(.+?)\s*(?:Schedule Types:|Course Attributes:|Restrictions:|Prerequisites:|$)",
    re.S,
)
RE_CD_SCHED = re.compile(
    r"Schedule Types:\s*(.+?)\s*(?:Course Attributes:|Grade Modes?:|Restrictions:|Prerequisites:|$)",
    re.S,
)
RE_CD_DEPT_TAIL = re.compile(r"\s*[\w/&.\- ]+Department\s*$")
RE_CD_BOILER = re.compile(r"you are following\.", re.I)


def parse_catalog_detail(
    raw_html: str | bytes, subject: str, course_number: str, term_id: str = "",
) -> Optional[CatalogDetail]:
    """Parse bwckctlg.p_disp_course_detail for course-level data (G2).

    Returns None for a course-not-found page (which only carries the bottom
    "Return to Previous" links cell).
    """
    tree = lxml_html.fromstring(raw_html)
    cell = None
    for c in tree.xpath('//td[@class="ntdefault"]'):
        if c.xpath('.//span[@class="fieldlabeltext"]') or "Credit hours" in c.text_content():
            cell = c
            break
    if cell is None:
        return None

    full = RE_WHITESPACE.sub(" ", cell.text_content())

    department = ""
    for t in cell.itertext():
        s = t.strip()
        if s.endswith("Department"):
            department = s
            break

    m = RE_CD_LEVELS.search(full)
    levels = m.group(1).strip() if m else ""
    m = RE_CD_SCHED.search(full)
    sched = m.group(1).strip() if m else ""
    if department and department in sched:
        sched = sched.replace(department, "").strip()
    sched = RE_CD_DEPT_TAIL.sub("", sched).strip()

    attrs = ""
    am = re.search(r"Course Attributes:(.*)", full, re.S)
    if am:
        block = am.group(1)
        b = RE_CD_BOILER.search(block)
        if b:
            block = block[b.end():]
        for stop in ("Restrictions:", "Prerequisites:", "Corequisites:"):
            j = block.find(stop)
            if j >= 0:
                block = block[:j]
        attrs = RE_WHITESPACE.sub(" ", block).strip()

    items, _ = extract_label_sections(
        cell, ("Prerequisites", "Corequisites", "Restrictions")
    )
    return CatalogDetail(
        subject=subject, course_number=course_number, term_id=term_id,
        levels=levels, schedule_types=sched, course_attributes=attrs,
        prerequisites=collapse_items(items["Prerequisites"]),
        corequisites=collapse_items(items["Corequisites"]),
        restrictions=collapse_items(items["Restrictions"]),
    )


def parse_detail_page(
    raw_html: str | bytes, crn: str, term_id: str,
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

    # ── Parse labelled sections (prereqs, coreqs, restrictions) in one pass ──
    items, links = extract_label_sections(
        main_td, ("Prerequisites", "Corequisites", "Restrictions")
    )

    # ── Structured dependency links (prerequisites first, then corequisites) ──
    deps: list[CourseDependency] = []
    for label, dep_type in (
        ("Prerequisites", "prerequisite"),
        ("Corequisites", "corequisite"),
    ):
        for a in links[label]:
            parts = a.text_content().split()
            if len(parts) < 2:
                continue
            grade_m = RE_MIN_GRADE.search((a.tail or "").strip())
            deps.append(CourseDependency(
                crn=crn, term_id=term_id, dep_type=dep_type,
                subject=parts[0], course_number=parts[1],
                minimum_grade=grade_m.group(1) if grade_m else "",
            ))

    detail = SectionDetail(
        crn=crn, term_id=term_id,
        prerequisites=collapse_items(items["Prerequisites"]),
        corequisites=collapse_items(items["Corequisites"]),
        restrictions=collapse_items(items["Restrictions"]),
        waitlist_capacity=waitlist_cap,
        waitlist_actual=waitlist_act,
        waitlist_remaining=waitlist_rem,
        fees=json.dumps(fees_list) if fees_list else "",
        prerequisites_json=requirement_json(items["Prerequisites"]),
        corequisites_json=requirement_json(items["Corequisites"]),
        restrictions_json=parse_restrictions(items["Restrictions"]),
    )

    return detail, deps


# ── Crawl Orchestration ──────────────────────────────────────────────────────


async def run(args: argparse.Namespace):
    """Main crawl pipeline."""
    conn = init_db(args.output, force=args.force)

    # Thread pool for CPU-bound HTML parsing (avoids blocking the event loop)
    parse_pool = concurrent.futures.ThreadPoolExecutor(max_workers=parse_pool_size())
    loop = asyncio.get_running_loop()

    async with make_client(args.workers) as client:
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

            # One flaky term must not abort the whole crawl: collect what we can.
            all_subj_lists = await asyncio.gather(
                *(fetch_subj(s.term_id) for s in semesters),
                return_exceptions=True,
            )

            subj_failures = sum(1 for r in all_subj_lists if isinstance(r, BaseException))
            if subj_failures:
                console.print(f"  [yellow]{subj_failures}[/] term(s) failed subject discovery (continuing)")

            # Deduplicate: keep the first (longest) long_name per code
            seen: dict[str, Subject] = {}
            for subj_list in all_subj_lists:
                if isinstance(subj_list, BaseException):
                    continue
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
        course_limiter = AdaptiveLimiter(start=args.workers, max_limit=args.workers, min_limit=2)
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
                async with course_limiter.slot():
                    if args.delay > 0:
                        await asyncio.sleep(args.delay)
                    params = [("term_in", semester.term_id)] + params_template[1:]
                    resp = await request_with_retry(
                        client, "POST", ENDPOINTS["courses"], form=params,
                        limiter=course_limiter,
                    )
                    # Parse bytes in the thread pool so neither decoding nor
                    # parsing blocks the event loop.
                    return await loop.run_in_executor(
                        parse_pool, parse_courses, resp.content, semester.term_id
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

            # On --resume, don't re-fetch catalog for subjects we already have.
            if args.resume:
                done_subjects = {r[0] for r in conn.execute("SELECT DISTINCT subject FROM catalog")}
                if done_subjects:
                    before = len(all_subjects)
                    all_subjects = [s for s in all_subjects if s not in done_subjects]
                    console.print(
                        f"  Resume: skipping catalog for [yellow]{before - len(all_subjects)}[/] "
                        "subject(s) already present"
                    )

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

        # Catalog detail: one fetch per unique (subject, course_number), using the
        # most recent term it was offered (course-level data is term-stable).
        cd_list: list[tuple[str, str, str]] = []
        if run_catalog:
            course_term: dict[tuple[str, str], str] = {}
            for semester, courses in results:
                for c in courses:
                    k = (c.subject, c.course_number)
                    if k not in course_term or semester.term_id > course_term[k]:
                        course_term[k] = semester.term_id
            for subj, num, mx in conn.execute(
                "SELECT subject, course_number, MAX(term_id) FROM courses "
                "GROUP BY subject, course_number"
            ):
                k = (subj, num)
                if k not in course_term or (mx or "") > course_term[k]:
                    course_term[k] = mx
            if args.resume:
                done = set(conn.execute(
                    "SELECT subject, course_number FROM catalog_detail"
                ).fetchall())
                for k in list(course_term):
                    if k in done:
                        del course_term[k]
            cd_list = sorted((s, n, t) for (s, n), t in course_term.items())

        if not subj_term_list and not crn_term_list and not cd_list:
            console.print("[bold]Phase 4+5:[/] Catalog and details already complete.")
        else:
            # GET endpoints start 429-ing above ~10 workers after Phase 3, so cap
            # the ceiling there and let the limiter back off further if needed.
            detail_workers = min(args.workers, GET_WORKER_CAP)
            get_limiter = AdaptiveLimiter(start=detail_workers, max_limit=detail_workers, min_limit=1)

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
                        async with get_limiter.slot():
                            if args.delay > 0:
                                await asyncio.sleep(args.delay)
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
                                    limiter=get_limiter,
                                )
                                entries = await loop.run_in_executor(
                                    parse_pool, parse_catalog_page, resp.content
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

            # ── Phase 4b: Catalog course detail (attributes, course-level prereqs) ──
            if cd_list:
                console.print(f"[bold]Phase 4b:[/] Fetching {len(cd_list):,} course detail pages ({detail_workers}w)...")
                cd_entries: list[CatalogDetail] = []
                cd_errors = 0

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
                    cd_task = progress.add_task("Catalog detail", total=len(cd_list))

                    async def fetch_cd(subj: str, num: str, term_id: str):
                        nonlocal cd_errors
                        async with get_limiter.slot():
                            if args.delay > 0:
                                await asyncio.sleep(args.delay)
                            try:
                                resp = await request_with_retry(
                                    client, "GET", ENDPOINTS["catalog_detail"],
                                    params={
                                        "cat_term_in": term_id,
                                        "subj_code_in": subj,
                                        "crse_numb_in": num,
                                    },
                                    limiter=get_limiter,
                                )
                                cd = await loop.run_in_executor(
                                    parse_pool, parse_catalog_detail,
                                    resp.content, subj, num, term_id,
                                )
                                if cd is not None:
                                    cd_entries.append(cd)
                            except Exception:
                                cd_errors += 1
                            progress.update(cd_task, advance=1)

                    await asyncio.gather(*(fetch_cd(s, n, t) for s, n, t in cd_list))

                save_catalog_detail(conn, cd_entries)
                cd_count = conn.execute("SELECT COUNT(*) FROM catalog_detail").fetchone()[0]
                console.print(
                    f"  Catalog detail: [cyan]{cd_count:,}[/] courses"
                    + (f", [yellow]{cd_errors}[/] errors" if cd_errors else "")
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
                        async with get_limiter.slot():
                            if args.delay > 0:
                                await asyncio.sleep(args.delay)
                            try:
                                resp = await request_with_retry(
                                    client, "GET", ENDPOINTS["detail"],
                                    params={"term_in": t, "crn_in": c},
                                    limiter=get_limiter,
                                )
                                detail, deps = await loop.run_in_executor(
                                    parse_pool, parse_detail_page, resp.content, c, t,
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
            "section_instructors": conn.execute("SELECT COUNT(*) FROM section_instructors").fetchone()[0],
            "catalog": conn.execute("SELECT COUNT(*) FROM catalog").fetchone()[0],
            "catalog_detail": conn.execute("SELECT COUNT(*) FROM catalog_detail").fetchone()[0],
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
        help=f"Seconds to pause before each request, per worker (default: {DEFAULT_DELAY})",
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
