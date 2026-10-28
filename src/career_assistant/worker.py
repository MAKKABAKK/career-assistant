"""One local worker; durable claiming, bounded retries, no automatic effect replay."""
from __future__ import annotations

from datetime import timedelta
import logging
import os
from pathlib import Path
import time
import threading
from typing import Callable
import uuid

from career_assistant.models import now_hk, to_iso
from .store import Store, decode, encode, stamp


class WorkerAlreadyRunning(RuntimeError):
    pass


class DefiniteTaskFailure(RuntimeError):
    """The handler knows the operation failed; a user may explicitly retry it."""




class FileLock:
    """OS releases this lock only when its owner closes it or the process exits."""

    def __init__(self, path: Path):
        self.path = path
        self.handle = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        try:
            if os.name == "nt":
                import msvcrt
                if handle.seek(0, 2) == 0:
                    handle.write(b"\0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            handle.close()
            raise WorkerAlreadyRunning("已有 Worker 持有进程锁；不会恢复其正在执行的任务。") from exc
        self.handle = handle

    def release(self) -> None:
        if self.handle is None:
            return
        if os.name == "nt":
            import msvcrt
            self.handle.seek(0)
            msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        self.handle.close()
        self.handle = None


def safe_error(exc: Exception) -> str:
    text = f"{type(exc).__name__}: {exc}"
    for key, value in os.environ.items():
        if value and len(value) >= 6 and any(word in key.upper() for word in ("KEY", "TOKEN", "PASSWORD", "AUTH_CODE")):
            text = text.replace(value, "[redacted]")
    return text[:2000]


class Worker:
    """Handlers accept the decoded task and return JSON-serializable results.

    start() acquires the OS lock before starting a thread. stop() never releases
    it while a handler is still running. run_once() is useful for deterministic
    local tests or CLI execution without a background thread.
    """

    def __init__(self, store: Store, handlers: dict[str, Callable], poll_interval: float = 0.5):
        self.store = store
        self.handlers = handlers
        self.owner = uuid.uuid4().hex
        self.lock = FileLock(Path(f"{store.path}.worker.lock"))
        self.poll_interval = poll_interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._run_guard = threading.Lock()
        self._next_cache_cleanup = 0.0

    def _acquire_and_recover(self) -> None:
        self.lock.acquire()
        try:
            with self.store.connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute("UPDATE course_tasks SET status='failed', error='v2 已停用自动填写，任务不会执行', "
                             "finished_at=?, updated_at=? WHERE kind='fill' AND status IN ('queued','running','uncertain')",
                             (stamp(), stamp()))
                # OS lock proves the previous worker no longer owns execution.
                conn.execute(
                    "UPDATE course_tasks SET status=CASE WHEN side_effect=1 THEN 'uncertain' "
                    "WHEN attempts>=max_attempts THEN 'failed' ELSE 'queued' END, "
                    "error='上次 Worker 中断；外部副作用不会自动重放', owner=NULL, "
                    "finished_at=CASE WHEN side_effect=1 OR attempts>=max_attempts "
                    "THEN ? ELSE NULL END, "
                    "available_at=?, updated_at=? WHERE status='running'",
                    (stamp(), stamp(), stamp()),
                )
        except BaseException:
            self.lock.release()
            raise

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._acquire_and_recover()
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="career-worker", daemon=True)
        try:
            self._thread.start()
        except BaseException:
            self._thread = None
            self.lock.release()
            raise

    def stop(self, timeout: float = 5.0) -> bool:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout)
            return not self._thread.is_alive()
        return True

    def _run(self) -> None:
        try:
            while not self._stop.is_set():
                if not self.run_once():
                    self._stop.wait(self.poll_interval)
        finally:
            self._cleanup_and_release()

    def _cleanup_and_release(self) -> None:
        self.lock.release()

    def _claim(self) -> dict | None:
        with self.store.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM course_tasks WHERE status='queued' AND kind!='fill' AND available_at<=? "
                "AND attempts<max_attempts ORDER BY id LIMIT 1", (stamp(),)
            ).fetchone()
            if row is None:
                return None
            conn.execute(
                "UPDATE course_tasks SET status='running', attempts=attempts+1, "
                "owner=?, started_at=?, updated_at=? WHERE id=? AND status='queued'",
                (self.owner, stamp(), stamp(), row["id"]),
            )
            return decode(conn.execute("SELECT * FROM course_tasks WHERE id=?", (row["id"],)).fetchone())

    def run_once(self) -> bool:
        if (self._thread and self._thread.is_alive()
                and threading.current_thread() is not self._thread):
            raise RuntimeError("后台 Worker 运行期间不能在其他线程执行任务")
        if not self._run_guard.acquire(blocking=False):
            return False
        temporary_lock = self.lock.handle is None
        try:
            if temporary_lock:
                self._acquire_and_recover()
            if time.monotonic() >= self._next_cache_cleanup:
                self.store.cleanup_job_cache()
                self._next_cache_cleanup = time.monotonic() + 3600
            task = self._claim()
            if task is None:
                return False
            try:
                handler = self.handlers.get(task["kind"])
                if handler is None:
                    raise DefiniteTaskFailure(f"未注册任务处理器：{task['kind']}")
                # No database transaction is held during network/browser/AI I/O.
                result = handler(task)
                serialized = encode(result)
            except Exception as exc:
                if isinstance(exc, DefiniteTaskFailure):
                    status = "failed"
                elif task["side_effect"]:
                    status = "uncertain"
                elif task["attempts"] < task["max_attempts"]:
                    status = "queued"
                else:
                    status = "failed"
                available = to_iso(now_hk() + timedelta(seconds=min(30, 2 ** task["attempts"])))
                with self.store.connect() as conn:
                    conn.execute(
                        "UPDATE course_tasks SET status=?, error=?, available_at=?, "
                        "finished_at=?, updated_at=? WHERE id=? AND owner=?",
                        (status, safe_error(exc), available,
                         None if status == "queued" else stamp(), stamp(), task["id"], self.owner),
                    )
            else:
                # SMTP returning a known refusal or an ambiguous DATA outcome
                # is a completed *attempt*, not a successful task. Neither is
                # automatically replayed; explicit retries retain the old row.
                outcome = result.get("status") if isinstance(result, dict) else None
                status = outcome if outcome in {"failed", "uncertain"} else "succeeded"
                error = ""
                if status != "succeeded":
                    error = safe_error(DefiniteTaskFailure(
                        result.get("error") or result.get("detail") or status
                    ))
                with self.store.connect() as conn:
                    conn.execute(
                        "UPDATE course_tasks SET status=?, result_json=?, "
                        "error=?, finished_at=?, updated_at=? WHERE id=? AND owner=?",
                        (status, serialized, error, stamp(), stamp(), task["id"], self.owner),
                    )
            return True
        finally:
            if temporary_lock and self.lock.handle is not None:
                self._cleanup_and_release()
            self._run_guard.release()
