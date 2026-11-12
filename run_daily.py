# -*- coding: utf-8 -*-
"""每日编排：BOSS 直聘采集 → 简历匹配打分 → 去重 → 发邮件。

只做筛选，不做投递：脚本只读搜索列表和岗位字段，不会点「立即沟通」「投递简历」。

用法：
    python run_daily.py --slot 12:30            # 正常跑（会真发邮件）
    python run_daily.py --slot 12:30 --dry-run  # 不发信，写 .eml 预览
    python run_daily.py --demo --dry-run        # 用假数据验证链路
    python run_daily.py --slot 12:30 --keep-open  # 跑完不关自动化浏览器（排障用）
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent


def _cli_value(flag: str) -> str:
    """在 import 依赖之前先读 --profile。

    match_engine / store 都是在 import 阶段读配置和数据库路径的，
    所以画像必须在那之前确定。
    """
    argv = sys.argv[1:]
    for i, arg in enumerate(argv):
        if arg == flag and i + 1 < len(argv):
            return argv[i + 1]
        if arg.startswith(flag + "="):
            return arg.split("=", 1)[1]
    return ""


import settings  # noqa: E402  （只依赖标准库，可以安全地先导入）

PROFILE = (_cli_value("--profile") or os.environ.get("JOB_PROFILE", "")).strip()
settings.apply_profile_env(PROFILE)

# 依赖缺失必须被自己捕获：collect 在 import 阶段就 raise SystemExit，
# 而计划任务用 pythonw 启动（没有控制台），异常只会变成 Task Scheduler 里的
# 退出码 1 —— 没有日志、没有告警邮件，看起来就像任务根本没跑。
# 2026-11-07..09 的「连续三天零邮件」就是这么来的。
_BOOT_ERROR: BaseException | None = None
try:
    import collect
    import links
    import match_engine
    import mailer
    import store

    CFG = match_engine.CFG
except BaseException as _exc:  # SystemExit 不是 Exception，必须抓 BaseException
    _BOOT_ERROR = _exc
    CFG = {}


def keywords_for_today() -> list[str]:
    kws = list(CFG["keywords"]["core"])
    extra = CFG["keywords"]["extra"]
    if extra:
        kws.append(extra[datetime.now().timetuple().tm_yday % len(extra)])
    return kws


def normalize(raw: dict) -> dict:
    job = dict(raw)
    job["job_id"] = store.make_job_id(
        job.get("source", ""), job.get("raw_id", ""), job.get("company", ""),
        job.get("title", ""), job.get("city", ""),
    )
    # 邮件里用官方 H5 页 + 手机网页 + APP 深链，不用桌面站链接
    links.apply(job, CFG.get("mail", {}).get("smartlink_base", ""))
    return job


def gather(page, slot: str = "12:30") -> tuple[list[dict], dict]:
    """只从 BOSS 采集；登录失效 / 风控的判定与退避都在 collect 里。"""
    search_cfg = CFG.get("search", {})
    jobs, diag = collect.collect_keywords(
        page,
        keywords_for_today(),
        max_pages=int(search_cfg.get("max_pages", 1)),
        delay=tuple(search_cfg.get("keyword_delay_seconds", [3.5, 7.0])),
        block_retries=int(search_cfg.get("block_retries", 1)),
    )

    jobs = [j for j in jobs if j.get("title") and (j.get("company") or j.get("salary_text"))]
    # 同一次运行内先去重（不同关键词可能命中同一岗位）
    seen, uniq = set(), []
    for j in jobs:
        key = (j.get("company"), j.get("title"), j.get("city"))
        if key in seen:
            continue
        seen.add(key)
        uniq.append(normalize(j))
    diag["boss"] = len(uniq)
    return uniq, diag


DEMO_JOBS = [
    {"source": "boss", "raw_id": "bd152cb9de9131791HB43d6_EFtQ", "title": "金融行业解决方案总监",
     "company": "阿里云", "salary_text": "60-90K·16薪", "city": "北京·朝阳",
     "tags": ["银行", "保险", "大模型"], "desc": "金融行业 AI 解决方案",
     "scale_text": "10000人以上",
     "url": "https://www.zhipin.com/job_detail/bd152cb9de9131791HB43d6_EFtQ.html"},
    {"source": "boss", "raw_id": "a1b2c3d4e5f6g7h8i9j0k1", "title": "财富管理产品专家",
     "company": "蚂蚁集团", "salary_text": "50-80K·15薪", "city": "杭州",
     "tags": ["财富管理", "智能投顾", "AI"], "desc": "财富管理产品设计",
     "scale_text": "10000人以上",
     "url": "https://www.zhipin.com/job_detail/a1b2c3d4e5f6g7h8i9j0k1.html"},
    {"source": "boss", "raw_id": "z9y8x7w6v5u4t3s2r1q0p9", "title": "银行数字化转型咨询总监",
     "company": "埃森哲", "salary_text": "45-65K·14薪", "city": "上海",
     "tags": ["银行", "数字化转型", "咨询"], "desc": "银行数字化咨询",
     "scale_text": "10000人以上",
     "url": "https://www.zhipin.com/job_detail/z9y8x7w6v5u4t3s2r1q0p9.html"},
    {"source": "boss", "raw_id": "d4d4d4d4d4d4d4d4d4d4d4", "title": "理财经理",
     "company": "某财富管理有限公司", "salary_text": "15-30K", "city": "北京",
     "tags": [], "desc": "", "url": "https://www.zhipin.com/job_detail/d4d4d4d4d4d4d4d4d4d4d4.html"},
    {"source": "boss", "raw_id": "d5d5d5d5d5d5d5d5d5d5d5", "title": "保险代理人",
     "company": "中国人寿财产保险股份有限公司", "salary_text": "10-20K", "city": "北京",
     "tags": [], "desc": "", "url": "https://www.zhipin.com/job_detail/d5d5d5d5d5d5d5d5d5d5d5.html"},
]

# 法务合规画像的 demo 数据：用来单独验证该画像的打分链路
# （1 条对口、1 条城市不符、1 条律所、1 条层级过低、1 条薪资不达标）
DEMO_JOBS_LEGAL = [
    {"source": "boss", "raw_id": "lw0001aaaaaaaaaaaaaaaa", "title": "法务总监（消费金融）",
     "company": "马上消费金融股份有限公司", "salary_text": "35-55K·15薪", "city": "北京·朝阳",
     "tags": ["合规", "不良资产", "诉讼"], "desc": "消费金融",
     "scale_text": "1000-9999人",
     "url": "https://www.zhipin.com/job_detail/lw0001aaaaaaaaaaaaaaaa.html"},
    {"source": "boss", "raw_id": "lw0002aaaaaaaaaaaaaaaa", "title": "合规负责人",
     "company": "某支付科技有限公司", "salary_text": "30-45K·14薪", "city": "上海·浦东新区",
     "tags": ["反洗钱", "支付", "合规"], "desc": "第三方支付",
     "scale_text": "500-999人",
     "url": "https://www.zhipin.com/job_detail/lw0002aaaaaaaaaaaaaaaa.html"},
    {"source": "boss", "raw_id": "lw0003aaaaaaaaaaaaaaaa", "title": "法务经理",
     "company": "某某银行股份有限公司", "salary_text": "25-35K·13薪", "city": "广州",
     "tags": ["银行", "合规"], "desc": "银行",
     "url": "https://www.zhipin.com/job_detail/lw0003aaaaaaaaaaaaaaaa.html"},
    {"source": "boss", "raw_id": "lw0004aaaaaaaaaaaaaaaa", "title": "授薪律师（金融诉讼）",
     "company": "北京市某某律师事务所", "salary_text": "15-25K", "city": "北京",
     "tags": [], "desc": "", "url": "https://www.zhipin.com/job_detail/lw0004aaaaaaaaaaaaaaaa.html"},
    {"source": "boss", "raw_id": "lw0005aaaaaaaaaaaaaaaa", "title": "法务专员",
     "company": "某金融科技有限公司", "salary_text": "8-12K", "city": "北京",
     "tags": [], "desc": "", "url": "https://www.zhipin.com/job_detail/lw0005aaaaaaaaaaaaaaaa.html"},
]


def log(payload: dict) -> None:
    from pathlib import Path

    d = Path(os.environ.get("JOB_LOG_DIR", Path(__file__).resolve().parent / "logs"))
    d.mkdir(parents=True, exist_ok=True)
    with (d / "daily.log").open("a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": datetime.now().isoformat(timespec="seconds"),
                            "profile": PROFILE or "default", **payload},
                           ensure_ascii=False) + "\n")


def _alert_message(diag: dict) -> str:
    last = collect.last_login_ok()
    suffix = f"（上次搜索成功：{last}）" if last else ""

    if diag.get("login"):
        return (
            "BOSS 登录态已失效：搜索页被重定向到登录页，本轮已立即停止，没有继续请求接口。"
            "请在电脑上运行 python login.py 扫码，完成后下一次运行会自动恢复" + suffix + "。"
        )
    if diag.get("verify"):
        return (
            "BOSS 环境校验页（security.html，用于刷新 __zp_stoken__）没有自动完成，"
            "本轮已停止。先在电脑上手动打开 BOSS 搜索页等它自动跳回；仍不行再运行 "
            "python login.py 打开窗口看一眼。这不是账号密码或扫码问题。"
        )
    if diag.get("circuit") or diag.get("blocked"):
        codes = sorted({str(b.get("code")) for b in diag.get("blocked") or []})
        code_s = "/".join(codes) or "35/37"
        return (
            f"BOSS 搜索接口触发风控（code={code_s}，环境/IP 异常），本轮已按设计熔断，"
            "没有继续硬打。这不是登录失效，不用扫码；等 30–60 分钟或切换网络出口后重试即可。"
        )
    return (
        "本次没有采集到岗位：可能是当天关键词无结果，或 BOSS 页面结构变化。"
        "可以先运行 python login.py --check 做一次体检。"
    )


def _notes(diag: dict, fresh: list[dict]) -> list[str]:
    notes: list[str] = []
    if diag.get("login"):
        notes.append(
            "采集过程中 BOSS 登录态失效，本次结果不完整。请运行 python login.py 扫码后重跑。"
        )
    if diag.get("verify"):
        notes.append(
            "BOSS 环境校验页（security.html）未自动完成，本次结果不完整。"
            "先手动打开 BOSS 搜索页等它跳回，或运行 python login.py 看一眼。"
        )
    if diag.get("circuit"):
        notes.append(
            "本轮有多个关键词被 BOSS 风控跳过，结果可能不完整；脚本已自动熔断，未做任何投递。"
        )
    elif diag.get("blocked"):
        notes.append(
            f"本轮有 {len(diag['blocked'])} 个关键词被 BOSS 风控跳过（code="
            + "/".join(sorted({str(b.get('code')) for b in diag['blocked']}))
            + "），结果可能不完整。"
        )
    if not notes and fresh:
        notes.append("仅从 BOSS 直聘筛选，未做任何投递或沟通动作。")
    return notes


def run(args) -> None:
    page = None
    # 时段标签按小时判断，避免改时间后（如 12:30）被误标成"晚间"
    hour = int(args.slot.split(":")[0])
    label = "午间" if hour < 15 else "晚间"
    run_label = f"{label} {args.slot}"

    # 只在工作日跑：计划任务本身已经是周一到周五，但 Windows 的
    # StartWhenAvailable 会把「周五没跑成」的那次补到周六开机时执行，
    # 所以代码里再挡一道（--force 可手动绕过）。
    search_cfg = CFG.get("search", {}) if CFG else {}
    if (not args.demo and not args.force
            and search_cfg.get("weekdays_only", True)
            and datetime.now().weekday() >= 5):
        log({"slot": args.slot, "collected": 0, "scored_pass": 0, "fresh": 0,
             "mail": "SKIP:weekend", "diag": {"reason": "周末不推送"}})
        return

    try:
        if args.demo:
            demo = DEMO_JOBS_LEGAL if PROFILE == "legal" else DEMO_JOBS
            raw_jobs = [normalize(j) for j in demo]
            diag = {"boss": 0, "keywords": [], "blocked": [], "errors": [],
                    "login": None, "verify": False, "circuit": False, "demo": True}
        else:
            try:
                page = collect.attach()
            except Exception as e:
                # 启动阶段失败也必须告警：12:30 那次就是静默崩在这里，用户没收到任何邮件
                log({"fatal_stage": "attach", "slot": args.slot, "error": str(e)})
                try:
                    mailer.send_alert(
                        "本轮任务在启动阶段失败：无法连接自动化 Chrome（"
                        + str(e) + "）。脚本已自动重试仍失败，通常是 9333 端口残留了"
                        "旧 Chrome 或调试端点未就绪。请运行 python login.py --check 体检；"
                        "仍不行就关掉所有自动化 Chrome 窗口后重跑。",
                        CFG, run_label, dry_run=args.dry_run,
                    )
                except Exception as mail_exc:
                    log({"fatal_mail": str(mail_exc), "slot": args.slot})
                raise
            raw_jobs, diag = gather(page, args.slot)

        scored = match_engine.rank(raw_jobs)
        limit = CFG["search"]["daily_count"]
        # demo 只用假数据验证链路，不应被生产去重库挡掉
        fresh = scored[:limit] if args.demo else store.filter_new(scored)[:limit]

        # 一条都没采到时，发告警邮件而不是空列表；
        # 采集正常、但当天没有新岗位时默认不发信（只记 SKIP），避免空邮件刷屏。
        skip_empty = bool(search_cfg.get("skip_when_no_new", True))
        if not raw_jobs and not args.demo:
            result = mailer.send_alert(_alert_message(diag), CFG, run_label,
                                       dry_run=args.dry_run)
        elif skip_empty and not fresh:
            result = "SKIP:no-new"
        else:
            result = mailer.send(fresh, CFG, run_label, dry_run=args.dry_run,
                                 notes=_notes(diag, fresh))

        if not args.dry_run and result.startswith("SENT") and fresh:
            store.remember(fresh, run_tag=f"{datetime.now():%Y-%m-%d} {args.slot}")

        payload = {
            "slot": args.slot, "collected": len(raw_jobs), "scored_pass": len(scored),
            "fresh": len(fresh), "mail": result, "diag": diag,
            "top": [{"t": j["title"], "c": j["company"], "s": j["score"]} for j in fresh[:10]],
        }
        log(payload)
        try:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        except Exception:
            pass  # pythonw 下 sys.stdout 为 None，忽略即可
    finally:
        if page is not None and not args.keep_open:
            collect.close_browser(page)


def _report_boot_failure(exc: BaseException, slot: str, dry_run: bool = False) -> None:
    """启动阶段依赖缺失：写日志 + 发告警，别再静默失败。"""
    log({"fatal_stage": "import", "slot": slot, "error": str(exc), "python": sys.executable})
    text = (
        "本轮在启动阶段就失败了：采集依赖没有导入成功，所以没有采集、也没有发信。\n\n"
        f"解释器：{sys.executable}\n"
        f"错误：{exc}\n\n"
        "最常见原因：DSH 桌面端升级时重建了它自己的 Python 运行时，把之前 pip 装进去的 "
        "DrissionPage 覆盖掉了，而计划任务恰好指向那个解释器。这种失败在 pythonw 下完全静默"
        "（没有控制台、没有日志），只能靠这封邮件发现。\n\n"
        "修复：在项目目录运行\n"
        "  powershell -ExecutionPolicy Bypass -File .\\install_task.ps1\n"
        "它会把任务重指到一个真的能 import DrissionPage 的解释器；"
        "然后 python login.py --check 看登录态，再 python run_daily.py --slot 12:30 手动补跑。"
    )
    try:
        try:
            cfg = CFG or settings.load_config()
        except BaseException:
            # 连画像配置都读不出来（比如 --profile 名字写错），退回基础配置发告警，
            # 至少让「任务挂了」这件事从邮箱里看得见。
            cfg = json.loads((BASE / "config.json").read_text(encoding="utf-8-sig"))
        import mailer as _mailer

        _mailer.send_alert(text, cfg, f"{slot} 启动失败", dry_run=dry_run)
    except Exception as mail_exc:
        log({"fatal_mail": str(mail_exc), "slot": slot})


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--slot", default="11:30")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--demo", action="store_true")
    ap.add_argument("--keep-open", action="store_true",
                    help="跑完不关闭自动化浏览器（排障用）")
    ap.add_argument("--force", action="store_true",
                    help="周末也强制跑一次（默认周末直接跳过）")
    ap.add_argument("--profile", default=PROFILE,
                    help="求职画像名（profiles/<name>/config.json），不传则用基础 config.json")
    args = ap.parse_args()

    if _BOOT_ERROR is not None:
        _report_boot_failure(_BOOT_ERROR, args.slot, args.dry_run)
        raise SystemExit(2)

    try:
        run(args)
    except Exception as e:
        import traceback

        log({"fatal": str(e), "trace": traceback.format_exc()[-1500:], "slot": args.slot})
        raise


if __name__ == "__main__":
    main()
