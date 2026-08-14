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

uv run --project . python crawl.py --latest          # only the newest term (cheapest real run)
uv run --project . python crawl.py -t 202620 202510  # specific term IDs
uv run --project . python crawl.py --resume          # skip terms already in the DB
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
  parse_html.py  the five HTML detail fragments -> models
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
4. **Catalog** — same pattern against `courseSearchResults`. **Descriptions arrive inline**, so there is no separate description phase.
5. **Details** — 5 stateless POSTs per unique `(subject, course_number, term_effective)`, one shared session, high parallelism. This is ~40,000 of the ~41,000 total requests.

### Cross-cutting design points

- **The search endpoints are session-stateful and `txt_term` is decorative.** The term comes from `POST /term/search`. Bind Fall 2026, ask for Spring 2015, and you get HTTP 200 with Fall 2026 data — or an empty result set. Both are silent. `verify_term` checks every record's `term` against the bound term, and `fetch_all_pages` rebinds once before refusing to record an empty term. **One session may have only one term in flight**, which is why parallelism comes from `SessionPool` rather than concurrent requests on one client. Do not "optimize" this away.
- **The detail endpoints are stateless** — they take `term` in the POST body and work on a cold client. A 500 from them means "no such course in that term", which is permanent; `DETAIL_RETRIES` is 2, and a fragment that will not load is recorded in `CourseDetail.missing_parts` and reported at the end rather than aborting the crawl.
- **Rate-limited, not concurrency-limited.** A global token-bucket `RateLimiter` paces request *starts* at `--rate` (default 10/s) with AIMD: a 429/503/challenge halves the rate, sustained success climbs back toward `MAX_RATE`. Worker count therefore does not change the load on Banner. Measured headroom is ~174 req/s with zero 429s — the low default is a deliberate courtesy, not a limit.
- **`request_with_retry` is the single choke point for all HTTP.** It retries only 403/408/429/5xx (permanent 4xx fails fast), uses equal-jitter exponential backoff, honors `Retry-After`, and detects Cloudflare/F5 interstitials via `is_blocked`. New endpoints go through it.
- **Banner 9 HTML-escapes text inside its JSON.** `courseTitle` arrives as `Qur&#39;an`. Every text field goes through `_txt()` in `parse_json.py`. Forgetting this on a new field is a silent data bug.
- **Legacy column formats are contractual.** `days` is `MW`/`TR` (R = Thursday, U = Sunday), `start_time` is `11:00 am`, `classroom` is `Building Name Room` or `TBA`, `date_range` is `Aug 24, 2026 - Dec 10, 2026`. `parse_json.py` has a helper per format and `tests/test_db_save.py` pins them. Changing one breaks every published query.
- **Chronological `first_seen`.** `save_sections` sorts by `term_id` before inserting so `INSERT OR IGNORE` keeps the earliest occurrence. `fix_first_seen` backfills `subjects` and `instructors` from a `MIN(term_id)`.
- **Migration is additive only.** `init_db` runs `migrate_schema` (ALTER TABLE ADD COLUMN, idempotent) before `CREATE TABLE IF NOT EXISTS`, so pointing `-o` at a copy of `aus_courses.db` upgrades it in place with every row preserved.
- **Saves upsert, they do not `INSERT OR IGNORE`.** Upgrading the shipped snapshot is the documented path, and ignoring conflicts would leave every Banner 9 column empty on the 75,000 rows that already exist — a bug that unit tests missed and only a full-scale run exposed. `_COURSE_UPSERT` refreshes everything Banner serves, with two deliberate exceptions: `registration_dates` (no source) and any stored `title` longer than the incoming one (Banner 8 section-title suffixes). `first_seen` is never updated, which is what keeps the term-ordered insert correct. `save_catalog` updates only `_CATALOG_UPDATE_COLS` so it cannot clobber the detail columns that `save_course_details` owns.
- **Stable browser fingerprint.** One current Chrome UA with matching `Sec-Fetch-*` / `Sec-CH-UA` / `Referer`. Do **not** add user-agent rotation — inconsistent identities are more anomalous than one consistent one. The same reasoning caps the session pool: a real browser is one session, so many short-lived sessions from one IP is a bot signal. `SESSION_POOL_SIZE` is sized for correctness (terms cannot share a session), not throughput.
- **Failures recover at the right layer.** `request_with_retry` retries a request; `fetch_all_pages` retries the whole *term* (`resetDataForm`, rebind, 3 attempts with backoff) because a bind that did not take cannot be fixed by repeating the same GET; `run_terms` keeps the successful terms when one fails and the CLI exits non-zero. An hour of crawling is never discarded for one glitchy term, and nothing fails silently.

## Schema notes

16 tables defined inline in the `SCHEMA` string in `db.py`. `courses` is the fact table; uniqueness is `(crn, term_id, class_type, days, start_time)` so a section with several meeting blocks produces several rows. `term_id` (e.g. `202620` = Spring 2026) is the join key across most tables.

- `meetings(crn, term_id, meeting_index, …)` — the unflattened form of the schedule columns in `courses`.
- `catalog_versions(subject, course_number, term_effective, …)` — full course history. `catalog` and `catalog_detail` are projections of its newest row per course, refreshed by `refresh_flat_catalog` / `refresh_catalog_detail`, so a re-crawl can only move them forward.
- `prereq_rules(subject, course_number, term_effective, seq, …)` — one row per row of Banner's prerequisite table, including test-score prerequisites. `catalog_versions.prerequisites_json` holds the same thing as a boolean tree built by `parse_html.prereq_tree`.

### Known gaps

- `registration_dates` has no Banner 9 source. The crawler never writes over existing values; new terms leave it empty.
- Section-title suffixes (`Calculus III (Take it with MTH 203R Sec.1)`) are not exposed by Banner 9. Historical rows keep them.
- `schedule_type` used to hold the literal string `"Schedule Type"` — an old parser bug. It now holds the real value.

**Refreshing the shipped DB:** copy `aus_courses.db`, run `uv run --project . python crawl.py -o <copy>` (~1 hour for all 101 terms at the default rate), cross-check a few terms with `scripts/crosscheck.py`, then checkpoint (`PRAGMA wal_checkpoint(TRUNCATE)` + `journal_mode=DELETE`) and swap the file in.

The Banner 9 endpoint reference lives in README.md under "Banner Technical Details"; the design rationale is in `docs/superpowers/specs/2026-08-14-banner9-rewrite-design.md`.
