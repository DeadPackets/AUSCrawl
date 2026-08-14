import asyncio

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


def test_verify_term_ignores_catalog_records_that_have_no_term_key():
    payload = {"totalCount": 1, "data": [{"termEffective": "202210"}]}
    session.verify_term(payload, "202710")


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


async def test_fetch_page_refuses_a_term_the_session_is_not_bound_to():
    def handler(request):
        return httpx.Response(200, json={"fwdURL": "/x"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        s = session.TermSession(client)
        await s.bind("202710", "search")
        with pytest.raises(session.TermMismatch):
            await s.fetch_page("sections", "201510", 0)


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


async def test_fetch_page_returns_raw_bytes_on_success():
    body = b'{"totalCount": 1, "data": [{"term": "202710"}]}'

    def handler(request):
        if "term" in request.url.path:
            return httpx.Response(200, json={"fwdURL": "/x"})
        return httpx.Response(200, content=body,
                              headers={"content-type": "application/json"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        s = session.TermSession(client)
        await s.bind("202710", "search")
        raw = await s.fetch_page("sections", "202710", 0)
    assert isinstance(raw, bytes)
    assert b"202710" in raw


async def test_pool_never_runs_two_terms_on_one_session():
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
