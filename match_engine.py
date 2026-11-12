# -*- coding: utf-8 -*-
"""岗位匹配引擎：硬性排除 + 六维打分（岗位族/业务域/AI属性/层级/薪资/公司规模）。

设计原则：简历匹配度是最高权重（业务域 30 + 岗位族 26 = 56 分），
公司规模与薪资只做加分项，避免"大厂小岗"挤掉"对口岗位"。
"""
from __future__ import annotations

import re
from pathlib import Path

import settings

BASE = Path(__file__).resolve().parent
# 基础画像 + 可选画像覆盖（profiles/<name>/config.json），见 settings.py
CFG = settings.load_config()

FIN_ENTITY_MARKERS = ["银行", "证券", "保险", "信托", "基金", "期货", "资产管理", "金融租赁", "消费金融", "财务公司", "理财"]
TECH_MARKERS = ["科技", "数科", "信息", "软件", "云", "智能", "数据", "数字", "网络", "系统", "技术", "金信", "计算"]

SALARY_RE = re.compile(r"(\d+(?:\.\d+)?)\s*-\s*(\d+(?:\.\d+)?)\s*[kK]")
SALARY_SINGLE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*[kK]")
SALARY_WAN_RE = re.compile(r"(\d+(?:\.\d+)?)\s*-\s*(\d+(?:\.\d+)?)\s*万")


def parse_salary(text: str) -> tuple[float | None, float | None]:
    """返回 (月薪下限k, 月薪上限k)。兼容 '60-85k·15薪' '50-80K' '30-50万/年'。"""
    if not text:
        return None, None
    t = text.replace(" ", "")
    m = SALARY_RE.search(t)
    if m:
        return float(m.group(1)), float(m.group(2))
    m = SALARY_WAN_RE.search(t)
    if m:
        low, high = float(m.group(1)), float(m.group(2))
        return round(low * 10 / 12, 1), round(high * 10 / 12, 1)
    m = SALARY_SINGLE_RE.search(t)
    if m:
        v = float(m.group(1))
        return v, v
    return None, None


def infer_scale(company: str, scale_text: str = "") -> tuple[int, str]:
    """推断公司规模档位，返回 (档位分, 档位标签)。档位分越高越大。"""
    s = (scale_text or "").replace(" ", "")
    if s:
        nums = [int(x) for x in re.findall(r"(\d{3,6})", s.replace(",", ""))]
        top = max(nums) if nums else 0
        if top >= 10000:
            return 5, "10000人以上"
        if top >= 1000:
            return 4, "1000-9999人"
        if top >= 500:
            return 3, "500-999人"
        if top > 0:
            return 2, "500人以下"
    if "上市" in s:
        return 4, "已上市"
    if "轮" in s or "未融资" in s or "天使" in s:
        return 2, s
    if "上市" in company:
        return 4, "已上市"
    hint = CFG["company_scale_hint"]
    for name in hint["10000+"]:
        if name in company:
            return 5, "10000人以上"
    for name in hint["1000-9999"]:
        if name in company:
            return 4, "1000-9999人"
    return 3, "未知"


def _hit_terms(text: str, terms: list[str]) -> list[str]:
    low = text.lower()
    return [t for t in terms if t.lower() in low]


def hard_exclude(job: dict) -> str | None:
    title = job.get("title", "")
    company = job.get("company", "")
    salary_text = job.get("salary_text", "")
    ex = CFG["exclude"]

    # 0) 城市白名单：BOSS 搜索是「全国」口径（city=100010000），
    #    画像里配了 city_allow 就只留这些城市，否则全国岗位会挤进日报。
    allow = CFG.get("search", {}).get("city_allow") or []
    city = job.get("city") or ""
    if allow and not any(c and c in city for c in allow):
        return f"城市不在范围（{city or '未标注'}）"

    # 0.5) 画像自定义岗位名黑名单：法务画像用来挡实习律师 / 律师助理 / 法务专员，
    #      这些人岗对 16 年经验的候选人属于降级，进日报只算噪音。
    for kw in ex.get("title_block", []):
        if kw in title:
            return f"岗位层级不符（{kw}）"

    # 1) 销售岗（售前/解决方案不算销售）
    if not re.search(r"售前|解决方案|方案|架构|产品|咨询", title):
        for kw in ex["title_sales"]:
            if kw in title:
                return f"销售类岗位（{kw}）"

    # 2) 保险公司主体（科技子公司除外）——法务画像要投保险公司法务，可关掉
    if ex.get("exclude_insurance_entity", True):
        if _hit_terms(company, ex["company_insurance_entity"]) and not _hit_terms(company, ex["insurance_tech_whitelist"]):
            return "保险公司主体"

    # 3) 第三方财富管理 / 投资咨询
    if ex.get("exclude_wealth_company", True):
        for kw in ex["company_wealth"]:
            if kw in company:
                return f"第三方财富/投资咨询类（{kw}）"

    # 4) 传统金融机构（非科技子公司）——法务画像的目标就是持牌机构，可关掉
    if ex.get("exclude_finance_entity", True):
        if _hit_terms(company, FIN_ENTITY_MARKERS) and not _hit_terms(company, TECH_MARKERS):
            return "传统金融机构主体"

    # 5) 律所主体：法务画像只要甲方 in-house
    for kw in ex.get("company_block", []):
        if kw in company:
            return f"主体不符（{kw}）"

    # 6) 薪资门槛
    low, high = parse_salary(salary_text)
    floor = CFG["search"]["min_salary_k"]
    if high is not None:
        if ex.get("strict_salary_floor") and high < floor:
            return f"薪资上限 {high:g}K 低于 {floor}K 下限"
        if high < floor * 0.7:
            return f"薪资明显低于{floor}K下限"

    return None


def score_job(job: dict) -> dict:
    """返回 {'score': float, 'parts': {...}, 'reasons': [...], 'excluded': str|None}"""
    title = job.get("title", "")
    company = job.get("company", "")
    blob = " ".join([title, company, " ".join(job.get("tags", []) or []), job.get("desc", "") or ""])
    sc = CFG["scoring"]
    parts: dict[str, float] = {}
    reasons: list[str] = []

    reason = hard_exclude(job)
    if reason:
        return {"score": 0.0, "parts": {}, "reasons": [f"排除：{reason}"], "excluded": reason}

    # --- 岗位族 ---
    role_score = 0.0
    best_family = None
    for fam, spec in CFG["role_families"].items():
        hits = _hit_terms(title, spec["titles"])
        if hits:
            s = sc["role_max"] * spec["weight"]
            if s > role_score:
                role_score = s
                best_family = (spec.get("label") or fam, hits[0])
    parts["role"] = round(role_score, 1)
    if best_family:
        reasons.append(f"岗位族：{best_family[0]}（命中「{best_family[1]}」）")

    # --- 业务域（最高权重：简历匹配的根本）---
    dom = 0.0
    dom_hits: list[str] = []
    for t in CFG["domain_terms"]["high"]:
        if t.lower() in blob.lower():
            dom += 6
            dom_hits.append(t)
    for t in CFG["domain_terms"]["mid"]:
        if t.lower() in blob.lower():
            dom += 4
            dom_hits.append(t)
    for t in CFG["domain_terms"]["low"]:
        if t.lower() in blob.lower():
            dom += 2
            dom_hits.append(t)
    if title and any(t.lower() in title.lower() for t in CFG["domain_terms"]["high"]):
        dom += 4

    # 白名单公司：百融、同盾、恒生、蚂蚁这类公司名里没有"金融"字样，
    # 但业务本身就是金融科技，不加这条会被当成泛行业扣分（实测误杀过百融云创）。
    fin_hit = next((c for c in CFG.get("fintech_companies", []) if c in company), None)
    if fin_hit:
        dom += 8
        dom_hits.append(f"{fin_hit}(金融科技)")

    # 非金融垂直行业扣分：避免"医学售前""储能方案"被公司白名单抬成高分
    off_hits = _hit_terms(title + " " + (job.get("industry") or ""), CFG.get("off_domain_terms", []))
    if off_hits:
        dom -= 3 * len(off_hits)
        reasons.append("非金融垂直：" + "、".join(off_hits[:3]))

    parts["domain"] = round(max(min(dom, sc["domain_max"]), 0.0), 1)
    if dom_hits:
        reasons.append("业务域：" + "、".join(dict.fromkeys(dom_hits))[:40])

    # --- AI / 科技属性 ---
    ai_hits = _hit_terms(blob, CFG["ai_tech_terms"])
    parts["ai"] = round(min(len(ai_hits) * 4, sc["ai_max"]), 1)
    if ai_hits:
        reasons.append("AI/科技：" + "、".join(ai_hits[:4]))

    # --- 层级 ---
    sen = 6.0
    if _hit_terms(title, CFG["seniority"]["top"]):
        sen = sc["seniority_max"]
    elif _hit_terms(title, CFG["seniority"]["high"]):
        sen = sc["seniority_max"] - 2
    elif _hit_terms(title, CFG["seniority"]["junior"]):
        sen = 1.0
    parts["seniority"] = sen

    # --- 薪资 ---
    low, high = parse_salary(job.get("salary_text", ""))
    floor = CFG["search"]["min_salary_k"]
    if high is None:
        parts["salary"] = 5.0
        reasons.append("薪资面议/未标注")
    elif high >= floor:
        parts["salary"] = sc["salary_max"]
    elif high >= floor * 0.9:
        parts["salary"] = sc["salary_max"] * 0.6
    else:
        parts["salary"] = sc["salary_max"] * 0.3

    # --- 公司规模 ---
    tier, tier_label = infer_scale(company, job.get("scale_text", ""))
    parts["scale"] = round(sc["scale_max"] * (tier / 5), 1)
    job["scale_tier"] = tier
    job["scale_label"] = tier_label

    total = round(sum(parts.values()), 1)

    # 业务域相关度门槛：纯技术岗（与金融履历无关）降权，避免"大厂通用架构师"挤掉对口岗位
    if parts["domain"] < sc.get("domain_floor", 6):
        total = round(total * 0.75, 1)
        reasons.append("业务域相关度偏低（降权）")

    return {"score": total, "parts": parts, "reasons": reasons, "excluded": None}


def rank(jobs: list[dict], limit: int | None = None) -> list[dict]:
    """打分 + 排序：匹配分 → 公司规模 → 发布时间。"""
    scored = []
    for j in jobs:
        r = score_job(j)
        j.update(r)
        if not r["excluded"] and r["score"] >= CFG["scoring"]["min_score_to_push"]:
            scored.append(j)
    scored.sort(key=lambda x: (-x["score"], -x.get("scale_tier", 0), -(x.get("publish_ts") or 0)))
    return scored[:limit] if limit else scored


if __name__ == "__main__":
    demo = [
        {"title": "金融行业解决方案总监", "company": "阿里云", "salary_text": "60-90K·16薪", "city": "北京",
         "tags": ["银行", "保险", "AI"], "desc": "负责金融行业大模型解决方案"},
        {"title": "理财经理", "company": "某财富管理有限公司", "salary_text": "20-40K", "city": "北京", "tags": [], "desc": ""},
        {"title": "保险代理人", "company": "中国人寿财产保险股份有限公司", "salary_text": "10-30K", "city": "北京", "tags": [], "desc": ""},
        {"title": "FDE 前沿部署工程师", "company": "第四范式", "salary_text": "50-80K·14薪", "city": "上海",
         "tags": ["金融", "大模型"], "desc": "驻场交付"},
        {"title": "私人银行产品经理", "company": "招商银行", "salary_text": "40-60K", "city": "深圳", "tags": ["私行"], "desc": ""},
        {"title": "金融科技产品总监", "company": "建信金科", "salary_text": "55-75K·15薪", "city": "北京",
         "tags": ["银行", "数字化转型"], "desc": ""},
    ]
    for j in rank(demo):
        print(f"{j['score']:>5} | {j['title']} @ {j['company']} | {j['salary_text']} | {j['scale_label']} | {j['reasons'][:2]}")
