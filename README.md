# AUSCrawl

A fast, async web crawler that scrapes [AUS Banner](https://banner.aus.edu/) for course data across **every semester since 2005** and stores it in an SQLite database.

Written in Python. Single file. ~14 minutes for a full crawl of 74,000+ course sections, catalog descriptions, prerequisites, and more.

## What it collects

AUSCrawl hits three Banner endpoints to build a comprehensive dataset:

| Phase | Source | Data |
|-------|--------|------|
| **Courses** | Schedule listing | CRN, subject, title, section, credits, schedule type, times, days, classroom, instructor, seat availability |
| **Catalog** | Course catalog | Description, credit/lecture/lab hours, department |
| **Details** | Section detail | Prerequisites, corequisites, restrictions, waitlist info, fees, structured dependency links with minimum grades |

All data is stored across **10 normalized tables** in SQLite with proper indexes for fast querying.

## Quick start

Requires Python 3.13+ and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/DeadPackets/AUSCrawl
cd AUSCrawl
uv run python crawl.py
```

That's it. No `pip install`, no virtual env setup — `uv` handles everything.

## Usage

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

### Examples

```bash
# Full crawl (all semesters, all data)
uv run python crawl.py --force

# Just the latest semester, skip slow detail scraping
uv run python crawl.py --latest --no-details

# Resume an interrupted crawl
uv run python crawl.py --resume

# Specific semesters only
uv run python crawl.py -t 202620 202510
```

## Database schema

The output SQLite database contains 10 tables:

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

### Example queries

```sql
-- All courses taught by a specific instructor
SELECT term_id, subject, course_number, title, days, start_time, end_time
FROM courses WHERE instructor_name LIKE '%Smith%'
ORDER BY term_id DESC;

-- Courses with prerequisites
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
```

## How it works

The crawler runs in 5 phases:

1. **Semester discovery** — fetches the list of all available terms from Banner's dropdown
2. **Subject catalog** — fetches subject codes from every semester and deduplicates (the dropdown varies per term)
3. **Course scraping** — POSTs to the schedule search endpoint for every semester with all subjects in a single batch, then parses the HTML response with lxml (50 concurrent workers)
4. **Catalog scraping** — GETs course catalog pages for a sample of 6 evenly-spaced terms to collect descriptions, hours, and departments (10 concurrent workers)
5. **Detail scraping** — GETs the section detail page for every unique CRN/term pair to extract prerequisites, corequisites, restrictions, waitlist info, and fees (10 concurrent workers)

### Performance

A full crawl typically produces:
- ~74,000 course sections across 98 semesters
- ~4,100 catalog entries
- ~72,000 section details
- ~153,000 dependency records
- ~73 MB database

Total runtime: **~14 minutes**.

### Technical details

- **Async HTTP/2** via `httpx` with connection pooling and automatic retry with exponential backoff
- **lxml** for HTML parsing (12x faster than BeautifulSoup)
- **ThreadPoolExecutor** offloads CPU-bound parsing from the async event loop
- **Catalog sampling** reduces catalog requests by ~80% while maintaining full course coverage
- **Cloudflare email protection** decoding (XOR-obfuscated instructor emails)
- **Crash resilience** — each phase saves to DB immediately; detail phase does periodic batch saves every 5,000 entries; `--resume` skips completed work
- **Rate-limit aware** — respects server 429 responses with differentiated backoff; GET endpoints capped at 10 workers to avoid triggering bans

## License

[MIT](LICENSE)
