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
daily.log，只要今天还没有一条成功记录（SENT 发信 / SKIP 无新增），就发邮件
说明情况；同一天只告警一次。

"SKIP:no-new" 也算成功：采集正常、只是当天没有新岗位，属于健康状态。

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
from datetime import datetime, timedelta
from pathlib import Path

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
OK_PREFIXES = ("SENT:", "SKIP:")


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


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-age-hours", type=float, default=0,
                    help="按最近多少小时内有成功记录来判断；0=按「今天」判断（默认）")
    ap.add_argument("--force", action="store_true", help="周末也检查（默认周末跳过）")
    ap.add_argument("--dry-run", action="store_true", help="不发信，只写 .eml 预览")
    ap.add_argument("--profile", default=PROFILE, help="求职画像名（默认读 JOB_PROFILE）")
    args = ap.parse_args()

    now = datetime.now()
    cfg = _cfg()
    search_cfg = cfg.get("search", {})

    # 周末本来就不推送，别在周末误报
    if search_cfg.get("weekdays_only", True) and now.weekday() >= 5 and not args.force:
        print("SKIP weekend")
        return 0

    daily = LOG_DIR / "daily.log"
    entries = _read_entries(daily)

    ok_rows = []
    for row in entries:
        if str(row.get("mail") or "").startswith(OK_PREFIXES):
            ts = _parse_ts(row.get("ts", ""))
            if ts:
                ok_rows.append((ts, row))
    last_ok_ts, last_ok_row = (max(ok_rows, key=lambda x: x[0]) if ok_rows else (None, None))

    if args.max_age_hours > 0:
        healthy = bool(last_ok_ts) and last_ok_ts >= now - timedelta(hours=args.max_age_hours)
        window_desc = f"最近 {args.max_age_hours:g} 小时"
    else:
        healthy = bool(last_ok_ts) and last_ok_ts.date() == now.date()
        window_desc = f"今天（{now:%Y-%m-%d}）"

    if healthy:
        print(f"OK {window_desc}已有成功记录：{last_ok_ts:%Y-%m-%d %H:%M} "
              f"({str(last_ok_row.get('mail'))[:40]})")
        return 0

    # 同一天只告警一次，避免重复轰炸
    state_path = LOG_DIR / "watchdog_state.json"
    today = now.strftime("%Y-%m-%d")
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        state = {}
    if state.get("last_alert_date") == today and not args.dry_run:
        print(f"SKIP already alerted on {today}")
        return 0

    last_any = entries[-1] if entries else None
    if last_ok_ts:
        head = (f"{window_desc}还没有成功推送过岗位。最近一次成功是 "
                f"{last_ok_ts:%Y-%m-%d %H:%M}，之后的任务没走完。")
    else:
        head = f"{window_desc}没有任何成功记录，日志里从来没发出去过一封。"

    lines = [
        head,
        "",
        "计划任务可能照常在触发，但「采集 → 筛选 → 发信」链路没有走完，所以你没收到日报。"
        "常见原因：",
        "  1) 计划任务指向的 Python 缺少 DrissionPage（DSH 升级重建运行时会导致）；",
        "  2) 登录态失效 —— 需要运行 python login.py 重新扫码；",
        "  3) BOSS 风控熔断（code=37） —— 等 30–60 分钟或换网络后重跑。",
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

    run_label = f"{now:%H:%M} 看门狗" + (f"（{PROFILE}）" if PROFILE else "")
    import mailer  # 只依赖标准库，解释器再破也能发出去
    result = mailer.send_alert(message, cfg, run_label, dry_run=args.dry_run)

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    with (LOG_DIR / "watchdog.log").open("a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": now.isoformat(timespec="seconds"),
                            "last_ok": last_ok_ts.isoformat(timespec="seconds") if last_ok_ts else None,
                            "result": result}, ensure_ascii=False) + "\n")
    if not args.dry_run:
        state_path.write_text(json.dumps({"last_alert_date": today,
                                          "last_ok": last_ok_ts.isoformat(timespec="seconds") if last_ok_ts else None},
                                         ensure_ascii=False), encoding="utf-8")
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
