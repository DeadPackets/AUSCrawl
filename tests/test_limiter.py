"""TDD for the global rate limiter (token-bucket + AIMD)."""

import asyncio

import crawl


def test_reserve_spaces_requests_by_one_over_rate():
    rl = crawl.RateLimiter(rate=10)  # 0.1s between grants
    assert rl._reserve(0.0) == 0.0
    assert abs(rl._reserve(0.0) - 0.1) < 1e-9
    assert abs(rl._reserve(0.0) - 0.2) < 1e-9


def test_reserve_no_wait_when_caller_is_already_late():
    rl = crawl.RateLimiter(rate=10)
    rl._reserve(0.0)                      # next free at 0.1
    assert rl._reserve(5.0) == 0.0        # caller arrived well after


def test_record_throttle_multiplicatively_decreases_to_floor():
    rl = crawl.RateLimiter(rate=20, min_rate=4, decrease=0.5)
    rl.record_throttle()
    assert rl.rate == 10.0
    rl.record_throttle()
    assert rl.rate == 5.0
    rl.record_throttle()
    assert rl.rate == 4.0  # floored at min_rate


def test_record_success_additively_increases_to_ceiling():
    rl = crawl.RateLimiter(rate=4, max_rate=6, increase=1.0)
    rl.record_success()
    assert rl.rate == 4.25            # 4 + 1/4
    for _ in range(100):
        rl.record_success()
    assert rl.rate == 6.0             # capped at max_rate


def test_acquire_completes_and_paces_in_order():
    async def scenario():
        rl = crawl.RateLimiter(rate=1000)  # fast; just exercise the await path
        order = []
        for i in range(4):
            await rl.acquire()
            order.append(i)
        return order

    assert asyncio.run(scenario()) == [0, 1, 2, 3]
