"""Wind engine coordination protocol v1 (identical in both engine packages).

One native collection worker per local OS user, independent of Trading Desk.
The worker owns the lock so an orphaned native call cannot overlap a new batch.
No credentials, SDK imports, subscriptions, or dashboard control here.
"""
from contextlib import contextmanager
import math
import os
from pathlib import Path
import tempfile
import time


class CollectionBusy(RuntimeError):
    pass


def collection_lock_path():
    # Windows TEMP is per-user; macOS gettempdir is per-user. UID also isolates Linux.
    suffix = str(os.getuid()) if hasattr(os, "getuid") else "user"
    return Path(tempfile.gettempdir()) / ("wind-engines-" + suffix) / "collection-v1.lock"


@contextmanager
def collection_lock():
    path = collection_lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        if os.name == "nt":
            import msvcrt
            if handle.seek(0, 2) == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            acquire = lambda: msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            release = lambda: (handle.seek(0), msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1))
        else:
            import fcntl
            acquire = lambda: fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            release = lambda: fcntl.flock(handle, fcntl.LOCK_UN)
        try:
            acquire()
        except OSError as exc:
            raise CollectionBusy("Another stock/commodities collection worker is active") from exc
        try:
            yield
        finally:
            release()


class RequestPacer:
    """Idle interval after each native call, including errors and login."""
    def __init__(self, seconds=2.0):
        if isinstance(seconds, bool) or not isinstance(seconds, (float, int)) or not math.isfinite(seconds) or seconds < 1:
            raise ValueError("Wind request interval must be finite and at least 1 second")
        self.seconds = seconds
        self.next_at = 0.0

    @contextmanager
    def slot(self):
        delay = self.next_at - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        try:
            yield
        finally:
            self.next_at = time.monotonic() + self.seconds
