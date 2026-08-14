<p align="center">
  <h1 align="center">AUSCrawl</h1>
  <p align="center">
    <strong>20 years of AUS course data, one SQLite file.</strong>
  </p>
  <p align="center">
    <a href="https://github.com/DeadPackets/AUSCrawl/blob/master/LICENSE"><img src="https://img.shields.io/github/license/DeadPackets/AUSCrawl?style=flat-square" alt="License"></a>
    <img src="https://img.shields.io/badge/python-3.13+-3776AB?style=flat-square&logo=python&logoColor=white" alt="Python 3.13+">
    <img src="https://img.shields.io/badge/SQLite-003B57?style=flat-square&logo=sqlite&logoColor=white" alt="SQLite">
    <img src="https://img.shields.io/badge/HTTP%2F2-async-blue?style=flat-square" alt="HTTP/2 Async">
    <img src="https://img.shields.io/badge/database-114%20MB-orange?style=flat-square" alt="Database 114 MB">
    <br/>
    <img src="https://img.shields.io/badge/courses-75%2C467-green?style=flat-square" alt="75,467 courses">
    <img src="https://img.shields.io/badge/semesters-101-green?style=flat-square" alt="101 semesters">
    <img src="https://img.shields.io/badge/instructors-1%2C987-green?style=flat-square" alt="1,987 instructors">
    <img src="https://img.shields.io/badge/made%20with-%E2%9D%A4-red?style=flat-square" alt="Made with love">
  </p>
</p>

---

> [!WARNING]
> **Do not run the crawler unless you know what you are doing.** The crawler makes tens of thousands of requests to AUS Banner and can easily overwhelm the server if misconfigured, which can result in service disruption and get you in trouble with the university. A pre-built database (`aus_courses.db`) is already included in this repository with a complete snapshot of all course data since 2005 — **use that instead.**

## What is this?

AUSCrawl is a fast, async web crawler that scrapes [AUS Banner](https://banner.aus.edu/) for course data across **every semester since 2005** and stores it in an SQLite database. But more importantly, it ships a **ready-to-use database** (as a [release](https://github.com/DeadPackets/AUSCrawl/releases/latest) download) so you never have to run the crawler yourself.

Written in Python. Single file. ~15 minutes for a full crawl of 74,000+ course sections, catalog descriptions, prerequisites, and more.

---

## The Database

Every [release](https://github.com/DeadPackets/AUSCrawl/releases/latest) ships **`aus_courses.db`** (gzipped, ~16 MB), a complete SQLite database containing every course, instructor, prerequisite, and catalog description from AUS Banner since Fall 2005. Just download it, `gunzip`, and start building. (It's distributed as a release asset rather than committed to the repo so clones stay small.)

<table>
<tr><th>Table</th><th>Records</th><th>Description</th></tr>
<tr><td><code>courses</code></td><td><strong>75,467</strong></td><td>Every course section ever offered</td></tr>
<tr><td><code>course_dependencies</code></td><td><strong>156,512</strong></td><td>Prerequisite/corequisite links with minimum grades</td></tr>
<tr><td><code>section_details</code></td><td><strong>73,778</strong></td><td>Prerequisites, corequisites, restrictions, waitlist, fees (+ structured JSON)</td></tr>
<tr><td><code>section_instructors</code></td><td><strong>72,476</strong></td><td>Every instructor per section (incl. co-taught), with primary flag</td></tr>
<tr><td><code>catalog</code></td><td><strong>3,046</strong></td><td>Course descriptions, credit/lecture/lab hours</td></tr>
<tr><td><code>catalog_detail</code></td><td><strong>3,532</strong></td><td>Course-level attributes (degree-requirement tags), schedule types, levels</td></tr>
<tr><td><code>instructors</code></td><td><strong>1,987</strong></td><td>All instructors with emails and first appearance</td></tr>
<tr><td><code>semesters</code></td><td><strong>101</strong></td><td>Every term from Fall 2005 to the present</td></tr>
<tr><td><code>subjects</code></td><td><strong>98</strong></td><td>All subject codes (COE, ENG, MTH, etc.)</td></tr>
<tr><td><code>attributes</code></td><td><strong>231</strong></td><td>Course attributes</td></tr>
<tr><td><code>levels</code></td><td><strong>9</strong></td><td>Academic levels (Undergraduate, Graduate, etc.)</td></tr>
</table>

---

### Build Something Cool

This dataset is a goldmine for AUS students. Use it to help your fellow students or sharpen your own skills:

- **Prerequisite visualizer** — build an interactive graph of course dependencies for your major
- **Schedule planner** — help students find open sections that fit their timetable
- **Instructor tracker** — see which professors teach what, and how their assignments changed over the years
- **Course trend analysis** — which courses are offered less frequently? Which departments are growing?
- **Grade requirement explorer** — find every course that requires a minimum grade of C- or higher
- **Data science projects** — 20 years of course data across 98 subjects is a great dataset for learning SQL, pandas, or building dashboards

If you build something with this data, open an issue and let us know — we'd love to see it.

---

### Getting Started

```bash
# Download the latest database (compressed, ~16 MB) from Releases
curl -L -o aus_courses.db.gz \
  https://github.com/DeadPackets/AUSCrawl/releases/latest/download/aus_courses.db.gz
gunzip aus_courses.db.gz

# Open it with sqlite3
sqlite3 aus_courses.db

# Or use Python
python3 -c "
import sqlite3
conn = sqlite3.connect('aus_courses.db')
for row in conn.execute('SELECT term_name, COUNT(*) FROM courses JOIN semesters ON courses.term_id = semesters.term_id GROUP BY courses.term_id ORDER BY courses.term_id DESC LIMIT 5'):
    print(row)
"
```

### Example Queries

```sql
-- All courses taught by a specific instructor
SELECT term_id, subject, course_number, title, days, start_time, end_time
FROM courses WHERE instructor_name LIKE '%Smith%'
ORDER BY term_id DESC;

-- Courses with prerequisites and minimum grades
SELECT d.subject, d.course_number, d.dep_type, d.minimum_grade,
       sd.prerequisites
FROM course_dependencies d
JOIN section_details sd ON sd.crn = d.crn AND sd.term_id = d.term_id
WHERE d.dep_type = 'prerequisite'
GROUP BY d.subject, d.course_number;

-- How many sections per semester
SELECT s.term_name, COUNT(*) as sections
FROM courses c JOIN semesters s ON c.term_id = s.term_id
GROUP BY c.term_id ORDER BY c.term_id;

-- Course catalog with hours breakdown
SELECT subject, course_number, description, credit_hours, lecture_hours, lab_hours
FROM catalog WHERE subject = 'COE';

-- Find all prerequisites for a specific course
SELECT d.subject, d.course_number, d.minimum_grade
FROM course_dependencies d
JOIN courses c ON c.crn = d.crn AND c.term_id = d.term_id
WHERE c.subject = 'COE' AND c.course_number = '390'
GROUP BY d.subject, d.course_number;
```

New in the Banner 9 release:

```sql
-- Sections that still have seats, with real counts rather than a boolean
SELECT subject, course_number, section, enrollment, max_enrollment,
       seats_available_count
FROM courses
WHERE term_id = '202710' AND seats_available_count > 0
ORDER BY subject, course_number;

-- What did CMP 305 require in 2015 versus today?
SELECT term_effective, prerequisites
FROM catalog_versions
WHERE subject = 'CMP' AND course_number = '305'
ORDER BY term_effective;

-- Every course that accepts a placement-test score in place of a prerequisite course
SELECT DISTINCT subject, course_number, test_code, test_score
FROM prereq_rules
WHERE test_code != ''
ORDER BY subject, course_number;

-- The busiest rooms in a term
SELECT building_name, room, COUNT(*) AS blocks
FROM meetings
WHERE term_id = '202710' AND room != ''
GROUP BY building_name, room
ORDER BY blocks DESC
LIMIT 20;

-- The prerequisite expression tree, exactly as Banner stores it
SELECT seq, connector, open_paren, close_paren,
       COALESCE(NULLIF(test_code, ''), req_subject || ' ' || req_course_number) AS req,
       COALESCE(NULLIF(test_score, ''), min_grade) AS threshold
FROM prereq_rules
WHERE subject = 'CMP' AND course_number = '305'
ORDER BY term_effective DESC, seq;
```

---

## Database Schema

16 normalized tables with indexes. Every table and column that existed before Banner 9 is still present and still populated, so queries written against older releases keep working.

**Core tables:**
- `semesters` — term ID and name (e.g. `202620`, `Spring 2026`)
- `subjects` — subject codes and full names (e.g. `COE`, `Computer Engineering`)
- `courses` — every course section with schedule, instructor, classroom, and now **real seat counts** (`enrollment`, `max_enrollment`, `seats_available_count`), `building` / `room` split out, `part_of_term`, and cross-listing
- `instructors` — deduplicated names, emails, `first_seen`, and **Banner IDs**
- `levels` — academic levels (Undergraduate, Graduate, etc.)
- `attributes` — course attributes with `first_seen`

**Extended tables:**
- `meetings` — *new*. One row per meeting block with structured day booleans, `building` / `building_name` / `room`, `start_date` / `end_date`, and `hours_week`. The `courses` columns flatten this; here it is unflattened
- `catalog_versions` — *new*. The full history of every course, keyed `(subject, course_number, term_effective)`. This is what makes "what did CMP 305 require in 2015?" answerable
- `prereq_rules` — *new*. One row per row of Banner's prerequisite table, so the boolean expression is queryable without parsing JSON. Includes **test-score prerequisites** (SAT, placement exams) that the old text parser could not represent at all
- `catalog` — the latest version of each course: description, credit/lecture/lab/other/bill hours, college, department
- `catalog_detail` — latest course-level attributes, schedule types, levels, **grading modes**, and prerequisites/corequisites/restrictions
- `section_details` — per-section prerequisites, corequisites, restrictions, plus `prerequisites_json` (boolean AND/OR trees) and `restrictions_json` (typed include/exclude groups)
- `section_instructors` — every instructor on each section including co-taught ones, with `is_primary` and `banner_id`
- `course_dependencies` — flat prerequisite/corequisite links with minimum grades

---

## Banner Technical Details

AUS runs [Ellucian Banner 9](https://www.ellucian.com/solutions/ellucian-banner) Student Registration Self-Service at `register.aus.edu`, behind Cloudflare. It serves **JSON**, needs no authentication, and covers all 101 terms from Spring 2005 to the present.

> The old Banner 8 OWA endpoints under `banner.aus.edu/axp3b21h/owa/` were removed and now return 404. AUSCrawl 3.0 targets Banner 9 exclusively.

Base URL: `https://register.aus.edu/StudentRegistrationSsb/ssb`

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/classSearch/getTerms` | GET | All term codes and names — stateless |
| `/term/termSelection?mode=…` | GET | Sets the session's search mode |
| `/term/search?mode=…` | POST | **Binds a term to the session** |
| `/searchResults/searchResults` | GET | Sections, 500 per page, with meetings, faculty, seats, attributes |
| `/courseSearchResults/courseSearchResults` | GET | Catalog, 500 per page, descriptions inline |
| `/classSearch/get_subject` · `get_instructor` · `get_attribute` | GET | Reference lists for a term |
| `/courseSearchResults/getPrerequisites` | POST | Prerequisite table (the boolean expression) |
| `/courseSearchResults/getCorequisites` · `getRestrictions` · `getCourseAttributes` | POST | Catalog fragments |
| `/courseSearchResults/getCourseCatalogDetails` | POST | Levels, grading modes, schedule types |

`pageMaxSize` silently clamps to **500** — asking for more just wastes the round trip.

### Two behaviours that will corrupt data if ignored

**The search endpoints are session-stateful and ignore `txt_term`.** The term comes from `POST /term/search`, not the query string. Bind Fall 2026 and ask for Spring 2015 and the server answers **HTTP 200 with Fall 2026 data**, or with an empty result set — never an error. AUSCrawl verifies every record's `term` against the bound term and refuses to record an empty term without rebinding first. Term-level parallelism therefore comes from a pool of independent sessions, never from concurrent requests on one session.

**The detail endpoints are stateless.** They take `term` in the POST body and work on a cold client, so they parallelize freely. A 500 from them means "no such course in that term" — permanent, not transient.

### Rate limiting

Measured headroom is high: 40 requests at concurrency 16 completed at ~174 req/s with zero 429s. **Headroom is not permission.** The default target is **10 req/s**, paced by a global token-bucket limiter with AIMD backoff, which puts a full 101-term crawl at roughly an hour. Neither `register.aus.edu` nor `banner.aus.edu` serves a `robots.txt`, so the limit is a self-imposed courtesy.

### Known gaps

Two fields the Banner 8 scraper collected have **no equivalent anywhere in Banner 9**:

- `registration_dates` — no endpoint exposes it (`getRegistrationDates` and friends all 404). Existing values in the shipped database are preserved; the crawler never writes an empty string over them.
- Section-title suffixes — Banner 8 showed titles like `Calculus III (Take it with MTH 203R Sec.1)`. Banner 9 returns only the catalog title. Historical rows keep their richer titles; new rows get the catalog title.

One field is now **fixed rather than lost**: `schedule_type` used to hold the literal string `"Schedule Type"` for every row — the old parser captured the column label instead of the value. It now holds the real value (`Lecture`, `Lab`, …).

Unlike Banner 8, Banner 9 **does** expose actual enrollment and seat counts.

---

## Crawler Documentation

> [!CAUTION]
> Only run the crawler if you need fresher data than what's in the included database. Be aware that aggressive crawling can take down AUS Banner and result in your IP being banned. The default settings are tuned to be safe, but modifying worker counts or running multiple instances simultaneously can cause problems.

<details>
<summary><strong>Click to expand crawler docs</strong></summary>

### Requirements

Python 3.13+ and [uv](https://docs.astral.sh/uv/).

### Usage

```
uv run python crawl.py [options]
```

| Flag | Description |
|------|-------------|
| `-o`, `--output` | SQLite output path (default: `aus_data.db`) |
| `-t`, `--terms` | Only crawl specific term IDs (e.g. `202620 202510`) |
| `--rate` | Target requests/sec, AIMD-paced (default: 10, ceiling 30). Lower for extra safety |
| `--latest` | Only crawl the most recent semester |
| `--resume` | Skip semesters and course versions already in the database |
| `--force` | Delete the database and start over |
| `--no-catalog` | Skip the catalog phase (and details, which depend on it) |
| `--no-details` | Skip the per-course detail phase |
| `-v`, `--verbose` | Debug-level logging |

### How It Works

Five phases, roughly **41,000 requests** for all 101 terms — down from ~95,000 under Banner 8:

| Phase | Requests | What it does |
|---|---|---|
| 1. Terms | 1 | `getTerms` → 101 terms |
| 2. Reference | ~200 | Subject and attribute lists per term |
| 3. Sections | ~250 | Pool of sessions; per term, bind + `ceil(n/500)` pages |
| 4. Catalog | ~450 | Same pattern; descriptions arrive inline |
| 5. Details | ~40,000 | 5 stateless POSTs per unique `(subject, course#, term_effective)` |

Phase 5 dominates, and it is only affordable because details are fetched **per course version** rather than per section — roughly 8,000 versions instead of 74,000 sections.

### Technical Details

- **Async HTTP/2** via `httpx`, connection pooling, jittered exponential backoff (equal-jitter, so retries don't synchronize), `Retry-After` honored
- **Global token-bucket limiter with AIMD** — paces request *starts*, so throughput is decoupled from worker count. A 429/503/challenge halves the rate; sustained success climbs back
- **Session pool** — the search endpoints are stateful, so each in-flight term gets its own cookie jar, and every page is verified against the bound term. The pool exists for *correctness*, not speed: throughput is set by the rate limiter, and the detail phase (90% of all requests) is stateless and already fully parallel on a single session. More sessions would not go faster, and one IP minting many sessions is itself a bot signal
- **Stable browser fingerprint** — one current Chrome identity with matching `Sec-Fetch-*` and `Sec-CH-UA` headers. User-agent *rotation* is itself a detection signal, so it is deliberately avoided
- **Failures recover at the right layer** — `request_with_retry` retries a request, `fetch_all_pages` retries the whole *term* (reset, rebind, 3 attempts) because a failed bind cannot be fixed by repeating the same GET, and `run_terms` keeps the successful terms when one fails. An hour of crawling is never discarded for one glitchy term; the run exits non-zero and `--resume` fills the gaps
- **Graceful degradation** — a course version whose fragment Banner refuses to serve is recorded as incomplete and reported at the end, never allowed to abort the crawl
- **Crash resilience** — each phase commits as it finishes; the detail phase batch-saves every 2,000 courses; `--resume` skips completed work
- **Additive migration and in-place refresh** — pointing `-o` at an existing database upgrades it with `ALTER TABLE` and then *refreshes* existing rows rather than skipping them, so the shipped snapshot gains every Banner 9 column while keeping its rows, its `registration_dates`, and its richer Banner 8 section titles

### Tests

```bash
uv run --project . pytest           # offline, fixture-driven
uv run --project . pytest -m live   # hits the real Banner server
```

The live suite is the canary: it fails if Banner changes the prerequisite table shape, stops ignoring `txt_term`, or alters the section payload.

</details>

---

<p align="center">
  <sub>Built for AUS students, by an AUS student.</sub>
  <br/>
  <a href="https://github.com/DeadPackets/AUSCrawl/blob/master/LICENSE">MIT License</a>
</p>
