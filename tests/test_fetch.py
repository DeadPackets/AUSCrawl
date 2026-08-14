import json

import httpx
import pytest

from auscrawl import fetch, session
from auscrawl.parse_json import parse_sections
from tests.conftest import read_b9


async def test_fetch_terms_parses_the_live_shape():
    def handler(request):
        return httpx.Response(200, content=read_b9("terms.json"),
                              headers={"content-type": "application/json"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        terms = await fetch.fetch_terms(c, None)
    assert len(terms) >= 100
    assert terms[0].term_id.isdigit()


async def test_fetch_reference_parses_a_code_list():
    def handler(request):
        return httpx.Response(200, content=read_b9("ref_subjects_202710.json"),
                              headers={"content-type": "application/json"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        refs = await fetch.fetch_reference(c, "202710", "subject", None)
    assert len(refs) > 50


async def test_fetch_all_pages_walks_the_offsets_until_total_is_reached():
    payload = json.loads(read_b9("sections_202710_p0.json"))
    payload["totalCount"] = 1200
    calls = []

    def handler(request):
        if "/term/" in request.url.path:
            return httpx.Response(200, json={"fwdURL": "/x"})
        offset = int(request.url.params["pageOffset"])
        calls.append(offset)
        body = dict(payload)
        body["data"] = payload["data"][:200] if offset == 1000 else payload["data"]
        return httpx.Response(200, json=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        sess = session.TermSession(c)
        await sess.bind("202710", "search")
        rows = await fetch.fetch_all_pages(sess, "sections", "202710",
                                           parse_sections, mode="search")

    assert calls == [0, 500, 1000]
    assert len(rows) == 1200


async def test_fetch_all_pages_stops_on_an_empty_page():
    def handler(request):
        if "/term/" in request.url.path:
            return httpx.Response(200, json={"fwdURL": "/x"})
        return httpx.Response(200, json={"totalCount": 5000, "data": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        sess = session.TermSession(c)
        await sess.bind("202710", "search")
        rows = await fetch.fetch_all_pages(sess, "sections", "202710",
                                           parse_sections, mode="search")
    assert rows == []


async def test_fetch_course_detail_merges_five_fragments():
    def handler(request):
        path = request.url.path
        if path.endswith("getPrerequisites"):
            return httpx.Response(200, content=read_b9("prereqs_CMP305.html"))
        if path.endswith("getCorequisites"):
            return httpx.Response(200, content=read_b9("coreqs_ACC201.html"))
        if path.endswith("getRestrictions"):
            return httpx.Response(200, content=read_b9("restrictions_BIO103.html"))
        if path.endswith("getCourseAttributes"):
            return httpx.Response(200, content=read_b9("attributes_ACC201.html"))
        return httpx.Response(200, content=read_b9("catalogdetails_MTH203.html"))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        d = await fetch.fetch_course_detail(c, "202710", "CMP", "305", "202610", None)

    assert d.subject == "CMP" and d.course_number == "305"
    assert d.term_effective == "202610"
    assert len(d.rules) == 3
    assert d.prerequisites_json.startswith('{"type":"and"')
    assert d.corequisites == ""
    assert "Undergraduate" in d.levels
    assert d.grade_modes == "Standard Letter S"
    assert "Colleges" in d.restrictions_json
    assert "Actuarial Math Minor_Elective" in d.course_attributes


async def test_one_failing_fragment_does_not_lose_the_others():
    """Banner answers 500 for a course that does not exist in a term. That must
    degrade to a missing fragment, never abort the whole crawl."""
    def handler(request):
        if request.url.path.endswith("getRestrictions"):
            return httpx.Response(500, text="<html>Ellucian error page</html>")
        if request.url.path.endswith("getPrerequisites"):
            return httpx.Response(200, content=read_b9("prereqs_CMP305.html"))
        return httpx.Response(200, content=read_b9("coreqs_ACC201.html"))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        d = await fetch.fetch_course_detail(c, "202610", "CMP", "305", "202610", None,
                                            max_retries=1)

    assert d.missing_parts == ["restrictions"]
    assert len(d.rules) == 3          # the prerequisites still made it through
    assert d.restrictions == ""


async def test_a_fully_missing_course_yields_an_empty_detail_not_an_exception():
    def handler(request):
        return httpx.Response(500, text="<html>Ellucian error page</html>")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        d = await fetch.fetch_course_detail(c, "202610", "EVR", "101", "202610", None,
                                            max_retries=1)

    assert len(d.missing_parts) == 5
    assert d.rules == []
    assert d.prerequisites_json == ""


async def test_max_retries_is_honoured_per_call():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(500, text="boom")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        await fetch.fetch_course_detail(c, "202610", "X", "1", "202610", None,
                                        max_retries=2)
    assert calls["n"] == 10          # 5 endpoints x 2 attempts


async def test_an_empty_first_page_triggers_one_rebind_before_giving_up():
    """A bind that did not take returns totalCount 0 with HTTP 200. Recording zero
    sections for a term silently would be far worse than retrying."""
    binds = {"n": 0}
    state = {"bound": False}

    def handler(request):
        if request.url.path.endswith("/term/search"):
            binds["n"] += 1
            state["bound"] = binds["n"] >= 2      # the first bind silently fails
            return httpx.Response(200, json={"fwdURL": "/x"})
        if request.url.path.endswith("/termSelection"):
            return httpx.Response(200, text="")
        if not state["bound"]:
            return httpx.Response(200, json={"totalCount": 0, "data": None})
        return httpx.Response(200, json=json.loads(read_b9("sections_202710_p0.json")))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        sess = session.TermSession(c)
        rows = await fetch.fetch_all_pages(sess, "sections", "202710",
                                           parse_sections, mode="search")

    assert binds["n"] == 2
    assert len(rows) > 0


async def test_a_term_that_stays_empty_raises_rather_than_recording_zero():
    def handler(request):
        if "/term/" in request.url.path:
            return httpx.Response(200, json={"fwdURL": "/x"})
        return httpx.Response(200, json={"totalCount": 0, "data": None})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        sess = session.TermSession(c)
        with pytest.raises(fetch.EmptyTerm):
            await fetch.fetch_all_pages(sess, "sections", "202710",
                                        parse_sections, mode="search")
