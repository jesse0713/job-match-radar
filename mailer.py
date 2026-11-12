# -*- coding: utf-8 -*-
"""邮件推送：SMTP over SSL（默认 QQ 邮箱）。

凭据只从工作区 .env 读取，不写进代码、不提交仓库：
    QQ_SMTP_AUTH=xxxxxxxxxxxxxxxx   # 邮箱授权码（不是登录密码）
    MAIL_FROM=you@qq.com            # 可选，覆盖 config.json 的 from
    MAIL_TO=you@qq.com              # 可选，覆盖 config.json 的 to

没有授权码时自动进入 dry-run：把邮件写成 .eml 预览文件，不会真的发送。
"""
from __future__ import annotations

import html
import os
import smtplib
import ssl
from datetime import datetime
from email.message import EmailMessage
from email.utils import formataddr
from pathlib import Path

BASE = Path(__file__).resolve().parent
OUT_DIR = Path(os.environ.get("JOB_OUT_DIR", BASE / "outbox"))


def _read_env() -> dict[str, str]:
    """极简 .env 解析，避免额外依赖。"""
    env_path = BASE / ".env"
    out: dict[str, str] = {}
    if not env_path.exists():
        return out
    for line in env_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        out[key.strip()] = val.strip().strip('"').strip("'")
    return out


ENV = _read_env()


def _setting(key: str, default: str = "") -> str:
    return os.environ.get(key) or ENV.get(key) or default


def _to(mail: dict) -> str:
    """收件人。

    三种情况，优先级从高到低：
    1. 画像配了 env_to_key：从 .env 读该键（真实地址不进仓库，如 MAIL_TO_LEGAL）；
    2. 画像配了 ignore_env_to：以画像的 to 为准（占位符，需自行确认）；
    3. 默认：允许 .env 的 MAIL_TO 覆盖 config 的 to（单人使用时方便）。
    """
    key = mail.get("env_to_key")
    if key:
        return _setting(key, mail["to"])
    if mail.get("ignore_env_to"):
        return mail["to"]
    return _setting("MAIL_TO", mail["to"])


def _from_name(mail: dict) -> str:
    return mail.get("from_name") or "岗位匹配助手"


def load_auth() -> str | None:
    return _setting("QQ_SMTP_AUTH") or None


def _esc(value: object) -> str:
    """岗位标题/公司名来自第三方站点，写进 HTML 前必须转义，避免邮件被注入标签。"""
    return html.escape(str(value or ""), quote=True)


def _safe_url(url: str, allow_app: bool = False) -> str:
    """只允许 http(s) 和 BOSS 的 APP scheme，挡住 javascript: 之类的伪协议。"""
    u = (url or "").strip()
    allowed = ("http://", "https://")
    if allow_app:
        allowed += ("bosszp://",)
    return _esc(u) if u.lower().startswith(allowed) else "#"


def _num(value: object) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _score_theme(score: float) -> tuple[str, str, str]:
    """返回 (文字色, 底色, 进度条色)。"""
    if score >= 80:
        return "#1b7f3a", "#e8f5e9", "#34a853"
    if score >= 65:
        return "#1a56db", "#eaf1ff", "#4a86f7"
    return "#8a5a00", "#fdf6ec", "#e0a63a"


def _score_parts(job: dict) -> str:
    """把六维得分压成一行，方便对照，不占太多版面。"""
    parts = job.get("parts") or {}
    labels = [
        ("role", "岗位"), ("domain", "业务"), ("ai", "AI"),
        ("seniority", "层级"), ("salary", "薪资"), ("scale", "规模"),
    ]
    bits = [f"{label} {round(_num(parts[key])):g}" for key, label in labels if key in parts]
    return " · ".join(bits)


def _source_label(source: str) -> str:
    return {"boss": "BOSS直聘"}.get((source or "").lower(), "岗位")


def _app_label(source: str) -> str:
    return {"boss": "打开 BOSS APP 看 JD"}.get(
        (source or "").lower(), "打开 APP 看 JD"
    )


def _card(index: int, job: dict) -> str:
    score = _num(job.get("score"))
    score_int = int(round(score))
    fg, bg, bar_color = _score_theme(score_int)
    bar_pct = int(round(max(0.0, min(100.0, score))))

    title = _esc(job.get("title"))
    company = _esc(job.get("company") or "公司未标注")
    salary = _esc(job.get("salary_text") or "面议")
    city = _esc(job.get("city") or "")
    reason = "；".join(job.get("reasons", [])[:2])

    web = job.get("web_url") or job.get("url") or ""
    # 首选 bosszp:// 深链：系统「邮件」/ Gmail 会交给系统，一次点击进 APP。
    # native_url 是 https 跳转页，只在没有深链时兜底（老岗位数据、或想留在 QQ 邮箱）。
    deep = job.get("native_scheme") or ""
    bridge = job.get("native_url") or job.get("app_url") or ""
    app = deep or bridge or job.get("boss_h5_url") or ""
    # 「手机网页版」优先用官方分享页：job_detail 对未登录浏览器会直接 302 到安全验证页
    web = job.get("boss_h5_url") or web
    web_href = _safe_url(web)
    title_href = _safe_url(app, allow_app=True) if app else web_href

    meta = f'{company} <span style="color:#c0392b;font-weight:600;">{salary}</span>'
    if city:
        meta += f" · {city}"

    reason_html = ""
    if reason:
        reason_html = (
            '<div style="font-size:12px;color:#6b7787;margin-top:6px;line-height:1.65;">'
            f"{_esc(reason)}</div>"
        )

    parts = _score_parts(job)
    parts_html = ""
    if parts:
        parts_html = (
            '<div style="font-size:11px;color:#9aa4b2;margin-top:5px;">'
            f"构成：{_esc(parts)}</div>"
        )

    # QQ 邮箱会抹掉自定义协议；既然改用系统「邮件」，只保留深链这一个 APP 入口。
    app_btn = ""
    if app:
        app_btn = (
            f'<a href="{_safe_url(app, allow_app=True)}" style="display:inline-block;'
            "background:#1a56db;color:#ffffff;font-size:13px;font-weight:600;"
            "padding:9px 14px;border-radius:7px;text-decoration:none;margin:0 8px 4px 0;"
            f'">{_esc(_app_label(job.get("source", "")))}</a>'
        )
    web_btn = (
        f'<a href="{web_href}" style="display:inline-block;border:1px solid #e6ebf2;'
        "background:#ffffff;color:#6b7787;font-size:12px;font-weight:600;"
        "padding:8px 12px;border-radius:7px;text-decoration:none;margin:0 0 4px;"
        '">手机网页版</a>'
    )

    progress = (
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        'style="margin-top:9px;border-collapse:collapse;">'
        f'<tr><td width="{bar_pct:g}%" style="height:4px;background:{bar_color};'
        'border-radius:2px 0 0 2px;font-size:0;line-height:0;">&nbsp;</td>'
        '<td style="height:4px;background:#eef2f7;border-radius:0 2px 2px 0;'
        'font-size:0;line-height:0;">&nbsp;</td></tr></table>'
    )

    return f"""
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
           style="border:1px solid #e6ebf2;border-radius:12px;margin:0 0 12px;background:#ffffff;">
      <tr><td style="padding:14px 16px 13px;">
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>
          <td valign="top" style="padding-right:10px;">
            <div style="font-size:11px;color:#9aa4b2;margin-bottom:3px;">#{index} · {_source_label(job.get("source", ""))}</div>
            <a href="{title_href}" style="font-size:16px;line-height:1.45;font-weight:700;color:#152238;text-decoration:none;">{title}</a>
          </td>
          <td valign="top" width="72" align="right">
            <div style="background:{bg};border-radius:10px;padding:7px 4px;text-align:center;">
              <div style="font-size:22px;font-weight:800;color:{fg};line-height:1.05;">{score_int}</div>
              <div style="font-size:10px;color:{fg};margin-top:2px;">匹配度</div>
            </div>
          </td>
        </tr></table>
        <div style="font-size:13px;color:#3d4a5c;margin-top:7px;">{meta}</div>
        {reason_html}
        {progress}
        {parts_html}
        <div style="margin-top:11px;">{app_btn}{web_btn}</div>
      </td></tr>
    </table>"""


def render_html(jobs: list[dict], cfg: dict, run_label: str, notes: list[str] | None = None) -> str:
    scores = [_num(j.get("score")) for j in jobs]
    top_score = max(scores) if scores else 0.0
    avg_score = (sum(scores) / len(scores)) if scores else 0.0
    top_fg, _, _ = _score_theme(int(round(top_score)))

    cards = "".join(_card(i, j) for i, j in enumerate(jobs, 1))

    summary = ""
    if jobs:
        summary = (
            '<div style="font-size:13px;color:#4a5568;margin:0 0 6px;">'
            f'<b style="color:{top_fg};">最高 {int(round(top_score))} 分</b> · 平均 {int(round(avg_score))} 分</div>'
        )

    preheader = ""
    if jobs:
        preheader = (
            '<div style="display:none;max-height:0;overflow:hidden;color:#ffffff;'
            'font-size:1px;line-height:1px;">'
            f"最高 {int(round(top_score))} 分：{_esc(jobs[0].get('title'))} 等 {len(jobs)} 条岗位"
            "</div>"
        )

    profile = cfg.get("candidate_profile", {}) or {}
    basis = profile.get("summary", "金融业务 + 数字化复合履历")
    excluded_note = profile.get(
        "excluded_note", "已排除保险公司主体、第三方财富管理、销售岗、小规模金融公司"
    )
    warn = ""
    if notes:
        items = "".join(f"<li>{_esc(n)}</li>" for n in notes)
        warn = (
            '<div style="border-left:3px solid #e6a23c;background:#fdf6ec;padding:10px 14px;'
            'margin:0 0 14px;font-size:13px;color:#8a5a00;line-height:1.7;">'
            f"<b>数据源提示</b><ul style=\"margin:6px 0 0 18px;padding:0;\">{items}</ul></div>"
        )

    body = f"""
    {preheader}
    <div style="font-family:'Microsoft YaHei',Arial,sans-serif;max-width:680px;margin:0 auto;color:#222;background:#f2f4f7;padding:18px 12px;">
      <div style="background:#ffffff;border-radius:14px;padding:18px 18px 14px;">
        <h2 style="font-size:19px;margin:0 0 6px;color:#152238;">{_esc(run_label)}｜今日匹配岗位 {len(jobs)} 条</h2>
        {summary}
        <p style="color:#8a94a6;font-size:12px;margin:0 0 16px;line-height:1.7;">
          匹配依据：{_esc(basis)}；{_esc(excluded_note)}。
          排序：匹配度 → 公司规模 → 发布时间。
        </p>
        {warn}
        {cards if jobs else '<p style="color:#888;">本次没有新增匹配岗位。</p>'}
        <div style="background:#f7f9fc;border-radius:10px;padding:12px 14px;font-size:12px;color:#5b6675;line-height:1.75;margin-top:16px;">
          <b style="color:#3d4a5c;">手机阅读提示</b>：点「打开 BOSS APP 看 JD」一次就进 APP 的岗位页，
          用的是 APP 里已登录的状态，不会再要求登录。页面是 App 内的 WebView 形式；
          若底部出现「在 BOSS 直聘 App 内打开」或下载提示，忽略即可。
        </div>
        <p style="color:#aaa;font-size:12px;margin-top:16px;">
          由岗位匹配系统自动生成 · {datetime.now():%Y-%m-%d %H:%M}
        </p>
      </div>
    </div>"""
    return body


def _safe_name(label: str) -> str:
    return label.replace(":", "-").replace(" ", "").replace("/", "-")


def send_alert(message: str, cfg: dict, run_label: str, dry_run: bool = False) -> str:
    """采集失败时的告警邮件，提醒人工补登。"""
    mail = cfg["mail"]
    html = f"""
    <div style="font-family:'Microsoft YaHei',Arial,sans-serif;max-width:700px;margin:0 auto;">
      <h2 style="font-size:17px;color:#c0392b;margin:0 0 12px;">{_esc(run_label)}｜岗位采集需要人工介入</h2>
      <p style="font-size:14px;line-height:1.7;color:#333;">{_esc(message)}</p>
      <p style="color:#aaa;font-size:12px;margin-top:20px;">由岗位匹配系统自动生成 · {datetime.now():%Y-%m-%d %H:%M}</p>
    </div>"""
    msg = EmailMessage()
    msg["From"] = formataddr((_from_name(mail), _setting("MAIL_FROM", mail["from"])))
    msg["To"] = _to(mail)
    msg["Subject"] = f"{mail['subject_prefix']}{run_label}｜需要人工处理"
    msg.set_content(message)
    msg.add_alternative(html, subtype="html")

    auth = load_auth()
    if dry_run or not auth:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        path = OUT_DIR / f"{datetime.now():%Y%m%d_%H%M%S}_{_safe_name(run_label)}_ALERT.eml"
        path.write_bytes(bytes(msg))
        return f"DRYRUN:{path}"

    ctx = ssl.create_default_context()
    with smtplib.SMTP_SSL(mail["smtp_host"], mail["smtp_port"], context=ctx, timeout=30) as s:
        s.login(_setting("MAIL_FROM", mail["from"]), auth)
        s.send_message(msg)
    return f"SENT:{_to(mail)}"


def send_custom(html: str, subject: str, cfg: dict, dry_run: bool = False) -> str:
    """发送自定义 HTML（用于深链/版式测试），不经过岗位模板。"""
    mail = cfg["mail"]
    msg = EmailMessage()
    msg["From"] = formataddr((_from_name(mail), _setting("MAIL_FROM", mail["from"])))
    msg["To"] = _to(mail)
    msg["Subject"] = subject
    msg.set_content("请使用支持 HTML 的邮件客户端查看。")
    msg.add_alternative(html, subtype="html")

    auth = load_auth()
    if dry_run or not auth:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        path = OUT_DIR / f"{datetime.now():%Y%m%d_%H%M%S}_CUSTOM.eml"
        path.write_bytes(bytes(msg))
        return f"DRYRUN:{path}"

    ctx = ssl.create_default_context()
    with smtplib.SMTP_SSL(mail["smtp_host"], mail["smtp_port"], context=ctx, timeout=30) as s:
        s.login(_setting("MAIL_FROM", mail["from"]), auth)
        s.send_message(msg)
    return f"SENT:{_to(mail)}"


def send(jobs: list[dict], cfg: dict, run_label: str, dry_run: bool = False,
         notes: list[str] | None = None) -> str:
    mail = cfg["mail"]
    html = render_html(jobs, cfg, run_label, notes=notes)
    top = max((_num(j.get("score")) for j in jobs), default=0)
    suffix = f"（最高 {int(round(top))} 分）" if jobs else ""
    subject = f"{mail['subject_prefix']}{run_label}｜{len(jobs)} 条匹配岗位{suffix}"

    msg = EmailMessage()
    msg["From"] = formataddr((_from_name(mail), _setting("MAIL_FROM", mail["from"])))
    msg["To"] = _to(mail)
    msg["Subject"] = subject
    msg.set_content("请使用支持 HTML 的邮件客户端查看。")
    msg.add_alternative(html, subtype="html")

    auth = load_auth()
    if dry_run or not auth:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        path = OUT_DIR / f"{datetime.now():%Y%m%d_%H%M%S}_{_safe_name(run_label)}.eml"
        path.write_bytes(bytes(msg))
        return f"DRYRUN:{path}"

    ctx = ssl.create_default_context()
    with smtplib.SMTP_SSL(mail["smtp_host"], mail["smtp_port"], context=ctx, timeout=30) as s:
        s.login(_setting("MAIL_FROM", mail["from"]), auth)
        s.send_message(msg)
    return f"SENT:{_to(mail)}"
