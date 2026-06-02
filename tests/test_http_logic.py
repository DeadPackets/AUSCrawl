"""TDD for the HTTP retry/backoff/WAF helpers (findings #1, #3, #6)."""

import crawl


# ── #1: retry classification ────────────────────────────────────────────────


def test_retries_transient_server_and_throttle_codes():
    for code in (403, 408, 429, 500, 502, 503, 504):
        assert crawl.should_retry_status(code) is True, code


def test_does_not_retry_permanent_client_errors():
    for code in (400, 401, 404, 410, 422):
        assert crawl.should_retry_status(code) is False, code


# ── #3: jittered backoff ────────────────────────────────────────────────────


def test_backoff_grows_with_attempt():
    # With no jitter (rand -> 0) the delay is the lower bound = cap/2 = base*2**(attempt-1)
    lows = [crawl.backoff_delay(a, base=2.0, rand=lambda: 0.0) for a in range(1, 6)]
    assert lows == [2.0, 4.0, 8.0, 16.0, 32.0]


def test_backoff_is_bounded_by_cap():
    # rand -> 1 gives the upper bound = cap = base*2**attempt
    for a in range(1, 6):
        cap = 2.0 * (2 ** a)
        hi = crawl.backoff_delay(a, base=2.0, rand=lambda: 1.0)
        assert hi == cap


def test_backoff_stays_within_equal_jitter_band():
    import random

    rng = random.Random(1234)
    for a in range(1, 6):
        cap = 2.0 * (2 ** a)
        for _ in range(50):
            d = crawl.backoff_delay(a, base=2.0, rand=rng.random)
            assert cap / 2 <= d <= cap


# ── #6: WAF detection on bounded bytes ──────────────────────────────────────


def test_detects_waf_block_marker_case_insensitive():
    assert crawl.is_waf_block(b"... open a Support Ticket with us ...") is True
    assert crawl.is_waf_block(b"please reference this support ticket id") is True


def test_normal_page_is_not_a_waf_block():
    assert crawl.is_waf_block(b"<html><table class='datadisplaytable'>...") is False


def test_waf_check_only_scans_bounded_prefix():
    # Marker beyond the scan limit must not be detected (the whole point is to
    # avoid lowercasing multi-MB course pages).
    blob = b"x" * 1000 + b"support ticket"
    assert crawl.is_waf_block(blob, limit=100) is False
    assert crawl.is_waf_block(blob, limit=2000) is True
