"""Live tests. Run explicitly: uv run --project . pytest -m live"""

import pytest

from auscrawl import fetch
from auscrawl.http import make_client
from auscrawl.session import TermSession

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
    assert d.missing_parts == []
    assert d.levels


async def test_section_search_still_pages_and_matches_its_own_total():
    from auscrawl.parse_json import parse_sections
    async with make_client(4) as c:
        sess = TermSession(c)
        await sess.bind("202710", "search")
        raw = await sess.fetch_page("sections", "202710", 0)
        total, first = parse_sections(raw, "202710")
        rows = await fetch.fetch_all_pages(sess, "sections", "202710",
                                           parse_sections, mode="search")
    assert len(first) == 500
    assert len(rows) == total


async def test_txt_term_is_still_ignored_by_the_server():
    """The whole session-pool design rests on this. Banner reads the term from
    session state; a mismatched txt_term either replays the bound term's data or
    answers with nothing. Both are silent, which is why the crawler guards on it."""
    import json

    async with make_client(2) as c:
        sess = TermSession(c)
        await sess.bind("202710", "search")
        sess.term = "201510"                      # pretend we asked for another term
        raw = await sess.fetch_page("sections", "201510", 0)

    payload = json.loads(raw)
    rows = payload.get("data") or []
    served = {r.get("term") for r in rows}
    assert served != {"201510"}, (
        "Banner now honours txt_term; the session-pool constraint can be revisited")


async def test_a_failed_bind_shows_up_as_an_empty_term_not_as_silent_zero():
    from auscrawl.parse_json import parse_sections

    async with make_client(2) as c:
        sess = TermSession(c)
        rows = await fetch.fetch_all_pages(sess, "sections", "201510",
                                           parse_sections, mode="search")
    assert len(rows) > 1000
