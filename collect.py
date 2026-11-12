# -*- coding: utf-8 -*-
"""BOSS 直聘采集器：只搜索、只读取岗位，不做任何投递 / 沟通动作。

取数顺序（越靠前越像真人，越不容易触发风控）：

1. 打开真实的搜索页，先让页面正常渲染、过掉登录态与环境校验。
2. 在页面上下文里 ``fetch`` 同源的 ``joblist.json``：带完整 cookie、referer，
   能拿到明文薪资、公司规模、``encryptBossId``，字段最全。
3. 接口被挡（如 37）时，读已经渲染出来的 DOM 卡片；BOSS 的薪资用私有区
   图标字体混淆，按 ``0xE031 + 数字`` 解码。
4. 监听页面自己发出的 ``joblist.json`` 响应，作为 SPA 版本的兜底。

**绝不把 /wapi/...joblist.json 当文档直接打开。** 这样请求没有 referer、没有页面
上下文，是 BOSS 风控最容易识别的机器人特征，轻则 ``code=35/37``，重则把账号登录态
踢掉——这正是之前「今天和昨天都提示登录失败」的主要诱因。

登录态 / 环境校验：
- 搜索页被重定向到 ``/web/user/``，或页面出现「扫码登录 / 验证码登录」= 登录失效；
  立即停止，不再打接口，也不再拿剩下的关键词硬试。
- ``code=37`` 大多数时候是 BOSS 每天刷新 ``__zp_stoken__`` 的环境校验页
  （``/web/passport/zp/security.html``），不是封禁。等页面脚本算出新 token 再继续；
  偶尔第一次加载卡住，刷新一次即可。
- stoken 缺失 / 过期时 BOSS 还可能先送回首页并带 ``?_security_check=...``；
  脚本会等首页生成新 token，再重新打开搜索页，每轮只回首页触发一次。
- 接口返回 ``code=7`` 同样按登录失效处理；``code=35/36`` 才是 IP / 账号级风控，
  退避后重试，连续 3 个关键词都被挡才熔断。

参考资料：
- DrissionPage 的 listen 网络监听（https://github.com/g1879/DrissionPage）
- can4hou6joeng4/boss-agent-cli：先做登录态只读探测，失效即停、风控即止
- Yang-hua6/boss-：对 ``__zp_stoken__`` 的说明；本项目不自己算签名，
  直接复用真实页面，所以不需要逆向
"""
from __future__ import annotations

import json
import os
import random
import re
import shutil
import subprocess
import time
from pathlib import Path
from urllib.parse import quote

try:
    from DrissionPage import ChromiumPage, ChromiumOptions
except ModuleNotFoundError as exc:  # 手动跑和计划任务可能不是同一个 Python
    import sys

    raise SystemExit(
        "缺少 DrissionPage，当前解释器是：\n"
        f"  {sys.executable}\n\n"
        "在项目目录执行：\n"
        "  python -m pip install -r requirements.txt\n\n"
        "如果计划任务也报这个错，直接重装任务即可（会自动挑一个带依赖的解释器）：\n"
        r"  powershell -ExecutionPolicy Bypass -File .\install_task.ps1" "\n\n"
        "注意：不要往会被自动升级重建的运行时（例如某些桌面端自带的 Python）里装依赖。"
        "那类目录会在升级时按固定清单重建，pip 装进去的 DrissionPage 会被清掉。"
    ) from exc

BASE = Path(__file__).resolve().parent
PORT = int(os.environ.get("CHROME_DEBUG_PORT", "9333"))
PROFILE = str(Path(os.environ.get("CHROME_PROFILE_DIR", BASE / "chrome-profile")).resolve())
LOGIN_STATE_PATH = BASE / "logs" / "boss_login.json"

BOSS_CITY_ALL = "100010000"  # 全国
BOSS_HOME = "https://www.zhipin.com/"
BOSS_LOGIN_URL = "https://www.zhipin.com/web/user/?ka=header-login"
BOSS_SEARCH_URL = "https://www.zhipin.com/web/geek/jobs"
BOSS_API_PATH = "/wapi/zpgeek/search/joblist.json"
PROBE_KEYWORD = "金融解决方案"

BOSS_CODES = {
    0: "正常",
    7: "未登录",
    13: "账号受限",
    35: "IP 异常",
    36: "账号异常",
    37: "环境异常（风控）",
}

_CHROME_CANDIDATES = [
    os.environ.get("CHROME_PATH", ""),
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    "/usr/bin/google-chrome",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
]


# ---------------------------------------------------------------- 浏览器
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


def _debug_info() -> dict | None:
    import urllib.request

    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/version", timeout=3) as r:
            data = json.loads(r.read().decode("utf-8", "ignore"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _debug_alive() -> bool:
    return _debug_info() is not None


def _ws_ok(ws_url: str) -> bool:
    """调试端口能开，不代表 browser WebSocket 能握手。

    12:30 那次崩溃就是 `/json/version` 还是 200，但 WebSocket 返回 404：
    端口被残留 Chrome 占着，浏览器端点是旧的。这里先真握手一次再交给 DrissionPage。
    """
    if not ws_url:
        return False
    try:
        from websocket import create_connection

        ws = create_connection(ws_url, timeout=4, suppress_origin=True)
        ws.close()
        return True
    except Exception:
        return False


def _browser_ready() -> bool:
    info = _debug_info()
    if not info:
        return False
    if "Chrome" not in str(info.get("Browser", "")):
        return False
    return _ws_ok(str(info.get("webSocketDebuggerUrl") or ""))


def _profile_pids() -> list[int]:
    """占用本自动化配置目录的 chrome.exe 进程号。"""
    marker = Path(PROFILE).name or "chrome-profile"
    ps = (
        "Get-CimInstance Win32_Process -Filter \"Name='chrome.exe'\" | "
        f"Where-Object {{ $_.CommandLine -like '*{marker}*' }} | "
        "ForEach-Object { $_.ProcessId }"
    )
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
            timeout=25, capture_output=True, text=True,
        ).stdout
    except Exception:
        return []
    pids = []
    for line in (out or "").splitlines():
        line = line.strip()
        if line.isdigit():
            pids.append(int(line))
    return pids


def _close_profile_chrome(wait: int = 6) -> int:
    """优雅关闭占用配置目录的 Chrome，必要时才强杀。

    以前直接 Stop-Process -Force：Chrome 来不及把 cookie 落盘，下次启动容易变成
    「登录态莫名失效」。现在先 taskkill（发关闭消息），等它自己退出，再兜底强杀。
    """
    pids = _profile_pids()
    if not pids:
        return 0
    for pid in pids:
        subprocess.run(["taskkill", "/PID", str(pid)], capture_output=True,
                       timeout=15, check=False)
    for _ in range(wait * 2):
        time.sleep(0.5)
        if not _profile_pids():
            break
    for pid in _profile_pids():
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True,
                       timeout=15, check=False)
    return len(pids)


def ensure_browser(timeout: int = 30, onscreen: bool = False) -> None:
    """定时任务里 Chrome 可能没开，自己拉起来（独立配置，不干扰日常浏览器）。

    onscreen=True 时窗口放在主屏可见位置（重新扫码登录时用）；
    默认挪到屏幕左侧外面，定时跑批时不打扰你。
    """
    if _browser_ready():
        return
    _close_profile_chrome()
    time.sleep(1.5)
    if _browser_ready():
        return
    args = [
        f"--user-data-dir={PROFILE}",
        f"--remote-debugging-port={PORT}",
        "--no-first-run", "--no-default-browser-check",
        "--disable-blink-features=AutomationControlled",
        "--disable-session-crashed-bubble", "--hide-crash-restore-bubble",
        "--window-size=1440,900", "--window-position=" + ("80,40" if onscreen else "-2400,0"),
        "--lang=zh-CN", "about:blank",
    ]
    subprocess.Popen([find_chrome(), *args], close_fds=True)
    for _ in range(timeout * 2):
        time.sleep(0.5)
        if _browser_ready():
            time.sleep(0.5)  # 给窗口和 cookie 恢复留一点时间
            return
    raise RuntimeError(
        f"自动化 Chrome 启动失败（端口 {PORT} 无响应或调试握手不可用）"
    )


def attach(retries: int = 2) -> ChromiumPage:
    """连接自动化 Chrome；握手失败时关掉重启再来一轮。

    单次 ChromiumPage 初始化遇到 404 手滑很常见（端口在、但浏览器端点还没就绪），
    直接抛异常会让整轮任务静默失败，所以这里必须重试。
    """
    last: Exception | None = None
    for _ in range(max(1, retries)):
        try:
            ensure_browser()
            co = ChromiumOptions().set_local_port(PORT)
            page = ChromiumPage(co)
            page.set.load_mode.eager()
            page.set.timeouts(base=12, page_load=30, script=12)
            return page
        except Exception as exc:
            last = exc
            try:
                _close_profile_chrome()
            except Exception:
                pass
            time.sleep(2)
    raise RuntimeError(f"连接自动化 Chrome 失败（端口 {PORT}）：{last}")


def close_browser(page: ChromiumPage | None = None) -> None:
    """收尾时正常关闭浏览器，让 cookie / 登录态落盘。

    Browser.close 是正常退出流程；关不掉才兜底强杀。定时任务默认每轮结束都关，
    下次运行重新拉起（约 5 秒），比一直挂着更不容易在睡眠 / 崩溃时丢 cookie。
    """
    if page is not None:
        try:
            page.quit(timeout=8, force=False)
        except Exception:
            pass
    for _ in range(30):
        time.sleep(0.4)
        if not _debug_alive():
            return
    _close_profile_chrome()


# ---------------------------------------------------------------- 页面判定
def classify_page(url: str, body: str, cards: int) -> str:
    """把「当前页面」归类：ok / login / security / verify / blank。

    - ``/web/passport/zp/security.html?code=37...`` 是 BOSS 刷新
      ``__zp_stoken__`` 的环境校验页，**不是封禁**。页面里的脚本会用 seed/ts
      算出新 token 再跳回原地址；这里必须等它跑完，而不是立刻判死。
    - 登录页最关键的特征是 URL 被重定向到 /web/user/（或 passport 域名），
      或者页面出现真正的登录表单（「BOSS直聘APP扫码登录」「验证码登录/注册」）。
    - 不要看页头那个「登录/注册」链接：页面还没渲染完时它也在，曾把正常的
      搜索页误判成掉登录（18:30 那封误报告警就是这么来的）。
    """
    u = (url or "").lower()
    b = body or ""
    if cards > 0:
        return "ok"
    if "/web/passport/zp/security" in u or "security.html" in u:
        return "security"
    if "/web/user" in u or "passport" in u:
        return "login"
    if "verify" in u or "安全验证" in b or "滑动验证" in b:
        return "verify"
    if "BOSS直聘APP扫码登录" in b or "扫码登录" in b or "验证码登录/注册" in b:
        return "login"
    if len(b.strip()) < 80:
        return "blank"
    return "ok"


def _page_state(tab) -> str:
    try:
        url = tab.url
    except Exception:
        url = ""
    try:
        cards = len(tab.eles("css:.job-card-box", timeout=0.4))
    except Exception:
        cards = 0
    try:
        body = tab.run_js("return document.body?document.body.innerText:''") or ""
    except Exception:
        body = ""
    return classify_page(url, body, cards)


def _wait_ready(tab, timeout: float = 10.0) -> str:
    """等页面稳定：要么出现岗位卡片，要么露出登录 / 校验页。"""
    end = time.time() + timeout
    state = "blank"
    while time.time() < end:
        state = _page_state(tab)
        if state in ("ok", "login", "verify", "security"):
            return state
        time.sleep(0.3)
    return state


def _resolve_security(tab, timeout: float = 45.0) -> str:
    """等 BOSS 的环境校验页自己生成 __zp_stoken__ 并跳走。

    实测第一次打开偶尔会停在「正在加载中…」（324KB 的 security JS 没跑起来），
    刷新一次就会正常生成 token 并跳回。所以这里只等，不提前判死。
    """
    end = time.time() + timeout
    while time.time() < end:
        state = _page_state(tab)
        if state != "security":
            return state
        time.sleep(0.5)
    return "security"


def _open_and_settle(tab, url: str, page_timeout: float = 10.0,
                     security_timeout: float = 45.0, tries: int = 3) -> str:
    """打开搜索页；处理环境校验、首页安全跳转，必要时重试。

    stoken 缺失 / 过期时，BOSS 有时不直接给 security.html，而是先把人送回
    首页并带上 ``?_security_check=1_...``；首页脚本会在几秒内生成新 token。
    所以这里要检查“到底有没有停在搜索页”，没停就等一会儿再开一次。
    """
    state = "blank"
    for _ in range(max(1, tries)):
        tab.get(url)
        state = _wait_ready(tab, timeout=page_timeout)

        if state == "security":
            state = _resolve_security(tab, timeout=security_timeout)
            if state == "security":
                # 第一次加载可能卡住：刷新一次通常就能把 token 生成出来
                try:
                    tab.refresh()
                except Exception:
                    pass
                state = _resolve_security(tab, timeout=security_timeout)
            if state == "security":
                return "security"
            time.sleep(1.0)
            continue

        if state in ("login", "verify"):
            return state

        try:
            current = tab.url or ""
        except Exception:
            current = ""
        if "/web/geek/job" in current:
            return state

        # 被送到首页做 _security_check：给首页脚本一点时间生成 token，再开搜索页
        time.sleep(2.5)

    return state


_TOKEN_BOUNCED = False


def _has_stoken(tab) -> bool:
    """确认存在一个还没过期的 __zp_stoken__。"""
    now = time.time()
    try:
        for c in tab.run_cdp("Network.getAllCookies").get("cookies", []):
            if c.get("name") != "__zp_stoken__" or "zhipin" not in str(c.get("domain", "")):
                continue
            exp = c.get("expires", -1)
            if not exp or exp > now + 60:
                return True
    except Exception:
        pass
    return False


def _bounce_for_token(tab, url: str) -> bool:
    """接口返回 37 且页面不是 security.html 时，用首页触发一次 token 刷新。

    首页会带 ``?_security_check=...`` 跑一遍 security JS；这里轮询 cookie，
    确认新的 ``__zp_stoken__`` 真的落下来，再回搜索页。每轮采集只做一次。
    """
    global _TOKEN_BOUNCED
    if _TOKEN_BOUNCED:
        return False
    _TOKEN_BOUNCED = True
    try:
        tab.get(BOSS_HOME)
        end = time.time() + 15
        while time.time() < end and not _has_stoken(tab):
            time.sleep(0.5)
        tab.get(url)
        _wait_ready(tab, timeout=10)
        time.sleep(2.0)  # 给 cookie 落盘和页面初始化留一点时间
        return True
    except Exception:
        return False


class BossError(Exception):
    """BOSS 采集异常基类。"""


class BossLoginRequired(BossError):
    """登录态失效：必须人工扫码，继续请求只会让风控更重。"""


class BossVerifyRequired(BossError):
    """卡在安全验证（滑块 / 验证码），需要人工在窗口里完成。"""


class BossBlocked(BossError):
    """接口风控（code=35/36/37），退避后可重试，不要硬打。"""

    def __init__(self, code: int | None, message: str = "", keyword: str = ""):
        self.code = code
        self.message = message or BOSS_CODES.get(code, "")
        self.keyword = keyword
        super().__init__(f"code={code} {self.message}".strip())


# ---------------------------------------------------------------- 取数
def _j(text: str) -> dict | None:
    """Chrome 会把 JSON 渲染成 <pre> 文本，这里剥掉外层再解析。"""
    import html as _html

    if not text:
        return None
    m = re.search(r"<pre[^>]*>(.*?)</pre>", text, re.S)
    raw = _html.unescape(m.group(1)) if m else text
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _loads(value) -> dict | None:
    if isinstance(value, dict):
        return value
    if isinstance(value, list):
        return None
    if not value:
        return None
    if isinstance(value, bytes):
        value = value.decode("utf-8", "ignore")
    raw = str(value).strip()
    if not raw or raw.startswith("<"):
        return None
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def search_url(keyword: str, page_no: int = 1) -> str:
    return (
        f"{BOSS_SEARCH_URL}?query={quote(keyword, safe='')}"
        f"&city={BOSS_CITY_ALL}&page={int(page_no)}"
    )


def api_url(keyword: str, page_no: int = 1) -> str:
    return (
        f"{BOSS_API_PATH}?scene=1&query={quote(keyword, safe='')}"
        f"&city={BOSS_CITY_ALL}&page={int(page_no)}&pageSize=30"
    )


def _boss_from_json(x: dict) -> dict:
    # jobLabels 是"经验/学历"，welfareList 是"五险一金"等福利——
    # 两者都不能进业务域匹配，否则会被"补充医疗保险"误判成保险行业。
    skills = x.get("skills") or []
    industry = x.get("brandIndustry") or ""
    return {
        "source": "boss",
        "raw_id": str(x.get("encryptJobId") or x.get("jobId") or ""),
        # 原生 jobview 深链需要 jid + uid；H5 页面里对应 sjid/sbid
        "encrypt_boss_id": str(x.get("encryptBossId") or ""),
        "job_id_num": str(x.get("jobId") or x.get("id") or ""),
        "boss_id_num": str(x.get("bossId") or ""),
        "title": x.get("jobName", ""),
        "company": x.get("brandName", ""),
        "salary_text": x.get("salaryDesc", ""),
        "city": "".join(filter(None, [
            x.get("cityName", ""), x.get("areaDistrict", ""), x.get("businessDistrict", "")
        ])),
        "tags": skills,
        "desc": industry,
        "industry": industry,
        "url": (f"https://www.zhipin.com/job_detail/{x.get('encryptJobId')}.html"
                if x.get("encryptJobId") else ""),
        "publish_ts": None,
        "scale_text": x.get("brandScaleName", "") or "",
    }


def decode_salary(text: str) -> str:
    """把 BOSS 搜索结果里的图标字体薪资还原成数字。

    页面 DOM 里的薪资是私有区字符（\\ue031–\\ue03a），肉眼和 innerText 都读不出来；
    实测 \\ue031=0、\\ue032=1 … \\ue03a=9，编码是“0xE031 + 数字”。
    接口路径本来就有明文薪资，这个解码只用于接口被挡时的 DOM 兜底。
    """
    if not text:
        return ""
    out = []
    for ch in str(text):
        code = ord(ch)
        out.append(str(code - 0xE031) if 0xE031 <= code <= 0xE03A else ch)
    return "".join(out)


def job_from_card(card: dict) -> dict:
    """把 DOM 卡片（或等价结构）归一成岗位字段。

    纯函数，方便单测；不依赖浏览器。
    """
    href = (card.get("href") or "").strip()
    m = re.search(r"/job_detail/([A-Za-z0-9_-]+)\.html", href)
    raw_id = m.group(1) if m else ""
    comp_tags = [str(t).strip() for t in (card.get("comp_tags") or []) if str(t).strip()]
    tags = [str(t).strip() for t in (card.get("tags") or []) if str(t).strip()]
    industry = str(card.get("industry") or (comp_tags[0] if comp_tags else "")).strip()
    scale = str(card.get("scale") or " ".join(comp_tags[1:])).strip()
    return {
        "source": "boss",
        "raw_id": raw_id,
        "encrypt_boss_id": "",
        "job_id_num": "",
        "boss_id_num": "",
        "title": str(card.get("title") or "").strip(),
        "company": str(card.get("company") or "").strip(),
        "salary_text": decode_salary(card.get("salary") or "").strip(),
        "city": str(card.get("area") or "").strip(),
        "tags": tags,
        "desc": industry,
        "industry": industry,
        "url": href or (f"https://www.zhipin.com/job_detail/{raw_id}.html" if raw_id else ""),
        "publish_ts": None,
        "scale_text": scale,
    }


_DOM_JS = r"""
return [...document.querySelectorAll('.job-card-box')].map(function (card) {
  function txt(sel) {
    var e = card.querySelector(sel);
    return e ? (e.innerText || '').trim() : '';
  }
  var a = card.querySelector('a[href*="/job_detail/"]') || card.querySelector('a.job-name');
  var tags = [].map.call(card.querySelectorAll('.tag-list li'), function (e) {
    return (e.innerText || '').trim();
  });
  var ct = [].map.call(card.querySelectorAll('.company-tag-list li'), function (e) {
    return (e.innerText || '').trim();
  });
  return {
    title: txt('.job-name') || txt('.job-title'),
    salary: txt('.job-salary') || txt('.salary'),
    area: txt('.company-location') || txt('.job-area') || txt('.job-area-wrapper'),
    company: txt('.boss-name') || txt('.company-name') || txt('.company-info .name'),
    href: a ? a.href : '',
    tags: tags,
    comp_tags: ct
  };
});
"""


def _boss_from_dom(tab) -> list[dict]:
    try:
        raw = tab.run_js(_DOM_JS) or []
    except Exception:
        return []
    jobs = [job_from_card(x) for x in raw if isinstance(x, dict)]
    jobs = [j for j in jobs if j["title"]]
    if len(jobs) < 8:
        # 首屏卡片太少时轻微滚动一次，触发懒加载
        try:
            tab.scroll.to_bottom()
            time.sleep(1.2)
            raw = tab.run_js(_DOM_JS) or []
            more = [job_from_card(x) for x in raw if isinstance(x, dict)]
            more = [j for j in more if j["title"]]
            if len(more) > len(jobs):
                jobs = more
        except Exception:
            pass
    return jobs


def _handle_api(data: dict, keyword: str = "") -> list[dict]:
    code = data.get("code")
    msg = str(data.get("message") or "")
    if code == 0:
        lst = (data.get("zpData") or {}).get("jobList") or []
        return [_boss_from_json(x) for x in lst if isinstance(x, dict)]
    if code == 7:
        raise BossLoginRequired("接口返回未登录（code=7）")
    if code in (35, 36, 37) or "异常" in msg:
        raise BossBlocked(code, msg or BOSS_CODES.get(code, ""), keyword)
    if code == 13:
        raise BossBlocked(code, msg or BOSS_CODES.get(code, ""), keyword)
    return []


def _fetch_in_page(tab, path: str) -> dict | None:
    """在页面上下文里 fetch 同源接口（带 cookie / referer），不打开新页面。"""
    expr = (
        "(async()=>{try{const r=await fetch(%s,{credentials:'include',"
        "headers:{'accept':'application/json, text/plain, */*'}});"
        "return JSON.stringify({s:r.status,t:(await r.text()).slice(0,400000)})}"
        "catch(e){return JSON.stringify({s:-1,t:String(e)})}})()"
    ) % json.dumps(path)
    try:
        out = tab.run_cdp(
            "Runtime.evaluate", expression=expr, awaitPromise=True, returnByValue=True
        )
    except Exception:
        return None
    val = ((out or {}).get("result") or {}).get("value")
    if not val:
        return None
    try:
        payload = json.loads(val)
    except Exception:
        return None
    text = payload.get("t") or ""
    if text.lstrip().startswith("<"):
        # 返回 HTML：大概率被重定向到登录 / 验证页
        if "登录" in text[:3000] or "passport" in text[:3000]:
            raise BossLoginRequired("接口被重定向到登录页")
        return None
    return _loads(text)


def _wait_packet(tab, keyword: str, timeout: float = 12.0):
    """只认当前关键词的 joblist 响应，忽略推荐流等其它请求。"""
    deadline = time.time() + timeout
    while True:
        remain = deadline - time.time()
        if remain <= 0:
            return None
        try:
            got = tab.listen.wait(timeout=remain)
        except Exception:
            return None
        if not got:
            return None
        try:
            got_url = got.url or ""
        except Exception:
            got_url = ""
        if quote(keyword, safe="") in got_url or keyword in got_url:
            return got


def search(page: ChromiumPage, keyword: str, page_no: int = 1, timeout: float = 15.0) -> list[dict]:
    """搜索一个关键词。返回岗位列表；登录失效 / 风控会抛对应异常。"""
    tab = page.latest_tab
    try:
        tab.listen.start("joblist.json")
    except Exception:
        pass
    try:
        state = _open_and_settle(tab, search_url(keyword, page_no))
        if state == "login":
            # 再等一拍：搜索页首屏可能还没渲染完，页头短暂出现「登录/注册」
            time.sleep(1.2)
            state = _page_state(tab)
        if state == "login":
            raise BossLoginRequired(f"搜索页要求登录（{tab.url}）")
        if state == "security":
            raise BossVerifyRequired("BOSS 环境校验页（__zp_stoken__ 刷新）未完成")
        if state == "verify":
            raise BossVerifyRequired("页面卡在安全验证")

        # 1) 页面内同源 fetch：字段最全（明文薪资、规模、encryptBossId），
        #    又是在真实页面里发 XHR，不是把接口当文档打开。
        api_error: BossError | None = None
        data = None
        for attempt in range(2):
            try:
                data = _fetch_in_page(tab, api_url(keyword, page_no))
            except BossLoginRequired as e:
                api_error = e
                break
            if data:
                try:
                    jobs = _handle_api(data, keyword)
                    if jobs:
                        mark_login_ok()
                        return jobs
                except BossError as e:
                    api_error = e
            # 37 且不在 security.html：可能是 stoken 过期但页面没跳校验页。
            # 每轮只回首页触发一次 token 刷新，再试一次接口。
            if (isinstance(api_error, BossBlocked) and api_error.code == 37
                    and attempt == 0
                    and _bounce_for_token(tab, search_url(keyword, page_no))):
                continue
            break

        # 2) 服务端直出的 DOM 兜底：卡片已经在页面里；薪资是图标字体，解码后可用。
        jobs = _boss_from_dom(tab)
        if jobs:
            mark_login_ok()
            return jobs

        # 3) 万一页面是 SPA 版本、接口被挡但页面自己发过 joblist，再收一次监听包
        packet = _wait_packet(tab, keyword, timeout=min(timeout, 10))
        if packet:
            body = None
            try:
                body = packet.response.body
            except Exception:
                body = None
            packet_data = _loads(body)
            if packet_data:
                try:
                    jobs = _handle_api(packet_data, keyword)
                    mark_login_ok()
                    return jobs
                except BossError as e:
                    api_error = api_error or e

        if api_error:
            raise api_error
        return []
    finally:
        try:
            tab.listen.stop()
        except Exception:
            pass


# ---------------------------------------------------------------- 登录态
def mark_login_ok() -> None:
    """记录最近一次「搜索成功」，给告警邮件提供上下文（每进程只写一次）。"""
    global _LOGIN_MARKED
    if _LOGIN_MARKED:
        return
    _LOGIN_MARKED = True
    try:
        from datetime import datetime

        LOGIN_STATE_PATH.parent.mkdir(exist_ok=True)
        LOGIN_STATE_PATH.write_text(
            json.dumps({"ok_at": datetime.now().isoformat(timespec="seconds")},
                       ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception:
        pass


_LOGIN_MARKED = False


def last_login_ok() -> str:
    try:
        data = json.loads(LOGIN_STATE_PATH.read_text(encoding="utf-8"))
        return str(data.get("ok_at") or "")
    except Exception:
        return ""


def login_state(page: ChromiumPage, tab=None) -> dict:
    """只读体检：打开一次搜索页，判断当前是已登录、掉登录、还是卡验证。

    只访问一次搜索页，不碰 wapi 接口；掉登录时也不会继续打接口。
    """
    tab = tab or page.latest_tab
    try:
        state = _open_and_settle(tab, search_url(PROBE_KEYWORD), page_timeout=12,
                                 security_timeout=30)
    except Exception as e:
        return {"state": "err", "detail": f"{type(e).__name__}: {e}", "url": "", "cards": 0}
    if state == "login":
        time.sleep(1.2)
        state = _page_state(tab)
    try:
        url = tab.url
    except Exception:
        url = ""
    try:
        cards = len(tab.eles("css:.job-card-box", timeout=0.4))
    except Exception:
        cards = 0
    return {"state": state, "url": url, "cards": cards}


def probe(page: ChromiumPage) -> dict:
    st = login_state(page)
    info: dict = {"state": st, "login_ok": st.get("state") == "ok", "count": 0, "sample": None}
    if info["login_ok"]:
        try:
            jobs = search(page, PROBE_KEYWORD)
            info["count"] = len(jobs)
            info["sample"] = jobs[0] if jobs else None
        except (BossLoginRequired, BossVerifyRequired, BossBlocked) as e:
            info["login_ok"] = False
            info["error"] = str(e)
    return info


# ---------------------------------------------------------------- 批量采集
def _say(msg: str) -> None:
    """计划任务用 pythonw 跑，sys.stdout 是 None，print 会抛异常——静默兜住。"""
    try:
        print(msg, flush=True)
    except Exception:
        pass


def collect_keywords(
    page: ChromiumPage,
    keywords: list[str],
    *,
    max_pages: int = 1,
    delay: tuple[float, float] = (3.5, 7.0),
    block_retries: int = 1,
    log=_say,
) -> tuple[list[dict], dict]:
    """按关键词逐个采集，内置退避、熔断与诊断信息。

    - 登录失效 / 安全验证：立即停止（继续打只会更糟）。
    - 单个关键词风控：等 12–25 秒重试一次；仍被挡就跳过它。
    - 连续 3 个关键词都被挡：熔断整轮。
    """
    jobs: list[dict] = []
    diag: dict = {
        "boss": 0, "keywords": [], "blocked": [], "errors": [],
        "login": None, "verify": False, "circuit": False, "elapsed": 0.0,
    }
    started = time.time()
    consecutive_blocks = 0

    for kw in keywords:
        rows: list[dict] = []
        attempt = 0
        while True:
            try:
                rows = []
                for pno in range(1, max(1, int(max_pages)) + 1):
                    part = search(page, kw, pno)
                    rows.extend(part)
                    if len(part) < 30:
                        break
                break
            except BossLoginRequired as e:
                diag["login"] = "required"
                diag["errors"].append(f"登录态失效：{e}")
                log("[boss] 登录态已失效，停止本轮采集")
                diag["boss"] = len(jobs)
                diag["elapsed"] = round(time.time() - started, 1)
                return jobs, diag
            except BossVerifyRequired as e:
                diag["verify"] = True
                diag["errors"].append(f"安全验证：{e}")
                log("[boss] 环境校验 / 安全验证未完成，停止本轮采集")
                diag["boss"] = len(jobs)
                diag["elapsed"] = round(time.time() - started, 1)
                return jobs, diag
            except BossBlocked as e:
                attempt += 1
                if attempt > block_retries:
                    diag["blocked"].append({"keyword": kw, "code": e.code, "msg": e.message})
                    diag["errors"].append(f"{kw}: 风控 code={e.code}")
                    diag["keywords"].append({"kw": kw, "n": 0, "blocked": True})
                    consecutive_blocks += 1
                    log(f"[boss] {kw} 仍被风控（code={e.code}），跳过")
                    if consecutive_blocks >= 3:
                        diag["circuit"] = True
                        log("[boss] 连续 3 个关键词被风控，熔断本轮")
                        diag["boss"] = len(jobs)
                        diag["elapsed"] = round(time.time() - started, 1)
                        return jobs, diag
                    time.sleep(random.uniform(10, 18))
                    break
                cool = random.uniform(12, 22) * attempt
                log(f"[boss] {kw} 风控 code={e.code}，{cool:.0f}s 后重试")
                time.sleep(cool)
            except Exception as e:
                attempt += 1
                if attempt <= block_retries:
                    cool = random.uniform(3, 6)
                    log(f"[boss] {kw} 页面异常（{type(e).__name__}），{cool:.0f}s 后重试")
                    time.sleep(cool)
                    continue
                diag["errors"].append(f"{kw}: {type(e).__name__}: {e}")
                log(f"[boss] {kw} 异常：{e}")
                break

        if rows:
            consecutive_blocks = 0
        jobs.extend(rows)
        diag["keywords"].append({"kw": kw, "n": len(rows)})
        log(f"[boss] {kw}: {len(rows)} 条")
        if kw != keywords[-1]:
            time.sleep(random.uniform(*delay))

    diag["boss"] = len(jobs)
    diag["elapsed"] = round(time.time() - started, 1)
    return jobs, diag


if __name__ == "__main__":
    p = attach()
    try:
        print(json.dumps(probe(p), ensure_ascii=False, indent=2))
    finally:
        close_browser(p)
