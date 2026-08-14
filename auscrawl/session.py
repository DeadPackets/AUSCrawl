"""Stateful term sessions.

The Banner 9 search endpoints read the term from session state set by
POST /term/search. The txt_term query parameter is decorative: bind Fall 2026 and
ask for Spring 2015 and the server answers 200 OK with Fall 2026 data. Every page is
therefore verified against the term that was bound, and one session may have only one
term in flight.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable

import httpx

from . import config
from .http import RateLimiter, make_client, request_with_retry

log = logging.getLogger("auscrawl")


class TermMismatch(RuntimeError):
    """The server answered with a term other than the one bound."""


def verify_term(payload: dict, expected_term: str, record_key: str = "term") -> None:
    for row in payload.get("data") or []:
        got = row.get(record_key)
        if got and got != expected_term:
            raise TermMismatch(
                f"requested term {expected_term} but records carry {got}; "
                "the session bind did not take"
            )


class TermSession:
    """One cookie jar, one bound term at a time."""

    def __init__(self, client: httpx.AsyncClient, rate: RateLimiter | None = None):
        self.client = client
        self.rate = rate
        self.term: str | None = None
        self.mode: str | None = None

    async def reset(self, mode: str) -> None:
        """Clear any search state left by a previous term on this session."""
        await request_with_retry(
            self.client, "POST", config.EP["reset"],
            params={"mode": mode}, rate=self.rate, max_retries=2,
        )
        self.term = None

    async def bind(self, term_id: str, mode: str) -> None:
        await request_with_retry(
            self.client, "GET", config.EP["term_selection"],
            params={"mode": mode}, rate=self.rate,
        )
        await request_with_retry(
            self.client, "POST", config.EP["term_search"],
            params={"mode": mode},
            form={"term": term_id, "studyPath": "", "studyPathText": "",
                  "startDatepicker": "", "endDatepicker": ""},
            rate=self.rate,
        )
        self.term = term_id
        self.mode = mode

    async def fetch_page(self, endpoint_key: str, term_id: str, offset: int,
                         max_retries: int | None = None) -> bytes:
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
            rate=self.rate, max_retries=max_retries,
        )
        verify_term(resp.json(), term_id)
        return resp.content


class SessionPool:
    """A fixed number of independent sessions; terms are handed out one per session."""

    def __init__(self, size: int = config.SESSION_POOL_SIZE,
                 rate: RateLimiter | None = None):
        self.size = size
        self.rate = rate
        self._sessions: list[TermSession] = []
        self._free: asyncio.Queue = asyncio.Queue()

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

        return await asyncio.gather(*(one(t) for t in terms),
                                    return_exceptions=True)
