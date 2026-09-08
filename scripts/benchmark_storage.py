"""Fresh-process stream benchmark; no network, database, hashing or media process."""

import argparse
import ctypes
import json
import platform
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "api"))

from app.errors import ApiError  # noqa: E402
from app.storage import LocalObjectStorage, RemoteObjectStorage  # noqa: E402

MIB = 1024 * 1024
SECRET = b"benchmark-only-storage-secret-32-bytes"


class LazyStream:
    def __init__(self, size: int):
        self.remaining = size

    def read(self, count: int = -1) -> bytes:
        if not 0 < count <= MIB:
            raise AssertionError("input must be read in bounded chunks")
        count = min(count, self.remaining)
        self.remaining -= count
        return b"x" * count


class DiskRemoteBackend:
    """Disk-backed SDK fake isolates adapter overhead from payload retention."""

    def __init__(self, root: Path):
        self.store = LocalObjectStorage(root, SECRET)

    def put(self, key, content, mime_type):
        self.store._put_stream(key, content, mime_type)

    def open(self, key):
        return self.store._open_object(key)

    def stat(self, key):
        try:
            return self.store.stat(key)
        except ApiError as exc:
            if exc.code == "STORAGE_OBJECT_NOT_FOUND":
                return None
            raise

    def delete(self, key):
        self.store.delete(key)

    def ready(self):
        return self.store.ready()


def memory():
    if sys.platform != "win32":
        import resource

        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        peak *= 1 if sys.platform == "darwin" else 1024
        return peak, peak

    class Counters(ctypes.Structure):
        _fields_ = [("cb", ctypes.c_ulong), ("faults", ctypes.c_ulong)] + [
            (name, ctypes.c_size_t)
            for name in (
                "peak",
                "working",
                "quota_peak_paged",
                "quota_paged",
                "quota_peak_nonpaged",
                "quota_nonpaged",
                "pagefile",
                "peak_pagefile",
                "private",
            )
        ]

    counters = Counters()
    counters.cb = ctypes.sizeof(counters)
    current = ctypes.windll.kernel32.GetCurrentProcess
    current.restype = ctypes.c_void_p
    query = ctypes.windll.psapi.GetProcessMemoryInfo
    query.argtypes = [ctypes.c_void_p, ctypes.POINTER(Counters), ctypes.c_ulong]
    if not query(current(), ctypes.byref(counters), counters.cb):
        raise ctypes.WinError()
    return counters.working, counters.peak


def measure(size_mib: int, backend: str):
    with tempfile.TemporaryDirectory(prefix="storage-benchmark-") as temporary:
        root = Path(temporary)
        store = (
            LocalObjectStorage(root, SECRET)
            if backend == "local"
            else RemoteObjectStorage(DiskRemoteBackend(root), SECRET)
        )
        baseline, _ = memory()
        started = time.perf_counter()
        claim = store.write_claim(
            "outputs", mime_type="video/mp4", max_bytes=size_mib * MIB
        )
        stored = store.put(claim, LazyStream(size_mib * MIB), "video/mp4")
        assert stored.size_bytes == size_mib * MIB
        with store.open(store.read_claim(stored.key)) as stream:
            copied = 0
            while chunk := stream.read(MIB):
                assert chunk == b"x" * len(chunk)
                copied += len(chunk)
        assert copied == stored.size_bytes
        _, peak = memory()
        delta = max(0, peak - baseline)
        result = {
            "backend": backend,
            "input_mib": size_mib,
            "baseline_working_mib": round(baseline / MIB, 3),
            "peak_working_mib": round(peak / MIB, 3),
            "extra_peak_mib": round(delta / MIB, 3),
            "seconds": round(time.perf_counter() - started, 3),
        }
        assert delta <= 64 * MIB, result
        return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--size", type=int)
    parser.add_argument("--backend", choices=["local", "remote"], default="local")
    args = parser.parse_args()
    if args.size is not None:
        if args.size <= 0:
            parser.error("size must be positive")
        print(json.dumps(measure(args.size, args.backend)))
        return
    rows = []
    for backend in ("local", "remote"):
        for size in (64, 512):
            result = subprocess.run(
                [
                    sys.executable,
                    str(Path(__file__).resolve()),
                    "--size",
                    str(size),
                    "--backend",
                    backend,
                ],
                capture_output=True,
                text=True,
                check=True,
            )
            rows.append(json.loads(result.stdout))
    print(
        json.dumps(
            {
                "platform": platform.platform(),
                "python": platform.python_version(),
                "results": rows,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
