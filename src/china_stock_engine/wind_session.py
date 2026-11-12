"""Mac/Windows Wind isolation. Importing this module never imports WindPy."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime
import importlib
import multiprocessing as mp
import os
from pathlib import Path
import socket
import threading

from contextlib import nullcontext
from .wind_coordination import CollectionBusy, collection_lock, RequestPacer


class WindError(RuntimeError):
    pass


class WindBusyError(WindError):
    pass


def dashboard_running() -> bool:
    try:
        with socket.create_connection(("127.0.0.1", 8765), timeout=0.5):
            return True
    except ConnectionRefusedError:
        return False
    except OSError as exc:
        # An inaccessible probe is not evidence that the other client is idle.
        raise WindBusyError("Cannot establish whether the Wind dashboard is idle") from exc


def require_exclusive_window(authorized: bool, allow_dashboard: bool = False) -> None:
    if not authorized:
        raise WindBusyError("Live SDK calls require an explicitly reserved exclusive window")
    if not allow_dashboard and dashboard_running():
        raise WindBusyError("Wind dashboard is running on port 8765; SDK was NOT imported")


@contextmanager
def collector_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        if os.name == "nt":
            import msvcrt
            if handle.seek(0, 2) == 0:
                handle.write(b"0"); handle.flush()
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
            raise WindBusyError("Another stock collector owns the session lock") from exc
        try:
            yield
        finally:
            release()


def _wire(value):
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, (list, tuple)):
        return [_wire(item) for item in value]
    return value


def _worker(connection, request_interval=2.0, allow_parallel_collectors=False):
    try:
        with nullcontext() if allow_parallel_collectors else collection_lock():
            _serve(connection, request_interval)
    except CollectionBusy as exc:
        connection.send({"error": str(exc), "busy": True})
        connection.close()


def _serve(connection, request_interval):
    # Native code exists in this child only. Never issue stop/cancel/logout to
    # a potentially shared desktop backend; exit this owned child instead.
    try:
        pacer = RequestPacer(request_interval)
        w = importlib.import_module("WindPy").w
        with pacer.slot():
            result = w.start(waitTime=15)
        if result.ErrorCode:
            raise WindError(f"Wind start failed with code {result.ErrorCode}")
        connection.send({"ready": True})
        while True:
            request = connection.recv()
            if request is None:
                return
            operation, arguments = request
            if operation not in {"wset", "wss", "wsd", "tdays"}:
                raise WindError("Unsupported read-only Wind operation")
            with pacer.slot():
                result = getattr(w, operation)(*arguments)
            # SDK error Data may contain account details; never send or log it.
            if result.ErrorCode:
                connection.send({"error": f"Wind {operation} failed with code {result.ErrorCode}"})
            else:
                connection.send({"fields": list(result.Fields or []),
                                 "codes": list(result.Codes or []),
                                 "times": _wire(list(result.Times or [])),
                                 "data": _wire(result.Data or [])})
    except (EOFError, BrokenPipeError):
        pass
    except WindError as exc:
        # Our own messages contain operation/error codes only, never SDK Data.
        try:
            connection.send({"error": str(exc)})
        except (OSError, EOFError):
            pass
    except Exception as exc:
        try:
            connection.send({"error": f"Wind child failed: {type(exc).__name__}"})
        except (OSError, EOFError):
            pass
    finally:
        connection.close()


class WindSession:
    """One owned subprocess and one serial request; no reconnect/retry storm."""
    def __init__(self, *, exclusive_window: bool = False, timeout: float = 120,
                 lock_path: Path | None = None, allow_dashboard: bool = False,
                 request_interval: float = 2.0, allow_parallel_collectors: bool = False):
        RequestPacer(request_interval)  # validate before spawning
        if type(allow_dashboard) is not bool:
            raise ValueError("allow_dashboard must be boolean")
        if type(allow_parallel_collectors) is not bool:
            raise ValueError("allow_parallel_collectors must be boolean")
        self.allow_parallel_collectors = allow_parallel_collectors
        self.allow_dashboard, self.request_interval = allow_dashboard, request_interval
        self.authorized, self.timeout = exclusive_window, timeout
        self.lock_path = lock_path or Path.home() / ".cache/china-stock-engine/wind.lock"
        self.process = self.connection = self._lock = None
        self.serial = threading.Lock()

    def __enter__(self):
        require_exclusive_window(self.authorized, self.allow_dashboard)
        self._lock = collector_lock(self.lock_path)
        self._lock.__enter__()
        try:
            require_exclusive_window(self.authorized, self.allow_dashboard)
            context = mp.get_context("spawn")
            self.connection, child = context.Pipe()
            self.process = context.Process(target=_worker, args=(child, self.request_interval, self.allow_parallel_collectors), daemon=True)
            self.process.start()
            child.close()
            if not self._receive(30).get("ready"):
                raise WindError("Wind child did not become ready")
            return self
        except BaseException:
            self.close()
            raise

    def _receive(self, timeout):
        if not self.connection.poll(timeout):
            raise WindError("Wind request timed out; no automatic reconnect was attempted")
        try:
            result = self.connection.recv()
        except EOFError as exc:
            raise WindError("Wind child exited unexpectedly") from exc
        if result.get("busy"):
            raise WindBusyError(result["error"])
        if "error" in result:
            raise WindError(result["error"])
        return result

    def request(self, operation, *arguments):
        if operation not in {"wset", "wss", "wsd", "tdays"}:
            raise WindError("Unsupported read-only Wind operation")
        with self.serial:
            try:
                require_exclusive_window(self.authorized, self.allow_dashboard)
                self.connection.send((operation, arguments))
                return self._receive(self.timeout)
            except BaseException:
                # A late reply must not become the next request's result.
                self.close()
                raise

    def close(self):
        if self.connection is not None:
            try:
                self.connection.send(None)
            except (OSError, EOFError):
                pass
            self.connection.close()
            self.connection = None
        if self.process is not None:
            self.process.join(timeout=2)
            if self.process.is_alive():
                self.process.terminate()  # This owned child only; never the dashboard.
                self.process.join(timeout=2)
            self.process = None
        if self._lock is not None:
            self._lock.__exit__(None, None, None)
            self._lock = None

    def __exit__(self, *_):
        self.close()
