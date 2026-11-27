"""持久化：当日抽取记录的单文件 JSON 存取。"""

from __future__ import annotations

import errno
import json
import os
import shutil
import tempfile
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any, TypeVar

from .log_types import DiagnosticLogger
from .models import DailyBoard

if os.name == "nt":
    import msvcrt
else:  # pragma: no cover - POSIX branch
    import fcntl

_T = TypeVar("_T")
_PATH_LOCKS_GUARD = threading.Lock()
_PATH_LOCKS: dict[str, threading.RLock] = {}

_IO_RETRY_DELAYS: tuple[float, ...] = (0.01, 0.02, 0.04, 0.08, 0.16, 0.25)
_LOCK_WAIT_TIMEOUT_SEC: float = 5.0
_TRANSIENT_ERRNOS: frozenset[int] = frozenset(
    code
    for code in (
        errno.EACCES,
        errno.EAGAIN,
        errno.EBUSY,
        errno.EPERM,
        getattr(errno, "EWOULDBLOCK", errno.EAGAIN),
    )
    if code is not None
)
_TRANSIENT_WINERRORS: frozenset[int] = frozenset({5, 32, 33})


def _empty_board() -> dict[str, Any]:
    """新建一份空白记录结构（每次返回新对象，避免共享可变默认值）。"""
    return {"date": "", "records": {}}


def _path_lock(path: Path) -> threading.RLock:
    key = str(path.resolve())
    with _PATH_LOCKS_GUARD:
        lock = _PATH_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _PATH_LOCKS[key] = lock
        return lock


def _is_transient_os_error(exc: OSError) -> bool:
    if isinstance(exc, PermissionError):
        return True
    if getattr(exc, "winerror", None) in _TRANSIENT_WINERRORS:
        return True
    return exc.errno in _TRANSIENT_ERRNOS


def _retry_io(op: Callable[[], _T]) -> _T:
    for delay in _IO_RETRY_DELAYS:
        try:
            return op()
        except OSError as exc:
            if not _is_transient_os_error(exc):
                raise
            time.sleep(delay)
    return op()


class BoardStore:
    """当日抽取记录的持久化。

    提供线程锁 + 跨进程文件锁保护与原子写入重试；仅在 JSON 实际损坏时才重建，
    绝不将 Windows 瞬态 ``PermissionError`` 或其他 ``OSError`` 误判为损坏而清空榜单。
    日期不匹配视为新的一天。
    """

    def __init__(self, path: Path, logger: DiagnosticLogger) -> None:
        self._path = path
        self._logger = logger
        self._thread_lock = _path_lock(path)
        self._lock_depth = threading.local()

    @property
    def _lock_file_path(self) -> Path:
        return self._path.parent / f".{self._path.name}.lock"

    @contextmanager
    def _acquire_lock(self) -> Iterator[None]:
        """Serialize access across threads and processes (re-entrant per thread)."""
        with self._thread_lock:
            depth = getattr(self._lock_depth, "count", 0)
            if depth > 0:
                self._lock_depth.count = depth + 1
                try:
                    yield
                finally:
                    self._lock_depth.count = depth
                return

            self._path.parent.mkdir(parents=True, exist_ok=True)
            lock_fd: int | None = None
            locked = False
            try:
                lock_fd = _retry_io(
                    lambda: os.open(
                        str(self._lock_file_path),
                        os.O_CREAT | os.O_RDWR,
                        0o600,
                    )
                )
                deadline = time.monotonic() + _LOCK_WAIT_TIMEOUT_SEC
                while True:
                    try:
                        if os.name == "nt":
                            os.lseek(lock_fd, 0, os.SEEK_SET)
                            msvcrt.locking(lock_fd, msvcrt.LK_NBLCK, 1)
                        else:  # pragma: no cover - POSIX branch
                            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        locked = True
                        break
                    except OSError:
                        if time.monotonic() >= deadline:
                            raise TimeoutError(
                                f"Timed out acquiring lock for {self._path}"
                            )
                        time.sleep(0.01)

                self._lock_depth.count = 1
                try:
                    yield
                finally:
                    self._lock_depth.count = 0
            finally:
                if lock_fd is not None:
                    if locked:
                        try:
                            if os.name == "nt":
                                os.lseek(lock_fd, 0, os.SEEK_SET)
                                msvcrt.locking(lock_fd, msvcrt.LK_UNLCK, 1)
                            else:  # pragma: no cover - POSIX branch
                                fcntl.flock(lock_fd, fcntl.LOCK_UN)
                        except OSError:
                            pass
                    try:
                        os.close(lock_fd)
                    except OSError:
                        pass

    def load(self, day: str) -> DailyBoard:
        """读取指定日期的记录板。"""
        with self._acquire_lock():
            return self._load_unlocked(day)

    def save(self, board: DailyBoard) -> None:
        """保存记录板。"""
        with self._acquire_lock():
            self._write_unlocked(board.to_mapping())

    def update(self, day: str, mutator: Callable[[DailyBoard], _T]) -> _T:
        """在互斥锁内原子执行“读取-修改-保存”，避免并发抽签丢失更新。"""
        with self._acquire_lock():
            board = self._load_unlocked(day)
            before = board.to_mapping()
            result = mutator(board)
            after = board.to_mapping()
            if after != before:
                self._write_unlocked(after)
            return result

    def _load_unlocked(self, day: str) -> DailyBoard:
        raw = self._read_unlocked()
        if not isinstance(raw, Mapping):
            return DailyBoard.empty(day)
        if raw.get("date") != day:
            prev_collections = (
                raw.get("collections")
                if isinstance(raw.get("collections"), Mapping)
                else None
            )
            return DailyBoard.empty(day, collections=prev_collections)
        return DailyBoard.from_mapping(raw, day)

    def _read(self) -> Any:
        """读取原始 JSON（带并发锁）。"""
        with self._acquire_lock():
            return self._read_unlocked()

    def _read_unlocked(self) -> Any:
        """读取原始 JSON；仅在文件缺失或 JSON 内容损坏时重建默认结构。"""
        if not self._path.exists():
            default = _empty_board()
            self._write_unlocked(default)
            return default
        try:
            text = _retry_io(lambda: self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            default = _empty_board()
            self._write_unlocked(default)
            return default
        except UnicodeDecodeError as exc:
            return self._recover_corrupt_unlocked(exc)
        except OSError as exc:
            # Never treat transient or permission OS errors as JSON corruption!
            self._logger.error(
                "读取记录文件发生 I/O 错误，已保留原文件未清空：%s（%s）",
                self._path,
                exc,
            )
            raise

        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            return self._recover_corrupt_unlocked(exc)

    def _recover_corrupt_unlocked(self, exc: Exception) -> dict[str, Any]:
        self._logger.error("JSON文件解析失败，备份并重置为默认值：%s（%s）", self._path, exc)
        try:
            backup = self._path.with_name(f"{self._path.name}.corrupt")
            shutil.copy2(self._path, backup)
        except OSError:
            pass
        default = _empty_board()
        self._write_unlocked(default)
        return default

    def _write(self, data: Any) -> None:
        """原子写入 JSON（带并发锁）。"""
        with self._acquire_lock():
            self._write_unlocked(data)

    def _write_unlocked(self, data: Any) -> None:
        """原子写入 JSON（自动创建父目录，并对瞬态 PermissionError 重试）。"""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self._path.parent,
                prefix=f".{self._path.name}.",
                suffix=".tmp",
                delete=False,
            ) as temporary_file:
                temporary_path = Path(temporary_file.name)
                json.dump(data, temporary_file, ensure_ascii=False, indent=2)
                temporary_file.flush()
                os.fsync(temporary_file.fileno())
            _retry_io(lambda: os.replace(temporary_path, self._path))
            temporary_path = None
        finally:
            if temporary_path is not None:
                try:
                    temporary_path.unlink(missing_ok=True)
                except OSError:
                    pass
