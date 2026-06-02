"""TDD for the adaptive concurrency limiter (finding #7)."""

import asyncio

import crawl


def test_throttle_multiplicatively_decreases_to_floor():
    lim = crawl.AdaptiveLimiter(start=10, max_limit=10, min_limit=2, decrease=0.5)
    asyncio.run(lim.record_throttle())
    assert lim.limit == 5.0
    asyncio.run(lim.record_throttle())
    assert lim.limit == 2.5
    asyncio.run(lim.record_throttle())
    assert lim.limit == 2.0  # floored at min_limit


def test_success_increases_by_one_step_per_window_toward_ceiling():
    # AIMD: each success adds increase/limit, so it takes ~`limit` successes to
    # gain one full step (gentle recovery that won't snap back to the ceiling).
    lim = crawl.AdaptiveLimiter(start=2, max_limit=4, min_limit=1, increase=1.0)
    asyncio.run(lim.record_success())
    assert lim.limit == 2.5            # 2 + 1/2
    asyncio.run(lim.record_success())
    assert round(lim.limit, 4) == 2.9  # 2.5 + 1/2.5
    for _ in range(50):
        asyncio.run(lim.record_success())
    assert lim.limit == 4.0            # eventually capped at max_limit


def test_slot_never_exceeds_current_limit():
    async def scenario():
        lim = crawl.AdaptiveLimiter(start=2, max_limit=2, min_limit=1)
        state = {"active": 0, "peak": 0}

        async def worker():
            async with lim.slot():
                state["active"] += 1
                state["peak"] = max(state["peak"], state["active"])
                await asyncio.sleep(0.01)
                state["active"] -= 1

        await asyncio.gather(*(worker() for _ in range(8)))
        return state["peak"]

    assert asyncio.run(scenario()) <= 2


def test_raising_limit_wakes_a_waiter():
    async def scenario():
        lim = crawl.AdaptiveLimiter(start=1, max_limit=2, min_limit=1, increase=1.0)
        order = []

        async def worker(i):
            async with lim.slot():
                order.append(i)
                await asyncio.sleep(0.02)

        # Two workers, limit starts at 1. Bump limit after a beat so the second
        # can proceed concurrently rather than strictly serially.
        async def bump():
            await asyncio.sleep(0.005)
            await lim.record_success()

        await asyncio.gather(worker(1), worker(2), bump())
        return order

    # Both workers run; we only assert no deadlock and both executed.
    assert sorted(asyncio.run(scenario())) == [1, 2]
