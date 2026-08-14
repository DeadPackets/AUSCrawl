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
                          headers={"content-type": "application/json"},
                          request=httpx.Request("GET", "https://x/"))
    assert http.is_blocked(resp) is False


def test_retry_after_parsed_when_present():
    resp = httpx.Response(429, headers={"Retry-After": "7"},
                          request=httpx.Request("GET", "https://x/"))
    assert http.retry_after_seconds(resp) == 7.0
    plain = httpx.Response(429, request=httpx.Request("GET", "https://x/"))
    assert http.retry_after_seconds(plain) is None


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


async def test_client_carries_the_browser_profile():
    async with http.make_client(4) as client:
        assert client.headers["user-agent"] == config.BROWSER_HEADERS["User-Agent"]
        assert client.headers["sec-fetch-mode"] == "cors"
