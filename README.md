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
    <img src="https://img.shields.io/github/repo-size/DeadPackets/AUSCrawl?style=flat-square&label=repo%20size" alt="Repo Size">
    <br/>
    <img src="https://img.shields.io/badge/courses-73%2C418-green?style=flat-square" alt="73,418 courses">
    <img src="https://img.shields.io/badge/semesters-98-green?style=flat-square" alt="98 semesters">
    <img src="https://img.shields.io/badge/dependencies-152%2C968-green?style=flat-square" alt="152,968 dependencies">
    <img src="https://img.shields.io/badge/made%20with-%E2%9D%A4-red?style=flat-square" alt="Made with love">
  </p>
</p>

---

> [!WARNING]
> **Do not run the crawler unless you know what you are doing.** The crawler makes tens of thousands of requests to AUS Banner and can easily overwhelm the server if misconfigured, which can result in service disruption and get you in trouble with the university. A pre-built database (`aus_courses.db`) is already included in this repository with a complete snapshot of all course data since 2005 — **use that instead.**

## What is this?

AUSCrawl is a fast, async web crawler that scrapes [AUS Banner](https://banner.aus.edu/) for course data across **every semester since 2005** and stores it in an SQLite database. But more importantly, this repo ships a **ready-to-use database** so you never have to run the crawler yourself.

Written in Python. Single file. ~15 minutes for a full crawl of 74,000+ course sections, catalog descriptions, prerequisites, and more.

---

## The Database

This repository includes **`aus_courses.db`**, a complete SQLite database containing every course, instructor, prerequisite, and catalog description from AUS Banner since Fall 2005. Just download it and start building.

<table>
<tr><th>Table</th><th>Records</th><th>Description</th></tr>
<tr><td><code>courses</code></td><td><strong>73,418</strong></td><td>Every course section ever offered</td></tr>
<tr><td><code>course_dependencies</code></td><td><strong>152,968</strong></td><td>Prerequisite/corequisite links with minimum grades</td></tr>
<tr><td><code>section_details</code></td><td><strong>71,754</strong></td><td>Prerequisites, corequisites, restrictions, waitlist, fees</td></tr>
<tr><td><code>catalog</code></td><td><strong>3,007</strong></td><td>Course descriptions, credit/lecture/lab hours</td></tr>
<tr><td><code>instructors</code></td><td><strong>1,649</strong></td><td>All instructors with emails and first appearance</td></tr>
<tr><td><code>semesters</code></td><td><strong>98</strong></td><td>Every term from Fall 2005 to the present</td></tr>
<tr><td><code>subjects</code></td><td><strong>98</strong></td><td>All subject codes (COE, ENG, MTH, etc.)</td></tr>
<tr><td><code>attributes</code></td><td><strong>225</strong></td><td>Course attributes</td></tr>
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
# Clone the repo — the database is included
git clone https://github.com/DeadPackets/AUSCrawl
cd AUSCrawl

# Open it directly with sqlite3
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

---

## Database Schema

The SQLite database contains 10 normalized tables with proper indexes:

**Core tables:**
- `semesters` — term ID and name (e.g. `202620`, `Fall 2025`)
- `subjects` — subject codes and full names (e.g. `COE`, `Computer Engineering`)
- `courses` — every course section with schedule, instructor, classroom, etc.
- `instructors` — deduplicated instructor names and emails with `first_seen`
- `levels` — academic levels (Undergraduate, Graduate, etc.)
- `attributes` — course attributes with `first_seen`

**Extended tables:**
- `catalog` — course descriptions, credit/lecture/lab hours, department
- `section_details` — prerequisites, corequisites, restrictions, waitlist, fees per section
- `course_dependencies` — structured prerequisite/corequisite links with minimum grade requirements

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
| `-w`, `--workers` | Max concurrent requests (default: 50) |
| `--delay` | Seconds between requests (default: 0) |
| `--latest` | Only crawl the most recent semester |
| `--resume` | Skip semesters already in the database |
| `--force` | Drop and recreate all tables |
| `--no-catalog` | Skip catalog description scraping |
| `--no-details` | Skip section detail scraping |
| `-v`, `--verbose` | Debug-level logging |

### How It Works

The crawler runs in 5 phases:

1. **Semester discovery** — fetches the list of all available terms from Banner's dropdown
2. **Subject catalog** — fetches subject codes from every semester and deduplicates (the dropdown varies per term)
3. **Course scraping** — POSTs to the schedule search endpoint for every semester with all subjects in a single batch, then parses the HTML response with lxml (50 concurrent workers)
4. **Catalog scraping** — GETs course catalog pages for a sample of 6 evenly-spaced terms to collect descriptions, hours, and departments (10 concurrent workers)
5. **Detail scraping** — GETs the section detail page for every unique CRN/term pair to extract prerequisites, corequisites, restrictions, waitlist info, and fees (10 concurrent workers)

### Technical Details

- **Async HTTP/2** via `httpx` with connection pooling and automatic retry with exponential backoff
- **lxml** for HTML parsing (12x faster than BeautifulSoup)
- **ThreadPoolExecutor** offloads CPU-bound parsing from the async event loop
- **Catalog sampling** reduces catalog requests by ~80% while maintaining full course coverage
- **Cloudflare email protection** decoding (XOR-obfuscated instructor emails)
- **Crash resilience** — each phase saves to DB immediately; detail phase does periodic batch saves every 5,000 entries; `--resume` skips completed work
- **Rate-limit aware** — respects server 429 responses with exponential backoff; GET endpoints capped at 10 workers to avoid triggering bans

</details>

---

<p align="center">
  <sub>Built for AUS students, by an AUS student.</sub>
  <br/>
  <a href="https://github.com/DeadPackets/AUSCrawl/blob/master/LICENSE">MIT License</a>
</p>
