# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A single-file async web crawler (`crawl.py`) that scrapes AUS Banner for ~20 years of course data into SQLite, plus a **pre-built, committed database** (`aus_courses.db`, ~74 MB) that is the actual product. Most work here is querying or building on top of `aus_courses.db`, **not** running the crawler.

> The crawler hits a live university server (`banner.aus.edu`) with tens of thousands of requests. Do not run it casually — it can overwhelm Banner and get the source IP banned. Default settings are tuned to be safe; raising `-w` or running multiple instances is not. Treat any crawler run as outward-facing and confirm intent before executing.

## Commands

Requires Python 3.13+ and [`uv`](https://docs.astral.sh/uv/). There is no test suite, linter, or build step.

```bash
# Run the crawler (writes to aus_data.db by default — NOT the shipped aus_courses.db)
uv run python crawl.py [options]

uv run python crawl.py --latest          # only the newest semester (cheapest real run)
uv run python crawl.py -t 202620 202510  # specific term IDs
uv run python crawl.py --resume          # skip semesters already in the DB
uv run python crawl.py --force           # drop & recreate all tables
uv run python crawl.py --no-catalog --no-details   # Phase 3 only (skip GET-heavy phases)

# Query the shipped database
sqlite3 aus_courses.db
```

`-o/--output` defaults to `aus_data.db`; the committed snapshot is `aus_courses.db`. They are intentionally separate so a crawl never clobbers the shipped DB unless you point `-o` at it. `*.db` is gitignored except `aus_courses.db`.

## Architecture

Everything lives in `crawl.py`, organized top-to-bottom by section comment banners: constants/regexes → dataclasses → DB layer → HTTP layer → fetchers → HTML parsers → `run()` orchestration → CLI. The crawl is a 5-phase async pipeline driven by `run()`:

1. **Semesters** — GET the term dropdown, parse `<OPTION>`s into `Semester`s.
2. **Subjects** — build the complete subject list. **Fast path:** if the target DB already has subjects and `--force` is not set, reuse them. **Fresh path:** fetch subjects from *every* semester concurrently and dedupe, because Banner's subject dropdown varies per term.
3. **Courses** — for each semester, POST all subject codes to the schedule-search endpoint and parse the returned HTML tables. Subjects are split into batches of 250 to stay under Banner's ~4500-byte WAF body limit; all batches for a semester fire concurrently. Runs at `-w` workers (default 50). Saved immediately as a crash-safe checkpoint.
4. **Catalog** — GET catalog descriptions/hours. Only **6 evenly-spaced sample terms** are crawled (course descriptions barely change over time), cutting ~80% of requests.
5. **Details** — GET per-section prerequisites, corequisites, restrictions, waitlist, and fees for every unique `(crn, term_id)`. Also extracts structured `course_dependencies` rows with minimum grades.

Cross-cutting design points that span multiple functions:

- **Two worker tiers + adaptive backoff.** The POST course-search endpoint tolerates `-w` (50) concurrent requests; the GET endpoints (catalog + details) are hard-capped at `min(-w, GET_WORKER_CAP=10)` because they start returning 429s above that, especially right after Phase 3. Don't lift this cap. Within each tier, an `AdaptiveLimiter` (AIMD) gates concurrency: a 429/503/WAF block halves the live limit, and each success nudges it back up by `1/limit` (one step per window) so it settles near the sustainable rate instead of snapping back to the ceiling and re-triggering the limit.
- **Async I/O + thread-pool parsing.** `httpx` (HTTP/2, connection pooling, via `make_client`) does the I/O; the response **bytes** (`resp.content`, not `resp.text`) are handed to a `ThreadPoolExecutor` (`parse_pool_size()`, CPU-scaled) via `loop.run_in_executor`, so both charset decoding and lxml parsing happen off the event loop. Parser functions (`parse_courses`, `parse_catalog_page`, `parse_detail_page`) are pure and accept `str | bytes`.
- **`request_with_retry`** is the single choke point for all HTTP. It retries only *recoverable* statuses (`RETRYABLE_STATUS` = 403/408/429/5xx — permanent 4xx like 404/400 fail fast), uses jittered exponential backoff (`backoff_delay`, equal-jitter so the fleet doesn't retry in lockstep), detects Cloudflare WAF blocks via `is_waf_block` (scans only the first 64 KB of the body), and feeds 429/503/WAF signals back to the optional `limiter`. New endpoints should go through it; pass the phase's limiter for adaptive feedback.
- **Chronological `first_seen`.** `bulk_save` sorts courses by `term_id` before inserting so that `INSERT OR IGNORE` naturally keeps the earliest occurrence — that's how `instructors`, `levels`, and `attributes` get a correct `first_seen` for free. `subjects` is the one exception: its `first_seen` is backfilled afterward by `fix_first_seen` (a `MIN(term_id)` over `courses`).
- **Crash resilience.** Each phase commits as it finishes; Phase 5 also batch-saves every `DETAIL_BATCH_SIZE` (5000) sections. Combined with `--resume` (skips done semesters, already-fetched detail rows, and already-present catalog subjects), an interrupted crawl can be continued without redoing work. DB PRAGMAs (`journal_mode=WAL`, `synchronous=NORMAL`) are tuned for write speed while staying safe against OS crash / power loss. Catalog writes are made monotonic by `save_catalog`/`better_catalog`, which merge against existing rows (longest description wins, missing hours/department filled) so a re-run can only improve a row.
- **Cloudflare email decoding.** Instructor emails are XOR-obfuscated in the HTML; `decode_cf_email` reverses it (first byte is the key).

## Schema notes

10 tables defined inline in the `SCHEMA` string. `courses` is the fact table; uniqueness is `(crn, term_id, class_type, days, start_time)` so a course with multiple meeting blocks (e.g. lecture + lab) produces multiple rows. `course_dependencies` is the structured/queryable form of the free-text prerequisites in `section_details`. `term_id` (e.g. `202620` = Fall 2025) is the join key across `courses`, `semesters`, `section_details`, and `course_dependencies`. See README.md for example queries and full table descriptions.

The Banner endpoint reference (URLs, methods, WAF/rate-limit behavior) is documented in README.md under "Banner Technical Details" — consult it before touching the HTTP layer.
