# -*- coding: utf-8 -*-
"""采集器：附着到已登录的自动化 Chrome（端口 9333），抓取 BOSS / 猎聘 岗位。

BOSS：站内 joblist JSON 接口（薪资明文、字段全）。
猎聘：关键词搜索页被平台风控拦截（返回空列表），改用其个性化推荐流
      （按账号的求职期望推荐，实测稳定返回 40+ 条），并兼容搜索页可用的情况。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import time
from pathlib import Path
from urllib.parse import quote

from DrissionPage import ChromiumPage, ChromiumOptions

BASE = Path(__file__).resolve().parent
PORT = int(os.environ.get("CHROME_DEBUG_PORT", "9333"))
PROFILE = str(Path(os.environ.get("CHROME_PROFILE_DIR", BASE / "chrome-profile")).resolve())
BOSS_CITY_ALL = "100010000"  # 全国

_CHROME_CANDIDATES = [
    os.environ.get("CHROME_PATH", ""),
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    "/usr/bin/google-chrome",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
]


def find_chrome() -> str:
    """定位本机 Chrome；可用环境变量 CHROME_PATH 指定。"""
    for p in _CHROME_CANDIDATES:
        if p and Path(p).exists():
            return p
    for name in ("chrome", "google-chrome", "chromium"):
        found = shutil.which(name)
        if found:
            return found
    raise RuntimeError("未找到 Chrome，请设置环境变量 CHROME_PATH 指向可执行文件")


def _debug_alive() -> bool:
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/version", timeout=3) as r:
            return r.status == 200
    except Exception:
        return False


def ensure_browser(timeout: int = 25) -> None:
    """定时任务里 Chrome 可能没开，自己拉起来（独立配置，不干扰日常浏览器）。"""
    if _debug_alive():
        return
    import subprocess
    args = [
        f"--user-data-dir={PROFILE}",
        f"--remote-debugging-port={PORT}",
        "--no-first-run", "--no-default-browser-check",
        "--disable-blink-features=AutomationControlled",
        "--window-size=1440,900", "--window-position=-2400,0",
        "--lang=zh-CN", "about:blank",
    ]
    subprocess.Popen([find_chrome(), *args], close_fds=True)
    for _ in range(timeout * 2):
        time.sleep(0.5)
        if _debug_alive():
            return
    raise RuntimeError("自动化 Chrome 启动失败（端口 9333 无响应）")


def attach() -> ChromiumPage:
    ensure_browser()
    co = ChromiumOptions().set_local_port(PORT)
    page = ChromiumPage(co)
    page.set.load_mode.eager()
    page.set.timeouts(base=15, page_load=25, script=15)
    return page


def _j(text: str) -> dict | None:
    """Chrome 会把 JSON 渲染成 <pre> 文本，这里剥掉外层再解析。"""
    import html as _html
    if not text:
        return None
    m = re.search(r"<pre[^>]*>(.*?)</pre>", text, re.S)
    raw = _html.unescape(m.group(1)) if m else text
    try:
        return json.loads(raw)
    except Exception:
        return None


# ---------------- BOSS ----------------
def boss_search(page: ChromiumPage, keyword: str, page_no: int = 1) -> list[dict]:
    url = (
        "https://www.zhipin.com/wapi/zpgeek/search/joblist.json"
        f"?scene=1&query={quote(keyword)}&city={BOSS_CITY_ALL}&page={page_no}&pageSize=30"
    )
    try:
        page.get(url)
        # eager 模式下 body 可能还没写入，重试几次再解析
        for _ in range(6):
            time.sleep(0.8)
            data = _j(page.html)
            if data and data.get("code") == 0:
                lst = (data.get("zpData") or {}).get("jobList") or []
                if lst:
                    return [_boss_from_json(x) for x in lst]
                break
    except Exception:
        pass
    return _boss_from_dom(page, keyword)


def _boss_from_json(x: dict) -> dict:
    # jobLabels 是"经验/学历"，welfareList 是"五险一金"等福利——
    # 两者都不能进业务域匹配，否则会被"补充医疗保险"误判成保险行业。
    skills = x.get("skills") or []
    industry = x.get("brandIndustry") or ""
    return {
        "source": "boss",
        "raw_id": str(x.get("encryptJobId") or x.get("jobId") or ""),
        "title": x.get("jobName", ""),
        "company": x.get("brandName", ""),
        "salary_text": x.get("salaryDesc", ""),
        "city": "".join(filter(None, [x.get("cityName", ""), x.get("areaDistrict", ""), x.get("businessDistrict", "")])),
        "tags": skills,
        "desc": industry,
        "industry": industry,
        "url": f"https://www.zhipin.com/job_detail/{x.get('encryptJobId','')}.html" if x.get("encryptJobId") else "",
        "publish_ts": None,
        "scale_text": x.get("brandScaleName", "") or "",
    }


def _boss_from_dom(page: ChromiumPage, keyword: str = "") -> list[dict]:
    if keyword:
        page.get(f"https://www.zhipin.com/web/geek/job?query={quote(keyword)}&city={BOSS_CITY_ALL}")
        time.sleep(3)
        page.scroll.to_bottom()
        time.sleep(1)
    out = []
    for card in page.eles("css:.job-card-box"):
        try:
            title = card.ele("css:.job-name", timeout=1)
            comp = card.ele("css:.company-name", timeout=1)
            area = card.ele("css:.job-area", timeout=1)
            salary = card.ele("css:.salary", timeout=1)
            link = card.ele("tag:a", timeout=1)
            tags = [t.text for t in card.eles("css:.tag-list li")]
            href = link.attr("href") if link else ""
            out.append({
                "source": "boss", "raw_id": "",
                "title": title.text if title else "",
                "company": comp.text if comp else "",
                "salary_text": salary.text if salary else "",
                "city": area.text if area else "",
                "tags": tags, "desc": "",
                "url": ("https://www.zhipin.com" + href) if href.startswith("/") else href,
                "publish_ts": None, "scale_text": "",
            })
        except Exception:
            continue
    return out


# ---------------- 猎聘 ----------------
_LP_JS = r"""
return [...document.querySelectorAll('[class*="jobCardPcContainer"]')].map(c => {
  const pick = s => c.querySelector(s);
  const txt = e => e ? e.textContent.trim() : '';
  const tb = pick('[class*="jobTitleBox"]');
  const inner = tb ? tb.querySelector('div') : null;
  const a = c.querySelector('a');
  const dq = pick('[class*="jobDqBox"]');
  const tags = [...c.querySelectorAll('[class*="companyTagsBox"] span')].map(x => x.textContent.trim());
  return {
    title: (inner ? inner.textContent.trim() : txt(tb)).replace(/^招聘/, '').trim(),
    salary: txt(pick('[class*="jobSalary"]')),
    city: dq ? dq.textContent.replace(/[【】\s]/g, '') : '',
    company: txt(pick('[class*="companyName"]')),
    industry: tags[0] || '',
    scale: tags[1] || '',
    labels: [...c.querySelectorAll('[class*="labelsTag"]')].map(x => x.textContent.trim()),
    href: a ? a.href : ''
  };
});
"""


def _lp_extract(page: ChromiumPage, scrolls: int = 3) -> list[dict]:
    out, seen = [], set()
    for _ in range(scrolls + 1):
        try:
            raw = page.run_js(_LP_JS) or []
        except Exception:
            raw = []
        for r in raw:
            href = r.get("href") or ""
            m = re.search(r"job_id=(\d+)", href) or re.search(r"/a/(\d+)\.shtml", href)
            job_id = m.group(1) if m else ""
            title = (r.get("title") or "").strip()
            if not title:
                continue
            key = job_id or (r.get("company"), title)
            if key in seen:
                continue
            seen.add(key)
            out.append({
                "source": "liepin",
                "raw_id": job_id,
                "title": title,
                "company": (r.get("company") or "").strip(),
                "salary_text": (r.get("salary") or "").strip(),
                "city": (r.get("city") or "").strip(),
                "tags": r.get("labels") or [],
                "desc": r.get("industry") or "",
                "industry": r.get("industry") or "",
                "url": href,
                "publish_ts": None,
                "scale_text": r.get("scale") or "",
            })
        try:
            page.run_js("window.scrollBy(0, 1600); return 1;")
        except Exception:
            pass
        time.sleep(1.8)
    return out


def liepin_search(page: ChromiumPage, keyword: str) -> list[dict]:
    """先试关键词搜索页；被风控返回空时自动回落到个性化推荐流。"""
    try:
        page.get(f"https://www.liepin.com/zhaopin/?key={quote(keyword)}&scene=init&workYearCode=0")
        time.sleep(4)
        got = _lp_extract(page, scrolls=1)
        if got:
            return got
    except Exception:
        pass
    return liepin_feed(page)


def liepin_feed(page: ChromiumPage) -> list[dict]:
    page.get("https://c.liepin.com/")
    time.sleep(4.5)
    return _lp_extract(page, scrolls=3)


def probe(page: ChromiumPage) -> dict:
    info = {"boss": {}, "liepin": {}}
    page.get("https://www.zhipin.com/web/geek/job?query=" + quote("金融解决方案") + "&city=" + BOSS_CITY_ALL)
    time.sleep(3)
    body = page.ele("tag:body").text
    info["boss"]["logged_in"] = ("登录状态已失效" not in body) and ("验证码登录" not in body[:600])
    jl = boss_search(page, "金融解决方案")
    info["boss"]["api_count"] = len(jl)
    info["boss"]["sample"] = jl[0] if jl else None

    lp = liepin_feed(page)
    info["liepin"]["count"] = len(lp)
    info["liepin"]["sample"] = lp[0] if lp else None
    return info


if __name__ == "__main__":
    p = attach()
    print(json.dumps(probe(p), ensure_ascii=False, indent=2))
