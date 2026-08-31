"""Windows-only signal adapter for pinned hatchet-sdk 1.38.1; no SDK files are patched."""

import asyncio
import signal

import structlog
from hatchet_sdk.worker.action_listener_process import worker_action_listener_process
from hatchet_sdk.worker.worker import Worker

logger = structlog.get_logger()


class WindowsSignalLoop(asyncio.ProactorEventLoop):
    """The SDK listener ignores SIGINT/SIGTERM; its parent controls shutdown via an Event."""

    def __init__(self):
        super().__init__()
        self._windows_signal_handlers = {}

    def add_signal_handler(self, sig, callback, *args):
        if sig not in {signal.SIGINT, signal.SIGTERM}:
            raise NotImplementedError("Only the Hatchet listener's INT/TERM signals are supported")
        self._windows_signal_handlers.setdefault(sig, signal.getsignal(sig))
        signal.signal(sig, lambda *_: self.call_soon_threadsafe(callback, *args))

    def remove_signal_handler(self, sig):
        if sig not in self._windows_signal_handlers:
            return False
        signal.signal(sig, self._windows_signal_handlers.pop(sig))
        return True

    def close(self):
        for sig in list(self._windows_signal_handlers):
            self.remove_signal_handler(sig)
        super().close()


class WindowsSignalPolicy(asyncio.WindowsProactorEventLoopPolicy):
    def new_event_loop(self):
        return WindowsSignalLoop()


def windows_listener_process(*args):
    # This runs only in the spawned listener, leaving API and parent loop policies unchanged.
    asyncio.set_event_loop_policy(WindowsSignalPolicy())
    worker_action_listener_process(*args)


class WindowsHatchetWorker(Worker):
    def _setup_signal_handlers(self):
        handler = (
            self._handle_force_quit_signal
            if self._config.force_shutdown_on_shutdown_signal else self._handle_exit_signal
        )
        signal.signal(signal.SIGTERM, handler)
        signal.signal(signal.SIGINT, handler)
        signal.signal(signal.SIGBREAK, self._handle_force_quit_signal)

    def _start_action_listener(self, *, enable_health_server=False, healthcheck_port=8001):
        # Same pinned SDK spawn contract, with only the target bootstrap adapted for Windows.
        process = self._ctx.Process(
            target=windows_listener_process,
            args=(
                self.name, list(self._action_registry.keys()), self._slot_config, self._config,
                self._action_queue, self._event_queue, self._handle_kill, self._client.debug,
                self._labels, self._worker_id_queue, self._stop_listener_event,
            ),
        )
        process.start()
        return process

    async def _check_engine_version(self):
        version = await super()._check_engine_version()
        if version is None:
            raise RuntimeError("Windows Hatchet requires an engine with slot_config support")
        return version

    async def _aio_start(self):
        try:
            await super()._aio_start()
        except Exception as exc:
            # SDK start otherwise leaves run_forever running after an async startup failure.
            self._windows_startup_failed = True
            logger.error("hatchet.worker_startup_failed", error_type=type(exc).__name__)
            self._loop.stop()

    def start(self, options=None):
        try:
            super().start(options)
        except SystemExit:
            if not getattr(self, "_windows_startup_failed", False):
                raise
        if getattr(self, "_windows_startup_failed", False):
            self._terminate_processes()
            self._close_queues()
            raise RuntimeError(
                "Windows Hatchet worker startup failed; "
                "check Cloud connectivity and engine version"
            ) from None
