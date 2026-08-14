import json

import httpx

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
        rows = await fetch.fetch_all_pages(sess, "sections", "202710", parse_sections)

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
        rows = await fetch.fetch_all_pages(sess, "sections", "202710", parse_sections)
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
