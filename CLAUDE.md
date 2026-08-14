# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

An async crawler for **Ellucian Banner 9** (`register.aus.edu/StudentRegistrationSsb/ssb`) that pulls ~21 years of AUS course data into SQLite, plus a **pre-built database** (`aus_courses.db`, ~115 MB, shipped via GitHub Releases) that is the actual product. Most work here is querying or building on top of `aus_courses.db`, **not** running the crawler.

The code lives in the `auscrawl/` package; `crawl.py` is a thin shim so the documented commands still work.

> The crawler hits a live university server. Default settings are tuned to be polite (10 req/s); raising `--rate` or running several instances is not. Treat any crawler run as outward-facing and confirm intent before executing.

> **Banner 8 is gone.** The old OWA endpoints under `banner.aus.edu/axp3b21h/owa/` return 404. Anything referencing `bwckschd`, `bwckctlg`, `p_get_crse_unsec`, Cloudflare email obfuscation, or WAF body-size batching describes a system that no longer exists.

## Commands

Requires Python 3.13+ and [`uv`](https://docs.astral.sh/uv/).

```bash
# Run the crawler (writes to aus_data.db by default — NOT the shipped aus_courses.db)
uv run --project . python crawl.py [options]

uv run --project . python crawl.py --latest          # refresh the newest term (~10k requests, ~5 min)
uv run --project . python crawl.py -t 202620 202510  # specific term IDs
uv run --project . python crawl.py --resume          # skip terms already in the DB
uv run --project . python crawl.py --import-legacy aus_courses.db  # Banner 8 leftovers
uv run --project . python crawl.py --force           # delete the DB and start over
uv run --project . python crawl.py --no-details      # skip the expensive phase 5
uv run --project . python crawl.py --rate 5          # go gentler than the default 10 req/s

# Tests
uv run --project . pytest            # offline, fixture-driven (~100 tests)
uv run --project . pytest -m live    # hits the real Banner server

# Re-capture fixtures after a Banner change
uv run --project . python tests/capture_banner9.py

# Compare a crawl against the shipped database
uv run --project . python scripts/crosscheck.py <new.db> aus_courses.db <term_id>

# Query the shipped database
sqlite3 aus_courses.db
```

`-o/--output` defaults to `aus_data.db`; the shipped snapshot is `aus_courses.db`. They are intentionally separate so a crawl never clobbers the shipped DB unless you point `-o` at it. `*.db` is gitignored except `aus_courses.db`.

## Architecture

```
auscrawl/
  config.py      endpoints, tuning constants, the browser header profile
  models.py      dataclasses: Semester, Section, Meeting, CatalogCourse, PrereqRule, CourseDetail
  http.py        RateLimiter (token bucket + AIMD), request_with_retry, make_client, is_blocked
  session.py     TermSession + SessionPool + verify_term — the stateful bind
  parse_json.py  section/catalog JSON -> models, plus the legacy-format helpers
  parse_html.py  the six HTML detail fragments -> models
  fetch.py       one coroutine per endpoint; all network access lives here
  db.py          SCHEMA, additive migration, bulk saves
  pipeline.py    the five phases and run()
  cli.py         argument parsing and entry point
```

Parsers are pure functions over `str | bytes` and do no I/O, so every parser test runs offline against real captured bytes in `tests/fixtures/banner9/`.

### The five phases

1. **Terms** — `getTerms`, 1 stateless request → 101 terms.
2. **Reference** — `get_subject` and `get_attribute` per term.
3. **Sections** — a pool of sessions; per term, bind then page `searchResults` at 500/page.
4. **Catalog** — same pattern against `courseSearchResults`. Descriptions arrive inline but **truncated to 100 characters** — the full text comes from the `getCourseDescription` fragment in phase 5, and `save_catalog` never overwrites a description on conflict so a revisit cannot clobber the full text.
5. **Details** — up to 6 stateless POSTs per unique `(subject, course_number, term_effective)` (`getCourseDescription` is skipped when the inline copy was null), one shared session, high parallelism. This is ~40,000 of the ~42,000 total requests.

### Cross-cutting design points

- **The search endpoints are session-stateful and `txt_term` is decorative.** The term comes from `POST /term/search`. Bind Fall 2026, ask for Spring 2015, and you get HTTP 200 with Fall 2026 data — or an empty result set. Both are silent. `verify_term` checks every record's `term` against the bound term, and `fetch_all_pages` rebinds once before refusing to record an empty term. **One session may have only one term in flight**, which is why parallelism comes from `SessionPool` rather than concurrent requests on one client. Do not "optimize" this away.
- **The detail endpoints are stateless** — they take `term` in the POST body and work on a cold client. A 500 from them means "no such course in that term", which is permanent; `DETAIL_RETRIES` is 2, and a fragment that will not load is recorded in `CourseDetail.missing_parts` and reported at the end rather than aborting the crawl.
- **Rate-limited, not concurrency-limited.** A global token-bucket `RateLimiter` paces request *starts* at `--rate` (default 10/s) with AIMD: a 429/503/challenge halves the rate, sustained success climbs back toward `MAX_RATE`. Worker count therefore does not change the load on Banner. Measured headroom is ~174 req/s with zero 429s — the low default is a deliberate courtesy, not a limit.
- **`request_with_retry` is the single choke point for all HTTP.** It retries only 403/408/429/5xx (permanent 4xx fails fast), uses equal-jitter exponential backoff, honors `Retry-After`, and detects Cloudflare/F5 interstitials via `is_blocked`. New endpoints go through it.
- **Banner 9 HTML-escapes text inside its JSON.** `courseTitle` arrives as `Qur&#39;an`. Every text field goes through `_txt()` in `parse_json.py`. Forgetting this on a new field is a silent data bug.
- **Legacy column formats are contractual.** `days` is `MW`/`TR` (R = Thursday, U = Sunday), `start_time` is `11:00 am`, `classroom` is `Building Name Room` or `TBA`, `date_range` is `Aug 24, 2026 - Dec 10, 2026`. `parse_json.py` has a helper per format and `tests/test_db_save.py` pins them. Changing one breaks every published query.
- **Chronological `first_seen`.** `save_sections` sorts by `term_id` before inserting, so the first write of an instructor or subject is its earliest occurrence and the upserts never touch `first_seen`. `fix_first_seen` backfills from a `MIN(term_id)` afterwards.
- **Saves upsert; they never `INSERT OR IGNORE`.** Ignoring conflicts silently skipped every pre-existing row in an earlier draft, so a re-crawl left the new columns empty — a bug unit tests on an empty database could not see. `save_catalog` updates only the columns the catalog search owns so it cannot reset the detail columns `save_course_details` writes, and `save_sections` deletes meeting blocks beyond the current count so a shrunken schedule leaves no ghosts.
- **Stable browser fingerprint.** One current Chrome UA with matching `Sec-Fetch-*` / `Sec-CH-UA` / `Referer`. Do **not** add user-agent rotation — inconsistent identities are more anomalous than one consistent one. The same reasoning caps the session pool: a real browser is one session, so many short-lived sessions from one IP is a bot signal. `SESSION_POOL_SIZE` is sized for correctness (terms cannot share a session), not throughput.
- **Failures recover at the right layer.** `request_with_retry` retries a request; `fetch_all_pages` retries the whole *term* (`resetDataForm`, rebind, 3 attempts with backoff) because a bind that did not take cannot be fixed by repeating the same GET; `run_terms` keeps the successful terms when one fails and the CLI exits non-zero. An hour of crawling is never discarded for one glitchy term, and nothing fails silently.

## Schema notes

11 tables plus 7 views, all defined inline in the `SCHEMA` string in `db.py`. The tables model what Banner 9 serves; the views carry the Banner 8 table names.

- `sections(crn, term_id)` is the section fact table — **one row per section**, so a changed room or time updates it instead of leaving a duplicate behind. `meetings(crn, term_id, meeting_index)` holds the schedule blocks.
- `course_versions(subject, course_number, term_effective)` is the catalog fact table: one row per version of a course, carrying description, hours, levels, grading modes and the prerequisite text and JSON. `prereq_rules` holds Banner's prerequisite table row by row, including test-score prerequisites.
- `instructors` is keyed by `banner_id`, which Banner 9 supplies for every faculty entry and is 1:1 with names.

### The compatibility views

`courses`, `catalog`, `catalog_detail`, `section_details`, `course_dependencies`, `semesters` and `levels` are **views**, not tables. They reproduce the old column shapes and value formats — `MW` days, `11:00 am` times, `Building Name Room`, `TBA` for an unassigned room or instructor, `is_lab` by exact equality with `'Lab'`.

They must stay views. An earlier draft of this rewrite kept them as real tables, and `course_dependencies`, `section_details` and `levels` were never written by the Banner 9 crawler — 230,000 rows that would have gone on returning confident answers frozen at the last Banner 8 crawl. A breaking change fails loudly; that failed quietly. **If you add a column to a view, do not "optimize" it into a table.**

The legacy value formats live in `_time_12h`, `_date_long`, `_DAYS` and `_CLASSROOM` at the top of `db.py`, and `tests/test_schema.py` pins each one. The whole `courses` view is verified against the shipped Banner 8 database for Fall 2015: 1,694 rows, exact key-for-key match.

### Known gaps

- `registration_dates` and Banner 8 section-title suffixes exist in no Banner 9 endpoint. `--import-legacy <old.db>` copies them once into `legacy_section_extras`, which the `courses` view joins. Without that import both are empty.
- `courses.schedule_type` used to hold the literal string `"Schedule Type"` for every row — an old parser bug capturing the label instead of the value. It now holds the real value.
- `section_details.fees` and `corequisites_json` are always empty: AUS publishes no fee data and Banner 9 gives corequisites as prose, not a table.

**Refreshing the shipped DB:** for a routine refresh, run `--latest` against the existing file — it re-crawls the newest term and re-fetches details for the course versions live in it (Banner amends live versions in place; frozen history is never re-crawled). ~10,000 requests, ~5 minutes. For a full rebuild, crawl into a fresh file with `--import-legacy aus_courses.db` (~25 minutes for all 101 terms as AIMD climbs from the default 10 req/s), cross-check a few terms with `scripts/crosscheck.py`, then checkpoint (`PRAGMA wal_checkpoint(TRUNCATE)` + `journal_mode=DELETE`) and swap the file in.

The Banner 9 endpoint reference lives in README.md under "Banner Technical Details"; the design rationale is in `docs/superpowers/specs/2026-08-14-banner9-rewrite-design.md`.
