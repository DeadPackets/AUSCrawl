# AUSCrawl — Banner 9 rewrite design

Date: 2026-08-14
Branch: `banner9-rewrite`
Status: implemented, with the schema decision revised mid-build (see Revision)

## Why

AUS replaced the Banner 8 self-service portal. The endpoints AUSCrawl scrapes are gone:

```
https://banner.aus.edu/axp3b21h/owa/bwckschd.p_disp_dyn_sched   -> 404
https://banner.aus.edu/axp3b21h/owa/bwckctlg.p_display_courses  -> 404
```

The replacement is Ellucian Banner 9 Student Registration Self-Service at
`https://register.aus.edu/StudentRegistrationSsb/ssb/`. It serves JSON, needs no
authentication, and exposes all 101 terms from Spring 2005 (`200520`) to Fall 2026
(`202710`) — the same range the shipped database covers.

The crawler is fully broken today. Every phase must be rewritten.

## What changes for the better

Measured against the shipped `aus_courses.db` (73,778 sections, 101 terms):

| Phase | Old requests | New requests |
|---|---|---|
| Sections | ~10,000 | ~250 |
| Catalog | ~9,000 | ~450 |
| Details | ~74,000 | ~40,000 |
| Reference | included above | ~300 |
| **Total** | **~95,000** | **~41,000** |

The section endpoint returns 500 records per response, so one term costs
1 bind + `ceil(sections/500)` pages — four requests for a 1,814-section term.

### Data the old scraper could not get

| Field | Source | Note |
|---|---|---|
| `enrollment`, `maximumEnrollment`, `seatsAvailable` | section JSON | Old DB stored only a boolean `seats_available` |
| `waitCapacity`, `waitCount`, `waitAvailable` | section JSON | Zero everywhere at AUS today; stored anyway |
| `faculty[].bannerId` | section JSON | Stable instructor identifier |
| `building`, `buildingDescription`, `room`, `campus` | meeting JSON | Old DB had one concatenated `classroom` string |
| `startDate`, `endDate`, `hoursWeek`, `creditHourSession` | meeting JSON | Per meeting block |
| `crossList`, `crossListCapacity/Count/Available` | section JSON | 18 of 1,814 sections in Fall 2026 |
| `partOfTerm`, `instructionalMethod` | section JSON | |
| `lectureHour*`, `labHour*`, `otherHour*`, `billHour*` | catalog JSON | Split hour types with low/high/indicator |
| `college`, `collegeCode`, `department`, `departmentCode` | catalog JSON | |
| `termEffective`, `termStart`, `termEnd` | catalog JSON | Course version identity |
| `preRequisiteCheckMethodCde` | catalog JSON | |
| Structured prerequisite rows | `getPrerequisites` HTML table | See below |
| Instructor directory | `classSearch/get_instructor` | 468 names + Banner IDs for Fall 2026 |
| Attribute directory | `classSearch/get_attribute` | 265 codes for Fall 2026 |

### Prerequisites become a real expression

The old crawler parsed a prose blob and reconstructed a tree heuristically. Banner 9
returns a table whose columns *are* the expression:

| And/Or | ( | Test | Score | Subject | Course Number | Level | Grade | ) |
|---|---|---|---|---|---|---|---|---|
| | | | | Computer Science | 220 | Undergraduate | C- | |
| And | ( | | | Computer Science | 213 | Undergraduate | C- | |
| Or | | | | Math | 213 | Undergraduate | C- | ) |

Reads as `CMP220(C-) AND (CMP213(C-) OR MTH213(C-))`.

It also carries **test-score prerequisites** the old text parser could not represent
at all — e.g. ACC201 accepts any of `Math Placement for Business >= 1`,
`SAT Subject Math Level 2 >= 600`, or `MTH 001/002/003/100 with C-`.

## Two behaviours that will silently corrupt data if ignored

### 1. Search endpoints are session-stateful and ignore `txt_term`

`searchResults` and `courseSearchResults` read the term from **session state**, set by
`POST /term/search?mode=...`. The `txt_term` query parameter is decorative.

Verified: bind `202710`, then request `txt_term=201510` — the response is
`totalCount: 1814` with every record stamped `"term": "202710"`, HTTP 200, no error.

Consequences for the design:

- A session may have exactly one term in flight at a time.
- Term-level parallelism comes from a **pool of independent sessions** (separate
  cookie jars), never from concurrent requests on one session.
- `mode=search` (sections) and `mode=courseSearch` (catalog) are also session state.
  Sections and catalog use separate session pools rather than switching modes.
- A cold session with no bind returns `totalCount: 0, data: null` for sections and
  HTTP 500 for catalog.

**The empty-result variant is the more dangerous one**, found during the first live
run. A bind that does not take makes the search answer HTTP 200 with
`totalCount: 0`, so verifying record terms catches nothing — there are no records to
check. An unguarded crawler records **zero sections for that term and reports
success**. Every AUS term has sections, so `fetch_all_pages` treats an empty first
page as a failed bind: it rebinds once and then raises `EmptyTerm` rather than
committing the emptiness.

### 2. Detail endpoints are stateless

`getPrerequisites`, `getCorequisites`, `getRestrictions`, `getCourseAttributes` and the
section-level equivalents all work on a cold client with no bind, taking `term` in the
POST body. The detail phase therefore runs on one shared session at full parallelism.

### Section-level vs catalog-level prerequisites

Sampled 12 random Fall 2026 sections: 11 were byte-identical between
`searchResults/getSectionPrerequisites` and `courseSearchResults/getPrerequisites`.
The one difference was a cosmetic `General Requirements:` label.

AUS does not use per-section prerequisite overrides. The crawler fetches details at
**catalog level only**, keyed by `(subject, courseNumber, termEffective)`. This is what
turns 74,000 requests into 40,000.

## Endpoint reference

Base: `https://register.aus.edu/StudentRegistrationSsb/ssb`

| Method | Path | Params | Returns |
|---|---|---|---|
| GET | `/classSearch/getTerms` | `searchTerm, offset, max` | JSON `[{code, description}]` — stateless |
| GET | `/term/termSelection` | `mode=search\|courseSearch` | HTML; sets session mode |
| POST | `/term/search` | `mode=...`; body `term` | JSON `{fwdURL}`; binds term |
| GET | `/searchResults/searchResults` | `pageOffset, pageMaxSize<=500` | JSON `{totalCount, data[]}` |
| GET | `/courseSearchResults/courseSearchResults` | `pageOffset, pageMaxSize<=500` | JSON `{totalCount, data[]}` |
| GET | `/classSearch/get_subject` | `searchTerm, term, offset, max` | JSON `[{code, description}]` |
| GET | `/classSearch/get_instructor` | same | JSON — Banner ID + name |
| GET | `/classSearch/get_attribute` | same | JSON |
| POST | `/courseSearchResults/getPrerequisites` | `term, subjectCode, courseNumber` | HTML fragment |
| POST | `/courseSearchResults/getCorequisites` | same | HTML fragment |
| POST | `/courseSearchResults/getRestrictions` | same | HTML fragment |
| POST | `/courseSearchResults/getCourseAttributes` | same | HTML fragment |
| POST | `/courseSearchResults/getCourseCatalogDetails` | same | HTML — Levels, Grading Modes, Schedule Types |

`pageMaxSize` silently clamps to 500; requesting 2000 returns 500 records.

Endpoints that return 404 at AUS and must not be called: `getSectionRestrictions`,
`getSectionCrosslistings`, `getMutualExclusion`, `getMutuallyExclusiveCourses`,
`getSectionsForCourse`, `getCourseLevels`, `getScheduleTypes`, `get_courseNumber`,
`get_levels`.

Endpoints that return 200 but are empty at AUS and are not worth crawling:
`get_campus`, `get_college`, `get_department`, `get_scheduleType`, `get_partOfTerm`,
`get_instructionalMethod`, `get_session`, `getSectionBookstoreDetails` (a static
Barnes & Noble link), `getFees` (no fee data), `getLinkedSections` (no linked sections).

Fields present in the JSON but empty across all of Fall 2026, so not modelled:
`reservedSeatSummary`, `isSectionLinked`, `linkIdentifier`, `ztcEncodedImage`,
`isZTCAttribute`, and every `waitCount > 0`.

## Crawl pipeline

```
1. Terms       1 request, stateless
2. Reference   3 requests per term (subject, instructor, attribute)  ~300
3. Sections    pool of sessions; per term: bind + ceil(n/500) pages  ~250
4. Catalog     pool of sessions; per term: bind + ceil(n/500) pages  ~450
5. Details     shared session; 5 POSTs per unique (subj, num, termEff)  ~40,000
```

The five detail endpoints are `getPrerequisites`, `getCorequisites`, `getRestrictions`,
`getCourseAttributes` and `getCourseCatalogDetails`.

Phase 5 dominates. Measured over an 11-term sample spread across the full range:
5,325 unique `(subject, courseNumber, termEffective)` triples and 3,432 unique
`(subject, courseNumber)` pairs. Extrapolated to 101 terms: roughly 7,000–9,000
triples, so 35,000–45,000 detail requests.

Each phase commits as it completes. Phase 5 batch-commits so an interrupted run
resumes without redoing work. `--resume` skips terms already present and triples
already in `catalog_versions`.

## Legacy column mapping and the one real data loss

The existing `courses` columns must keep their exact current formats so published
queries keep working. Observed formats and their new sources:

| Column | Current format | New source |
|---|---|---|
| `days` | `MW`, `TR`, `U`, `WS` | `monday`..`sunday` booleans, joined as `M T W R F S U` (R = Thursday, U = Sunday) |
| `start_time` / `end_time` | `11:00 am`, `1:45 pm` | `beginTime` `"1100"` / `endTime` `"1215"`, converted to 12-hour lowercase, no leading zero |
| `classroom` | `School of Business Administrtn 1104` | `buildingDescription + " " + room` |
| `date_range` | `Aug 24, 2026 - Dec 10, 2026` | `startDate` / `endDate` (`08/24/2026`) reformatted |
| `class_type` | `Class` | `meetingTypeDescription` |
| `seats_available` | `0` / `1` | `seatsAvailable > 0` |
| `levels` | `Graduate, Post Bachelor, Undergraduate` | `getCourseCatalogDetails` Levels section |
| `attributes` | comma-joined descriptions | `sectionAttributes[].description` |
| `is_lab` | `0` / `1` | `scheduleTypeDescription` or `meetingTypeDescription` containing `Lab` |
| `registration_dates` | `Apr 13, 2026 to Aug 31, 2026` | **no source — see below** |

`schedule_type` currently holds the literal string `"Schedule Type"` for every row —
the old parser captured the label instead of the value. The new crawler writes the real
value (`Lecture`, `Lab`, …). This changes existing data, and it is a fix, not a
regression; it is called out in the README.

**Banner 9 HTML-escapes text inside its JSON.** `courseTitle` arrives as
`The Language of the Qur&#39;an`, `college` as `Arts &amp; Sciences`. Every text
field is passed through `html.unescape`. This was caught by comparing a crawled
historical term against the shipped database: 65 of 1,694 rows differed by entity
alone.

**A 500 from a detail endpoint means "no such course in that term"** — permanent,
not transient. Retrying it five times and then aborting would let one retired course
kill a 40,000-request phase. Detail fetches use two attempts and record an
unavailable fragment in `CourseDetail.missing_parts`, reported in the run summary.
On the newest term this affected 11 of 1,718 course versions.

Section-level title overrides are a second permanent gap. Banner 8 showed titles
like `Calculus III (Take it with MTH 203R Sec.1)`; the Banner 9 section payload has
only `courseTitle`. 56 of 1,694 rows in Fall 2015 differ this way. `INSERT OR IGNORE`
means historical rows keep their richer titles.

`registration_dates` has no equivalent anywhere in Banner 9's public endpoints.
`getRegistrationDates`, `getSectionRegistrationDates` and `getPartOfTermDates` all 404.
The crawler therefore **never writes an empty string over an existing
`registration_dates` value**; historical values in the shipped database are preserved
and new terms leave the column empty. This is documented as a known gap.

`getCourseCatalogDetails` also yields **grading modes**, which the old scraper never
captured — stored on `catalog_versions.grade_modes`.

## Politeness and detection avoidance

Measured headroom: 40 requests at concurrency 16 completed at 174 req/s with zero
429s, zero 403s, p50 68 ms. The server is behind Cloudflare (`cf-ray`, `__cf_bm`) and
an F5 ASM (`TS01...` cookies). Neither challenged a plain HTTP/2 client.

Headroom is not permission. The defaults are deliberately far below what the server
tolerates:

- **Default target 10 req/s**, `--rate` to override. Full crawl ≈ 70 minutes.
- Global token-bucket limiter pacing request *starts*, carried over from the current
  design. It paces the aggregate, so worker count does not change the load.
- AIMD: 429/503/challenge halves the rate; sustained success adds `+1/rate` per
  success up to a ceiling.
- `Retry-After` honored when present.
- Jittered exponential backoff (equal jitter) so retries do not synchronise.
- Retry only 403/408/429/5xx. Permanent 4xx fails fast. Transient 500s were observed
  during recon and recovered on retry, so 5xx retry is required, not optional.

Fingerprint hygiene:

- One stable, current Chrome user-agent with the matching `Sec-CH-UA`,
  `Sec-Fetch-Site/Mode/Dest`, `Accept`, `Accept-Language` and `Referer` set. A
  consistent identity is less anomalous than a rotating one; UA rotation is itself a
  detection signal.
- `X-Requested-With: XMLHttpRequest` on the JSON calls, matching what the real UI sends.
- HTTP/2 with connection reuse and keep-alive, one client per session in the pool.
- Cloudflare challenge detection (`cf-mitigated`, challenge HTML, 403 with
  `cf-ray`) treated as a rate signal, not a fatal error.
- No robots.txt exists on `register.aus.edu` or `banner.aus.edu` (both 404), so there
  are no crawl directives to honor. The rate limit is a self-imposed courtesy.

## Revision: the schema decision was reversed

The section below records the original decision — extend the Banner 8 schema and stay
backward compatible. **It was reversed during implementation**, and the reasoning is
worth keeping because the trigger was not the one either option anticipated.

Compatibility was already broken, silently. The Banner 9 crawler never writes
`course_dependencies` (156,512 rows), `section_details` (73,778) or `levels`. Keeping
them as real tables meant they would return confident answers frozen at the final
Banner 8 crawl forever, and two of the four example queries in README.md join them. A
breaking change fails loudly; that failed quietly, which is worse.

The redesign keeps 11 normalized tables — `sections`, `meetings`, `course_versions`,
`prereq_rules`, `section_instructors`, `section_attributes` and the reference lists —
and re-exposes every old table name as a **view** over them. Old queries keep working
*and* cannot go stale. `sections` is keyed `(crn, term_id)`, so a changed room updates
the row instead of accumulating a duplicate, which the old key
`(crn, term_id, class_type, days, start_time)` could not do.

`registration_dates` and Banner 8 section-title suffixes exist in no Banner 9 endpoint
and cannot be regenerated, so `--import-legacy` copies them once into
`legacy_section_extras`, which the `courses` view joins.

Verified against the shipped Banner 8 database for Fall 2015: 1,694 rows, exact
key-for-key match, zero missing or extra. The only field differences are the new data
being more correct — 150 sections whose `credits` the old crawler left `NULL`, a level
AUS renamed, and 25 instructor names Banner now spells differently.

## Schema (original decision, superseded)

Decision: **extend, stay backward compatible**. Every existing table and column keeps
its current meaning and stays populated, so the queries in README.md and any
downstream consumer of the published database continue to work. New data lands in new
columns and new tables.

### Existing tables, extended

`courses` — unchanged columns keep their current semantics, including the concatenated
`classroom` string and the boolean `seats_available`. Added:

```
part_of_term          TEXT
building              TEXT
building_name         TEXT
room                  TEXT
campus_code           TEXT
enrollment            INTEGER
max_enrollment        INTEGER
seats_available_count INTEGER
waitlist_capacity     INTEGER
waitlist_count        INTEGER
waitlist_available    INTEGER
cross_list            TEXT
cross_list_capacity   INTEGER
cross_list_count      INTEGER
cross_list_available  INTEGER
open_section          BOOLEAN
section_id            INTEGER   -- Banner's internal section id
```

The `UNIQUE(crn, term_id, class_type, days, start_time)` constraint is unchanged, so
the row-per-meeting-block model is preserved.

`catalog` — keyed `(subject, course_number)`, holding the **latest** version. Added:

```
lecture_hours_high, lab_hours_high, other_hours_low, other_hours_high,
bill_hours_low, bill_hours_high, college, college_code, department_code,
term_effective, term_start, term_end, prereq_check_method
```

`catalog_detail` — keyed `(subject, course_number)`, latest version, unchanged shape.

`instructors` — add `banner_id TEXT`.

`section_instructors` — add `banner_id TEXT`.

`section_details`, `course_dependencies`, `semesters`, `subjects`, `levels`,
`attributes` — unchanged.

### New tables

`meetings` — the structured meeting blocks the old `courses` columns flatten:

```
crn, term_id, meeting_index, meeting_type, meeting_type_desc,
begin_time, end_time, monday..sunday (BOOLEAN each),
building, building_name, room, campus, campus_desc,
start_date, end_date, hours_week, credit_hour_session, schedule_type
UNIQUE(crn, term_id, meeting_index)
```

`catalog_versions` — the full history behind the flat `catalog` table:

```
subject, course_number, term_effective, term_start, term_end,
title, description, college, college_code, department, department_code,
credit_hours_low/high, lecture_hours_low/high, lab_hours_low/high,
other_hours_low/high, bill_hours_low/high,
prerequisites, corequisites, restrictions, course_attributes,
levels, grade_modes, schedule_types,
prerequisites_json, restrictions_json
UNIQUE(subject, course_number, term_effective)
```

This answers "what did CMP 305 require in 2015?" — impossible with the current schema.

`prereq_rules` — one row per row of the Banner prerequisite table, so the expression is
queryable without parsing JSON:

```
subject, course_number, term_effective, seq,
connector,        -- 'And' | 'Or' | '' for the first row
open_paren, close_paren,
test_code, test_score,          -- test-score prerequisites
req_subject, req_course_number, -- course prerequisites
req_level, min_grade
UNIQUE(subject, course_number, term_effective, seq)
```

`prerequisites_json` on `catalog_versions` holds the same information as a nested
boolean tree, built from these rows.

### Migration

`init_db` migrates an existing database in place with `ALTER TABLE ... ADD COLUMN` for
every new column and `CREATE TABLE IF NOT EXISTS` for new tables, so pointing the
crawler at a copy of the shipped `aus_courses.db` upgrades it rather than rebuilding
it. No destructive migration; `--force` still drops and recreates.

**Widening the schema is not enough — the saves must upsert.** The first full-scale
run exposed this: with `INSERT OR IGNORE`, all 75,000 pre-existing rows were skipped,
so only 220 rows ever received a seat count, no row received a cross-listing, and
75,396 rows kept the old `schedule_type` parser bug. Unit tests on an empty database
could not see it. Saves therefore upsert, refreshing everything Banner serves, with
two exceptions carried by explicit `ON CONFLICT` clauses:

- `registration_dates` is never written, because Banner 9 has no source for it.
- `title` is replaced only when the incoming title is longer than the stored one, so
  Banner 8 section-title suffixes survive while genuinely absent titles get filled.

`first_seen` is likewise never updated, which is what keeps the term-ordered insert
producing a correct earliest-occurrence value. `save_catalog` updates only the columns
it owns so it cannot reset the detail columns written by phase 5.

## Module structure

`crawl.py` is 2,112 lines and every phase inside it is being replaced. Splitting it is
part of this work, not unrelated refactoring:

```
auscrawl/
  __init__.py
  config.py      constants, term/URL builders, CLI defaults
  db.py          SCHEMA, init_db/migration, bulk save functions
  http.py        make_client, RateLimiter, request_with_retry, header profile
  session.py     TermSession + SessionPool (the stateful bind, mode handling)
  models.py      dataclasses: Semester, Section, Meeting, CatalogCourse, PrereqRow
  parse_json.py  section/catalog JSON -> models (pure)
  parse_html.py  detail HTML fragments -> models (pure)
  pipeline.py    the five phases and run()
  cli.py         argument parsing, entry point
crawl.py         thin shim that calls auscrawl.cli:main, so documented commands
                 and `uv run python crawl.py` keep working
```

Parsers stay pure functions over `str | bytes` so the existing fixture-driven tests
keep their shape.

## Testing

The repo has a real pytest suite (`tests/`, 5 modules plus fixtures) despite CLAUDE.md
claiming otherwise; CLAUDE.md gets corrected as part of this work.

Test-driven, in this order:

1. **Fixtures** — capture real responses once into `tests/fixtures/banner9/`: one
   section page, one catalog page, term list, and the four detail fragments for a
   simple course (MTH 203), a nested-parenthesis course (CMP 305), a test-score course
   (ACC 201), and an empty case. Captured via `tests/capture_fixtures.py`, extended.
2. **Parser tests** — JSON and HTML parsers against those fixtures, asserting the
   exact expression for CMP 305 and the test-score rows for ACC 201.
3. **Session tests** — a stub transport proving the pool never issues two term binds on
   one session concurrently, and that a response whose records carry the wrong `term`
   raises rather than saving.
4. **DB tests** — migration from a copy of the real shipped schema adds every column
   and preserves existing rows; `catalog_versions` and `prereq_rules` round-trip.
5. **Limiter tests** — existing tests carried over.
6. **End-to-end** — `--latest` against the live server, then assert the newest term's
   section count matches `totalCount` and spot-check known sections.

A **cross-check against the shipped database** is the acceptance gate: crawl a
historical term with both the new crawler and compare against `aus_courses.db`. Section
counts and CRN sets must match; differences must be explainable (new fields, or fields
the old parser got wrong).

## Verification before merge

Merge to `master` only after all of:

- Full pytest suite green.
- A complete crawl of all 101 terms finishes without unretried errors.
- CRN sets per term match the shipped database, or every difference is explained.
- No row in `courses` has a `term_id` that disagrees with its source term (the
  stateful-session trap).
- README.md rewritten: new endpoint reference, new tables, new example queries.
- CLAUDE.md updated: new architecture, new commands, the test-suite correction.

## Out of scope

- Anything requiring authentication (grades, registration, holds, degree audit).
- The `banstu`/`banfac`/`banssb` SSO applications.
- Re-publishing the database release; that is a separate step after the crawl is
  validated.
