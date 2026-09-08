"""Bound synchronous work and drain it before releasing cancellation ownership."""

import asyncio
from collections.abc import Callable
from typing import ParamSpec, TypeVar
from weakref import ReferenceType, WeakKeyDictionary, ref

import anyio

P = ParamSpec("P")
T = TypeVar("T")
MAX_BLOCKING_IO = 4


class BlockingIO:
    def __init__(self, concurrency: int = MAX_BLOCKING_IO) -> None:
        if concurrency < 1:
            raise ValueError("blocking IO concurrency must be positive")
        self._slots = asyncio.Semaphore(concurrency)

    async def run(self, operation: Callable[P, T], *args: P.args, **kwargs: P.kwargs) -> T:
        # Acquire before submitting: queued callers do not create queued threads.
        async with self._slots:
            work = asyncio.create_task(asyncio.to_thread(operation, *args, **kwargs))
            try:
                return await asyncio.shield(work)
            except asyncio.CancelledError:
                # Python cannot kill an IO thread. Keep its slot and await resource
                # cleanup, including repeated task.cancel() calls during shutdown.
                with anyio.CancelScope(shield=True):
                    while not work.done():
                        try:
                            await asyncio.shield(work)
                        except asyncio.CancelledError:
                            continue
                        except Exception:
                            break
                if not work.cancelled():
                    work.exception()
                raise


_pools: WeakKeyDictionary[asyncio.AbstractEventLoop, ReferenceType[BlockingIO]] = (
    WeakKeyDictionary()
)


async def run_blocking(operation: Callable[P, T], *args: P.args, **kwargs: P.kwargs) -> T:
    loop = asyncio.get_running_loop()
    reference = _pools.get(loop)
    pool = reference() if reference is not None else None
    if pool is None:
        pool = BlockingIO()
        _pools[loop] = ref(pool)
    return await pool.run(operation, *args, **kwargs)
