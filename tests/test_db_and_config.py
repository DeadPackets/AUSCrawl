"""TDD for DB schema/config fixes (#2, #10, #13), catalog merge (#8),
client timeout (#11), and parse-pool sizing (#12)."""

import asyncio

import crawl


# ── #2: durable-but-fast PRAGMA ─────────────────────────────────────────────


def test_synchronous_is_normal_not_off():
    conn = crawl.init_db(":memory:")
    # 0 = OFF, 1 = NORMAL, 2 = FULL. OFF risks corruption on OS crash.
    assert conn.execute("PRAGMA synchronous").fetchone()[0] == 1
    conn.close()


# ── #13: composite index for (crn, term_id) lookups/joins ───────────────────


def test_composite_crn_term_index_exists():
    conn = crawl.init_db(":memory:")
    names = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index'"
    )}
    assert "idx_courses_crn_term" in names
    conn.close()


# ── #10: meeting blocks differing only by room/end-time are not dropped ─────


def _course(**kw) -> "crawl.Course":
    defaults = dict(
        crn="11179", term_id="202710", subject="PHI", course_number="201",
        title="Intro", section="01", class_type="Class", days="TR",
        start_time="8:00 am", end_time="9:15 am", classroom="Room A",
    )
    return crawl.Course(**{**defaults, **kw})  # type: ignore[arg-type]


def test_distinct_meeting_blocks_are_kept():
    conn = crawl.init_db(":memory:")
    sem = crawl.Semester(term_id="202710", term_name="Fall 2026")
    courses = [
        _course(classroom="Room A"),
        _course(classroom="Room B"),                 # different room
        _course(end_time="10:45 am"),                # different end time
    ]
    crawl.bulk_save(conn, [sem], [], [(sem, courses)])
    assert conn.execute("SELECT COUNT(*) FROM courses").fetchone()[0] == 3
    conn.close()


def test_truly_identical_rows_are_deduped():
    conn = crawl.init_db(":memory:")
    sem = crawl.Semester(term_id="202710", term_name="Fall 2026")
    courses = [_course(), _course()]
    crawl.bulk_save(conn, [sem], [], [(sem, courses)])
    assert conn.execute("SELECT COUNT(*) FROM courses").fetchone()[0] == 1
    conn.close()


# ── #8: catalog merge keeps the longest description and fills gaps ───────────


def test_better_catalog_keeps_longest_description():
    short = crawl.CatalogEntry(subject="PHI", course_number="201", description="Short.")
    long = crawl.CatalogEntry(
        subject="PHI", course_number="201",
        description="A much longer, more complete description of the course.",
    )
    merged = crawl.better_catalog(short, long)
    assert merged.description == long.description


def test_better_catalog_fills_missing_fields_from_other():
    a = crawl.CatalogEntry(
        subject="PHI", course_number="201",
        description="Longer description wins as the base entry here.",
        credit_hours=3.0, department="",
    )
    b = crawl.CatalogEntry(
        subject="PHI", course_number="201",
        description="short", credit_hours=None,
        department="International Studies Department", lab_hours=1.0,
    )
    merged = crawl.better_catalog(a, b)
    assert merged.description == a.description       # longest wins
    assert merged.credit_hours == 3.0               # kept from base
    assert merged.department == "International Studies Department"  # filled from b
    assert merged.lab_hours == 1.0                  # filled from b


def test_save_catalog_does_not_degrade_existing_description():
    conn = crawl.init_db(":memory:")
    good = crawl.CatalogEntry(
        subject="PHI", course_number="201",
        description="A complete, long course description.",
        credit_hours=3.0, department="International Studies Department",
    )
    crawl.save_catalog(conn, [good])
    # A later run captures a worse (shorter) description.
    worse = crawl.CatalogEntry(subject="PHI", course_number="201", description="x")
    crawl.save_catalog(conn, [worse])
    row = conn.execute(
        "SELECT description, credit_hours, department FROM catalog "
        "WHERE subject='PHI' AND course_number='201'"
    ).fetchone()
    assert row[0] == "A complete, long course description."
    assert row[1] == 3.0
    assert row[2] == "International Studies Department"
    conn.close()


# ── #11: split connect timeout ──────────────────────────────────────────────


def test_make_client_uses_short_connect_long_read():
    client = crawl.make_client(workers=50)
    try:
        assert client.timeout.connect == 10.0
        assert client.timeout.read == 120.0
    finally:
        asyncio.run(client.aclose())


# ── #12: parse pool scales with CPU, within sane bounds ─────────────────────


def test_parse_pool_size_bounds():
    assert crawl.parse_pool_size(1) == 4      # floor
    assert crawl.parse_pool_size(2) == 4      # floor
    assert crawl.parse_pool_size(8) == 8      # scales with cpu
    assert crawl.parse_pool_size(64) == 16    # cap


# ── default GET rate: fast but safely under the ~30 req/s 429 threshold ─────


def test_default_get_rate_is_safe_and_bounded():
    # Pacing is governed by the rate limiter now; the AIMD ceiling must stay
    # comfortably under the observed ~30 req/s where Banner starts 429-ing.
    assert 5 <= crawl.DEFAULT_RATE <= crawl.GET_MAX_RATE
    assert crawl.GET_MAX_RATE <= 30
    assert crawl.GET_CONCURRENCY >= crawl.GET_MAX_RATE  # enough workers to saturate
