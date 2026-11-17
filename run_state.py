# -*- coding: utf-8 -*-
"""画像级「日报正在跑」标记。

看门狗和日报可能在同一次 StartWhenAvailable 补跑里同时被拉起。标记文件
让看门狗知道该等，而不是拿「昨天的 last_ok」立刻发告警。

每个画像一个文件（默认 ``logs/run_in_progress.json``，法务是
``logs/legal/run_in_progress.json``），和 daily.log 放在同一目录。
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent
MARKER_NAME = "run_in_progress.json"


def log_dir(explicit: Path | None = None) -> Path:
    if explicit is not None:
        return Path(explicit)
    return Path(os.environ.get("JOB_LOG_DIR", BASE / "logs"))


def marker_path(directory: Path | None = None) -> Path:
    return log_dir(directory) / MARKER_NAME


def _atomic_write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def mark_run_started(
    slot: str,
    profile: str = "",
    *,
    phase: str = "starting",
    directory: Path | None = None,
) -> Path:
    now = datetime.now().isoformat(timespec="seconds")
    payload = {
        "pid": os.getpid(),
        "profile": profile or os.environ.get("JOB_PROFILE") or "default",
        "slot": slot,
        "phase": phase,
        "started": now,
        "updated": now,
    }
    path = marker_path(directory)
    _atomic_write(path, payload)
    return path


def update_run(
    *,
    phase: str | None = None,
    slot: str | None = None,
    directory: Path | None = None,
    **extra,
) -> None:
    path = marker_path(directory)
    data = read_run_marker(path) or {}
    now = datetime.now().isoformat(timespec="seconds")
    data.setdefault("pid", os.getpid())
    data["pid"] = os.getpid()
    data.setdefault("profile", os.environ.get("JOB_PROFILE") or "default")
    data.setdefault("started", now)
    if slot:
        data["slot"] = slot
    if phase:
        data["phase"] = phase
    data["updated"] = now
    for key, value in extra.items():
        data[key] = value
    _atomic_write(path, data)


def clear_run_marker(directory: Path | None = None) -> None:
    path = marker_path(directory)
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp.unlink()
    except FileNotFoundError:
        pass


def read_run_marker(path: Path | None = None) -> dict | None:
    target = marker_path() if path is None else Path(path)
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def pid_alive(pid: int) -> bool:
    """进程还在不在。Windows 用 OpenProcess，其他系统用 signal 0。"""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        # PROCESS_QUERY_LIMITED_INFORMATION
        handle = kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        try:
            code = wintypes.DWORD()
            ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
            if not ok:
                return False
            return code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def marker_is_live(
    marker: dict | None,
    *,
    now: datetime,
    alive=pid_alive,
    stale_seconds: float = 6 * 3600,
) -> bool:
    """标记对应的日报进程还活着，而且是今天启动的。

    昨天崩溃留下的文件、或者 pid 已经退出，都不能让看门狗一直等。
    """
    if not isinstance(marker, dict):
        return False
    try:
        pid = int(marker.get("pid") or 0)
    except (TypeError, ValueError):
        return False
    if pid <= 0 or not alive(pid):
        return False
    started = _parse_ts(marker.get("started"))
    if started is None:
        return False
    if started.date() != now.date():
        return False
    if (now - started).total_seconds() > stale_seconds:
        return False
    return True


def _parse_ts(value) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
