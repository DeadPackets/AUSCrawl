"""One coroutine per endpoint. All network access lives here."""

import asyncio
import logging
from collections.abc import Callable

import httpx

from . import config, parse_html
from .http import RateLimiter, backoff_delay, request_with_retry
from .models import CourseDetail
from .parse_json import parse_code_list, parse_terms
from .session import TermSession

log = logging.getLogger("auscrawl")

_REF_ENDPOINTS = {"subject": "ref_subject", "instructor": "ref_instructor",
                  "attribute": "ref_attribute"}

# A 500 from a detail endpoint usually means "no such course in that term", which
# no amount of retrying fixes. Two attempts covers the genuinely transient case.
DETAIL_RETRIES = 2

# Search pages get few HTTP-level retries because the real recovery is the term-level
# rebind in fetch_all_pages, not another identical request.
PAGE_RETRIES = 2
TERM_ATTEMPTS = 3


async def fetch_terms(client: httpx.AsyncClient, rate: RateLimiter | None):
    resp = await request_with_retry(
        client, "GET", config.EP["terms"],
        params={"searchTerm": "", "offset": 1, "max": 500}, rate=rate)
    return parse_terms(resp.content)


async def fetch_reference(client: httpx.AsyncClient, term_id: str, kind: str,
                          rate: RateLimiter | None):
    resp = await request_with_retry(
        client, "GET", config.EP[_REF_ENDPOINTS[kind]],
        params={"searchTerm": "", "term": term_id, "offset": 1, "max": 5000},
        rate=rate)
    return parse_code_list(resp.content)


class EmptyTerm(RuntimeError):
    """A term returned no rows at all, which at AUS always means a failed bind."""


async def fetch_all_pages(sess: TermSession, endpoint_key: str, term_id: str,
                          parser: Callable, mode: str,
                          backoff: Callable[[int], float] = backoff_delay) -> list:
    """Bind the term, then page through the endpoint until totalCount is covered.

    A bind that does not take shows up two ways, and neither is fixable by retrying
    the page request itself — only a rebind helps, so the retry lives here:

    * sections answer HTTP 200 with totalCount 0, so an unguarded crawler would
      record zero sections for the term and never notice;
    * the catalog answers HTTP 500.

    Every AUS term has rows, so either symptom means rebind and start the term over.
    Observed failures were transient and load-related rather than deterministic, so
    the retry is patient: three attempts, backoff between them, and a resetDataForm
    to clear whatever search state the session's previous term left behind.
    """
    last_error: Exception | None = None
    for attempt in range(1, TERM_ATTEMPTS + 1):
        try:
            if attempt > 1:
                await asyncio.sleep(backoff(attempt))
            await sess.reset(mode)
            await sess.bind(term_id, mode)
            out: list = []
            offset = 0
            while True:
                raw = await sess.fetch_page(endpoint_key, term_id, offset,
                                            max_retries=PAGE_RETRIES)
                total, rows = parser(raw, term_id)
                out.extend(rows)
                offset += config.PAGE_SIZE
                if offset >= total or not rows:
                    break
            if total or out:
                return out
            log.warning("%s for term %s came back empty; rebinding (attempt %d)",
                        endpoint_key, term_id, attempt)
        except (httpx.HTTPError, RuntimeError) as e:
            last_error = e
            log.warning("%s for term %s failed (%s); rebinding (attempt %d)",
                        endpoint_key, term_id, e, attempt)

    raise EmptyTerm(f"{endpoint_key} returned no rows for term {term_id} after a "
                    f"rebind; the session bind is not taking"
                    + (f" (last error: {last_error})" if last_error else ""))


_DETAIL_PARTS = ("prereqs", "coreqs", "restrictions", "course_attributes",
                 "course_catalog_details")


async def fetch_course_detail(client: httpx.AsyncClient, term_id: str, subject: str,
                              course_number: str, term_effective: str,
                              rate: RateLimiter | None,
                              max_retries: int = DETAIL_RETRIES) -> CourseDetail:
    """Fetch the five catalog fragments for one course version.

    Banner answers 500 for a course that does not exist in the given term, which is
    permanent rather than transient. A fragment that will not load is recorded in
    missing_parts instead of aborting the crawl for every other course.
    """
    form = {"term": term_id, "subjectCode": subject, "courseNumber": course_number}
    missing: list[str] = []

    async def one(key: str) -> bytes:
        try:
            resp = await request_with_retry(client, "POST", config.EP[key],
                                            form=form, rate=rate,
                                            max_retries=max_retries)
            return resp.content
        except (httpx.HTTPError, RuntimeError) as e:
            log.debug("%s %s%s unavailable: %s", key, subject, course_number, e)
            missing.append(key)
            return b""

    prereq, coreq, restr, attrs, cat = await asyncio.gather(
        *(one(k) for k in _DETAIL_PARTS))

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
        missing_parts=[p for p in _DETAIL_PARTS if p in missing],
    )
