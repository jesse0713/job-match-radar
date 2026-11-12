# -*- coding: utf-8 -*-
r"""岗位链接：把 BOSS 采集到的岗位归一成手机可用的网页地址，并生成 APP 深链。

深链格式不是猜的，来自 BOSS 官方 APK（com.hpbr.bosszhipin）的字符串表：

    bosszp://bosszhipin.app/openwith?type=newwebview&fullScreen=1&url=%s

AndroidManifest 中注册了 scheme=bosszp、host=bosszhipin.app、path=/openwith。
openwith 只暴露了 newwebview，没有原生的 jobview 类型，所以用官方 newwebview
在 APP 内打开岗位页；APP 已登录时会复用登录态，不会再弹登录框。

为什么不能把 scheme 直接放进邮件按钮：
1) QQ 邮箱、微信等客户端会把 <a href="bosszp://..."> 直接抹掉，点了没反应；
2) 就算没抹掉，自定义协议在扫码/内置浏览器里也常被拦。

所以按钮指向 BOSS 自己的 https 跳转页（d.bosszhipin.com/dl/openprotocol），
它 302 到 m.zhipin.com/mpa/html/weijd/weijd-toapp，该页加载后执行
`window.location.href = url`，只在 url 匹配
`^bosszp://bosszhipin.app/openwith\?*.*` 时才跳（见 weijd 的 toapp 打包代码）。
https 链接邮件客户端不会拦，跳到 scheme 的动作由 BOSS 自己的页面完成。

邮件里的链接分三层：
1) native_url：https 跳转页 + newwebview 深链，点一下进 APP 内岗位页（首选）；
2) boss_h5_url：BOSS 官方 H5 分享页，APP 没装时看，但未登录会要求登录；
3) web_url：手机网页版，兜底，同样要求登录。

只保留 BOSS，其他招聘站的深链已删除。
"""
from __future__ import annotations

import re
from urllib.parse import quote, urlencode

# BOSS 的 encryptJobId 是大小写字母、数字、下划线、短横线组成的短 token
BOSS_ID_RE = re.compile(r"^[A-Za-z0-9_-]{6,64}$")
BOSS_URL_ID_RE = re.compile(r"/job_detail/([A-Za-z0-9_-]+)\.html")


def _boss_id(job: dict) -> str:
    raw_id = str(job.get("raw_id") or "")
    if not raw_id:
        m = BOSS_URL_ID_RE.search(job.get("url") or "")
        raw_id = m.group(1) if m else ""
    return raw_id if BOSS_ID_RE.match(raw_id) else ""


def _boss_openwith(web: str) -> str:
    """官方 newwebview 深链：在 APP 内用 WebView 打开岗位页。

    实测这条路会开成「App 内的浏览器视图」，页面不认自己是 App，
    底部给一个「在 BOSS 直聘 App 内打开」，点下去退化成下载页。
    所以只在没有 uid、拿不到 jobview 时兜底。
    """
    return (
        "bosszp://bosszhipin.app/openwith?type=newwebview&fullScreen=1&url="
        + quote(web, safe="")
    )


def _boss_jobview(job: dict, raw_id: str) -> str:
    """原生职位详情深链：type=jobview + jid + uid。

    实测不可用：APP 收到后报「数据异常」。带 encryptJobId + encryptBossId 也一样，
    说明它要的是 APP 内部 ID（以及可能的签名参数），而搜索接口已经不返回数字 ID。
    保留函数只为记录结论，默认不启用。
    """
    uid = str(job.get("encrypt_boss_id") or "")
    if not uid:
        return ""
    return (
        "bosszp://bosszhipin.app/openwith?type=jobview"
        f"&jid={quote(raw_id, safe='')}&uid={quote(uid, safe='')}"
    )


def _boss_bridge(scheme: str) -> str:
    """把 scheme 包进 BOSS 官方的 https 跳转页，供邮件按钮直接使用。

    客户端会拦 bosszp://，但不会拦这个 https 链接；页面自己负责跳到 scheme。

    `openTag=1` 不能省：weijd-toapp 的 `showOpen` 是
    `openTag === 1 && !isWX && !isQQ`，不传就只有「下载 APP」一个按钮，
    点下去是下载安装包，不会唤起已装好的 APP —— 就是「停在跳转页没反应」。
    """
    return "https://d.bosszhipin.com/dl/openprotocol?openTag=1&url=" + quote(scheme, safe="")


def _boss_links(job: dict) -> tuple[str, str]:
    raw_id = _boss_id(job)
    if not raw_id:
        return "", ""

    # 手机页比 www 桌面页更可能在登录后留在岗位页，而不是被送回首页
    web = f"https://m.zhipin.com/job_detail/{raw_id}.html"
    # jobview 只在 APP 内部可用（外部传参报「数据异常」），这里仍走 WebView 兜底。
    return web, _boss_bridge(_boss_openwith(web))


def build(job: dict) -> tuple[str, str]:
    """返回 (手机网页版, APP 深链)。无法识别的岗位返回空字符串。"""
    source = (job.get("source") or "").lower()
    web, app = _boss_links(job) if source == "boss" else ("", "")

    if not web:
        original = (job.get("url") or "").strip()
        if original.lower().startswith(("http://", "https://")):
            web = original
    return web, app


def build_smartlink(base: str, source: str, job_id: str, web: str, title: str = "") -> str:
    """生成 https 跳转页链接；base 为空时返回空串。"""
    base = (base or "").strip()
    if not base or not job_id:
        return ""
    params = {
        "s": source,
        "id": job_id,
        "u": web or "",
        "t": title or "",
    }
    sep = "&" if "?" in base else "?"
    return base + sep + urlencode(params, quote_via=quote)


def apply(job: dict, smartlink_base: str = "") -> dict:
    """就地写入 web_url / app_url / smart_url / native_url，返回同一个 dict。"""
    web, app = build(job)
    job["web_url"] = web
    job["app_url"] = app
    job["smart_url"] = ""
    job["native_scheme"] = ""
    job["native_url"] = ""
    job["webview_scheme"] = ""
    job["boss_h5_url"] = ""

    if (job.get("source") or "").lower() != "boss":
        return job

    job_id = _boss_id(job)
    if not job_id:
        return job

    # jobview 外部不可用（见 _boss_jobview 的说明），仍用 APP 内 WebView 打开岗位页。
    scheme = _boss_openwith(web)
    job["native_scheme"] = scheme
    job["native_url"] = _boss_bridge(scheme)
    job["webview_scheme"] = _boss_openwith(web)

    # BOSS 官方 H5 分享页：APP 没装时在浏览器里看，未登录会要求登录。
    job["boss_h5_url"] = f"https://m.zhipin.com/mpa/html/weijd/weijd-job/{job_id}/"
    job["smart_url"] = build_smartlink(smartlink_base, "boss", job_id, web, job.get("title", ""))
    return job
