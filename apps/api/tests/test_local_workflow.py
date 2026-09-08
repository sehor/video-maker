import asyncio
import uuid
from datetime import timedelta

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.local_workflow import LocalWorkflowStarter
from app.provider_execution import ProviderExecutionStep
from app.workflow import WorkflowStartRequest


@pytest.fixture(autouse=True)
def clean_database():
    """Scheduler tests have no database dependency."""
    yield


def settings() -> Settings:
    return Settings(_env_file=None, environment="development", workflow_backend="local")


def request(key: str | None = None) -> WorkflowStartRequest:
    job_id = uuid.uuid4()
    return WorkflowStartRequest(job_id, key or f"generation-job:{job_id}:v1", {})


class BlockingExecutor:
    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.calls: list[uuid.UUID] = []

    async def execute(self, job_id: uuid.UUID) -> ProviderExecutionStep:
        self.calls.append(job_id)
        self.entered.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise
        return ProviderExecutionStep(is_complete=True, poll_count=1)


def test_start_returns_before_execution_and_concurrent_duplicates_reuse_one_run() -> None:
    async def scenario():
        executor = BlockingExecutor()
        runner = LocalWorkflowStarter(settings(), executor_factory=lambda: executor)
        item = request("x" * 255)
        assert not runner.ready()
        await runner.startup()
        try:
            result = await runner.start(item)
            assert executor.calls == []
            assert result.workflow_id.startswith("local:")
            assert len(result.workflow_id) <= 255
            results = await asyncio.gather(*(runner.start(item) for _ in range(20)))
            await executor.entered.wait()
            assert executor.calls == [item.job_id]
            assert all(duplicate == result for duplicate in results)
            executor.release.set()
            await runner.wait_idle()
            assert await runner.start(item) == result
            assert executor.calls == [item.job_id]
        finally:
            await runner.shutdown()
        assert not runner.ready()

    asyncio.run(scenario())


def test_independent_jobs_can_run_while_another_is_waiting() -> None:
    async def scenario():
        executor = BlockingExecutor()
        runner = LocalWorkflowStarter(settings(), executor_factory=lambda: executor)
        items = [request(), request()]
        await runner.startup()
        try:
            results = await asyncio.gather(*(runner.start(item) for item in items))
            async def both_entered():
                while len(executor.calls) < len(items):
                    await asyncio.sleep(0.001)

            await asyncio.wait_for(both_entered(), timeout=1)
            assert not executor.release.is_set()
            assert set(executor.calls) == {item.job_id for item in items}
            assert len({result.workflow_id for result in results}) == 2
            executor.release.set()
            await runner.wait_idle()
        finally:
            await runner.shutdown()

    asyncio.run(scenario())


def test_idempotency_key_cannot_be_rebound_to_another_job() -> None:
    async def scenario():
        executor = BlockingExecutor()
        runner = LocalWorkflowStarter(settings(), executor_factory=lambda: executor)
        await runner.startup()
        try:
            await runner.start(request("same-key"))
            with pytest.raises(ValueError, match="another job"):
                await runner.start(request("same-key"))
            with pytest.raises(ValidationError):
                await runner.start(WorkflowStartRequest(uuid.uuid4(), "", {}))
        finally:
            await runner.shutdown()

    asyncio.run(scenario())


@pytest.mark.parametrize("retry_after", [None, timedelta(milliseconds=20)])
def test_polling_obeys_executor_backoff_and_yields_without_one(retry_after) -> None:
    async def scenario():
        times = []

        class Executor:
            async def execute(self, job_id):
                times.append(asyncio.get_running_loop().time())
                return ProviderExecutionStep(
                    is_complete=len(times) == 3,
                    poll_count=len(times),
                    retry_after=retry_after,
                )

        runner = LocalWorkflowStarter(settings(), executor_factory=Executor)
        await runner.startup()
        try:
            await runner.start(request())
            await asyncio.wait_for(runner.wait_idle(), timeout=2)
        finally:
            await runner.shutdown()
        minimum = retry_after.total_seconds() if retry_after else 0.01
        assert len(times) == 3
        assert all(
            later - earlier >= minimum - 0.002
            for earlier, later in zip(times, times[1:], strict=False)
        )

    asyncio.run(scenario())


def test_shutdown_cancels_work_and_closes_runner_and_replay_keeps_stable_id() -> None:
    async def scenario():
        executor = BlockingExecutor()
        runner = LocalWorkflowStarter(settings(), executor_factory=lambda: executor)
        item = request()
        await runner.startup()
        first = await runner.start(item)
        await executor.entered.wait()
        await asyncio.wait_for(runner.shutdown(), timeout=1)
        assert executor.cancelled.is_set()
        assert not runner.ready()
        with pytest.raises(RuntimeError, match="not running"):
            await runner.start(item)
        assert not [task for task in asyncio.all_tasks() if task.get_name().startswith("local:")]
        await runner.shutdown()
        await runner.startup()
        try:
            executor.release.set()
            assert await runner.start(item) == first
            await runner.wait_idle()
            assert executor.calls == [item.job_id, item.job_id]
        finally:
            await runner.shutdown()

    asyncio.run(scenario())


def test_cancelling_a_waiter_does_not_cancel_the_job() -> None:
    async def scenario():
        executor = BlockingExecutor()
        runner = LocalWorkflowStarter(settings(), executor_factory=lambda: executor)
        await runner.startup()
        try:
            await runner.start(request())
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(runner.wait_idle(), timeout=0.01)
            assert not executor.cancelled.is_set()
            executor.release.set()
            await runner.wait_idle()
        finally:
            await runner.shutdown()

    asyncio.run(scenario())


def test_failed_run_can_reenter_execution_without_changing_workflow_id() -> None:
    async def scenario():
        calls = []
        unhandled = []
        loop = asyncio.get_running_loop()
        previous_handler = loop.get_exception_handler()
        loop.set_exception_handler(lambda loop, context: unhandled.append(context))

        class Executor:
            async def execute(self, job_id):
                calls.append(job_id)
                if len(calls) == 1:
                    raise RuntimeError("simulated interruption")
                return ProviderExecutionStep(is_complete=True, poll_count=1)

        runner = LocalWorkflowStarter(settings(), executor_factory=Executor)
        item = request()
        await runner.startup()
        try:
            first = await runner.start(item)
            with pytest.raises(RuntimeError, match="simulated interruption"):
                await runner.wait_idle()
            assert await runner.start(item) == first
            await runner.wait_idle()
            assert calls == [item.job_id, item.job_id]
            assert unhandled == []
        finally:
            await runner.shutdown()
            loop.set_exception_handler(previous_handler)

    asyncio.run(scenario())


def test_runner_rejects_cross_loop_start_and_shutdown() -> None:
    async def scenario():
        runner = LocalWorkflowStarter(settings())
        await runner.startup()
        try:
            for operation in (lambda: runner.start(request()), runner.shutdown):
                with pytest.raises(RuntimeError, match="lifespan event loop"):
                    await asyncio.to_thread(lambda operation=operation: asyncio.run(operation()))
            assert runner.ready()
        finally:
            await runner.shutdown()

    asyncio.run(scenario())


def test_runner_rechecks_production_guard_at_startup() -> None:
    configured = settings()
    runner = LocalWorkflowStarter(configured)
    configured.environment = "production"
    with pytest.raises(ValueError, match="development-only"):
        asyncio.run(runner.startup())
    assert not runner.ready()
    with pytest.raises(ValueError, match="development-only"):
        LocalWorkflowStarter(configured)
