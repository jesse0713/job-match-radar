# -*- coding: utf-8 -*-
"""跨进程锁：两个画像共用同一个 Chrome 调试端口。

默认画像（12:30）和法务画像（12:45）都连 9333。平时计划任务错开，
互不影响；电脑中午关机时，Windows 的 StartWhenAvailable 会在开机后把
错过的任务同一秒拉起。2026-11-17 就是这样：法务跑完调用 Browser.close，
默认画像剩下的关键词全部 ConnectionError 到 127.0.0.1:9333。

锁盖住「附着 → 采集 → 关闭」整段。等锁的一方打日志（wait），拿到锁之后
如果前一个进程已经把 Chrome 关了，就自己再拉起（acquire）。超时写 timeout。
释放写 release。

Windows 用命名互斥量 ``Local\\JobMatch-Chrome-<port>``（不依赖目录是不是
junction）。其他系统用项目目录里的文件锁，单元测试走同一条等待 / 超时逻辑。
同一进程内可重入：run_daily 持锁时，attach 再取一次不会把自己卡死。
"""
from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent

# 一次完整采集大约数分钟到十几分钟；补跑时后启动的画像要等前一个跑完。
# 计划任务的 ExecutionTimeLimit 要大于这个等待加上自己的采集时间。
DEFAULT_TIMEOUT_S = 45 * 60
DEFAULT_POLL_S = 15.0

_SINGLETONS: dict[int, "ChromeLock"] = {}
_SINGLETON_GUARD = threading.Lock()


class ChromeLockTimeout(TimeoutError):
    """等另一个画像放开 Chrome 端口，直到超时。"""

    def __init__(self, port: int, timeout: float, waited: float):
        self.port = port
        self.timeout = timeout
        self.waited = waited
        super().__init__(
            f"等待 Chrome 端口 {port} 的跨进程锁超时（已等 {waited:.0f}s / 上限 {timeout:.0f}s）。"
            "另一个画像可能仍在采集；本进程没有启动或关闭浏览器。"
        )


def default_timeout() -> float:
    raw = os.environ.get("CHROME_LOCK_TIMEOUT", "").strip()
    if not raw:
        return float(DEFAULT_TIMEOUT_S)
    try:
        value = float(raw)
    except ValueError:
        return float(DEFAULT_TIMEOUT_S)
    return value if value > 0 else float(DEFAULT_TIMEOUT_S)


def default_log_path() -> Path:
    return BASE / "logs" / "chrome_lock.log"


def log_event(event: str, log_path: Path | None = None, echo: bool = False, **fields) -> dict:
    """写一条锁 / 浏览器事件。pythonw 没有控制台，所以必须落盘。"""
    rec = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "event": event,
        "pid": os.getpid(),
        "profile": os.environ.get("JOB_PROFILE") or "default",
        **fields,
    }
    path = default_log_path() if log_path is None else log_path
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            fh.flush()
    except Exception:
        pass
    if echo:
        try:
            extra = " ".join(f"{k}={v}" for k, v in fields.items() if v is not None)
            print(f"[chrome-lock] {event} {extra}".rstrip(), flush=True)
        except Exception:
            pass
    return rec


def _owner_blob(port: int) -> dict:
    return {
        "pid": os.getpid(),
        "profile": os.environ.get("JOB_PROFILE") or "default",
        "port": port,
        "since": datetime.now().isoformat(timespec="seconds"),
    }


def _read_json(path: Path | None) -> dict | None:
    if path is None or not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


class FileLockBackend:
    """项目目录里的排他文件锁。Windows 用 msvcrt，其他系统用 flock。"""

    def __init__(self, path: Path):
        self.path = path
        self._fh = None
        self.abandoned = False

    def _file(self):
        if self._fh is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # 至少一个字节，msvcrt.locking 才能锁到区间
            self._fh = open(self.path, "a+b")
            if self._fh.seek(0, os.SEEK_END) == 0:
                self._fh.write(b"\0")
                self._fh.flush()
        return self._fh

    def try_acquire(self, timeout: float) -> bool:
        self.abandoned = False
        fh = self._file()
        deadline = time.monotonic() + max(0.0, float(timeout))
        while True:
            try:
                if os.name == "nt":
                    import msvcrt

                    fh.seek(0)
                    msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return True
            except OSError:
                if time.monotonic() >= deadline:
                    return False
                time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))

    def release(self) -> None:
        fh = self._fh
        if fh is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass

    def close(self) -> None:
        fh = getattr(self, "_fh", None)
        self._fh = None
        if fh is not None:
            try:
                fh.close()
            except Exception:
                pass

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass


class MutexBackend:
    """Windows 命名互斥量。安全属性为 NULL，句柄不可继承，Chrome 子进程不会把锁带走。"""

    def __init__(self, name: str):
        import ctypes
        from ctypes import wintypes

        self.name = name
        self.abandoned = False
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
        kernel32.CreateMutexW.restype = wintypes.HANDLE
        kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel32.WaitForSingleObject.restype = wintypes.DWORD
        kernel32.ReleaseMutex.argtypes = [wintypes.HANDLE]
        kernel32.ReleaseMutex.restype = wintypes.BOOL
        handle = kernel32.CreateMutexW(None, False, name)
        if not handle:
            err = ctypes.get_last_error()
            raise OSError(err, f"CreateMutexW failed for {name}")
        self._handle = handle
        self._wait = kernel32.WaitForSingleObject
        self._release = kernel32.ReleaseMutex

    def try_acquire(self, timeout: float) -> bool:
        import ctypes

        self.abandoned = False
        ms = 0 if timeout <= 0 else min(int(timeout * 1000), 0xFFFFFFFE)
        rc = self._wait(self._handle, ms)
        if rc == 0:  # WAIT_OBJECT_0
            return True
        if rc == 0x80:  # WAIT_ABANDONED：上一个持有者崩溃了，锁已经到我们手里
            self.abandoned = True
            return True
        if rc == 0x102:  # WAIT_TIMEOUT
            return False
        raise OSError(ctypes.get_last_error(), f"WaitForSingleObject failed rc={rc}")

    def release(self) -> None:
        self._release(self._handle)


class ChromeLock:
    """持有共享 Chrome 端口的跨进程锁。

    ``try_acquire(timeout)`` 的实现必须自己阻塞到超时，而不是立刻返回 False，
    否则等待循环会空转。
    """

    def __init__(
        self,
        port: int = 9333,
        timeout: float | None = None,
        *,
        kind: str | None = None,
        lock_path: Path | None = None,
        log_path: Path | None = None,
        owner_path: Path | None = None,
        echo: bool = True,
        poll: float = DEFAULT_POLL_S,
        backend=None,
        on_event=None,
    ):
        self.port = int(port)
        self.timeout = float(default_timeout() if timeout is None else timeout)
        self.poll = float(poll) if poll and poll > 0 else DEFAULT_POLL_S
        self.echo = echo
        self.log_path = log_path
        self.on_event = on_event
        self._depth = 0
        self._owner_tid: int | None = None
        self._acquired_at = 0.0
        self._guard = threading.Lock()
        if lock_path is None:
            lock_path = BASE / "logs" / f"chrome-{self.port}.lock"
        self.lock_path = Path(lock_path)
        if owner_path is None:
            owner_path = Path(str(self.lock_path) + ".owner.json")
        self.owner_path = Path(owner_path)
        self._backend = backend if backend is not None else _make_backend(self.port, kind, self.lock_path)

    def held_by_current_thread(self) -> bool:
        return self._depth > 0 and self._owner_tid == threading.get_ident()

    def acquire(self) -> None:
        me = threading.get_ident()
        with self._guard:
            if self._depth > 0 and self._owner_tid == me:
                self._depth += 1
                self._emit("acquire", waited_s=0, reenter=True, depth=self._depth)
                return

        if self._backend.try_acquire(0):
            self._mark_held(me, waited=0.0)
            return

        start = time.monotonic()
        last_wait_log = -1e9
        while True:
            elapsed = time.monotonic() - start
            if elapsed >= self.timeout:
                self._emit(
                    "timeout",
                    waited_s=round(elapsed, 1),
                    timeout_s=self.timeout,
                    owner=self._owner_snapshot(),
                )
                raise ChromeLockTimeout(self.port, self.timeout, elapsed)
            if elapsed - last_wait_log >= self.poll or last_wait_log < 0:
                self._emit(
                    "wait",
                    waited_s=round(elapsed, 1),
                    timeout_s=self.timeout,
                    owner=self._owner_snapshot(),
                )
                last_wait_log = elapsed
            slice_s = min(self.poll, self.timeout - elapsed)
            if self._backend.try_acquire(slice_s):
                self._mark_held(me, waited=time.monotonic() - start)
                return

    def release(self) -> None:
        with self._guard:
            if self._depth <= 0 or self._owner_tid != threading.get_ident():
                return
            self._depth -= 1
            depth = self._depth
        if depth > 0:
            return
        held_s = round(time.monotonic() - self._acquired_at, 1) if self._acquired_at else None
        self._clear_owner()
        try:
            self._backend.release()
        finally:
            with self._guard:
                self._owner_tid = None
            self._emit("release", held_s=held_s)

    def __enter__(self) -> "ChromeLock":
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()

    def _mark_held(self, tid: int, waited: float) -> None:
        with self._guard:
            self._owner_tid = tid
            self._depth = 1
        self._acquired_at = time.monotonic()
        self._write_owner()
        self._emit(
            "acquire",
            waited_s=round(waited, 1),
            abandoned=bool(getattr(self._backend, "abandoned", False)),
            reenter=False,
        )

    def _owner_snapshot(self) -> dict | None:
        return _read_json(self.owner_path)

    def _write_owner(self) -> None:
        try:
            self.owner_path.parent.mkdir(parents=True, exist_ok=True)
            self.owner_path.write_text(
                json.dumps(_owner_blob(self.port), ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception:
            pass

    def _clear_owner(self) -> None:
        try:
            self.owner_path.unlink()
        except FileNotFoundError:
            pass
        except Exception:
            pass

    def _emit(self, event: str, **fields) -> None:
        rec = log_event(
            event,
            log_path=self.log_path if self.log_path is not None else default_log_path(),
            echo=self.echo,
            port=self.port,
            **fields,
        )
        if self.on_event is not None:
            try:
                self.on_event(rec)
            except Exception:
                pass


def _make_backend(port: int, kind: str | None, lock_path: Path):
    chosen = (kind or ("mutex" if os.name == "nt" else "file")).lower()
    if chosen == "mutex":
        try:
            return MutexBackend(f"Local\\JobMatch-Chrome-{int(port)}")
        except OSError:
            return FileLockBackend(lock_path)
    if chosen == "file":
        return FileLockBackend(lock_path)
    raise ValueError(f"unknown chrome lock kind: {kind}")


def get_chrome_lock(
    port: int | None = None,
    timeout: float | None = None,
    *,
    log_path: Path | None = None,
) -> ChromeLock:
    """进程内单例。嵌套的 attach / close 必须拿到同一把锁，不能各开一个文件描述符。"""
    resolved = int(port if port is not None else os.environ.get("CHROME_DEBUG_PORT", "9333"))
    with _SINGLETON_GUARD:
        lock = _SINGLETONS.get(resolved)
        if lock is None:
            lock = ChromeLock(port=resolved, timeout=timeout, log_path=log_path)
            _SINGLETONS[resolved] = lock
        elif timeout is not None:
            lock.timeout = float(timeout)
        return lock


def reset_for_tests() -> None:
    with _SINGLETON_GUARD:
        _SINGLETONS.clear()


_DISCONNECT_NAMES = (
    "disconnect",
    "connectionerror",
    "connectionrefused",
    "connectionreset",
    "connectionaborted",
    "brokenpipe",
)
_DISCONNECT_HINTS = (
    "127.0.0.1",
    "websocket",
    "disconnected",
    "connection refused",
    "connection aborted",
    "connection reset",
    "10061",
    "10054",
    "target closed",
    "browser has been closed",
    "page disconnected",
    "与页面的连接已断开",
    "连接已断开",
    "远程主机强迫关闭",
    "failed to fetch browser",
    "handshake status 404",
    "remote-debugging",
    "browser disconnected",
)
_NOT_DISCONNECT = {"BossLoginRequired", "BossVerifyRequired", "BossBlocked", "BossError"}


def is_chrome_disconnect(exc: BaseException, *, port_alive: bool | None = None) -> bool:
    """采集循环里判断「调试端口掉了」，而不是风控 / 掉登录。

    ``port_alive is False`` 时，页面对象上的任意异常都按断开处理：端口已经没了，
    继续用旧的 page 只会让后面的关键词全是 0。
    """
    if isinstance(exc, (KeyboardInterrupt, SystemExit)):
        return False
    if type(exc).__name__ in _NOT_DISCONNECT:
        return False
    if isinstance(exc, (ConnectionError, BrokenPipeError)):
        return True
    name = type(exc).__name__.lower()
    if any(tok in name for tok in _DISCONNECT_NAMES):
        return True
    msg = f"{type(exc).__name__}: {exc}".lower()
    if any(hint in msg for hint in _DISCONNECT_HINTS):
        return True
    if port_alive is False:
        return True
    return False
