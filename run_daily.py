# -*- coding: utf-8 -*-
"""每日编排：采集 → 匹配打分 → 去重 → 发邮件。

用法：
    python run_daily.py --slot 11:30            # 正常跑（会真发邮件）
    python run_daily.py --slot 11:30 --dry-run  # 不发信，写 .eml 预览
    python run_daily.py --demo --dry-run        # 用假数据验证链路
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime

import collect
import match_engine
import mailer
import store

CFG = match_engine.CFG


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
    return job


def gather(page) -> tuple[list[dict], dict]:
    """BOSS 按关键词搜索；猎聘走个性化推荐流（关键词搜索页被风控拦截）。"""
    jobs: list[dict] = []
    diag = {"boss": 0, "liepin": 0, "errors": []}

    for kw in keywords_for_today():
        try:
            b = collect.boss_search(page, kw)
            diag["boss"] += len(b)
            jobs.extend(b)
        except Exception as e:
            diag["errors"].append(f"boss:{kw}:{e}")

    try:
        l = collect.liepin_feed(page)
        diag["liepin"] = len(l)
        jobs.extend(l)
    except Exception as e:
        diag["errors"].append(f"liepin_feed:{e}")
    jobs = [j for j in jobs if j.get("title") and (j.get("company") or j.get("salary_text"))]
    # 同一次运行内先去重（不同关键词可能命中同一岗位）
    seen, uniq = set(), []
    for j in jobs:
        key = (j.get("company"), j.get("title"), j.get("city"))
        if key in seen:
            continue
        seen.add(key)
        uniq.append(normalize(j))
    return uniq, diag


DEMO_JOBS = [
    {"source": "demo", "raw_id": "d1", "title": "金融行业解决方案总监", "company": "阿里云", "salary_text": "60-90K·16薪",
     "city": "北京·朝阳", "tags": ["银行", "保险", "大模型"], "desc": "金融行业 AI 解决方案", "url": "https://example.com/1"},
    {"source": "demo", "raw_id": "d2", "title": "银行数字化转型咨询总监", "company": "埃森哲", "salary_text": "50-70K·14薪",
     "city": "上海", "tags": ["银行", "数字化"], "desc": "", "url": "https://example.com/2"},
    {"source": "demo", "raw_id": "d3", "title": "财富管理产品专家", "company": "蚂蚁集团", "salary_text": "55-85K·16薪",
     "city": "杭州", "tags": ["财富管理", "智能投顾", "AI"], "desc": "", "url": "https://example.com/3"},
    {"source": "demo", "raw_id": "d4", "title": "理财经理", "company": "某财富管理有限公司", "salary_text": "15-30K",
     "city": "大连", "tags": [], "desc": "", "url": "https://example.com/4"},
    {"source": "demo", "raw_id": "d5", "title": "保险代理人", "company": "中国人寿财产保险股份有限公司", "salary_text": "10-20K",
     "city": "大连", "tags": [], "desc": "", "url": "https://example.com/5"},
]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--slot", default="11:30")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--demo", action="store_true")
    args = ap.parse_args()

    try:
        run(args)
    except Exception as e:
        import traceback
        log({"fatal": str(e), "trace": traceback.format_exc()[-1500:], "slot": args.slot})
        raise


def log(payload: dict) -> None:
    from pathlib import Path
    d = Path(__file__).resolve().parent / "logs"
    d.mkdir(exist_ok=True)
    with (d / "daily.log").open("a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": datetime.now().isoformat(timespec="seconds"), **payload}, ensure_ascii=False) + "\n")


def run(args) -> None:
    if args.demo:
        raw_jobs, diag = [normalize(j) for j in DEMO_JOBS], {"boss": 0, "liepin": 0, "errors": [], "demo": True}
    else:
        page = collect.attach()
        raw_jobs, diag = gather(page)

    scored = match_engine.rank(raw_jobs)
    limit = CFG["search"]["daily_count"]
    fresh = store.filter_new(scored)[:limit]

    label = "午间" if args.slot.startswith("11") else "晚间"
    run_label = f"{label} {args.slot}"

    # 一条都没采到时，发告警邮件而不是空列表
    if not raw_jobs and not args.demo:
        result = mailer.send_alert(
            "本次采集到 0 条岗位，通常是 BOSS/猎聘 登录态失效或触发安全验证。"
            "请打开自动化浏览器窗口（已登录的那个）重新扫码登录后，我下次运行会自动恢复。",
            CFG, run_label, dry_run=args.dry_run,
        )
    else:
        result = mailer.send(fresh, CFG, run_label, dry_run=args.dry_run)

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


if __name__ == "__main__":
    main()
