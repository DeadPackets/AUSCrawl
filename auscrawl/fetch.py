"""One coroutine per endpoint. All network access lives here."""

import asyncio
from typing import Callable, Optional

import httpx

from . import config, parse_html
from .http import RateLimiter, request_with_retry
from .models import CourseDetail
from .parse_json import parse_code_list, parse_terms
from .session import TermSession

_REF_ENDPOINTS = {"subject": "ref_subject", "instructor": "ref_instructor",
                  "attribute": "ref_attribute"}


async def fetch_terms(client: httpx.AsyncClient, rate: Optional[RateLimiter]):
    resp = await request_with_retry(
        client, "GET", config.EP["terms"],
        params={"searchTerm": "", "offset": 1, "max": 500}, rate=rate)
    return parse_terms(resp.content)


async def fetch_reference(client: httpx.AsyncClient, term_id: str, kind: str,
                          rate: Optional[RateLimiter]):
    resp = await request_with_retry(
        client, "GET", config.EP[_REF_ENDPOINTS[kind]],
        params={"searchTerm": "", "term": term_id, "offset": 1, "max": 5000},
        rate=rate)
    return parse_code_list(resp.content)


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
