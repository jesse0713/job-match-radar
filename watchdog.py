# -*- coding: utf-8 -*-
"""看门狗：检查当天有没有跑完一轮、有没有把邮件发出去，没有就回一封告警。

为什么需要它
------------
2026-11-07 起连续三天没收到日报，但计划任务每天都准点触发、退出码 1，
`logs/daily.log` 里一行记录都没有：DSH 桌面端升级时重建了自己的 Python
运行时，把 pip 装进去的 DrissionPage 覆盖掉了，`import collect` 直接
SystemExit；pythonw 没有控制台，于是失败是「完全静默」的。

run_daily.py 现在会自己捕获这种启动失败并发告警，但那只覆盖「能启动」的情况。
这个看门狗是第二道保险：它只依赖标准库 + mailer，每个工作日 13:30 检查一次
daily.log，只要今天还没有一条成功采集，就发邮件说明情况；同一天只告警一次。

成功指：采集到了岗位之后的 ``SENT``（日报），或采集成功但没有新岗位的
``SKIP:no-new``。``send_alert`` 失败时也会返回 ``SENT:``，但 ``collected=0``、
熔断（circuit）或环境校验失败（verify）不算成功，last_ok 不往前走。告警信
照发。午间因环境 token 失败时，晚间补跑任务会再试一次，看门狗不负责补采。

开机补跑（StartWhenAvailable）会把日报和看门狗在同一秒拉起。这时 last_ok
还是昨天，但日报马上就会成功。看门狗先等本画像的 run_in_progress 标记，
或等一条今天的成功记录落盘，再决定要不要告警。

用法：
    python watchdog.py                 # 检查并告警
    python watchdog.py --dry-run       # 只写 .eml 预览，不发信
    python watchdog.py --force         # 周末也检查
    python watchdog.py --max-age-hours 26   # 改成按小时数判断，而不是「今天」
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import run_state

BASE = Path(__file__).resolve().parent


def _cli_value(flag: str) -> str:
    argv = sys.argv[1:]
    for i, arg in enumerate(argv):
        if arg == flag and i + 1 < len(argv):
            return argv[i + 1]
        if arg.startswith(flag + "="):
            return arg.split("=", 1)[1]
    return ""


import settings  # noqa: E402  （只依赖标准库）

PROFILE = (_cli_value("--profile") or os.environ.get("JOB_PROFILE", "")).strip()
settings.apply_profile_env(PROFILE)

LOG_DIR = Path(os.environ.get("JOB_LOG_DIR", BASE / "logs"))
# 晚间补跑和午间失败至少隔这么久，避免 code=37 之后紧挨着再打一轮。
CATCHUP_MIN_BACKOFF_S = 60 * 60
# 同一秒补跑时，日报可能还在 import，或正在等另一画像放开 Chrome。
# 正常失败日多等这一分钟再告警；补跑日则一直等到这轮写出 SENT / SKIP。
CATCHUP_GRACE_S = 60.0
CATCHUP_WAIT_S = 45 * 60.0
CATCHUP_POLL_S = 5.0
CATCHUP_TAIL_S = 15.0


def _cfg() -> dict:
    return settings.load_config()


def _read_entries(path: Path) -> list[dict]:
    out: list[dict] = []
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict) and row.get("ts"):
            out.append(row)
    return out


def _parse_ts(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def _describe(entry: dict) -> str:
    """把一条日志压成一句话，方便写进告警正文。"""
    ts = entry.get("ts", "?")
    if "fatal" in entry:
        return f"{ts} 运行失败：{str(entry['fatal'])[:300]}"
    if "fatal_stage" in entry:
        return f"{ts} 启动阶段失败：{str(entry.get('error'))[:300]}"
    return (
        f"{ts} slot={entry.get('slot', '?')} 采集={entry.get('collected', '?')} "
        f"去重后={entry.get('fresh', '?')} 结果={str(entry.get('mail'))[:120]}"
    )


def row_is_success(row: dict, *, honor_flag: bool = True) -> bool:
    """这条日报算不算「采集成功」。

    - ``SKIP:no-new`` 且确实采到过岗位：成功（没有新岗位可发）。
    - ``SENT:`` 且 ``collected>0``、没有熔断 / 校验页 / 掉登录：成功（日报）。
    - ``send_alert`` 也返回 ``SENT:``。``collected=0``、``circuit``、``verify``
      或显式 ``ok: false`` 都是告警，不是 last_ok。
    - 旧日志没有 ``collected`` 时，仍把纯 ``SENT:`` / ``SKIP:no-new`` 视为成功。

    ``honor_flag=False`` 用来在写日志时计算 ``ok``，避免读到自己刚写的标记。
    """
    if not isinstance(row, dict):
        return False
    if honor_flag and row.get("ok") is True:
        return True
    if honor_flag and row.get("ok") is False:
        return False
    mail = str(row.get("mail") or "")
    diag = row.get("diag") if isinstance(row.get("diag"), dict) else {}
    # --demo 用的是假岗位，不能当成当天 BOSS 采集成功，也不能抵消晚间补跑。
    if diag.get("demo"):
        return False
    if mail.startswith("SKIP:no-new"):
        if diag.get("verify") or diag.get("login"):
            return False
        collected = row.get("collected")
        if collected is None:
            return True
        try:
            return int(collected) > 0
        except (TypeError, ValueError):
            return False
    if not mail.startswith("SENT:"):
        return False
    if diag.get("verify") or diag.get("circuit") or diag.get("login"):
        return False
    collected = row.get("collected")
    if collected is None:
        return True
    try:
        return int(collected) > 0
    except (TypeError, ValueError):
        return False


def _blocked_code_37(diag: dict) -> bool:
    for item in diag.get("blocked") or []:
        if isinstance(item, dict) and str(item.get("code")) == "37":
            return True
    blob = json.dumps(diag, ensure_ascii=False)
    if "环境异常" in blob or "__zp_stoken__" in blob or "环境校验" in blob:
        return True
    if "security.html" in blob:
        return True
    return False


def needs_token_catchup(row: dict) -> bool:
    """午间这次失败，是否值得晚上用官方页面再试一次。

    熔断、校验页没完成、或 collected=0 且诊断里是 code=37 / 环境异常。
    掉登录要人工扫码，补跑解决不了。``SKIP:no-new`` 不是失败。
    """
    if not isinstance(row, dict) or row_is_success(row):
        return False
    mail = str(row.get("mail") or "")
    if mail.startswith("SKIP:"):
        return False
    if "mail" not in row and "diag" not in row:
        return False
    diag = row.get("diag") if isinstance(row.get("diag"), dict) else {}
    if diag.get("login"):
        return False
    if diag.get("verify") or diag.get("circuit"):
        return True
    try:
        empty = row.get("collected") is not None and int(row.get("collected")) <= 0
    except (TypeError, ValueError):
        empty = False
    if empty or mail.startswith("SENT:"):
        return _blocked_code_37(diag)
    return False


def catchup_decision(
    entries,
    *,
    now: datetime,
    min_backoff_s: float = CATCHUP_MIN_BACKOFF_S,
    marker: dict | None = None,
    own_pid: int | None = None,
    marker_live=None,
) -> tuple[bool, str]:
    """晚间任务要不要真的再采一次。

    只有「今天已经有一次环境/token 失败，而且距离那次至少 ``min_backoff_s``」
    才返回 True。成功采集、``SKIP:no-new``、掉登录、刚失败不到退避时间、
    今天已经补跑过，都不再打。另一个进程还占着本画像时返回 in-progress，
    这次不算把补跑机会用掉。
    """
    if marker_live is None:
        marker_live = run_state.marker_is_live
    if isinstance(marker, dict) and own_pid is not None:
        try:
            if int(marker.get("pid") or -1) == int(own_pid):
                marker = None
        except (TypeError, ValueError):
            pass
    if marker and marker_live(marker, now=now):
        return False, "in-progress"

    today = []
    for row in entries or []:
        if not isinstance(row, dict):
            continue
        ts = _parse_ts(row.get("ts", ""))
        if ts is None or ts.date() != now.date():
            continue
        today.append((ts, row))
    if any(row_is_success(row) for _, row in today):
        return False, "already-ok"
    retried = [
        row for _, row in today
        if row.get("catchup") and str(row.get("mail") or "")
        and not str(row.get("mail") or "").startswith("SKIP:catchup")
    ]
    if retried:
        return False, "already-retried"
    failures = [(ts, row) for ts, row in today if needs_token_catchup(row)]
    if not failures:
        return False, "no-failed-collect"
    latest_ts = max(ts for ts, _ in failures)
    if (now - latest_ts).total_seconds() < float(min_backoff_s):
        return False, "too-soon"
    return True, "retry-token-failure"


def success_in_window(entries, *, started: datetime, max_age_hours: float = 0):
    """最近一条成功采集是否落在「今天」或给定的小时窗口里。"""
    ok_rows = []
    for row in entries or []:
        if not row_is_success(row):
            continue
        ts = _parse_ts(row.get("ts", ""))
        if ts is None:
            continue
        ok_rows.append((ts, row))
    if not ok_rows:
        return None, None, False
    last_ok_ts, last_ok_row = max(ok_rows, key=lambda item: item[0])
    if max_age_hours > 0:
        healthy = last_ok_ts >= started - timedelta(hours=max_age_hours)
    else:
        healthy = last_ok_ts.date() == started.date()
    return last_ok_ts, last_ok_row, healthy


def poll_until_settled(
    read_entries,
    read_marker,
    pid_alive_fn,
    *,
    grace: float = CATCHUP_GRACE_S,
    wait: float = CATCHUP_WAIT_S,
    poll: float = CATCHUP_POLL_S,
    tail: float = CATCHUP_TAIL_S,
    max_age_hours: float = 0.0,
    monotonic=None,
    sleep=None,
    now_fn=None,
    on_wait=None,
) -> dict:
    """等日报结果，或确认这轮并没有跑起来。

    - 今天已经有成功采集（有结果的 SENT，或 SKIP:no-new）：立刻返回，不发告警。
      告警邮件的 SENT（collected=0 / 熔断 / 校验失败）不算。
    - 宽限期内出现「正在跑」标记，或随后写出成功记录：继续等，成功则不告警。
    - 宽限期内什么都没出现：按失败告警（和平时 13:30 的行为一致，只是晚一分钟）。
    - 标记消失后仍再等一小段，避免成功日志比标记晚一点点落盘。
    """
    monotonic = monotonic or time.monotonic
    sleep = sleep or time.sleep
    now_fn = now_fn or datetime.now
    poll = float(poll) if poll and poll > 0 else CATCHUP_POLL_S

    start_mono = monotonic()
    start_dt = now_fn()
    saw_live = False
    last_marker = None
    tail_until = None
    last_note = -1e9

    def pack(reason: str, healthy: bool, last_ok_ts, last_ok_row, marker) -> dict:
        return {
            "healthy": healthy,
            "reason": reason,
            "waited_s": round(max(0.0, monotonic() - start_mono), 1),
            "last_ok_ts": last_ok_ts,
            "last_ok_row": last_ok_row,
            "marker": marker,
            "should_alert": not healthy,
        }

    while True:
        now = now_fn()
        elapsed = monotonic() - start_mono
        entries = list(read_entries() or [])
        last_ok_ts, last_ok_row, healthy = success_in_window(
            entries, started=start_dt, max_age_hours=max_age_hours,
        )
        if healthy:
            if saw_live:
                reason = "success-during-wait"
            elif elapsed <= 0.05:
                reason = "already-ok"
            else:
                reason = "success-during-grace"
            return pack(reason, True, last_ok_ts, last_ok_row, last_marker)

        marker = read_marker()
        if marker:
            last_marker = marker
        live = run_state.marker_is_live(marker, now=now, alive=pid_alive_fn)
        if live:
            saw_live = True
            tail_until = None
        elif saw_live:
            if tail_until is None:
                tail_until = monotonic() + max(0.0, float(tail))
            if monotonic() >= tail_until:
                return pack(
                    "run-finished-without-success",
                    False, last_ok_ts, last_ok_row, last_marker,
                )
        elif elapsed >= float(grace):
            return pack("no-run-started", False, last_ok_ts, last_ok_row, last_marker)

        if elapsed >= float(wait):
            return pack("wait-timeout", False, last_ok_ts, last_ok_row, last_marker)

        if on_wait is not None and saw_live and (elapsed - last_note >= 30 or last_note < 0):
            try:
                on_wait({
                    "elapsed_s": round(elapsed, 1),
                    "phase": (marker or last_marker or {}).get("phase"),
                    "pid": (marker or last_marker or {}).get("pid"),
                })
            except Exception:
                pass
            last_note = elapsed

        step = poll
        remaining_wait = max(0.0, float(wait) - elapsed)
        step = min(step, remaining_wait if remaining_wait > 0 else poll)
        if tail_until is not None:
            step = min(step, max(0.0, tail_until - monotonic()))
        elif not saw_live:
            step = min(step, max(0.0, float(grace) - elapsed))
        sleep(step if step > 0 else poll)


def _append_watchdog_log(payload: dict) -> None:
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        row = {"ts": datetime.now().isoformat(timespec="seconds"), **payload}
        with (LOG_DIR / "watchdog.log").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _window_desc(now: datetime, max_age_hours: float) -> str:
    if max_age_hours > 0:
        return f"最近 {max_age_hours:g} 小时"
    return f"今天（{now:%Y-%m-%d}）"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-age-hours", type=float, default=0,
                    help="按最近多少小时内有成功记录来判断；0=按「今天」判断（默认）")
    ap.add_argument("--force", action="store_true", help="周末也检查（默认周末跳过）")
    ap.add_argument("--dry-run", action="store_true", help="不发信，只写 .eml 预览")
    ap.add_argument("--profile", default=PROFILE, help="求职画像名（默认读 JOB_PROFILE）")
    ap.add_argument("--catchup-grace", type=float, default=CATCHUP_GRACE_S,
                    help="启动后先等这么多秒，看同一秒补跑的日报会不会出现（默认 60）")
    ap.add_argument("--catchup-wait", type=float, default=CATCHUP_WAIT_S,
                    help="日报进程还活着时，最多再等多少秒（默认 45 分钟）")
    ap.add_argument("--catchup-tail", type=float, default=CATCHUP_TAIL_S,
                    help="日报进程结束后再等多少秒，给成功日志落盘（默认 15）")
    ap.add_argument("--poll-interval", type=float, default=CATCHUP_POLL_S,
                    help="等待期间检查日志的间隔秒数")
    args = ap.parse_args()

    now = datetime.now()
    cfg = _cfg()
    search_cfg = cfg.get("search", {})

    # 周末本来就不推送，别在周末误报
    if search_cfg.get("weekdays_only", True) and now.weekday() >= 5 and not args.force:
        print("SKIP weekend")
        return 0

    # 同一天只告警一次，避免重复轰炸。放在等待之前，第二次手动跑不必再等一轮。
    state_path = LOG_DIR / "watchdog_state.json"
    today = now.strftime("%Y-%m-%d")
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        state = {}
    if state.get("last_alert_date") == today and not args.dry_run:
        print(f"SKIP already alerted on {today}")
        return 0

    daily = LOG_DIR / "daily.log"
    marker_path = LOG_DIR / "run_in_progress.json"
    window_desc = _window_desc(now, args.max_age_hours)

    def _note_wait(info: dict) -> None:
        phase = info.get("phase") or "?"
        pid = info.get("pid")
        try:
            print(f"[watchdog] 本画像日报仍在运行（pid={pid} phase={phase}），"
                  f"已等 {info.get('elapsed_s')}s", flush=True)
        except Exception:
            pass
        _append_watchdog_log({"event": "wait", **info, "profile": PROFILE or "default"})

    outcome = poll_until_settled(
        lambda: _read_entries(daily),
        lambda: run_state.read_run_marker(marker_path),
        run_state.pid_alive,
        grace=args.catchup_grace,
        wait=args.catchup_wait,
        poll=args.poll_interval,
        tail=args.catchup_tail,
        max_age_hours=args.max_age_hours,
        on_wait=_note_wait,
    )
    entries = _read_entries(daily)
    last_ok_ts = outcome["last_ok_ts"]
    last_ok_row = outcome["last_ok_row"] or {}

    if not outcome["should_alert"]:
        if outcome["waited_s"] >= 1:
            _append_watchdog_log({
                "event": "suppressed",
                "reason": outcome["reason"],
                "waited_s": outcome["waited_s"],
                "profile": PROFILE or "default",
                "last_ok": last_ok_ts.isoformat(timespec="seconds") if last_ok_ts else None,
            })
        when = last_ok_ts.strftime("%Y-%m-%d %H:%M") if last_ok_ts else "?"
        print(f"OK {window_desc}已有成功记录：{when} "
              f"({str(last_ok_row.get('mail'))[:40]})"
              + (f"；已等 {outcome['waited_s']:.0f}s（{outcome['reason']}）"
                 if outcome["waited_s"] >= 1 else ""))
        return 0

    waited_note = ""
    if outcome["reason"] == "wait-timeout":
        marker = outcome.get("marker") or {}
        waited_note = (
            f"本画像的日报进程仍在运行（pid={marker.get('pid')}，阶段={marker.get('phase') or '?'}），"
            f"看门狗已等 {outcome['waited_s']:.0f} 秒到达上限。"
            "请看该画像日志目录里的 run_in_progress.json 和项目 logs/chrome_lock.log。\n\n"
        )
    elif outcome["waited_s"] >= 1:
        waited_note = (
            f"看门狗启动后先等了 {outcome['waited_s']:.0f} 秒"
            f"（{outcome['reason']}），没有看到本画像今天的成功采集"
            "（有结果的日报，或采集成功后的 SKIP:no-new）。"
            "告警邮件不算成功。\n\n"
        )

    last_any = entries[-1] if entries else None
    if last_ok_ts:
        head = (f"{window_desc}还没有成功推送过岗位。最近一次成功是 "
                f"{last_ok_ts:%Y-%m-%d %H:%M}，之后的任务没走完。")
    else:
        head = f"{window_desc}没有任何成功记录，日志里从来没发出去过一封。"
    head = waited_note + head

    lines = [
        head,
        "",
        "计划任务可能照常在触发，但「采集 → 筛选 → 发信」链路没有走完，所以你没收到日报。"
        "常见原因：",
        "  1) 计划任务指向的 Python 缺少 DrissionPage（DSH 升级重建运行时会导致）；",
        "  2) 登录态失效 —— 需要运行 python login.py 重新扫码；",
        "  3) BOSS 环境 token（code=37 / security.html）没在无人值守的 Chrome 里写完。",
        "     午间告警邮件不算成功。晚间补跑任务会再试一次，不会立刻连打；",
        "     仍失败就运行 python login.py，让窗口里的官方校验页走完。",
        "",
        f"最近一条日志：{_describe(last_any) if last_any else '（日志为空）'}",
        "",
        "排查：在项目目录运行",
        "  powershell -ExecutionPolicy Bypass -File .\\install_task.ps1",
        "它会逐个探测解释器并把任务重指到能 import DrissionPage 的那个；",
        "然后 python login.py --check 看登录态，最后 python run_daily.py --slot 12:30"
        + (f" --profile {PROFILE}" if PROFILE else "") + " 手动补跑一次。",
    ]
    message = "\n".join(lines)

    alert_at = datetime.now()
    run_label = f"{alert_at:%H:%M} 看门狗" + (f"（{PROFILE}）" if PROFILE else "")
    import mailer  # 只依赖标准库，解释器再破也能发出去
    result = mailer.send_alert(message, cfg, run_label, dry_run=args.dry_run)

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    with (LOG_DIR / "watchdog.log").open("a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": alert_at.isoformat(timespec="seconds"),
                            "last_ok": last_ok_ts.isoformat(timespec="seconds") if last_ok_ts else None,
                            "reason": outcome["reason"],
                            "waited_s": outcome["waited_s"],
                            "result": result}, ensure_ascii=False) + "\n")
    if not args.dry_run:
        state_path.write_text(json.dumps({"last_alert_date": today,
                                          "last_ok": last_ok_ts.isoformat(timespec="seconds") if last_ok_ts else None},
                                         ensure_ascii=False), encoding="utf-8")
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
