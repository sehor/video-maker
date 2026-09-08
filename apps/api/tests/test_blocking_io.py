import asyncio
import threading
import time
import uuid
from contextlib import contextmanager
from unittest.mock import Mock

import pytest

from app.blocking_io import BlockingIO, run_blocking
from app.provider_execution import GenerationExecutionService


async def measure_heartbeat(operation):
    ticks = []

    async def pulse():
        while True:
            ticks.append(time.perf_counter())
            await asyncio.sleep(0.02)

    heartbeat = asyncio.create_task(pulse())
    await asyncio.sleep(0.04)
    try:
        await operation()
        await asyncio.sleep(0.04)
    finally:
        heartbeat.cancel()
        await asyncio.gather(heartbeat, return_exceptions=True)
    return max(end - start for start, end in zip(ticks, ticks[1:], strict=False))


def test_slow_database_execution_keeps_heartbeat_and_session_ownership(record_property):
    threads = []

    @contextmanager
    def session():
        threads.append(threading.get_ident())
        time.sleep(2)
        try:
            yield Mock(get=Mock(return_value=None))
        finally:
            threads.append(threading.get_ident())

    service = GenerationExecutionService(
        Mock(),
        provider_registry=Mock(),
        route_registry=Mock(),
        callback_claim_issuer=Mock(),
        session_factory=session,
    )
    gap = asyncio.run(measure_heartbeat(lambda: service.execute(uuid.uuid4())))
    record_property("max_heartbeat_gap_ms", gap * 1000)
    assert gap < 0.5
    assert threads[0] == threads[-1] != threading.get_ident()


def test_slow_file_io_keeps_heartbeat_and_closes_handle(tmp_path, record_property):
    closed = []

    def write():
        with (tmp_path / "output").open("wb") as handle:
            time.sleep(2)
            handle.write(b"complete")
        closed.append(handle.closed)

    gap = asyncio.run(measure_heartbeat(lambda: run_blocking(write)))
    record_property("max_heartbeat_gap_ms", gap * 1000)
    assert gap < 0.5
    assert closed == [True]


def test_pool_bounds_concurrency_and_recovers_after_failure():
    active = maximum = 0
    lock = threading.Lock()

    def work():
        nonlocal active, maximum
        with lock:
            active += 1
            maximum = max(active, maximum)
        time.sleep(0.03)
        with lock:
            active -= 1

    async def scenario():
        pool = BlockingIO(2)
        await asyncio.gather(*(pool.run(work) for _ in range(8)))
        with pytest.raises(ValueError):
            await pool.run(lambda: int("invalid"))
        assert await pool.run(lambda: 7) == 7

    asyncio.run(scenario())
    assert maximum == 2


@pytest.mark.parametrize("timeout", [False, True])
def test_cancellation_waits_for_worker_cleanup_and_retains_its_slot(tmp_path, timeout):
    started, release, finished = (threading.Event() for _ in range(3))
    second = threading.Event()

    def work():
        try:
            with (tmp_path / "temporary").open("wb") as handle:
                started.set()
                assert release.wait(2), "test failed to release worker"
                handle.write(b"done")
        finally:
            (tmp_path / "temporary").unlink(missing_ok=True)
            finished.set()

    async def scenario():
        pool = BlockingIO(1)
        running = asyncio.create_task(
            asyncio.wait_for(pool.run(work), 0.03) if timeout else pool.run(work)
        )
        while not started.is_set():
            await asyncio.sleep(0.005)
        if not timeout:
            running.cancel()
            await asyncio.sleep(0)
            running.cancel()  # shutdown may repeat cancellation
        queued = asyncio.create_task(pool.run(second.set))
        await asyncio.sleep(0.06)
        assert not second.is_set()
        assert not running.done()
        release.set()
        with pytest.raises(TimeoutError if timeout else asyncio.CancelledError):
            await running
        await queued

    asyncio.run(scenario())
    assert finished.is_set() and second.is_set()
    assert not (tmp_path / "temporary").exists()


def test_cancelled_queued_work_never_starts():
    async def scenario():
        pool = BlockingIO(1)
        release, started = threading.Event(), threading.Event()
        dispatched = Mock()

        def hold():
            started.set()
            assert release.wait(2)

        first = asyncio.create_task(pool.run(hold))
        while not started.is_set():
            await asyncio.sleep(0.005)
        queued = asyncio.create_task(pool.run(dispatched))
        await asyncio.sleep(0)
        queued.cancel()
        with pytest.raises(asyncio.CancelledError):
            await queued
        release.set()
        await first
        dispatched.assert_not_called()

    asyncio.run(scenario())
