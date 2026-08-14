# Banner 9 Rewrite Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace AUSCrawl's dead Banner 8 HTML scraper with a Banner 9 JSON-API crawler that fills the existing database schema plus new tables, in ~41,000 requests instead of ~95,000.

**Architecture:** Split the 2,112-line `crawl.py` into an `auscrawl/` package. Sections and catalog come from JSON endpoints that are session-stateful (one bound term per session, parallelism via a session pool). Course details come from five stateless HTML-fragment endpoints, fetched once per unique `(subject, course_number, term_effective)` and run at high parallelism on one shared session. `crawl.py` stays as a thin shim so documented commands keep working.

**Tech Stack:** Python 3.13+, `httpx[http2]`, `lxml`, `rich`, `sqlite3`, `pytest`, `uv`.

**Spec:** `docs/superpowers/specs/2026-08-14-banner9-rewrite-design.md`

## Global Constraints

- Python `>=3.13`; run everything through `uv run --project . ...`.
- Base URL is exactly `https://register.aus.edu/StudentRegistrationSsb/ssb`.
- `pageMaxSize` is `500`. The server silently clamps anything larger.
- Default rate is `10.0` req/s. Never raise a default; `--rate` exists for that.
- The shipped database `aus_courses.db` must keep every existing table and column,
  with existing value formats unchanged. Migration is additive only.
- `registration_dates` has no Banner 9 source. Never write an empty string over an
  existing value.
- Any response whose records carry a `term` other than the requested one is a bug, not
  data. Raise `TermMismatch`; never save it.
- Parsers are pure functions accepting `str | bytes`. No I/O inside a parser.
- Commit after every task.

---

### Task 1: Package skeleton, config, and models

**Files:**
- Create: `auscrawl/__init__.py`, `auscrawl/config.py`, `auscrawl/models.py`
- Test: `tests/test_config_models.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `auscrawl.config.BASE`, `auscrawl.config.EP` (dict of endpoint URLs),
  `PAGE_SIZE`, `DEFAULT_RATE`, `MAX_RATE`, `MIN_RATE`, `SESSION_POOL_SIZE`,
  `DETAIL_CONCURRENCY`, `MAX_RETRIES`, `DETAIL_BATCH_SIZE`, `RETRYABLE_STATUS`,
  `THROTTLE_STATUS`, `BROWSER_HEADERS`, `term_name_to_sort_key`.
  `auscrawl.models` dataclasses: `Semester`, `Meeting`, `InstructorRef`, `Section`,
  `CatalogCourse`, `PrereqRule`, `CourseDetail`, `CodeRef`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_config_models.py
from auscrawl import config, models


def test_endpoints_all_hang_off_the_banner9_base():
    assert config.BASE == "https://register.aus.edu/StudentRegistrationSsb/ssb"
    assert len(config.EP) == 13
    for name, url in config.EP.items():
        assert url.startswith(config.BASE + "/"), name


def test_page_size_matches_server_clamp():
    assert config.PAGE_SIZE == 500


def test_browser_headers_are_internally_consistent():
    h = config.BROWSER_HEADERS
    assert "Chrome/" in h["User-Agent"]
    assert h["Sec-Fetch-Mode"] == "cors"
    assert h["Sec-Fetch-Dest"] == "empty"
    assert h["Sec-Fetch-Site"] == "same-origin"
    assert h["X-Requested-With"] == "XMLHttpRequest"
    assert "Chrome" in h["sec-ch-ua"]


def test_retryable_covers_transient_but_not_permanent():
    assert 500 in config.RETRYABLE_STATUS
    assert 429 in config.RETRYABLE_STATUS
    assert 403 in config.RETRYABLE_STATUS
    assert 404 not in config.RETRYABLE_STATUS
    assert 400 not in config.RETRYABLE_STATUS


def test_section_defaults_are_empty_not_none():
    s = models.Section(crn="1", term_id="202710", subject="ACC",
                       course_number="201", title="T")
    assert s.meetings == []
    assert s.instructors == []
    assert s.attributes == []
    assert s.registration_dates == ""


def test_prereq_rule_holds_either_a_course_or_a_test():
    course = models.PrereqRule(seq=0, req_subject="Computer Science",
                               req_course_number="220", min_grade="C-")
    test = models.PrereqRule(seq=1, connector="Or",
                             test_code="SAT Subject Math Level 2", test_score="600")
    assert course.test_code == ""
    assert test.req_subject == ""
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --project . pytest tests/test_config_models.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'auscrawl'`

- [ ] **Step 3: Write minimal implementation**

```python
# auscrawl/__init__.py
"""AUSCrawl — Banner 9 course data crawler for the American University of Sharjah."""
__version__ = "3.0.0"
```

```python
# auscrawl/config.py
"""Endpoints, tuning constants, and the browser header profile."""

BASE = "https://register.aus.edu/StudentRegistrationSsb/ssb"

EP = {
    "terms": f"{BASE}/classSearch/getTerms",
    "term_selection": f"{BASE}/term/termSelection",
    "term_search": f"{BASE}/term/search",
    "sections": f"{BASE}/searchResults/searchResults",
    "catalog": f"{BASE}/courseSearchResults/courseSearchResults",
    "ref_subject": f"{BASE}/classSearch/get_subject",
    "ref_instructor": f"{BASE}/classSearch/get_instructor",
    "ref_attribute": f"{BASE}/classSearch/get_attribute",
    "prereqs": f"{BASE}/courseSearchResults/getPrerequisites",
    "coreqs": f"{BASE}/courseSearchResults/getCorequisites",
    "restrictions": f"{BASE}/courseSearchResults/getRestrictions",
    "course_attributes": f"{BASE}/courseSearchResults/getCourseAttributes",
    "course_catalog_details": f"{BASE}/courseSearchResults/getCourseCatalogDetails",
}

# The server clamps pageMaxSize to 500; asking for more just wastes the round trip.
PAGE_SIZE = 500

DEFAULT_RATE = 10.0
MAX_RATE = 20.0
MIN_RATE = 2.0
SESSION_POOL_SIZE = 6
DETAIL_CONCURRENCY = 12
MAX_RETRIES = 5
RETRY_BASE = 2.0
DETAIL_BATCH_SIZE = 2000

RETRYABLE_STATUS = frozenset({403, 408, 429}) | frozenset(range(500, 600))
THROTTLE_STATUS = frozenset({429, 503})

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)

# A single coherent Chrome identity. Rotating user agents is itself a detection
# signal; a consistent, current one is not.
BROWSER_HEADERS = {
    "User-Agent": _UA,
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Accept-Language": "en-US,en;q=0.9",
    "X-Requested-With": "XMLHttpRequest",
    "Sec-Fetch-Site": "same-origin",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Dest": "empty",
    "sec-ch-ua": '"Chromium";v="140", "Not=A?Brand";v="24", "Google Chrome";v="140"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"macOS"',
    "Referer": f"{BASE}/classSearch/classSearch",
}

_SEASON_ORDER = {"10": 0, "11": 1, "20": 2, "30": 3, "40": 4}


def term_name_to_sort_key(term_id: str) -> tuple[int, int]:
    """Chronological key for a Banner term id such as '202611'."""
    return (int(term_id[:4]), _SEASON_ORDER.get(term_id[4:], 9))
```

```python
# auscrawl/models.py
"""Dataclasses mirroring the Banner 9 payloads."""

from dataclasses import dataclass, field


@dataclass(slots=True)
class Semester:
    term_id: str
    term_name: str


@dataclass(slots=True)
class CodeRef:
    code: str
    description: str


@dataclass(slots=True)
class InstructorRef:
    name: str
    email: str = ""
    banner_id: str = ""
    is_primary: bool = False


@dataclass(slots=True)
class Meeting:
    crn: str
    term_id: str
    meeting_index: int
    meeting_type: str = ""
    meeting_type_desc: str = ""
    begin_time: str = ""
    end_time: str = ""
    monday: bool = False
    tuesday: bool = False
    wednesday: bool = False
    thursday: bool = False
    friday: bool = False
    saturday: bool = False
    sunday: bool = False
    building: str = ""
    building_name: str = ""
    room: str = ""
    campus: str = ""
    campus_desc: str = ""
    start_date: str = ""
    end_date: str = ""
    hours_week: float | None = None
    credit_hour_session: float | None = None
    schedule_type: str = ""


@dataclass(slots=True)
class Section:
    crn: str
    term_id: str
    subject: str
    course_number: str
    title: str
    section: str = ""
    credits: float | None = None
    schedule_type: str = ""
    instructional_method: str = ""
    campus: str = ""
    levels: str = ""
    attributes_text: str = ""
    registration_dates: str = ""
    part_of_term: str = ""
    section_id: int | None = None
    enrollment: int | None = None
    max_enrollment: int | None = None
    seats_available_count: int | None = None
    waitlist_capacity: int | None = None
    waitlist_count: int | None = None
    waitlist_available: int | None = None
    cross_list: str = ""
    cross_list_capacity: int | None = None
    cross_list_count: int | None = None
    cross_list_available: int | None = None
    open_section: bool = False
    meetings: list[Meeting] = field(default_factory=list)
    instructors: list[InstructorRef] = field(default_factory=list)
    attributes: list[CodeRef] = field(default_factory=list)


@dataclass(slots=True)
class CatalogCourse:
    subject: str
    course_number: str
    title: str
    term_effective: str
    description: str = ""
    term_start: str = ""
    term_end: str = ""
    college: str = ""
    college_code: str = ""
    department: str = ""
    department_code: str = ""
    credit_hours_low: float | None = None
    credit_hours_high: float | None = None
    lecture_hours_low: float | None = None
    lecture_hours_high: float | None = None
    lab_hours_low: float | None = None
    lab_hours_high: float | None = None
    other_hours_low: float | None = None
    other_hours_high: float | None = None
    bill_hours_low: float | None = None
    bill_hours_high: float | None = None
    prereq_check_method: str = ""


@dataclass(slots=True)
class PrereqRule:
    seq: int
    connector: str = ""       # 'And' | 'Or' | '' on the first row
    open_paren: bool = False
    close_paren: bool = False
    test_code: str = ""
    test_score: str = ""
    req_subject: str = ""
    req_course_number: str = ""
    req_level: str = ""
    min_grade: str = ""


@dataclass(slots=True)
class CourseDetail:
    subject: str
    course_number: str
    term_effective: str
    prerequisites: str = ""
    corequisites: str = ""
    restrictions: str = ""
    course_attributes: str = ""
    levels: str = ""
    grade_modes: str = ""
    schedule_types: str = ""
    prerequisites_json: str = ""
    restrictions_json: str = ""
    rules: list[PrereqRule] = field(default_factory=list)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --project . pytest tests/test_config_models.py -v`
Expected: PASS, 6 tests

- [ ] **Step 5: Commit**

```bash
git add auscrawl/ tests/test_config_models.py
git commit -m "Add auscrawl package skeleton: config and models"
```

---

### Task 2: HTTP layer — limiter, retry, Cloudflare detection

**Files:**
- Create: `auscrawl/http.py`
- Test: `tests/test_http.py`
- Reference: `crawl.py:673-796` (existing `RateLimiter`, `request_with_retry`,
  `backoff_delay`, `is_waf_block`, `should_retry_status` — carried over, not reinvented)

**Interfaces:**
- Consumes: `auscrawl.config`.
- Produces: `RateLimiter(rate, max_rate, min_rate, decrease, increase, now)` with
  `.acquire()`, `.record_throttle()`, `.record_success()`, `._reserve(now)`;
  `make_client(concurrency) -> httpx.AsyncClient`;
  `async request_with_retry(client, method, url, *, form=None, params=None, rate=None) -> httpx.Response`;
  `backoff_delay(attempt, base=RETRY_BASE, jitter=random.random) -> float`;
  `is_blocked(resp) -> bool`; `should_retry_status(code) -> bool`;
  `retry_after_seconds(resp) -> float | None`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_http.py
import httpx
import pytest

from auscrawl import config, http


def test_reserve_spaces_requests_by_one_over_rate():
    rl = http.RateLimiter(rate=10)
    assert rl._reserve(0.0) == 0.0
    assert abs(rl._reserve(0.0) - 0.1) < 1e-9
    assert abs(rl._reserve(0.0) - 0.2) < 1e-9


def test_record_throttle_halves_down_to_floor():
    rl = http.RateLimiter(rate=20, min_rate=4, decrease=0.5)
    rl.record_throttle()
    assert rl.rate == 10.0
    rl.record_throttle()
    assert rl.rate == 5.0
    rl.record_throttle()
    assert rl.rate == 4.0


def test_record_success_climbs_back_to_ceiling():
    rl = http.RateLimiter(rate=4, max_rate=6, increase=1.0)
    rl.record_success()
    assert rl.rate == 4.25
    for _ in range(200):
        rl.record_success()
    assert rl.rate == 6.0


def test_backoff_grows_and_is_jittered_within_half():
    assert http.backoff_delay(1, jitter=lambda: 0.0) == 1.0
    assert http.backoff_delay(2, jitter=lambda: 0.0) == 2.0
    assert http.backoff_delay(1, jitter=lambda: 1.0) == 2.0


def test_cloudflare_challenge_detected():
    resp = httpx.Response(403, headers={"cf-mitigated": "challenge", "cf-ray": "x"},
                          request=httpx.Request("GET", "https://x/"))
    assert http.is_blocked(resp) is True


def test_challenge_body_detected():
    body = b"<html><head><title>Just a moment...</title></head></html>"
    resp = httpx.Response(200, content=body, request=httpx.Request("GET", "https://x/"))
    assert http.is_blocked(resp) is True


def test_ordinary_json_not_flagged():
    resp = httpx.Response(200, content=b'{"success":true}',
                          request=httpx.Request("GET", "https://x/"))
    assert http.is_blocked(resp) is False


def test_retry_after_parsed_when_present():
    resp = httpx.Response(429, headers={"Retry-After": "7"},
                          request=httpx.Request("GET", "https://x/"))
    assert http.retry_after_seconds(resp) == 7.0
    plain = httpx.Response(429, request=httpx.Request("GET", "https://x/"))
    assert http.retry_after_seconds(plain) is None


@pytest.mark.asyncio
async def test_retries_a_500_then_succeeds():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(500, text="boom")
        return httpx.Response(200, json={"ok": True})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        rl = http.RateLimiter(rate=1000)
        resp = await http.request_with_retry(client, "GET", "https://x/y", rate=rl)
    assert resp.status_code == 200
    assert calls["n"] == 2


@pytest.mark.asyncio
async def test_404_fails_fast_without_retrying():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(404, text="nope")

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(httpx.HTTPStatusError):
            await http.request_with_retry(client, "GET", "https://x/y")
    assert calls["n"] == 1


def test_client_carries_the_browser_profile():
    client = http.make_client(4)
    assert client.headers["user-agent"] == config.BROWSER_HEADERS["User-Agent"]
    assert client.headers["sec-fetch-mode"] == "cors"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --project . pytest tests/test_http.py -v`
Expected: FAIL with `ImportError: cannot import name 'http'`

- [ ] **Step 3: Add the pytest-asyncio dev dependency**

`request_with_retry` is async, so the suite needs an async runner.

```bash
uv add --dev pytest-asyncio
```

Then add to `pyproject.toml` under `[tool.pytest.ini_options]`:

```toml
asyncio_mode = "auto"
```

- [ ] **Step 4: Write the implementation**

```python
# auscrawl/http.py
"""Rate-paced HTTP with jittered retry and block detection.

Throughput is governed by request-start pacing, not worker count: Banner throttles
on aggregate requests per second, so many concurrent workers plus a global limiter
is both faster and safer than few workers plus a per-worker sleep.
"""

import asyncio
import logging
import random
import time
from typing import Callable, Optional
from urllib.parse import urlencode

import httpx

from . import config

log = logging.getLogger("auscrawl")

FORM_CONTENT_TYPE = {"content-type": "application/x-www-form-urlencoded"}
BLOCK_SCAN_LIMIT = 65536
_BLOCK_MARKERS = (
    b"Just a moment...",
    b"cf-browser-verification",
    b"Attention Required! | Cloudflare",
    b"The requested URL was rejected",
)


def should_retry_status(code: int) -> bool:
    return code in config.RETRYABLE_STATUS


def backoff_delay(
    attempt: int,
    base: float = config.RETRY_BASE,
    jitter: Callable[[], float] = random.random,
) -> float:
    """Equal-jitter exponential backoff, so a fleet does not retry in lockstep."""
    full = base ** attempt
    return full / 2 + (full / 2) * jitter()


def retry_after_seconds(resp: httpx.Response) -> Optional[float]:
    raw = resp.headers.get("retry-after")
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def is_blocked(resp: httpx.Response) -> bool:
    """True for a Cloudflare or F5 interstitial rather than a real response."""
    if resp.headers.get("cf-mitigated") == "challenge":
        return True
    ctype = resp.headers.get("content-type", "")
    if "json" in ctype:
        return False
    body = resp.content[:BLOCK_SCAN_LIMIT]
    return any(marker in body for marker in _BLOCK_MARKERS)


class RateLimiter:
    """Global token-bucket pacing of request starts, with AIMD feedback."""

    def __init__(
        self,
        rate: float,
        max_rate: Optional[float] = None,
        min_rate: float = config.MIN_RATE,
        decrease: float = 0.5,
        increase: float = 1.0,
        now: Callable[[], float] = time.monotonic,
    ):
        self.rate = float(rate)
        self.max_rate = float(max_rate if max_rate is not None else rate)
        self.min_rate = float(min_rate)
        self.decrease = decrease
        self.increase = increase
        self._now = now
        self._next_free: Optional[float] = None
        self._lock = asyncio.Lock()

    def _reserve(self, now: float) -> float:
        start = now if self._next_free is None else max(self._next_free, now)
        self._next_free = start + 1.0 / self.rate
        return max(0.0, start - now)

    async def acquire(self):
        async with self._lock:
            wait = self._reserve(self._now())
        if wait > 0:  # sleep outside the lock so waiters pace in parallel
            await asyncio.sleep(wait)

    def record_throttle(self):
        self.rate = max(self.min_rate, self.rate * self.decrease)

    def record_success(self):
        if self.rate < self.max_rate:
            self.rate = min(self.max_rate, self.rate + self.increase / self.rate)


def make_client(concurrency: int) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=httpx.Timeout(120.0, connect=10.0),
        follow_redirects=True,
        http2=True,
        headers=dict(config.BROWSER_HEADERS),
        limits=httpx.Limits(
            max_connections=concurrency + 5,
            max_keepalive_connections=concurrency + 5,
        ),
    )


async def request_with_retry(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    form: dict[str, str] | list[tuple[str, str]] | None = None,
    params: dict | None = None,
    rate: Optional[RateLimiter] = None,
) -> httpx.Response:
    kwargs: dict = {}
    if form is not None:
        kwargs["content"] = urlencode(form)
        kwargs["headers"] = FORM_CONTENT_TYPE
    if params is not None:
        kwargs["params"] = params

    for attempt in range(1, config.MAX_RETRIES + 1):
        last = attempt == config.MAX_RETRIES
        if rate:
            await rate.acquire()
        try:
            resp = await client.request(method, url, **kwargs)
            resp.raise_for_status()

            if is_blocked(resp):
                if rate:
                    rate.record_throttle()
                if last:
                    break
                wait = backoff_delay(attempt)
                log.warning("Challenge page (attempt %d), retrying in %.0fs", attempt, wait)
                await asyncio.sleep(wait)
                continue

            if rate:
                rate.record_success()
            return resp
        except httpx.HTTPStatusError as e:
            code = e.response.status_code
            if rate and code in config.THROTTLE_STATUS:
                rate.record_throttle()
            if not should_retry_status(code):
                raise
            if last:
                break
            wait = retry_after_seconds(e.response) or backoff_delay(attempt)
            log.warning("HTTP %d (attempt %d), retrying in %.1fs", code, attempt, wait)
            await asyncio.sleep(wait)
        except httpx.RequestError as e:
            if last:
                raise
            wait = backoff_delay(attempt)
            log.warning("Network error (attempt %d): %s, retrying in %.0fs", attempt, e, wait)
            await asyncio.sleep(wait)

    raise RuntimeError(f"Failed after {config.MAX_RETRIES} retries: {method} {url}")
```

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run --project . pytest tests/test_http.py -v`
Expected: PASS, 11 tests

- [ ] **Step 6: Commit**

```bash
git add auscrawl/http.py tests/test_http.py pyproject.toml uv.lock
git commit -m "Add HTTP layer: AIMD limiter, jittered retry, challenge detection"
```

---

### Task 3: Session pool with the term-mismatch guard

This is the task that prevents silent data corruption. `txt_term` is ignored by the
server; the term comes from session state.

**Files:**
- Create: `auscrawl/session.py`
- Test: `tests/test_session.py`

**Interfaces:**
- Consumes: `auscrawl.config`, `auscrawl.http`.
- Produces: `class TermMismatch(RuntimeError)`;
  `class TermSession` with `async bind(term_id, mode)` and
  `async fetch_page(endpoint_key, term_id, offset) -> bytes` (raw body, already
  verified — returning bytes keeps the parsers the single JSON decode point);
  `class SessionPool` with `async __aenter__/__aexit__` and
  `async map_terms(terms, handler)`;
  `verify_term(payload, expected_term, record_key="term") -> None`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_session.py
import httpx
import pytest

from auscrawl import session


def test_verify_term_accepts_matching_records():
    payload = {"totalCount": 2, "data": [{"term": "202710"}, {"term": "202710"}]}
    session.verify_term(payload, "202710")  # must not raise


def test_verify_term_rejects_the_silent_wrong_term_response():
    # The exact failure mode: HTTP 200, plausible data, wrong term.
    payload = {"totalCount": 1814, "data": [{"term": "202710"}]}
    with pytest.raises(session.TermMismatch) as exc:
        session.verify_term(payload, "201510")
    assert "201510" in str(exc.value)
    assert "202710" in str(exc.value)


def test_verify_term_accepts_empty_data():
    session.verify_term({"totalCount": 0, "data": None}, "202710")


def test_verify_term_uses_term_effective_for_catalog_records():
    payload = {"totalCount": 1, "data": [{"termEffective": "202210"}]}
    session.verify_term(payload, "202710", record_key="term")  # catalog has no 'term'


async def test_bind_issues_term_selection_then_term_search():
    seen = []

    def handler(request):
        seen.append((request.method, request.url.path, dict(request.url.params)))
        if request.url.path.endswith("/termSelection"):
            return httpx.Response(200, text="<html></html>")
        return httpx.Response(200, json={"fwdURL": "/x"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        s = session.TermSession(client)
        await s.bind("202710", "search")

    assert seen[0][0] == "GET" and seen[0][1].endswith("/term/termSelection")
    assert seen[0][2]["mode"] == "search"
    assert seen[1][0] == "POST" and seen[1][1].endswith("/term/search")
    assert seen[1][2]["mode"] == "search"


async def test_fetch_page_raises_when_the_server_returns_another_term():
    def handler(request):
        if request.url.path.endswith("/termSelection"):
            return httpx.Response(200, text="")
        if request.url.path.endswith("/term/search"):
            return httpx.Response(200, json={"fwdURL": "/x"})
        return httpx.Response(200, json={"totalCount": 1, "data": [{"term": "202710"}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        s = session.TermSession(client)
        await s.bind("201510", "search")
        with pytest.raises(session.TermMismatch):
            await s.fetch_page("sections", "201510", 0)


async def test_pool_never_runs_two_terms_on_one_session():
    import asyncio

    active: dict[int, str] = {}
    overlaps = []

    async def handler(sess_id, term):
        if sess_id in active:
            overlaps.append((active[sess_id], term))
        active[sess_id] = term
        await asyncio.sleep(0.01)
        del active[sess_id]
        return term

    async with session.SessionPool(size=3, rate=None) as pool:
        results = await pool.map_terms(
            [f"20{n:04d}" for n in range(1000, 1012)],
            lambda sess, term: handler(id(sess), term),
        )

    assert overlaps == []
    assert len(results) == 12
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --project . pytest tests/test_session.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'auscrawl.session'`

- [ ] **Step 3: Write the implementation**

```python
# auscrawl/session.py
"""Stateful term sessions.

The Banner 9 search endpoints read the term from session state set by
POST /term/search. The txt_term query parameter is decorative: bind Fall 2026 and
ask for Spring 2015 and the server answers 200 OK with Fall 2026 data. Every page is
therefore verified against the term that was bound, and one session may have only one
term in flight.
"""

import asyncio
import logging
from typing import Awaitable, Callable, Optional

import httpx

from . import config
from .http import RateLimiter, make_client, request_with_retry

log = logging.getLogger("auscrawl")


class TermMismatch(RuntimeError):
    """The server answered with a term other than the one bound."""


def verify_term(payload: dict, expected_term: str, record_key: str = "term") -> None:
    rows = payload.get("data") or []
    for row in rows:
        got = row.get(record_key)
        if got and got != expected_term:
            raise TermMismatch(
                f"requested term {expected_term} but records carry {got}; "
                "the session bind did not take"
            )


class TermSession:
    """One cookie jar, one bound term at a time."""

    def __init__(self, client: httpx.AsyncClient, rate: Optional[RateLimiter] = None):
        self.client = client
        self.rate = rate
        self.term: Optional[str] = None
        self.mode: Optional[str] = None

    async def bind(self, term_id: str, mode: str) -> None:
        await request_with_retry(
            self.client, "GET", config.EP["term_selection"],
            params={"mode": mode}, rate=self.rate,
        )
        await request_with_retry(
            self.client, "POST", config.EP["term_search"],
            params={"mode": mode}, form={"term": term_id, "studyPath": "",
                                         "studyPathText": "", "startDatepicker": "",
                                         "endDatepicker": ""},
            rate=self.rate,
        )
        self.term = term_id
        self.mode = mode

    async def fetch_page(self, endpoint_key: str, term_id: str, offset: int) -> bytes:
        if self.term != term_id:
            raise TermMismatch(
                f"session is bound to {self.term}, refusing to fetch {term_id}"
            )
        resp = await request_with_retry(
            self.client, "GET", config.EP[endpoint_key],
            params={
                "txt_term": term_id,
                "pageOffset": offset,
                "pageMaxSize": config.PAGE_SIZE,
                "sortColumn": "subjectDescription",
                "sortDirection": "asc",
            },
            rate=self.rate,
        )
        verify_term(resp.json(), term_id)
        return resp.content


class SessionPool:
    """A fixed number of independent sessions; terms are handed out one per session."""

    def __init__(self, size: int = config.SESSION_POOL_SIZE,
                 rate: Optional[RateLimiter] = None):
        self.size = size
        self.rate = rate
        self._sessions: list[TermSession] = []
        self._free: asyncio.Queue[TermSession] = asyncio.Queue()

    async def __aenter__(self) -> "SessionPool":
        for _ in range(self.size):
            s = TermSession(make_client(4), self.rate)
            self._sessions.append(s)
            self._free.put_nowait(s)
        return self

    async def __aexit__(self, *exc) -> None:
        for s in self._sessions:
            await s.client.aclose()

    async def map_terms(
        self,
        terms: list[str],
        handler: Callable[[TermSession, str], Awaitable],
    ) -> list:
        async def one(term: str):
            sess = await self._free.get()
            try:
                return await handler(sess, term)
            finally:
                self._free.put_nowait(sess)

        return await asyncio.gather(*(one(t) for t in terms))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --project . pytest tests/test_session.py -v`
Expected: PASS, 7 tests

- [ ] **Step 5: Commit**

```bash
git add auscrawl/session.py tests/test_session.py
git commit -m "Add session pool with the term-mismatch guard"
```

---

### Task 4: Capture real fixtures

Every later parser test runs against real captured bytes, not invented HTML.

**Files:**
- Create: `tests/capture_banner9.py`
- Create: `tests/fixtures/banner9/` (generated)

**Interfaces:**
- Consumes: `auscrawl.config`.
- Produces: fixture files listed below and `tests/fixtures/banner9/manifest.txt`.

- [ ] **Step 1: Write the capture script**

```python
# tests/capture_banner9.py
"""Capture live Banner 9 responses once, so parser tests run offline.

Run:  uv run --project . python tests/capture_banner9.py
"""

import pathlib
import time

import httpx

from auscrawl import config

OUT = pathlib.Path(__file__).parent / "fixtures" / "banner9"
TERM = "202710"

# One simple course, one with nested parentheses, one with test-score prereqs,
# one with no prerequisites at all.
COURSES = [("MTH", "203"), ("CMP", "305"), ("ACC", "201"), ("BIO", "103")]

DETAIL_EPS = {
    "prereqs": "prereqs",
    "coreqs": "coreqs",
    "restrictions": "restrictions",
    "course_attributes": "attributes",
    "course_catalog_details": "catalogdetails",
}


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    c = httpx.Client(http2=True, headers=dict(config.BROWSER_HEADERS),
                     timeout=60, follow_redirects=True)
    manifest = []

    r = c.get(config.EP["terms"], params={"searchTerm": "", "offset": 1, "max": 500})
    (OUT / "terms.json").write_bytes(r.content)
    manifest.append(f"terms.json={config.EP['terms']}")

    c.get(config.EP["term_selection"], params={"mode": "search"})
    c.post(config.EP["term_search"], params={"mode": "search"}, data={"term": TERM})
    r = c.get(config.EP["sections"], params={"txt_term": TERM, "pageOffset": 0,
                                             "pageMaxSize": config.PAGE_SIZE})
    (OUT / "sections_202710_p0.json").write_bytes(r.content)
    manifest.append(f"sections_202710_p0.json=term {TERM} offset 0")

    for key, ref in (("ref_subject", "subjects"), ("ref_instructor", "instructors"),
                     ("ref_attribute", "attributes")):
        r = c.get(config.EP[key], params={"searchTerm": "", "term": TERM,
                                          "offset": 1, "max": 5000})
        (OUT / f"ref_{ref}_202710.json").write_bytes(r.content)
        manifest.append(f"ref_{ref}_202710.json={key} term {TERM}")
        time.sleep(0.3)

    c.get(config.EP["term_selection"], params={"mode": "courseSearch"})
    c.post(config.EP["term_search"], params={"mode": "courseSearch"}, data={"term": TERM})
    r = c.get(config.EP["catalog"], params={"txt_term": TERM, "pageOffset": 0,
                                            "pageMaxSize": config.PAGE_SIZE})
    (OUT / "catalog_202710_p0.json").write_bytes(r.content)
    manifest.append(f"catalog_202710_p0.json=catalog term {TERM} offset 0")

    for subj, num in COURSES:
        for ep_key, tag in DETAIL_EPS.items():
            r = c.post(config.EP[ep_key], data={"term": TERM, "subjectCode": subj,
                                                "courseNumber": num})
            name = f"{tag}_{subj}{num}.html"
            (OUT / name).write_bytes(r.content)
            manifest.append(f"{name}={ep_key} {subj} {num} term {TERM}")
            time.sleep(0.3)

    (OUT / "manifest.txt").write_text("\n".join(manifest) + "\n")
    print(f"captured {len(manifest)} fixtures into {OUT}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Run it against the live server**

Run: `uv run --project . python tests/capture_banner9.py`
Expected: `captured 26 fixtures into .../tests/fixtures/banner9`

- [ ] **Step 3: Sanity-check what came back**

Run:
```bash
ls tests/fixtures/banner9/ && \
python3 -c "import json;d=json.load(open('tests/fixtures/banner9/sections_202710_p0.json'));print(d['totalCount'], len(d['data']))"
```
Expected: 26 files listed; `1814 500` (the exact total may drift as AUS edits the term —
record whatever it prints, later tests assert against the fixture, not a hard-coded number).

- [ ] **Step 4: Add the fixture loader to conftest**

Append to `tests/conftest.py`:

```python
B9 = FIXTURES / "banner9"


@pytest.fixture(scope="session")
def b9_dir() -> Path:
    return B9


def read_b9(name: str) -> bytes:
    return (B9 / name).read_bytes()
```

- [ ] **Step 5: Commit**

```bash
git add tests/capture_banner9.py tests/fixtures/banner9/ tests/conftest.py
git commit -m "Capture Banner 9 response fixtures"
```

---

### Task 5: JSON parsers for sections and meetings

**Files:**
- Create: `auscrawl/parse_json.py`
- Test: `tests/test_parse_json.py`

**Interfaces:**
- Consumes: `auscrawl.models`, `auscrawl.session.verify_term`.
- Produces: `parse_terms(raw) -> list[Semester]`;
  `parse_code_list(raw) -> list[CodeRef]`;
  `parse_sections(raw, expected_term) -> tuple[int, list[Section]]`;
  `days_string(meeting) -> str`; `to_12h(hhmm) -> str`;
  `format_date_range(start, end) -> str`; `classroom_string(meeting) -> str`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_parse_json.py
import json

import pytest

from auscrawl import models, parse_json
from auscrawl.session import TermMismatch
from tests.conftest import read_b9


def test_parse_terms_reads_code_and_description():
    terms = parse_json.parse_terms(read_b9("terms.json"))
    assert len(terms) >= 100
    assert terms[0].term_id.isdigit() and len(terms[0].term_id) == 6
    assert any(t.term_id == "200520" for t in terms)
    # descriptions keep Banner's "(View Only)" suffix stripped
    assert "(View Only)" not in terms[-1].term_name


def test_parse_sections_returns_total_and_rows():
    total, sections = parse_json.parse_sections(
        read_b9("sections_202710_p0.json"), "202710")
    assert total > 1000
    assert len(sections) == 500
    s = sections[0]
    assert s.term_id == "202710"
    assert s.crn.isdigit()
    assert s.subject and s.course_number and s.title


def test_parse_sections_rejects_a_wrong_term_payload():
    raw = read_b9("sections_202710_p0.json")
    with pytest.raises(TermMismatch):
        parse_json.parse_sections(raw, "201510")


def test_seat_counts_are_carried_through():
    _, sections = parse_json.parse_sections(
        read_b9("sections_202710_p0.json"), "202710")
    seated = [s for s in sections if s.max_enrollment]
    assert seated, "expected at least one section with a capacity"
    s = seated[0]
    assert s.enrollment is not None
    assert s.seats_available_count is not None


def test_instructors_carry_banner_id_and_primary_flag():
    _, sections = parse_json.parse_sections(
        read_b9("sections_202710_p0.json"), "202710")
    withfac = [s for s in sections if s.instructors]
    assert withfac
    ins = withfac[0].instructors[0]
    assert ins.name
    assert ins.banner_id.isdigit()
    assert isinstance(ins.is_primary, bool)


def test_meetings_are_structured_not_concatenated():
    _, sections = parse_json.parse_sections(
        read_b9("sections_202710_p0.json"), "202710")
    withmt = [s for s in sections if s.meetings and s.meetings[0].room]
    assert withmt
    m = withmt[0].meetings[0]
    assert m.building and m.room
    assert m.building_name != m.building
    assert m.meeting_index == 0


# --- legacy format compatibility --------------------------------------------

def test_days_string_uses_the_legacy_letters():
    m = models.Meeting(crn="1", term_id="t", meeting_index=0,
                       monday=True, wednesday=True)
    assert parse_json.days_string(m) == "MW"
    m2 = models.Meeting(crn="1", term_id="t", meeting_index=0,
                        tuesday=True, thursday=True)
    assert parse_json.days_string(m2) == "TR"
    m3 = models.Meeting(crn="1", term_id="t", meeting_index=0,
                        saturday=True, sunday=True)
    assert parse_json.days_string(m3) == "SU"
    assert parse_json.days_string(models.Meeting(crn="1", term_id="t",
                                                 meeting_index=0)) == ""


def test_to_12h_matches_the_shipped_database_format():
    assert parse_json.to_12h("1100") == "11:00 am"
    assert parse_json.to_12h("1215") == "12:15 pm"
    assert parse_json.to_12h("1345") == "1:45 pm"
    assert parse_json.to_12h("0800") == "8:00 am"
    assert parse_json.to_12h("0000") == "12:00 am"
    assert parse_json.to_12h("1200") == "12:00 pm"
    assert parse_json.to_12h("") == ""
    assert parse_json.to_12h(None) == ""


def test_format_date_range_matches_the_shipped_database_format():
    assert parse_json.format_date_range("08/24/2026", "12/10/2026") == \
        "Aug 24, 2026 - Dec 10, 2026"
    assert parse_json.format_date_range("", "") == ""


def test_classroom_string_matches_the_shipped_database_format():
    m = models.Meeting(crn="1", term_id="t", meeting_index=0,
                       building_name="School of Business Administrtn", room="1104")
    assert parse_json.classroom_string(m) == "School of Business Administrtn 1104"
    assert parse_json.classroom_string(
        models.Meeting(crn="1", term_id="t", meeting_index=0)) == ""


def test_code_list_parses_reference_endpoints():
    subjects = parse_json.parse_code_list(read_b9("ref_subjects_202710.json"))
    assert len(subjects) > 50
    assert all(s.code and s.description for s in subjects)
    instructors = parse_json.parse_code_list(read_b9("ref_instructors_202710.json"))
    assert len(instructors) > 100
    assert instructors[0].code.isdigit()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --project . pytest tests/test_parse_json.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'auscrawl.parse_json'`

- [ ] **Step 3: Write the implementation**

```python
# auscrawl/parse_json.py
"""Pure parsers from Banner 9 JSON into models."""

import json

from .models import CodeRef, InstructorRef, Meeting, Section, Semester
from .session import verify_term

_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")

# Legacy day letters: R is Thursday, U is Sunday.
_DAY_LETTERS = (
    ("monday", "M"), ("tuesday", "T"), ("wednesday", "W"), ("thursday", "R"),
    ("friday", "F"), ("saturday", "S"), ("sunday", "U"),
)


def _load(raw: str | bytes):
    return json.loads(raw)


def parse_terms(raw: str | bytes) -> list[Semester]:
    return [
        Semester(term_id=t["code"],
                 term_name=t["description"].replace("(View Only)", "").strip())
        for t in _load(raw)
    ]


def parse_code_list(raw: str | bytes) -> list[CodeRef]:
    return [CodeRef(code=r["code"], description=r["description"]) for r in _load(raw)]


def to_12h(hhmm: str | None) -> str:
    """'1345' -> '1:45 pm', matching the format already in the shipped database."""
    if not hhmm or len(hhmm) != 4 or not hhmm.isdigit():
        return ""
    h, m = int(hhmm[:2]), hhmm[2:]
    suffix = "am" if h < 12 else "pm"
    h12 = h % 12 or 12
    return f"{h12}:{m} {suffix}"


def format_date_range(start: str | None, end: str | None) -> str:
    """'08/24/2026','12/10/2026' -> 'Aug 24, 2026 - Dec 10, 2026'."""
    def one(d):
        if not d or d.count("/") != 2:
            return ""
        mm, dd, yy = d.split("/")
        return f"{_MONTHS[int(mm) - 1]} {int(dd)}, {yy}"

    a, b = one(start), one(end)
    return f"{a} - {b}" if a and b else ""


def days_string(m: Meeting) -> str:
    return "".join(letter for attr, letter in _DAY_LETTERS if getattr(m, attr))


def classroom_string(m: Meeting) -> str:
    parts = [p for p in (m.building_name or m.building, m.room) if p]
    return " ".join(parts)


def _meeting(raw: dict, crn: str, term_id: str, index: int) -> Meeting:
    mt = raw.get("meetingTime") or {}
    return Meeting(
        crn=crn, term_id=term_id, meeting_index=index,
        meeting_type=mt.get("meetingType") or "",
        meeting_type_desc=mt.get("meetingTypeDescription") or "",
        begin_time=mt.get("beginTime") or "",
        end_time=mt.get("endTime") or "",
        monday=bool(mt.get("monday")), tuesday=bool(mt.get("tuesday")),
        wednesday=bool(mt.get("wednesday")), thursday=bool(mt.get("thursday")),
        friday=bool(mt.get("friday")), saturday=bool(mt.get("saturday")),
        sunday=bool(mt.get("sunday")),
        building=mt.get("building") or "",
        building_name=mt.get("buildingDescription") or "",
        room=mt.get("room") or "",
        campus=mt.get("campus") or "",
        campus_desc=mt.get("campusDescription") or "",
        start_date=mt.get("startDate") or "",
        end_date=mt.get("endDate") or "",
        hours_week=mt.get("hoursWeek"),
        credit_hour_session=mt.get("creditHourSession"),
        schedule_type=mt.get("meetingScheduleType") or "",
    )


def parse_sections(raw: str | bytes, expected_term: str) -> tuple[int, list[Section]]:
    payload = _load(raw)
    verify_term(payload, expected_term)
    out: list[Section] = []
    for r in payload.get("data") or []:
        crn = r["courseReferenceNumber"]
        attrs = [CodeRef(code=a.get("code") or "", description=a.get("description") or "")
                 for a in (r.get("sectionAttributes") or [])]
        out.append(Section(
            crn=crn,
            term_id=r["term"],
            subject=r["subject"],
            course_number=r["courseNumber"],
            title=r.get("courseTitle") or "",
            section=r.get("sequenceNumber") or "",
            credits=r.get("creditHourLow"),
            schedule_type=r.get("scheduleTypeDescription") or "",
            instructional_method=r.get("instructionalMethodDescription") or "",
            campus=r.get("campusDescription") or "",
            attributes_text=", ".join(a.description for a in attrs),
            part_of_term=r.get("partOfTerm") or "",
            section_id=r.get("id"),
            enrollment=r.get("enrollment"),
            max_enrollment=r.get("maximumEnrollment"),
            seats_available_count=r.get("seatsAvailable"),
            waitlist_capacity=r.get("waitCapacity"),
            waitlist_count=r.get("waitCount"),
            waitlist_available=r.get("waitAvailable"),
            cross_list=r.get("crossList") or "",
            cross_list_capacity=r.get("crossListCapacity"),
            cross_list_count=r.get("crossListCount"),
            cross_list_available=r.get("crossListAvailable"),
            open_section=bool(r.get("openSection")),
            meetings=[_meeting(m, crn, r["term"], i)
                      for i, m in enumerate(r.get("meetingsFaculty") or [])],
            instructors=[InstructorRef(
                name=f.get("displayName") or "",
                email=f.get("emailAddress") or "",
                banner_id=str(f.get("bannerId") or ""),
                is_primary=bool(f.get("primaryIndicator")),
            ) for f in (r.get("faculty") or [])],
            attributes=attrs,
        ))
    return payload.get("totalCount") or 0, out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --project . pytest tests/test_parse_json.py -v`
Expected: PASS, 12 tests

- [ ] **Step 5: Commit**

```bash
git add auscrawl/parse_json.py tests/test_parse_json.py
git commit -m "Parse section JSON into models with legacy-format helpers"
```

---

### Task 6: JSON parser for the catalog

**Files:**
- Modify: `auscrawl/parse_json.py`
- Modify: `tests/test_parse_json.py`

**Interfaces:**
- Consumes: `auscrawl.models.CatalogCourse`.
- Produces: `parse_catalog(raw, expected_term) -> tuple[int, list[CatalogCourse]]`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_parse_json.py`:

```python
def test_parse_catalog_returns_total_and_courses():
    total, courses = parse_json.parse_catalog(
        read_b9("catalog_202710_p0.json"), "202710")
    assert total > 1000
    assert len(courses) == 500
    c = courses[0]
    assert c.subject and c.course_number and c.title
    assert c.term_effective.isdigit() and len(c.term_effective) == 6


def test_catalog_carries_description_inline_so_no_extra_request_is_needed():
    _, courses = parse_json.parse_catalog(
        read_b9("catalog_202710_p0.json"), "202710")
    described = [c for c in courses if c.description]
    assert len(described) > len(courses) * 0.5


def test_catalog_splits_hour_types():
    _, courses = parse_json.parse_catalog(
        read_b9("catalog_202710_p0.json"), "202710")
    lectured = [c for c in courses if c.lecture_hours_low is not None]
    assert lectured
    assert any(c.lab_hours_low is not None for c in courses)


def test_catalog_carries_college_and_department_codes():
    _, courses = parse_json.parse_catalog(
        read_b9("catalog_202710_p0.json"), "202710")
    assert all(c.college_code for c in courses)
    assert any(c.department_code for c in courses)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --project . pytest tests/test_parse_json.py -k catalog -v`
Expected: FAIL with `AttributeError: module 'auscrawl.parse_json' has no attribute 'parse_catalog'`

- [ ] **Step 3: Write the implementation**

Add to `auscrawl/parse_json.py` (and add `CatalogCourse` to the model import):

```python
def parse_catalog(raw: str | bytes,
                  expected_term: str) -> tuple[int, list[CatalogCourse]]:
    """Catalog rows. Records carry termEffective, not term, so there is nothing
    to verify against the bound term — the guard belongs on the section path."""
    payload = _load(raw)
    out: list[CatalogCourse] = []
    for r in payload.get("data") or []:
        out.append(CatalogCourse(
            subject=r["subject"],
            course_number=r["courseNumber"],
            title=r.get("courseTitle") or "",
            term_effective=r.get("termEffective") or "",
            description=(r.get("courseDescription") or "").strip(),
            term_start=r.get("termStart") or "",
            term_end=r.get("termEnd") or "",
            college=r.get("college") or "",
            college_code=r.get("collegeCode") or "",
            department=r.get("department") or "",
            department_code=r.get("departmentCode") or "",
            credit_hours_low=r.get("creditHourLow"),
            credit_hours_high=r.get("creditHourHigh"),
            lecture_hours_low=r.get("lectureHourLow"),
            lecture_hours_high=r.get("lectureHourHigh"),
            lab_hours_low=r.get("labHourLow"),
            lab_hours_high=r.get("labHourHigh"),
            other_hours_low=r.get("otherHourLow"),
            other_hours_high=r.get("otherHourHigh"),
            bill_hours_low=r.get("billHourLow"),
            bill_hours_high=r.get("billHourHigh"),
            prereq_check_method=r.get("preRequisiteCheckMethodCde") or "",
        ))
    return payload.get("totalCount") or 0, out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --project . pytest tests/test_parse_json.py -v`
Expected: PASS, 16 tests

- [ ] **Step 5: Commit**

```bash
git add auscrawl/parse_json.py tests/test_parse_json.py
git commit -m "Parse catalog JSON into models"
```

---

### Task 7: Prerequisite table parser and expression tree

The highest-value new capability. The table's columns *are* the boolean expression.

**Files:**
- Create: `auscrawl/parse_html.py`
- Test: `tests/test_parse_html.py`

**Interfaces:**
- Consumes: `auscrawl.models.PrereqRule`, `lxml.html`.
- Produces: `parse_prereq_rules(raw) -> list[PrereqRule]`;
  `prereq_tree(rules) -> dict | None`; `prereq_json(rules) -> str`;
  `rule_label(rule) -> str`.

Tree node shapes: a leaf is
`{"type": "course", "subject": str, "course_number": str, "level": str, "min_grade": str}`
or `{"type": "test", "test": str, "score": str}`; a branch is
`{"type": "and"|"or", "operands": [...]}`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_parse_html.py
from auscrawl import parse_html
from tests.conftest import read_b9


def test_simple_single_course_prerequisite():
    rules = parse_html.parse_prereq_rules(read_b9("prereqs_MTH203.html"))
    assert len(rules) == 1
    r = rules[0]
    assert r.req_subject == "Math"
    assert r.req_course_number == "104"
    assert r.req_level == "Undergraduate"
    assert r.min_grade == "C-"
    assert r.connector == ""
    assert r.open_paren is False and r.close_paren is False


def test_simple_prerequisite_tree_is_a_bare_leaf():
    rules = parse_html.parse_prereq_rules(read_b9("prereqs_MTH203.html"))
    tree = parse_html.prereq_tree(rules)
    assert tree == {"type": "course", "subject": "Math", "course_number": "104",
                    "level": "Undergraduate", "min_grade": "C-"}


def test_nested_parentheses_are_captured_as_rows():
    rules = parse_html.parse_prereq_rules(read_b9("prereqs_CMP305.html"))
    assert len(rules) == 3
    assert rules[0].connector == ""
    assert rules[1].connector == "And" and rules[1].open_paren is True
    assert rules[2].connector == "Or" and rules[2].close_paren is True


def test_nested_parentheses_build_the_right_tree():
    rules = parse_html.parse_prereq_rules(read_b9("prereqs_CMP305.html"))
    tree = parse_html.prereq_tree(rules)
    # CMP220 AND (CMP213 OR MTH213)
    assert tree["type"] == "and"
    assert len(tree["operands"]) == 2
    assert tree["operands"][0]["course_number"] == "220"
    inner = tree["operands"][1]
    assert inner["type"] == "or"
    assert {o["course_number"] for o in inner["operands"]} == {"213", "213"}
    assert {o["subject"] for o in inner["operands"]} == {"Computer Science", "Math"}


def test_test_score_prerequisites_are_captured():
    rules = parse_html.parse_prereq_rules(read_b9("prereqs_ACC201.html"))
    tests = [r for r in rules if r.test_code]
    assert tests, "ACC201 has placement-test prerequisites"
    assert any(t.test_code.startswith("SAT") for t in tests)
    assert all(t.test_score for t in tests)
    assert all(t.req_subject == "" for t in tests)


def test_a_flat_or_chain_becomes_one_or_node():
    rules = parse_html.parse_prereq_rules(read_b9("prereqs_ACC201.html"))
    tree = parse_html.prereq_tree(rules)
    assert tree["type"] == "or"
    assert len(tree["operands"]) == len(rules)
    assert any(o["type"] == "test" for o in tree["operands"])
    assert any(o["type"] == "course" for o in tree["operands"])


def test_no_prerequisites_yields_no_rules_and_no_tree():
    rules = parse_html.parse_prereq_rules(read_b9("prereqs_BIO103.html"))
    assert rules == []
    assert parse_html.prereq_tree(rules) is None
    assert parse_html.prereq_json(rules) == ""


def test_rule_label_renders_a_readable_line():
    from auscrawl.models import PrereqRule
    course = PrereqRule(seq=0, req_subject="Math", req_course_number="104",
                        req_level="Undergraduate", min_grade="C-")
    assert parse_html.rule_label(course) == "Math 104 (Undergraduate, min grade C-)"
    test = PrereqRule(seq=1, test_code="SAT Subject Math Level 2", test_score="600")
    assert parse_html.rule_label(test) == "SAT Subject Math Level 2 >= 600"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --project . pytest tests/test_parse_html.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'auscrawl.parse_html'`

- [ ] **Step 3: Write the implementation**

```python
# auscrawl/parse_html.py
"""Pure parsers for the Banner 9 HTML detail fragments."""

import json
import re

from lxml import html as lxml_html

from .models import PrereqRule

RE_WS = re.compile(r"\s+")


def _text(el) -> str:
    return RE_WS.sub(" ", el.text_content()).strip()


def parse_prereq_rules(raw: str | bytes) -> list[PrereqRule]:
    """Read the prerequisite table, whose columns are the boolean expression.

    Columns: And/Or | ( | Test | Score | Subject | Course Number | Level | Grade | )
    """
    if not raw:
        return []
    tree = lxml_html.fromstring(raw)
    tables = tree.xpath('//table[contains(@class, "basePreqTable")]')
    if not tables:
        return []

    rules: list[PrereqRule] = []
    for i, row in enumerate(tables[0].xpath(".//tbody/tr")):
        cells = [_text(td) for td in row.xpath("./td")]
        if len(cells) < 9:
            continue
        connector, open_p, test, score, subj, num, level, grade, close_p = cells[:9]
        if not (test or subj):
            continue
        rules.append(PrereqRule(
            seq=len(rules),
            connector=connector,
            open_paren="(" in open_p,
            close_paren=")" in close_p,
            test_code=test,
            test_score=score,
            req_subject=subj,
            req_course_number=num,
            req_level=level,
            min_grade=grade,
        ))
    return rules


def _leaf(r: PrereqRule) -> dict:
    if r.test_code:
        return {"type": "test", "test": r.test_code, "score": r.test_score}
    return {"type": "course", "subject": r.req_subject,
            "course_number": r.req_course_number,
            "level": r.req_level, "min_grade": r.min_grade}


def _combine(op: str, left, right):
    """Flatten same-operator chains so 'a or b or c' is one node, not a spine."""
    if left is None:
        return right
    if left.get("type") == op:
        return {"type": op, "operands": left["operands"] + [right]}
    return {"type": op, "operands": [left, right]}


def prereq_tree(rules: list[PrereqRule]):
    """Fold the rows into a boolean tree, honouring the paren columns."""
    if not rules:
        return None

    stack: list[tuple] = []          # (accumulated node, pending operator)
    node = None
    op = None

    for r in rules:
        if r.connector:
            op = r.connector.lower()
        if r.open_paren:
            stack.append((node, op))
            node, op = None, None

        leaf = _leaf(r)
        node = leaf if node is None else _combine(op or "and", node, leaf)

        if r.close_paren and stack:
            outer, outer_op = stack.pop()
            node = _combine(outer_op or "and", outer, node)
            op = outer_op

    while stack:                      # unbalanced parens in the source
        outer, outer_op = stack.pop()
        node = _combine(outer_op or "and", outer, node)

    return node


def prereq_json(rules: list[PrereqRule]) -> str:
    tree = prereq_tree(rules)
    return json.dumps(tree, separators=(",", ":")) if tree else ""


def rule_label(r: PrereqRule) -> str:
    if r.test_code:
        return f"{r.test_code} >= {r.test_score}" if r.test_score else r.test_code
    quals = ", ".join(p for p in (r.req_level,
                                  f"min grade {r.min_grade}" if r.min_grade else "") if p)
    base = f"{r.req_subject} {r.req_course_number}".strip()
    return f"{base} ({quals})" if quals else base
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --project . pytest tests/test_parse_html.py -v`
Expected: PASS, 8 tests

- [ ] **Step 5: Commit**

```bash
git add auscrawl/parse_html.py tests/test_parse_html.py
git commit -m "Parse the Banner 9 prerequisite table into rules and a boolean tree"
```

---

### Task 8: Remaining HTML fragment parsers

**Files:**
- Modify: `auscrawl/parse_html.py`
- Modify: `tests/test_parse_html.py`

**Interfaces:**
- Produces: `fragment_text(raw) -> str`;
  `parse_restriction_groups(raw) -> list[dict]`; `restrictions_json(raw) -> str`;
  `parse_attributes(raw) -> list[tuple[str, str]]`;
  `parse_catalog_details(raw) -> dict` with keys `levels`, `grade_modes`,
  `schedule_types` (each a comma-joined string).

- [ ] **Step 1: Write the failing test**

Append to `tests/test_parse_html.py`:

```python
def test_fragment_text_strips_markup_and_the_no_information_boilerplate():
    assert parse_html.fragment_text(read_b9("coreqs_ACC201.html")) == ""
    txt = parse_html.fragment_text(read_b9("restrictions_ACC201.html"))
    assert "Undergraduate" in txt
    assert "<" not in txt


def test_restriction_groups_are_typed_include_or_exclude():
    groups = parse_html.parse_restriction_groups(read_b9("restrictions_ACC201.html"))
    assert groups
    g = groups[0]
    assert g["mode"] in ("include", "exclude")
    assert g["kind"] == "Levels"
    assert "Undergraduate (UG)" in g["values"]


def test_restrictions_json_round_trips():
    import json
    raw = parse_html.restrictions_json(read_b9("restrictions_ACC201.html"))
    assert json.loads(raw)[0]["kind"] == "Levels"


def test_attributes_parse_into_description_and_code_pairs():
    attrs = parse_html.parse_attributes(read_b9("attributes_ACC201.html"))
    assert attrs
    desc, code = attrs[0]
    assert code.isupper() and len(code) == 4
    assert desc and not desc.endswith(code)


def test_catalog_details_yield_levels_grade_modes_and_schedule_types():
    d = parse_html.parse_catalog_details(read_b9("catalogdetails_ACC201.html"))
    assert "Undergraduate" in d["levels"]
    assert d["grade_modes"]
    assert d["schedule_types"]


def test_catalog_details_on_a_sparse_course_do_not_crash():
    d = parse_html.parse_catalog_details(b"<section></section>")
    assert d == {"levels": "", "grade_modes": "", "schedule_types": ""}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --project . pytest tests/test_parse_html.py -k "fragment or restriction or attributes or catalog_details" -v`
Expected: FAIL with `AttributeError: module 'auscrawl.parse_html' has no attribute 'fragment_text'`

- [ ] **Step 3: Write the implementation**

Add to `auscrawl/parse_html.py`:

```python
RE_NO_INFO = re.compile(r"No .*information available\.?", re.IGNORECASE)
RE_RESTR_HEADER = re.compile(
    r"^(must|may not)\s+be\s+enrolled\s+in\s+one\s+of\s+the\s+following\s+(.+?):\s*$",
    re.IGNORECASE,
)
RE_ATTR = re.compile(r"^(.*?)\s{2,}([A-Z0-9]{2,6})$")


def fragment_text(raw: str | bytes) -> str:
    """Readable text of a fragment; the 'no information' filler collapses to ''."""
    if not raw:
        return ""
    tree = lxml_html.fromstring(raw)
    for h in tree.xpath("//h3"):
        h.drop_tree()
    txt = RE_WS.sub(" ", tree.text_content()).strip()
    txt = RE_NO_INFO.sub("", txt).strip()
    return txt


def _fragment_lines(raw: str | bytes) -> list[str]:
    """Fragment content split on <br/>, since Banner separates items that way."""
    if not raw:
        return []
    tree = lxml_html.fromstring(raw)
    html_str = lxml_html.tostring(tree, encoding="unicode")
    html_str = re.sub(r"<br\s*/?>", "\n", html_str, flags=re.IGNORECASE)
    text = lxml_html.fromstring(html_str).text_content()
    return [RE_WS.sub(" ", ln).strip() for ln in text.split("\n") if ln.strip()]


def parse_restriction_groups(raw: str | bytes) -> list[dict]:
    """Typed groups: each header line opens a group the following lines belong to."""
    groups: list[dict] = []
    current: dict | None = None
    for line in _fragment_lines(raw):
        m = RE_RESTR_HEADER.match(line)
        if m:
            current = {
                "mode": "exclude" if m.group(1).lower() == "may not" else "include",
                "kind": m.group(2).strip().rstrip(":"),
                "values": [],
            }
            groups.append(current)
            continue
        if current is not None and not RE_NO_INFO.match(line) and \
                "Not all restrictions are applicable" not in line:
            current["values"].append(line)
    return [g for g in groups if g["values"]]


def restrictions_json(raw: str | bytes) -> str:
    groups = parse_restriction_groups(raw)
    return json.dumps(groups, separators=(",", ":")) if groups else ""


def parse_attributes(raw: str | bytes) -> list[tuple[str, str]]:
    """'Actuarial Math Minor_Elective  AMTN' -> ('Actuarial Math Minor_Elective','AMTN')."""
    out: list[tuple[str, str]] = []
    for line in _fragment_lines(raw):
        m = RE_ATTR.match(line)
        if m:
            out.append((m.group(1).strip(), m.group(2)))
        elif line and not RE_NO_INFO.match(line):
            parts = line.rsplit(" ", 1)
            if len(parts) == 2 and parts[1].isupper():
                out.append((parts[0].strip(), parts[1]))
    return out


_CATALOG_HEADERS = {
    "Levels:": "levels",
    "Grading Modes:": "grade_modes",
    "Schedule Types:": "schedule_types",
}


def parse_catalog_details(raw: str | bytes) -> dict:
    """Pull the Levels / Grading Modes / Schedule Types sections out of the fragment."""
    out = {"levels": "", "grade_modes": "", "schedule_types": ""}
    key: str | None = None
    buckets: dict[str, list[str]] = {k: [] for k in out}
    for line in _fragment_lines(raw):
        if line in _CATALOG_HEADERS:
            key = _CATALOG_HEADERS[line]
            continue
        if line.endswith(":"):
            key = None
            continue
        if key:
            buckets[key].append(line)
    for k, vals in buckets.items():
        out[k] = ", ".join(vals)
    return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --project . pytest tests/test_parse_html.py -v`
Expected: PASS, 14 tests

- [ ] **Step 5: Commit**

```bash
git add auscrawl/parse_html.py tests/test_parse_html.py
git commit -m "Parse restriction, attribute, and catalog-detail fragments"
```

---

### Task 9: Schema and additive migration

**Files:**
- Create: `auscrawl/db.py`
- Test: `tests/test_db.py`

**Interfaces:**
- Consumes: `auscrawl.models`.
- Produces: `SCHEMA` (str); `init_db(path, force=False) -> sqlite3.Connection`;
  `migrate_schema(conn) -> list[str]` returning the names of columns it added;
  `NEW_COLUMNS` (dict of table -> list of `(column, ddl_type)`).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_db.py
import sqlite3

import pytest

from auscrawl import db


def cols(conn, table):
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def test_fresh_database_has_every_table(tmp_path):
    conn = db.init_db(str(tmp_path / "new.db"))
    names = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    for t in ("semesters", "subjects", "instructors", "levels", "attributes",
              "courses", "catalog", "section_details", "course_dependencies",
              "section_instructors", "catalog_detail",
              "meetings", "catalog_versions", "prereq_rules"):
        assert t in names, t


def test_migration_adds_new_columns_to_a_legacy_database(tmp_path):
    """Simulate the shipped database: old schema, real rows, then migrate."""
    p = str(tmp_path / "legacy.db")
    conn = sqlite3.connect(p)
    conn.executescript("""
        CREATE TABLE courses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            crn TEXT NOT NULL, term_id TEXT NOT NULL, subject TEXT NOT NULL,
            course_number TEXT NOT NULL, title TEXT NOT NULL, section TEXT,
            credits REAL, schedule_type TEXT, instructional_method TEXT,
            campus TEXT, levels TEXT, attributes TEXT, registration_dates TEXT,
            class_type TEXT, start_time TEXT, end_time TEXT, days TEXT,
            seats_available BOOLEAN, classroom TEXT, date_range TEXT,
            instructor_name TEXT, instructor_email TEXT, is_lab BOOLEAN DEFAULT 0,
            UNIQUE(crn, term_id, class_type, days, start_time)
        );
        CREATE TABLE instructors (
            id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
            email TEXT, first_seen TEXT, UNIQUE(name, email)
        );
        CREATE TABLE section_instructors (
            id INTEGER PRIMARY KEY AUTOINCREMENT, crn TEXT NOT NULL,
            term_id TEXT NOT NULL, name TEXT NOT NULL, email TEXT,
            is_primary BOOLEAN DEFAULT 0, UNIQUE(crn, term_id, name)
        );
        CREATE TABLE catalog (
            id INTEGER PRIMARY KEY AUTOINCREMENT, subject TEXT NOT NULL,
            course_number TEXT NOT NULL, description TEXT DEFAULT '',
            credit_hours REAL, lecture_hours REAL, lab_hours REAL,
            department TEXT DEFAULT '', UNIQUE(subject, course_number)
        );
        INSERT INTO courses (crn, term_id, subject, course_number, title,
                             registration_dates, days, start_time, class_type)
        VALUES ('10394','202710','ACC','201','Fund of Financial Accounting',
                'Apr 13, 2026 to Aug 31, 2026','MW','11:00 am','Class');
    """)
    conn.commit()
    conn.close()

    conn = db.init_db(p)
    c = cols(conn, "courses")
    for new in ("part_of_term", "building", "room", "enrollment", "max_enrollment",
                "seats_available_count", "cross_list", "section_id", "open_section"):
        assert new in c, new
    assert "banner_id" in cols(conn, "instructors")
    assert "banner_id" in cols(conn, "section_instructors")
    assert "term_effective" in cols(conn, "catalog")


def test_migration_preserves_existing_rows_and_registration_dates(tmp_path):
    p = str(tmp_path / "legacy2.db")
    conn = sqlite3.connect(p)
    conn.executescript("""
        CREATE TABLE courses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            crn TEXT NOT NULL, term_id TEXT NOT NULL, subject TEXT NOT NULL,
            course_number TEXT NOT NULL, title TEXT NOT NULL, section TEXT,
            credits REAL, schedule_type TEXT, instructional_method TEXT,
            campus TEXT, levels TEXT, attributes TEXT, registration_dates TEXT,
            class_type TEXT, start_time TEXT, end_time TEXT, days TEXT,
            seats_available BOOLEAN, classroom TEXT, date_range TEXT,
            instructor_name TEXT, instructor_email TEXT, is_lab BOOLEAN DEFAULT 0,
            UNIQUE(crn, term_id, class_type, days, start_time)
        );
        INSERT INTO courses (crn, term_id, subject, course_number, title,
                             registration_dates, days, start_time, class_type)
        VALUES ('10394','202710','ACC','201','T','Apr 13, 2026 to Aug 31, 2026',
                'MW','11:00 am','Class');
    """)
    conn.commit()
    conn.close()

    conn = db.init_db(p)
    row = conn.execute(
        "SELECT registration_dates, title FROM courses WHERE crn='10394'").fetchone()
    assert row[0] == "Apr 13, 2026 to Aug 31, 2026"
    assert row[1] == "T"


def test_migration_is_idempotent(tmp_path):
    p = str(tmp_path / "twice.db")
    conn = db.init_db(p)
    conn.close()
    conn = db.init_db(p)          # must not raise "duplicate column name"
    assert "building" in cols(conn, "courses")


def test_force_recreates_from_scratch(tmp_path):
    p = str(tmp_path / "forced.db")
    conn = db.init_db(p)
    conn.execute("INSERT INTO semesters (term_id, term_name) VALUES ('202710','Fall')")
    conn.commit()
    conn.close()
    conn = db.init_db(p, force=True)
    assert conn.execute("SELECT COUNT(*) FROM semesters").fetchone()[0] == 0


def test_write_pragmas_are_set(tmp_path):
    conn = db.init_db(str(tmp_path / "p.db"))
    assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --project . pytest tests/test_db.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'auscrawl.db'`

- [ ] **Step 3: Write the implementation**

```python
# auscrawl/db.py
"""SQLite schema, additive migration, and bulk writes."""

import logging
import os
import sqlite3

log = logging.getLogger("auscrawl")

SCHEMA = """
CREATE TABLE IF NOT EXISTS semesters (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    term_id TEXT UNIQUE NOT NULL,
    term_name TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS subjects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    short_name TEXT NOT NULL, long_name TEXT NOT NULL, first_seen TEXT,
    UNIQUE(short_name)
);
CREATE TABLE IF NOT EXISTS instructors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL, email TEXT, first_seen TEXT, banner_id TEXT,
    UNIQUE(name, email)
);
CREATE TABLE IF NOT EXISTS levels (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    level TEXT UNIQUE NOT NULL, first_seen TEXT
);
CREATE TABLE IF NOT EXISTS attributes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    attribute TEXT UNIQUE NOT NULL, first_seen TEXT
);
CREATE TABLE IF NOT EXISTS courses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    crn TEXT NOT NULL, term_id TEXT NOT NULL, subject TEXT NOT NULL,
    course_number TEXT NOT NULL, title TEXT NOT NULL, section TEXT,
    credits REAL, schedule_type TEXT, instructional_method TEXT, campus TEXT,
    levels TEXT, attributes TEXT, registration_dates TEXT,
    class_type TEXT, start_time TEXT, end_time TEXT, days TEXT,
    seats_available BOOLEAN, classroom TEXT, date_range TEXT,
    instructor_name TEXT, instructor_email TEXT, is_lab BOOLEAN DEFAULT 0,
    part_of_term TEXT, building TEXT, building_name TEXT, room TEXT,
    campus_code TEXT, enrollment INTEGER, max_enrollment INTEGER,
    seats_available_count INTEGER, waitlist_capacity INTEGER,
    waitlist_count INTEGER, waitlist_available INTEGER,
    cross_list TEXT, cross_list_capacity INTEGER, cross_list_count INTEGER,
    cross_list_available INTEGER, open_section BOOLEAN, section_id INTEGER,
    UNIQUE(crn, term_id, class_type, days, start_time)
);
CREATE TABLE IF NOT EXISTS catalog (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    subject TEXT NOT NULL, course_number TEXT NOT NULL,
    description TEXT DEFAULT '', credit_hours REAL, lecture_hours REAL,
    lab_hours REAL, department TEXT DEFAULT '',
    lecture_hours_high REAL, lab_hours_high REAL,
    other_hours_low REAL, other_hours_high REAL,
    bill_hours_low REAL, bill_hours_high REAL,
    credit_hours_high REAL, college TEXT DEFAULT '', college_code TEXT DEFAULT '',
    department_code TEXT DEFAULT '', term_effective TEXT DEFAULT '',
    term_start TEXT DEFAULT '', term_end TEXT DEFAULT '',
    prereq_check_method TEXT DEFAULT '', title TEXT DEFAULT '',
    UNIQUE(subject, course_number)
);
CREATE TABLE IF NOT EXISTS section_details (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    crn TEXT NOT NULL, term_id TEXT NOT NULL,
    prerequisites TEXT DEFAULT '', corequisites TEXT DEFAULT '',
    restrictions TEXT DEFAULT '', waitlist_capacity INTEGER DEFAULT 0,
    waitlist_actual INTEGER DEFAULT 0, waitlist_remaining INTEGER DEFAULT 0,
    fees TEXT DEFAULT '', prerequisites_json TEXT DEFAULT '',
    corequisites_json TEXT DEFAULT '', restrictions_json TEXT DEFAULT '',
    UNIQUE(crn, term_id)
);
CREATE TABLE IF NOT EXISTS course_dependencies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    crn TEXT NOT NULL, term_id TEXT NOT NULL, dep_type TEXT NOT NULL,
    subject TEXT NOT NULL, course_number TEXT NOT NULL,
    minimum_grade TEXT DEFAULT '',
    UNIQUE(crn, term_id, dep_type, subject, course_number)
);
CREATE TABLE IF NOT EXISTS section_instructors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    crn TEXT NOT NULL, term_id TEXT NOT NULL, name TEXT NOT NULL,
    email TEXT, is_primary BOOLEAN DEFAULT 0, banner_id TEXT,
    UNIQUE(crn, term_id, name)
);
CREATE TABLE IF NOT EXISTS catalog_detail (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    subject TEXT NOT NULL, course_number TEXT NOT NULL, term_id TEXT DEFAULT '',
    levels TEXT DEFAULT '', schedule_types TEXT DEFAULT '',
    course_attributes TEXT DEFAULT '', prerequisites TEXT DEFAULT '',
    corequisites TEXT DEFAULT '', restrictions TEXT DEFAULT '',
    grade_modes TEXT DEFAULT '',
    UNIQUE(subject, course_number)
);
CREATE TABLE IF NOT EXISTS meetings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    crn TEXT NOT NULL, term_id TEXT NOT NULL, meeting_index INTEGER NOT NULL,
    meeting_type TEXT DEFAULT '', meeting_type_desc TEXT DEFAULT '',
    begin_time TEXT DEFAULT '', end_time TEXT DEFAULT '',
    monday BOOLEAN DEFAULT 0, tuesday BOOLEAN DEFAULT 0,
    wednesday BOOLEAN DEFAULT 0, thursday BOOLEAN DEFAULT 0,
    friday BOOLEAN DEFAULT 0, saturday BOOLEAN DEFAULT 0, sunday BOOLEAN DEFAULT 0,
    building TEXT DEFAULT '', building_name TEXT DEFAULT '', room TEXT DEFAULT '',
    campus TEXT DEFAULT '', campus_desc TEXT DEFAULT '',
    start_date TEXT DEFAULT '', end_date TEXT DEFAULT '',
    hours_week REAL, credit_hour_session REAL, schedule_type TEXT DEFAULT '',
    UNIQUE(crn, term_id, meeting_index)
);
CREATE TABLE IF NOT EXISTS catalog_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    subject TEXT NOT NULL, course_number TEXT NOT NULL,
    term_effective TEXT NOT NULL, term_start TEXT DEFAULT '',
    term_end TEXT DEFAULT '', title TEXT DEFAULT '', description TEXT DEFAULT '',
    college TEXT DEFAULT '', college_code TEXT DEFAULT '',
    department TEXT DEFAULT '', department_code TEXT DEFAULT '',
    credit_hours_low REAL, credit_hours_high REAL,
    lecture_hours_low REAL, lecture_hours_high REAL,
    lab_hours_low REAL, lab_hours_high REAL,
    other_hours_low REAL, other_hours_high REAL,
    bill_hours_low REAL, bill_hours_high REAL,
    prereq_check_method TEXT DEFAULT '',
    prerequisites TEXT DEFAULT '', corequisites TEXT DEFAULT '',
    restrictions TEXT DEFAULT '', course_attributes TEXT DEFAULT '',
    levels TEXT DEFAULT '', grade_modes TEXT DEFAULT '',
    schedule_types TEXT DEFAULT '',
    prerequisites_json TEXT DEFAULT '', restrictions_json TEXT DEFAULT '',
    UNIQUE(subject, course_number, term_effective)
);
CREATE TABLE IF NOT EXISTS prereq_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    subject TEXT NOT NULL, course_number TEXT NOT NULL,
    term_effective TEXT NOT NULL, seq INTEGER NOT NULL,
    connector TEXT DEFAULT '', open_paren BOOLEAN DEFAULT 0,
    close_paren BOOLEAN DEFAULT 0, test_code TEXT DEFAULT '',
    test_score TEXT DEFAULT '', req_subject TEXT DEFAULT '',
    req_course_number TEXT DEFAULT '', req_level TEXT DEFAULT '',
    min_grade TEXT DEFAULT '',
    UNIQUE(subject, course_number, term_effective, seq)
);
CREATE INDEX IF NOT EXISTS idx_courses_term ON courses(term_id);
CREATE INDEX IF NOT EXISTS idx_courses_subject ON courses(subject);
CREATE INDEX IF NOT EXISTS idx_courses_crn ON courses(crn);
CREATE INDEX IF NOT EXISTS idx_courses_instructor ON courses(instructor_name);
CREATE INDEX IF NOT EXISTS idx_courses_crn_term ON courses(crn, term_id);
CREATE INDEX IF NOT EXISTS idx_catalog_subject ON catalog(subject);
CREATE INDEX IF NOT EXISTS idx_section_details_crn ON section_details(crn, term_id);
CREATE INDEX IF NOT EXISTS idx_deps_crn ON course_dependencies(crn, term_id);
CREATE INDEX IF NOT EXISTS idx_deps_target
    ON course_dependencies(subject, course_number);
CREATE INDEX IF NOT EXISTS idx_section_instructors
    ON section_instructors(crn, term_id);
CREATE INDEX IF NOT EXISTS idx_section_instructors_name
    ON section_instructors(name);
CREATE INDEX IF NOT EXISTS idx_catalog_detail_subject ON catalog_detail(subject);
CREATE INDEX IF NOT EXISTS idx_meetings_crn_term ON meetings(crn, term_id);
CREATE INDEX IF NOT EXISTS idx_catalog_versions_course
    ON catalog_versions(subject, course_number);
CREATE INDEX IF NOT EXISTS idx_prereq_rules_course
    ON prereq_rules(subject, course_number, term_effective);
CREATE INDEX IF NOT EXISTS idx_prereq_rules_target
    ON prereq_rules(req_subject, req_course_number);
"""

# Columns added to tables that already exist in the shipped database. Migration is
# additive so that pointing the crawler at aus_courses.db upgrades it in place.
NEW_COLUMNS: dict[str, list[tuple[str, str]]] = {
    "courses": [
        ("part_of_term", "TEXT"), ("building", "TEXT"), ("building_name", "TEXT"),
        ("room", "TEXT"), ("campus_code", "TEXT"), ("enrollment", "INTEGER"),
        ("max_enrollment", "INTEGER"), ("seats_available_count", "INTEGER"),
        ("waitlist_capacity", "INTEGER"), ("waitlist_count", "INTEGER"),
        ("waitlist_available", "INTEGER"), ("cross_list", "TEXT"),
        ("cross_list_capacity", "INTEGER"), ("cross_list_count", "INTEGER"),
        ("cross_list_available", "INTEGER"), ("open_section", "BOOLEAN"),
        ("section_id", "INTEGER"),
    ],
    "instructors": [("banner_id", "TEXT")],
    "section_instructors": [("banner_id", "TEXT")],
    "catalog": [
        ("lecture_hours_high", "REAL"), ("lab_hours_high", "REAL"),
        ("other_hours_low", "REAL"), ("other_hours_high", "REAL"),
        ("bill_hours_low", "REAL"), ("bill_hours_high", "REAL"),
        ("credit_hours_high", "REAL"), ("college", "TEXT DEFAULT ''"),
        ("college_code", "TEXT DEFAULT ''"), ("department_code", "TEXT DEFAULT ''"),
        ("term_effective", "TEXT DEFAULT ''"), ("term_start", "TEXT DEFAULT ''"),
        ("term_end", "TEXT DEFAULT ''"), ("prereq_check_method", "TEXT DEFAULT ''"),
        ("title", "TEXT DEFAULT ''"),
    ],
    "catalog_detail": [("grade_modes", "TEXT DEFAULT ''")],
}


def migrate_schema(conn: sqlite3.Connection) -> list[str]:
    """Add any missing column to a pre-existing table. Idempotent."""
    added: list[str] = []
    existing = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    for table, columns in NEW_COLUMNS.items():
        if table not in existing:
            continue
        have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        for name, ddl in columns:
            if name not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
                added.append(f"{table}.{name}")
    if added:
        conn.commit()
        log.info("migrated %d columns: %s", len(added), ", ".join(added))
    return added


def init_db(db_path: str, force: bool = False) -> sqlite3.Connection:
    if force and os.path.exists(db_path):
        os.remove(db_path)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    migrate_schema(conn)          # widen old tables before CREATE IF NOT EXISTS
    conn.executescript(SCHEMA)
    conn.commit()
    return conn
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --project . pytest tests/test_db.py -v`
Expected: PASS, 6 tests

- [ ] **Step 5: Verify migration against a copy of the real shipped database**

Run:
```bash
cp aus_courses.db /tmp/migrate_check.db && \
uv run --project . python -c "
from auscrawl import db
conn = db.init_db('/tmp/migrate_check.db')
print('courses rows:', conn.execute('SELECT COUNT(*) FROM courses').fetchone()[0])
print('has building:', 'building' in {r[1] for r in conn.execute('PRAGMA table_info(courses)')})
print('reg dates kept:', conn.execute(\"SELECT registration_dates FROM courses WHERE registration_dates != '' LIMIT 1\").fetchone())
"
```
Expected: `courses rows: 75467`, `has building: True`, a non-empty registration_dates value.

- [ ] **Step 6: Commit**

```bash
git add auscrawl/db.py tests/test_db.py
git commit -m "Add Banner 9 schema with additive migration for the shipped database"
```

---

### Task 10: Bulk save functions

**Files:**
- Modify: `auscrawl/db.py`
- Test: `tests/test_db_save.py`

**Interfaces:**
- Consumes: `auscrawl.models`, `auscrawl.parse_json` helpers.
- Produces: `save_semesters(conn, semesters)`; `save_subjects(conn, refs, term_id)`;
  `save_sections(conn, sections)`; `save_catalog(conn, courses)`;
  `save_course_details(conn, details)`; `fix_first_seen(conn)`;
  `done_terms(conn) -> set[str]`; `done_course_versions(conn) -> set[tuple]`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_db_save.py
from auscrawl import db, models


def _section(crn="10394", term="202710", **kw):
    s = models.Section(crn=crn, term_id=term, subject="ACC", course_number="201",
                       title="Fund of Financial Accounting", section="01",
                       schedule_type="Lecture", campus="Main Campus",
                       enrollment=18, max_enrollment=18, seats_available_count=0,
                       **kw)
    s.meetings = [models.Meeting(
        crn=crn, term_id=term, meeting_index=0, meeting_type_desc="Class",
        begin_time="1100", end_time="1215", monday=True, wednesday=True,
        building="SBA", building_name="School of Business Administrtn", room="1104",
        start_date="08/24/2026", end_date="12/10/2026")]
    s.instructors = [models.InstructorRef(name="Karen Hawa", email="khawa@aus.edu",
                                          banner_id="220388", is_primary=True)]
    s.attributes = [models.CodeRef(code="AMTN", description="Actuarial Math Minor")]
    return s


def test_saving_a_section_fills_the_legacy_columns_in_legacy_formats(tmp_path):
    conn = db.init_db(str(tmp_path / "a.db"))
    db.save_sections(conn, [_section()])
    row = conn.execute("""SELECT days, start_time, end_time, classroom, date_range,
                                 class_type, seats_available, schedule_type
                          FROM courses WHERE crn='10394'""").fetchone()
    assert row == ("MW", "11:00 am", "12:15 pm",
                   "School of Business Administrtn 1104",
                   "Aug 24, 2026 - Dec 10, 2026", "Class", 0, "Lecture")


def test_saving_a_section_fills_the_new_columns(tmp_path):
    conn = db.init_db(str(tmp_path / "b.db"))
    db.save_sections(conn, [_section()])
    row = conn.execute("""SELECT building, room, enrollment, max_enrollment,
                                 seats_available_count FROM courses
                          WHERE crn='10394'""").fetchone()
    assert row == ("SBA", "1104", 18, 18, 0)


def test_meetings_are_written_as_their_own_rows(tmp_path):
    conn = db.init_db(str(tmp_path / "c.db"))
    db.save_sections(conn, [_section()])
    row = conn.execute("""SELECT meeting_index, building, room, monday, friday
                          FROM meetings WHERE crn='10394'""").fetchone()
    assert row == (0, "SBA", "1104", 1, 0)


def test_instructors_carry_banner_id_into_both_tables(tmp_path):
    conn = db.init_db(str(tmp_path / "d.db"))
    db.save_sections(conn, [_section()])
    assert conn.execute(
        "SELECT banner_id FROM instructors WHERE name='Karen Hawa'").fetchone()[0] == "220388"
    assert conn.execute(
        "SELECT banner_id, is_primary FROM section_instructors WHERE crn='10394'"
    ).fetchone() == ("220388", 1)


def test_registration_dates_is_never_overwritten_with_empty(tmp_path):
    conn = db.init_db(str(tmp_path / "e.db"))
    db.save_sections(conn, [_section()])
    conn.execute("UPDATE courses SET registration_dates='Apr 13, 2026 to Aug 31, 2026'")
    conn.commit()
    db.save_sections(conn, [_section()])          # a re-crawl, no reg dates available
    assert conn.execute(
        "SELECT registration_dates FROM courses WHERE crn='10394'"
    ).fetchone()[0] == "Apr 13, 2026 to Aug 31, 2026"


def test_first_seen_is_the_earliest_term(tmp_path):
    conn = db.init_db(str(tmp_path / "f.db"))
    db.save_sections(conn, [_section(term="202710"), _section(crn="99", term="200520")])
    db.fix_first_seen(conn)
    assert conn.execute(
        "SELECT first_seen FROM instructors WHERE name='Karen Hawa'").fetchone()[0] == "200520"
    assert conn.execute(
        "SELECT first_seen FROM subjects WHERE short_name='ACC'").fetchone()[0] == "200520"


def test_catalog_versions_and_flat_catalog_both_written(tmp_path):
    conn = db.init_db(str(tmp_path / "g.db"))
    old = models.CatalogCourse(subject="ACC", course_number="201", title="T",
                               term_effective="201510", description="old text",
                               credit_hours_low=3.0)
    new = models.CatalogCourse(subject="ACC", course_number="201", title="T",
                               term_effective="202610", description="new text",
                               credit_hours_low=3.0)
    db.save_catalog(conn, [old, new])
    assert conn.execute("SELECT COUNT(*) FROM catalog_versions").fetchone()[0] == 2
    # the flat table holds the newest version
    assert conn.execute(
        "SELECT description, term_effective FROM catalog WHERE subject='ACC'"
    ).fetchone() == ("new text", "202610")


def test_course_details_write_rules_and_json(tmp_path):
    conn = db.init_db(str(tmp_path / "h.db"))
    db.save_catalog(conn, [models.CatalogCourse(
        subject="CMP", course_number="305", title="T", term_effective="202610")])
    d = models.CourseDetail(
        subject="CMP", course_number="305", term_effective="202610",
        prerequisites="CMP 220 and (CMP 213 or MTH 213)",
        levels="Undergraduate", grade_modes="Standard Letter",
        prerequisites_json='{"type":"and","operands":[]}',
        rules=[models.PrereqRule(seq=0, req_subject="Computer Science",
                                 req_course_number="220", min_grade="C-")])
    db.save_course_details(conn, [d])
    assert conn.execute("SELECT COUNT(*) FROM prereq_rules").fetchone()[0] == 1
    assert conn.execute(
        "SELECT levels, grade_modes FROM catalog_versions WHERE subject='CMP'"
    ).fetchone() == ("Undergraduate", "Standard Letter")


def test_resume_helpers_report_what_is_already_stored(tmp_path):
    conn = db.init_db(str(tmp_path / "i.db"))
    db.save_sections(conn, [_section()])
    db.save_catalog(conn, [models.CatalogCourse(
        subject="ACC", course_number="201", title="T", term_effective="202610")])
    assert db.done_terms(conn) == {"202710"}
    assert ("ACC", "201", "202610") in db.done_course_versions(conn)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --project . pytest tests/test_db_save.py -v`
Expected: FAIL with `AttributeError: module 'auscrawl.db' has no attribute 'save_sections'`

- [ ] **Step 3: Write the implementation**

Add to `auscrawl/db.py` (import the parse helpers at the top:
`from .parse_json import classroom_string, days_string, format_date_range, to_12h`):

```python
def save_semesters(conn: sqlite3.Connection, semesters) -> None:
    conn.executemany(
        "INSERT OR IGNORE INTO semesters (term_id, term_name) VALUES (?, ?)",
        [(s.term_id, s.term_name) for s in semesters],
    )
    conn.commit()


def save_subjects(conn: sqlite3.Connection, refs, term_id: str) -> None:
    conn.executemany(
        "INSERT OR IGNORE INTO subjects (short_name, long_name, first_seen) "
        "VALUES (?, ?, ?)",
        [(r.code, r.description, term_id) for r in refs],
    )
    conn.commit()


def save_attributes(conn: sqlite3.Connection, refs, term_id: str) -> None:
    conn.executemany(
        "INSERT OR IGNORE INTO attributes (attribute, first_seen) VALUES (?, ?)",
        [(r.description, term_id) for r in refs],
    )
    conn.commit()


_COURSE_COLS = (
    "crn, term_id, subject, course_number, title, section, credits, schedule_type, "
    "instructional_method, campus, attributes, class_type, start_time, end_time, "
    "days, seats_available, classroom, date_range, instructor_name, "
    "instructor_email, is_lab, part_of_term, building, building_name, room, "
    "campus_code, enrollment, max_enrollment, seats_available_count, "
    "waitlist_capacity, waitlist_count, waitlist_available, cross_list, "
    "cross_list_capacity, cross_list_count, cross_list_available, open_section, "
    "section_id"
)


def _course_rows(s):
    """One row per meeting block, preserving the legacy row model."""
    primary = next((i for i in s.instructors if i.is_primary),
                   s.instructors[0] if s.instructors else None)
    base = (s.crn, s.term_id, s.subject, s.course_number, s.title, s.section,
            s.credits, s.schedule_type, s.instructional_method, s.campus,
            s.attributes_text)
    tail_static = (primary.name if primary else "",
                   primary.email if primary else "")
    extra = (s.part_of_term,)
    counts = (s.enrollment, s.max_enrollment, s.seats_available_count,
              s.waitlist_capacity, s.waitlist_count, s.waitlist_available,
              s.cross_list, s.cross_list_capacity, s.cross_list_count,
              s.cross_list_available, int(s.open_section), s.section_id)

    meetings = s.meetings or [None]
    for m in meetings:
        if m is None:
            yield base + ("", "", "", "", None, "", "") + tail_static + \
                (int("lab" in s.title.lower()),) + extra + ("", "", "", "") + counts
            continue
        is_lab = int("lab" in (s.schedule_type or "").lower()
                     or "lab" in (m.meeting_type_desc or "").lower())
        yield base + (
            m.meeting_type_desc, to_12h(m.begin_time), to_12h(m.end_time),
            days_string(m),
            None if s.seats_available_count is None else int(s.seats_available_count > 0),
            classroom_string(m), format_date_range(m.start_date, m.end_date),
        ) + tail_static + (is_lab,) + extra + (
            m.building, m.building_name, m.room, m.campus,
        ) + counts


def save_sections(conn: sqlite3.Connection, sections) -> None:
    """Insert sections, meetings and instructors.

    Sorted by term so INSERT OR IGNORE keeps the earliest occurrence, which is how
    first_seen comes out right for free. registration_dates is deliberately absent
    from the column list: Banner 9 has no source for it, and an UPDATE would erase
    values the old crawler collected.
    """
    ordered = sorted(sections, key=lambda s: s.term_id)
    placeholders = ", ".join("?" * len(_COURSE_COLS.split(", ")))
    rows = [r for s in ordered for r in _course_rows(s)]
    conn.executemany(
        f"INSERT OR IGNORE INTO courses ({_COURSE_COLS}) VALUES ({placeholders})",
        rows,
    )
    conn.executemany(
        """INSERT OR IGNORE INTO meetings (
               crn, term_id, meeting_index, meeting_type, meeting_type_desc,
               begin_time, end_time, monday, tuesday, wednesday, thursday, friday,
               saturday, sunday, building, building_name, room, campus, campus_desc,
               start_date, end_date, hours_week, credit_hour_session, schedule_type)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        [(m.crn, m.term_id, m.meeting_index, m.meeting_type, m.meeting_type_desc,
          m.begin_time, m.end_time, int(m.monday), int(m.tuesday), int(m.wednesday),
          int(m.thursday), int(m.friday), int(m.saturday), int(m.sunday),
          m.building, m.building_name, m.room, m.campus, m.campus_desc,
          m.start_date, m.end_date, m.hours_week, m.credit_hour_session,
          m.schedule_type)
         for s in ordered for m in s.meetings],
    )
    conn.executemany(
        "INSERT OR IGNORE INTO instructors (name, email, first_seen, banner_id) "
        "VALUES (?,?,?,?)",
        [(i.name, i.email, s.term_id, i.banner_id)
         for s in ordered for i in s.instructors],
    )
    conn.executemany(
        "INSERT OR IGNORE INTO section_instructors "
        "(crn, term_id, name, email, is_primary, banner_id) VALUES (?,?,?,?,?,?)",
        [(s.crn, s.term_id, i.name, i.email, int(i.is_primary), i.banner_id)
         for s in ordered for i in s.instructors],
    )
    conn.executemany(
        "INSERT OR IGNORE INTO attributes (attribute, first_seen) VALUES (?,?)",
        [(a.description, s.term_id) for s in ordered for a in s.attributes],
    )
    conn.commit()


def save_catalog(conn: sqlite3.Connection, courses) -> None:
    """Write every version, then refresh the flat table from the newest one."""
    conn.executemany(
        """INSERT OR IGNORE INTO catalog_versions (
               subject, course_number, term_effective, term_start, term_end, title,
               description, college, college_code, department, department_code,
               credit_hours_low, credit_hours_high, lecture_hours_low,
               lecture_hours_high, lab_hours_low, lab_hours_high, other_hours_low,
               other_hours_high, bill_hours_low, bill_hours_high, prereq_check_method)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        [(c.subject, c.course_number, c.term_effective, c.term_start, c.term_end,
          c.title, c.description, c.college, c.college_code, c.department,
          c.department_code, c.credit_hours_low, c.credit_hours_high,
          c.lecture_hours_low, c.lecture_hours_high, c.lab_hours_low,
          c.lab_hours_high, c.other_hours_low, c.other_hours_high,
          c.bill_hours_low, c.bill_hours_high, c.prereq_check_method)
         for c in courses],
    )
    conn.commit()
    refresh_flat_catalog(conn)


def refresh_flat_catalog(conn: sqlite3.Connection) -> None:
    """Project the newest catalog_versions row per course into the flat table."""
    conn.execute("""
        INSERT INTO catalog (subject, course_number, description, credit_hours,
                             lecture_hours, lab_hours, department, lecture_hours_high,
                             lab_hours_high, other_hours_low, other_hours_high,
                             bill_hours_low, bill_hours_high, credit_hours_high,
                             college, college_code, department_code, term_effective,
                             term_start, term_end, prereq_check_method, title)
        SELECT subject, course_number, description, credit_hours_low,
               lecture_hours_low, lab_hours_low, department, lecture_hours_high,
               lab_hours_high, other_hours_low, other_hours_high, bill_hours_low,
               bill_hours_high, credit_hours_high, college, college_code,
               department_code, term_effective, term_start, term_end,
               prereq_check_method, title
        FROM catalog_versions v
        WHERE v.term_effective = (SELECT MAX(term_effective) FROM catalog_versions w
                                  WHERE w.subject = v.subject
                                    AND w.course_number = v.course_number)
        ON CONFLICT(subject, course_number) DO UPDATE SET
            description = excluded.description,
            credit_hours = excluded.credit_hours,
            lecture_hours = excluded.lecture_hours,
            lab_hours = excluded.lab_hours,
            department = excluded.department,
            lecture_hours_high = excluded.lecture_hours_high,
            lab_hours_high = excluded.lab_hours_high,
            other_hours_low = excluded.other_hours_low,
            other_hours_high = excluded.other_hours_high,
            bill_hours_low = excluded.bill_hours_low,
            bill_hours_high = excluded.bill_hours_high,
            credit_hours_high = excluded.credit_hours_high,
            college = excluded.college,
            college_code = excluded.college_code,
            department_code = excluded.department_code,
            term_effective = excluded.term_effective,
            term_start = excluded.term_start,
            term_end = excluded.term_end,
            prereq_check_method = excluded.prereq_check_method,
            title = excluded.title
    """)
    conn.commit()


def save_course_details(conn: sqlite3.Connection, details) -> None:
    conn.executemany(
        """UPDATE catalog_versions SET prerequisites=?, corequisites=?, restrictions=?,
               course_attributes=?, levels=?, grade_modes=?, schedule_types=?,
               prerequisites_json=?, restrictions_json=?
           WHERE subject=? AND course_number=? AND term_effective=?""",
        [(d.prerequisites, d.corequisites, d.restrictions, d.course_attributes,
          d.levels, d.grade_modes, d.schedule_types, d.prerequisites_json,
          d.restrictions_json, d.subject, d.course_number, d.term_effective)
         for d in details],
    )
    conn.executemany(
        """INSERT OR REPLACE INTO prereq_rules (
               subject, course_number, term_effective, seq, connector, open_paren,
               close_paren, test_code, test_score, req_subject, req_course_number,
               req_level, min_grade)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        [(d.subject, d.course_number, d.term_effective, r.seq, r.connector,
          int(r.open_paren), int(r.close_paren), r.test_code, r.test_score,
          r.req_subject, r.req_course_number, r.req_level, r.min_grade)
         for d in details for r in d.rules],
    )
    conn.commit()
    refresh_catalog_detail(conn)


def refresh_catalog_detail(conn: sqlite3.Connection) -> None:
    """Keep the legacy catalog_detail table in step with the newest version."""
    conn.execute("""
        INSERT INTO catalog_detail (subject, course_number, term_id, levels,
                                    schedule_types, course_attributes, prerequisites,
                                    corequisites, restrictions, grade_modes)
        SELECT subject, course_number, term_effective, levels, schedule_types,
               course_attributes, prerequisites, corequisites, restrictions,
               grade_modes
        FROM catalog_versions v
        WHERE v.term_effective = (SELECT MAX(term_effective) FROM catalog_versions w
                                  WHERE w.subject = v.subject
                                    AND w.course_number = v.course_number)
        ON CONFLICT(subject, course_number) DO UPDATE SET
            term_id = excluded.term_id, levels = excluded.levels,
            schedule_types = excluded.schedule_types,
            course_attributes = excluded.course_attributes,
            prerequisites = excluded.prerequisites,
            corequisites = excluded.corequisites,
            restrictions = excluded.restrictions,
            grade_modes = excluded.grade_modes
    """)
    conn.commit()


def fix_first_seen(conn: sqlite3.Connection) -> None:
    """Backfill first_seen from the earliest term each entity actually appears in."""
    conn.execute("""
        UPDATE subjects SET first_seen = (
            SELECT MIN(term_id) FROM courses WHERE courses.subject = subjects.short_name
        ) WHERE EXISTS (
            SELECT 1 FROM courses WHERE courses.subject = subjects.short_name)
    """)
    conn.execute("""
        UPDATE instructors SET first_seen = (
            SELECT MIN(term_id) FROM section_instructors si
            WHERE si.name = instructors.name
        ) WHERE EXISTS (
            SELECT 1 FROM section_instructors si WHERE si.name = instructors.name)
    """)
    conn.commit()


def done_terms(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute("SELECT DISTINCT term_id FROM courses")}


def done_course_versions(conn: sqlite3.Connection) -> set[tuple[str, str, str]]:
    return {(r[0], r[1], r[2]) for r in conn.execute(
        "SELECT subject, course_number, term_effective FROM catalog_versions "
        "WHERE levels != '' OR prerequisites != '' OR restrictions != ''")}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --project . pytest tests/test_db_save.py -v`
Expected: PASS, 9 tests

- [ ] **Step 5: Commit**

```bash
git add auscrawl/db.py tests/test_db_save.py
git commit -m "Add bulk save functions preserving legacy column formats"
```

---

### Task 11: Fetchers for every endpoint

**Files:**
- Create: `auscrawl/fetch.py`
- Test: `tests/test_fetch.py`

**Interfaces:**
- Consumes: `auscrawl.http`, `auscrawl.session`, `auscrawl.parse_json`,
  `auscrawl.parse_html`.
- Produces: `async fetch_terms(client, rate) -> list[Semester]`;
  `async fetch_reference(client, term_id, kind, rate) -> list[CodeRef]`;
  `async fetch_all_pages(sess, endpoint_key, term_id, parser) -> list`;
  `async fetch_course_detail(client, term_id, subject, number, rate) -> CourseDetail`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_fetch.py
import httpx

from auscrawl import fetch, session
from tests.conftest import read_b9


async def test_fetch_terms_parses_the_live_shape():
    def handler(request):
        return httpx.Response(200, content=read_b9("terms.json"),
                              headers={"content-type": "application/json"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        terms = await fetch.fetch_terms(c, None)
    assert len(terms) >= 100
    assert terms[0].term_id.isdigit()


async def test_fetch_all_pages_walks_the_offsets_until_total_is_reached():
    page = read_b9("sections_202710_p0.json")
    import json
    payload = json.loads(page)
    payload["totalCount"] = 1200
    calls = []

    def handler(request):
        if "termSelection" in str(request.url) or "term/search" in str(request.url):
            return httpx.Response(200, json={"fwdURL": "/x"})
        offset = int(request.url.params["pageOffset"])
        calls.append(offset)
        body = dict(payload)
        body["data"] = payload["data"][:200] if offset == 1000 else payload["data"]
        return httpx.Response(200, json=body)

    from auscrawl.parse_json import parse_sections
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        sess = session.TermSession(c)
        await sess.bind("202710", "search")
        rows = await fetch.fetch_all_pages(sess, "sections", "202710", parse_sections)

    assert calls == [0, 500, 1000]
    assert len(rows) == 1200


async def test_fetch_course_detail_merges_five_fragments():
    def handler(request):
        path = request.url.path
        if path.endswith("getPrerequisites"):
            return httpx.Response(200, content=read_b9("prereqs_CMP305.html"))
        if path.endswith("getCorequisites"):
            return httpx.Response(200, content=read_b9("coreqs_ACC201.html"))
        if path.endswith("getRestrictions"):
            return httpx.Response(200, content=read_b9("restrictions_ACC201.html"))
        if path.endswith("getCourseAttributes"):
            return httpx.Response(200, content=read_b9("attributes_ACC201.html"))
        return httpx.Response(200, content=read_b9("catalogdetails_ACC201.html"))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        d = await fetch.fetch_course_detail(c, "202710", "CMP", "305", "202610", None)

    assert d.subject == "CMP" and d.term_effective == "202610"
    assert len(d.rules) == 3
    assert d.prerequisites_json.startswith('{"type":"and"')
    assert d.corequisites == ""
    assert "Undergraduate" in d.levels
    assert d.grade_modes
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --project . pytest tests/test_fetch.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'auscrawl.fetch'`

- [ ] **Step 3: Write the implementation**

```python
# auscrawl/fetch.py
"""One coroutine per endpoint. All network access lives here."""

import asyncio
from typing import Callable, Optional

import httpx

from . import config, parse_html, parse_json
from .http import RateLimiter, request_with_retry
from .models import CourseDetail
from .session import TermSession

_REF_ENDPOINTS = {"subject": "ref_subject", "instructor": "ref_instructor",
                  "attribute": "ref_attribute"}


async def fetch_terms(client: httpx.AsyncClient, rate: Optional[RateLimiter]):
    resp = await request_with_retry(
        client, "GET", config.EP["terms"],
        params={"searchTerm": "", "offset": 1, "max": 500}, rate=rate)
    return parse_json.parse_terms(resp.content)


async def fetch_reference(client: httpx.AsyncClient, term_id: str, kind: str,
                          rate: Optional[RateLimiter]):
    resp = await request_with_retry(
        client, "GET", config.EP[_REF_ENDPOINTS[kind]],
        params={"searchTerm": "", "term": term_id, "offset": 1, "max": 5000},
        rate=rate)
    return parse_json.parse_code_list(resp.content)


async def fetch_all_pages(sess: TermSession, endpoint_key: str, term_id: str,
                          parser: Callable) -> list:
    """Page through a search endpoint until totalCount is covered."""
    out: list = []
    offset = 0
    while True:
        raw = await sess.fetch_page(endpoint_key, term_id, offset)
        total, rows = parser(raw, term_id)
        out.extend(rows)
        offset += config.PAGE_SIZE
        if offset >= total or not rows:
            return out


async def fetch_course_detail(client: httpx.AsyncClient, term_id: str, subject: str,
                              course_number: str, term_effective: str,
                              rate: Optional[RateLimiter]) -> CourseDetail:
    form = {"term": term_id, "subjectCode": subject, "courseNumber": course_number}

    async def one(key: str) -> bytes:
        resp = await request_with_retry(client, "POST", config.EP[key],
                                        form=form, rate=rate)
        return resp.content

    prereq, coreq, restr, attrs, cat = await asyncio.gather(
        one("prereqs"), one("coreqs"), one("restrictions"),
        one("course_attributes"), one("course_catalog_details"),
    )

    rules = parse_html.parse_prereq_rules(prereq)
    details = parse_html.parse_catalog_details(cat)
    return CourseDetail(
        subject=subject,
        course_number=course_number,
        term_effective=term_effective,
        prerequisites=parse_html.fragment_text(prereq),
        corequisites=parse_html.fragment_text(coreq),
        restrictions=parse_html.fragment_text(restr),
        course_attributes=", ".join(d for d, _ in parse_html.parse_attributes(attrs)),
        levels=details["levels"],
        grade_modes=details["grade_modes"],
        schedule_types=details["schedule_types"],
        prerequisites_json=parse_html.prereq_json(rules),
        restrictions_json=parse_html.restrictions_json(restr),
        rules=rules,
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --project . pytest tests/test_fetch.py -v`
Expected: PASS, 3 tests

- [ ] **Step 5: Commit**

```bash
git add auscrawl/fetch.py tests/test_fetch.py
git commit -m "Add fetchers for every Banner 9 endpoint"
```

---

### Task 12: Pipeline orchestration

**Files:**
- Create: `auscrawl/pipeline.py`
- Test: `tests/test_pipeline.py`

**Interfaces:**
- Consumes: everything above.
- Produces: `async run(opts) -> None` where `opts` is the parsed CLI namespace;
  `select_terms(all_terms, opts, existing) -> list[Semester]`;
  `pending_versions(catalog_courses, done) -> list[tuple[str, str, str]]`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_pipeline.py
import argparse

from auscrawl import models, pipeline


def _t(*ids):
    return [models.Semester(term_id=i, term_name=i) for i in ids]


def opts(**kw):
    base = dict(terms=None, latest=False, resume=False, force=False,
                no_catalog=False, no_details=False)
    base.update(kw)
    return argparse.Namespace(**base)


def test_latest_selects_only_the_newest_term():
    got = pipeline.select_terms(_t("202710", "202640", "200520"), opts(latest=True), set())
    assert [s.term_id for s in got] == ["202710"]


def test_explicit_terms_are_honoured_and_filtered_to_real_ones():
    got = pipeline.select_terms(_t("202710", "202640"), opts(terms=["202640", "999999"]), set())
    assert [s.term_id for s in got] == ["202640"]


def test_resume_skips_terms_already_stored():
    got = pipeline.select_terms(_t("202710", "202640"), opts(resume=True), {"202640"})
    assert [s.term_id for s in got] == ["202710"]


def test_force_ignores_what_is_already_stored():
    got = pipeline.select_terms(_t("202710", "202640"), opts(force=True), {"202640"})
    assert len(got) == 2


def test_pending_versions_dedupes_across_terms_and_skips_done():
    courses = [
        models.CatalogCourse(subject="ACC", course_number="201", title="T",
                             term_effective="202610"),
        models.CatalogCourse(subject="ACC", course_number="201", title="T",
                             term_effective="202610"),   # same version, other term
        models.CatalogCourse(subject="CMP", course_number="305", title="T",
                             term_effective="201510"),
    ]
    pending = pipeline.pending_versions(courses, done={("CMP", "305", "201510")})
    assert pending == [("ACC", "201", "202610")]


def test_pending_versions_returns_the_representative_term_for_each_version():
    courses = [models.CatalogCourse(subject="ACC", course_number="201", title="T",
                                    term_effective="202610")]
    pending = pipeline.pending_versions(courses, done=set())
    assert pending[0][2] == "202610"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --project . pytest tests/test_pipeline.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'auscrawl.pipeline'`

- [ ] **Step 3: Write the implementation**

```python
# auscrawl/pipeline.py
"""The five crawl phases.

Sections and catalog run on a pool of independent sessions because the search
endpoints are stateful — one bound term per session. Details run on one shared
session at higher parallelism because those endpoints are stateless.
"""

import asyncio
import logging

from rich.console import Console
from rich.progress import BarColumn, Progress, TextColumn, TimeRemainingColumn

from . import config, db, fetch
from .http import RateLimiter, make_client
from .models import Semester
from .parse_json import parse_catalog, parse_sections
from .session import SessionPool

log = logging.getLogger("auscrawl")
console = Console()


def select_terms(all_terms: list[Semester], opts, existing: set[str]) -> list[Semester]:
    terms = sorted(all_terms, key=lambda s: s.term_id, reverse=True)
    if opts.latest:
        return terms[:1]
    if opts.terms:
        wanted = set(opts.terms)
        return [s for s in terms if s.term_id in wanted]
    if opts.resume and not opts.force:
        return [s for s in terms if s.term_id not in existing]
    return terms


def pending_versions(catalog_courses, done: set) -> list[tuple[str, str, str]]:
    """Unique (subject, course_number, term_effective) still needing detail calls."""
    seen: set[tuple[str, str, str]] = set()
    out: list[tuple[str, str, str]] = []
    for c in catalog_courses:
        key = (c.subject, c.course_number, c.term_effective)
        if key in seen or key in done or not c.term_effective:
            continue
        seen.add(key)
        out.append(key)
    return out


def _progress() -> Progress:
    return Progress(
        TextColumn("[bold blue]{task.description}"),
        BarColumn(),
        TextColumn("{task.completed}/{task.total}"),
        TimeRemainingColumn(),
        console=console,
    )


async def run(opts) -> None:
    conn = db.init_db(opts.output, force=opts.force)
    rate = RateLimiter(opts.rate, max_rate=config.MAX_RATE, min_rate=config.MIN_RATE)

    async with make_client(4) as meta_client:
        all_terms = await fetch.fetch_terms(meta_client, rate)
        db.save_semesters(conn, all_terms)
        console.print(f"[green]Terms:[/green] {len(all_terms)} available")

        terms = select_terms(all_terms, opts, db.done_terms(conn))
        console.print(f"[green]Crawling:[/green] {len(terms)} terms")
        if not terms:
            return

        # Phase 2 — reference data
        with _progress() as bar:
            task = bar.add_task("Reference", total=len(terms))
            for s in terms:
                subjects = await fetch.fetch_reference(meta_client, s.term_id,
                                                       "subject", rate)
                db.save_subjects(conn, subjects, s.term_id)
                attrs = await fetch.fetch_reference(meta_client, s.term_id,
                                                    "attribute", rate)
                db.save_attributes(conn, attrs, s.term_id)
                bar.advance(task)

    # Phase 3 — sections
    with _progress() as bar:
        task = bar.add_task("Sections", total=len(terms))

        async def one_term(sess, term_id):
            await sess.bind(term_id, "search")
            rows = await fetch.fetch_all_pages(sess, "sections", term_id,
                                               parse_sections)
            db.save_sections(conn, rows)
            bar.advance(task)
            return len(rows)

        async with SessionPool(config.SESSION_POOL_SIZE, rate) as pool:
            counts = await pool.map_terms([s.term_id for s in terms], one_term)
    console.print(f"[green]Sections:[/green] {sum(counts)} rows")
    db.fix_first_seen(conn)

    if opts.no_catalog:
        return

    # Phase 4 — catalog
    catalog_courses: list = []
    with _progress() as bar:
        task = bar.add_task("Catalog", total=len(terms))

        async def one_catalog(sess, term_id):
            await sess.bind(term_id, "courseSearch")
            rows = await fetch.fetch_all_pages(sess, "catalog", term_id, parse_catalog)
            db.save_catalog(conn, rows)
            catalog_courses.extend(rows)
            bar.advance(task)
            return len(rows)

        async with SessionPool(config.SESSION_POOL_SIZE, rate) as pool:
            counts = await pool.map_terms([s.term_id for s in terms], one_catalog)
    console.print(f"[green]Catalog:[/green] {sum(counts)} rows")

    if opts.no_details:
        return

    # Phase 5 — course details, stateless and parallel
    done = set() if opts.force else db.done_course_versions(conn)
    pending = pending_versions(catalog_courses, done)
    console.print(f"[green]Details:[/green] {len(pending)} course versions to fetch")
    if not pending:
        return

    sem = asyncio.Semaphore(config.DETAIL_CONCURRENCY)
    batch: list = []
    async with make_client(config.DETAIL_CONCURRENCY) as client, _progress() as bar:
        task = bar.add_task("Details", total=len(pending))

        async def one_detail(subject, number, term_effective):
            async with sem:
                d = await fetch.fetch_course_detail(
                    client, term_effective, subject, number, term_effective, rate)
            batch.append(d)
            bar.advance(task)
            if len(batch) >= config.DETAIL_BATCH_SIZE:
                db.save_course_details(conn, batch[:])
                batch.clear()

        await asyncio.gather(*(one_detail(*key) for key in pending))

    if batch:
        db.save_course_details(conn, batch)
    console.print("[bold green]Done.[/bold green]")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --project . pytest tests/test_pipeline.py -v`
Expected: PASS, 6 tests

- [ ] **Step 5: Commit**

```bash
git add auscrawl/pipeline.py tests/test_pipeline.py
git commit -m "Add the five-phase crawl pipeline"
```

---

### Task 13: CLI and the crawl.py shim

**Files:**
- Create: `auscrawl/cli.py`
- Modify: `crawl.py` (replace entirely with a shim)
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: `auscrawl.pipeline`.
- Produces: `build_parser() -> argparse.ArgumentParser`; `main(argv=None) -> int`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_cli.py
import pytest

from auscrawl import cli, config


def test_defaults_match_the_documented_behaviour():
    a = cli.build_parser().parse_args([])
    assert a.output == "aus_data.db"
    assert a.rate == config.DEFAULT_RATE
    assert a.latest is False and a.resume is False and a.force is False


def test_output_never_defaults_to_the_shipped_database():
    a = cli.build_parser().parse_args([])
    assert a.output != "aus_courses.db"


def test_documented_flags_all_parse():
    p = cli.build_parser()
    assert p.parse_args(["--latest"]).latest is True
    assert p.parse_args(["-t", "202620", "202510"]).terms == ["202620", "202510"]
    assert p.parse_args(["--resume"]).resume is True
    assert p.parse_args(["--force"]).force is True
    assert p.parse_args(["--no-catalog", "--no-details"]).no_details is True
    assert p.parse_args(["--rate", "4"]).rate == 4.0
    assert p.parse_args(["-o", "x.db"]).output == "x.db"


def test_rate_above_the_ceiling_is_rejected():
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["--rate", "500"])


def test_crawl_shim_still_exposes_main():
    import crawl
    assert callable(crawl.main)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --project . pytest tests/test_cli.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'auscrawl.cli'`

- [ ] **Step 3: Write the implementation**

```python
# auscrawl/cli.py
"""Command line entry point."""

import argparse
import asyncio
import logging

from rich.logging import RichHandler

from . import config
from .pipeline import run

RATE_CEILING = 30.0


def _rate(value: str) -> float:
    r = float(value)
    if not 0 < r <= RATE_CEILING:
        raise argparse.ArgumentTypeError(
            f"rate must be between 0 and {RATE_CEILING} req/s")
    return r


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="crawl.py",
        description="Crawl AUS Banner 9 course data into SQLite.",
    )
    p.add_argument("-o", "--output", default="aus_data.db",
                   help="output database (default: aus_data.db; the shipped "
                        "snapshot aus_courses.db is deliberately not the default)")
    p.add_argument("-t", "--terms", nargs="+", metavar="TERM",
                   help="specific term ids, e.g. 202620 202510")
    p.add_argument("--latest", action="store_true",
                   help="crawl only the newest term")
    p.add_argument("--resume", action="store_true",
                   help="skip terms and course versions already stored")
    p.add_argument("--force", action="store_true",
                   help="delete the database and start over")
    p.add_argument("--no-catalog", action="store_true",
                   help="skip the catalog phase (and details, which depend on it)")
    p.add_argument("--no-details", action="store_true",
                   help="skip the per-course detail phase")
    p.add_argument("--rate", type=_rate, default=config.DEFAULT_RATE,
                   help=f"target requests per second (default: {config.DEFAULT_RATE})")
    p.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(message)s", handlers=[RichHandler(rich_tracebacks=True)],
    )
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        logging.getLogger("auscrawl").warning("interrupted; progress is committed")
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

```python
#!/usr/bin/env python3
"""AUSCrawl entry point.

The implementation lives in the auscrawl package; this shim keeps the documented
`uv run python crawl.py ...` commands working.
"""

from auscrawl.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --project . pytest tests/test_cli.py -v`
Expected: PASS, 5 tests

- [ ] **Step 5: Verify the shim runs**

Run: `uv run --project . python crawl.py --help`
Expected: usage text listing `--latest`, `--resume`, `--force`, `--rate`.

- [ ] **Step 6: Commit**

```bash
git add auscrawl/cli.py crawl.py tests/test_cli.py
git commit -m "Add CLI and reduce crawl.py to a shim"
```

---

### Task 14: Delete the dead Banner 8 code and tests

The old parsers target HTML that no longer exists at any URL. Leaving them means
dead code and a red test suite.

**Files:**
- Delete: `tests/test_parsers.py`, `tests/test_coverage.py`, `tests/test_limiter.py`,
  `tests/test_http_logic.py`, `tests/test_db_and_config.py`, `tests/capture_fixtures.py`
- Delete: `tests/fixtures/*.html`, `tests/fixtures/detail_golden.json`,
  `tests/fixtures/manifest.txt`
- Modify: `tests/conftest.py`

- [ ] **Step 1: Confirm nothing still imports the old module**

Run: `grep -rn "^import crawl\|from crawl import\|crawl\." tests/ auscrawl/ | grep -v test_cli`
Expected: no output. If there is output, that test still depends on Banner 8 internals
and is being deleted in the next step.

- [ ] **Step 2: Delete the Banner 8 tests and fixtures**

```bash
git rm tests/test_parsers.py tests/test_coverage.py tests/test_limiter.py \
       tests/test_http_logic.py tests/test_db_and_config.py tests/capture_fixtures.py
git rm tests/fixtures/*.html tests/fixtures/detail_golden.json tests/fixtures/manifest.txt
```

- [ ] **Step 3: Trim conftest to the Banner 9 fixtures**

Replace `tests/conftest.py` with:

```python
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

FIXTURES = Path(__file__).resolve().parent / "fixtures"
B9 = FIXTURES / "banner9"


@pytest.fixture(scope="session")
def b9_dir() -> Path:
    return B9


def read_b9(name: str) -> bytes:
    return (B9 / name).read_bytes()
```

- [ ] **Step 4: Run the whole suite**

Run: `uv run --project . pytest -v`
Expected: PASS, 0 failures, 0 errors. Record the total count.

- [ ] **Step 5: Commit**

```bash
git add -A tests/
git commit -m "Remove the Banner 8 parsers' tests and fixtures"
```

---

### Task 15: Live smoke test and cross-check against the shipped database

This is the acceptance gate. Nothing merges until this passes.

**Files:**
- Create: `tests/test_live.py` (marked, not run by default)
- Create: `scripts/crosscheck.py`

- [ ] **Step 1: Crawl one term into a scratch database**

Run:
```bash
uv run --project . python crawl.py --latest -o /tmp/latest.db --rate 8
```
Expected: all five phases complete; final line `Done.`

- [ ] **Step 2: Check the term came out whole**

Run:
```bash
sqlite3 /tmp/latest.db "
SELECT 'sections', COUNT(DISTINCT crn) FROM courses;
SELECT 'rows', COUNT(*) FROM courses;
SELECT 'meetings', COUNT(*) FROM meetings;
SELECT 'wrong term', COUNT(*) FROM courses WHERE term_id != '202710';
SELECT 'no title', COUNT(*) FROM courses WHERE title = '';
SELECT 'versions', COUNT(*) FROM catalog_versions;
SELECT 'with levels', COUNT(*) FROM catalog_versions WHERE levels != '';
SELECT 'prereq rules', COUNT(*) FROM prereq_rules;"
```
Expected: `wrong term` is 0, `no title` is 0, `sections` is within 1% of the 1,814
`totalCount` the API reports for the term, `prereq rules` is greater than 0.

- [ ] **Step 3: Write the cross-check script**

```python
# scripts/crosscheck.py
"""Compare a freshly crawled term against the shipped database.

Usage: uv run --project . python scripts/crosscheck.py <new.db> <old.db> <term_id>
"""

import sqlite3
import sys


def crns(path, term):
    conn = sqlite3.connect(path)
    try:
        return {r[0] for r in conn.execute(
            "SELECT DISTINCT crn FROM courses WHERE term_id = ?", (term,))}
    finally:
        conn.close()


def main():
    new_db, old_db, term = sys.argv[1], sys.argv[2], sys.argv[3]
    a, b = crns(new_db, term), crns(old_db, term)
    only_new, only_old = sorted(a - b), sorted(b - a)
    print(f"term {term}: new={len(a)} old={len(b)} shared={len(a & b)}")
    print(f"only in new ({len(only_new)}): {only_new[:20]}")
    print(f"only in old ({len(only_old)}): {only_old[:20]}")
    return 0 if not only_old else 1


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Cross-check a historical term**

Run:
```bash
uv run --project . python crawl.py -t 201510 -o /tmp/hist.db --rate 8 && \
uv run --project . python scripts/crosscheck.py /tmp/hist.db aus_courses.db 201510
```
Expected: `only in old` is empty, or every CRN in it is explainable (for example a
section AUS later deleted). Investigate any non-empty result before continuing; a
missing CRN means a paging or parsing bug.

- [ ] **Step 5: Verify the migration path on a copy of the shipped database**

Run:
```bash
cp aus_courses.db /tmp/upgrade.db && \
uv run --project . python crawl.py --latest -o /tmp/upgrade.db --rate 8 && \
sqlite3 /tmp/upgrade.db "
SELECT 'total rows', COUNT(*) FROM courses;
SELECT 'reg dates kept', COUNT(*) FROM courses WHERE registration_dates != '';
SELECT 'new cols filled', COUNT(*) FROM courses WHERE enrollment IS NOT NULL;"
```
Expected: `total rows` at least 75,467 (nothing lost), `reg dates kept` greater than 0
(the old values survived), `new cols filled` greater than 0.

- [ ] **Step 6: Add the live test behind a marker**

```python
# tests/test_live.py
"""Live tests. Run explicitly: uv run --project . pytest -m live"""

import pytest

from auscrawl import fetch
from auscrawl.http import make_client

pytestmark = pytest.mark.live


async def test_terms_endpoint_still_answers():
    async with make_client(2) as c:
        terms = await fetch.fetch_terms(c, None)
    assert len(terms) > 90
    assert all(t.term_id.isdigit() for t in terms)


async def test_detail_endpoints_still_return_a_prereq_table():
    async with make_client(2) as c:
        d = await fetch.fetch_course_detail(c, "202710", "CMP", "305", "202610", None)
    assert d.rules, "the prerequisite table shape changed"
```

Register the marker in `pyproject.toml`:

```toml
[tool.pytest.ini_options]
testpaths = ["tests"]
asyncio_mode = "auto"
markers = ["live: hits the real Banner server"]
addopts = "-m 'not live'"
```

- [ ] **Step 7: Run both suites**

Run: `uv run --project . pytest -v && uv run --project . pytest -m live -v`
Expected: both PASS.

- [ ] **Step 8: Commit**

```bash
git add scripts/crosscheck.py tests/test_live.py pyproject.toml
git commit -m "Add live smoke tests and the shipped-database cross-check"
```

---

### Task 16: Documentation

**Files:**
- Modify: `README.md`
- Modify: `CLAUDE.md`

- [ ] **Step 1: Rewrite README.md**

Replace the "Banner Technical Details" section with the endpoint table from the spec.
Replace the schema section with the current 16 tables. Add these example queries:

````markdown
```sql
-- Which sections still have seats, with real counts rather than a boolean
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

-- Every course that accepts a placement-test score instead of a prerequisite course
SELECT DISTINCT subject, course_number, test_code, test_score
FROM prereq_rules
WHERE test_code != ''
ORDER BY subject, course_number;

-- Rooms used most heavily in a term
SELECT building_name, room, COUNT(*) AS blocks
FROM meetings
WHERE term_id = '202710' AND room != ''
GROUP BY building_name, room
ORDER BY blocks DESC
LIMIT 20;
```
````

Add a "Known gaps" section stating that `registration_dates` has no Banner 9 source,
that existing values are preserved, and that `schedule_type` now holds the real
schedule type rather than the literal string `"Schedule Type"` that the Banner 8
parser stored.

- [ ] **Step 2: Rewrite CLAUDE.md**

Update: the "What this is" section (package layout, not a single file); the commands
(unchanged flags, new `--rate` default of 10); the architecture section (five phases,
the stateful-session trap, the stateless detail endpoints); the schema notes (16
tables); and correct the false claim that there is no test suite — there is, run with
`uv run --project . pytest`, plus `pytest -m live` for the network tests.

- [ ] **Step 3: Verify the documented commands actually work**

Run:
```bash
uv run --project . python crawl.py --help && \
uv run --project . pytest -q && \
sqlite3 /tmp/latest.db "SELECT COUNT(*) FROM prereq_rules WHERE test_code != ''"
```
Expected: help text prints, tests pass, the count is greater than 0.

- [ ] **Step 4: Commit**

```bash
git add README.md CLAUDE.md
git commit -m "Document the Banner 9 crawler"
```

---

### Task 17: Full crawl and merge

- [ ] **Step 1: Run the complete crawl**

Run:
```bash
cp aus_courses.db /tmp/full.db && \
uv run --project . python crawl.py -o /tmp/full.db --rate 10 2>&1 | tee /tmp/crawl.log
```
Expected: roughly 70 minutes, all 101 terms, ending in `Done.`

- [ ] **Step 2: Check the log for anything that did not recover**

Run: `grep -ciE "error|failed|mismatch" /tmp/crawl.log`
Expected: retries logged as warnings are fine; zero unrecovered failures and zero
`TermMismatch`. Investigate any mismatch before proceeding — it means the session
guard caught corruption.

- [ ] **Step 3: Compare coverage against the shipped database**

Run:
```bash
for t in 200520 201010 201510 202010 202510 202710; do
  uv run --project . python scripts/crosscheck.py /tmp/full.db aus_courses.db $t
done
```
Expected: `only in old` empty for every term.

- [ ] **Step 4: Confirm the new data is populated across history**

Run:
```bash
sqlite3 /tmp/full.db "
SELECT 'terms', COUNT(DISTINCT term_id) FROM courses;
SELECT 'sections', COUNT(DISTINCT crn || term_id) FROM courses;
SELECT 'meetings', COUNT(*) FROM meetings;
SELECT 'catalog versions', COUNT(*) FROM catalog_versions;
SELECT 'versions with prereqs', COUNT(*) FROM catalog_versions WHERE prerequisites != '';
SELECT 'prereq rules', COUNT(*) FROM prereq_rules;
SELECT 'test-score rules', COUNT(*) FROM prereq_rules WHERE test_code != '';
SELECT 'banner ids', COUNT(*) FROM instructors WHERE banner_id != '';
SELECT 'wrong term rows', COUNT(*) FROM courses c
  WHERE NOT EXISTS (SELECT 1 FROM semesters s WHERE s.term_id = c.term_id);"
```
Expected: 101 terms, at least 73,778 sections, `wrong term rows` 0, and every other
count greater than 0.

- [ ] **Step 5: Run the full suite one last time**

Run: `uv run --project . pytest -v && uv run --project . pytest -m live -v`
Expected: both PASS.

- [ ] **Step 6: Merge**

Only after every check above passes:

```bash
git checkout master
git merge --no-ff banner9-rewrite -m "Rewrite AUSCrawl for Banner 9 JSON APIs"
```

Swapping the crawled database into `aus_courses.db` and publishing a release is a
separate decision, not part of this merge.
