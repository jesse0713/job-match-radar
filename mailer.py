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
OUT_DIR = BASE / "outbox"


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


def load_auth() -> str | None:
    return _setting("QQ_SMTP_AUTH") or None


def _esc(value: object) -> str:
    """岗位标题/公司名来自第三方站点，写进 HTML 前必须转义，避免邮件被注入标签。"""
    return html.escape(str(value or ""), quote=True)


def _safe_url(url: str) -> str:
    """只允许 http(s) 链接，挡住 javascript: 之类的伪协议。"""
    u = (url or "").strip()
    return _esc(u) if u.lower().startswith(("http://", "https://")) else "#"


def render_html(jobs: list[dict], cfg: dict, run_label: str) -> str:
    rows = []
    for i, j in enumerate(jobs, 1):
        salary = j.get("salary_text") or "面议"
        reason = "；".join(j.get("reasons", [])[:2])
        rows.append(
            f"""
        <tr>
          <td style="padding:10px 8px;border-bottom:1px solid #eee;color:#888;font-size:12px;text-align:center;">{i}</td>
          <td style="padding:10px 8px;border-bottom:1px solid #eee;">
            <a href="{_safe_url(j.get('url', ''))}" style="color:#1a56db;text-decoration:none;font-weight:600;font-size:15px;">{_esc(j.get('title'))}</a>
            <div style="color:#999;font-size:12px;margin-top:4px;">{_esc(reason)}</div>
          </td>
          <td style="padding:10px 8px;border-bottom:1px solid #eee;font-size:14px;">{_esc(j.get('company'))}</td>
          <td style="padding:10px 8px;border-bottom:1px solid #eee;color:#c0392b;font-weight:600;font-size:14px;white-space:nowrap;">{_esc(salary)}</td>
          <td style="padding:10px 8px;border-bottom:1px solid #eee;color:#555;font-size:13px;">{_esc(j.get('city'))}</td>
        </tr>"""
        )

    table = f"""
    <table style="border-collapse:collapse;width:100%;font-family:'Microsoft YaHei',Arial,sans-serif;">
      <thead>
        <tr style="background:#f5f7fa;">
          <th style="padding:8px;font-size:12px;color:#666;">#</th>
          <th style="padding:8px;text-align:left;font-size:12px;color:#666;">岗位</th>
          <th style="padding:8px;text-align:left;font-size:12px;color:#666;">公司</th>
          <th style="padding:8px;text-align:left;font-size:12px;color:#666;">薪资</th>
          <th style="padding:8px;text-align:left;font-size:12px;color:#666;">城市</th>
        </tr>
      </thead>
      <tbody>{''.join(rows)}</tbody>
    </table>"""

    profile = cfg.get("candidate_profile", {}) or {}
    basis = profile.get("summary", "金融业务 + 数字化复合履历")
    body = f"""
    <div style="font-family:'Microsoft YaHei',Arial,sans-serif;max-width:860px;margin:0 auto;color:#222;">
      <h2 style="font-size:18px;margin:0 0 4px;">{_esc(run_label)}｜今日匹配岗位 {len(jobs)} 条</h2>
      <p style="color:#888;font-size:12px;margin:0 0 16px;">
        匹配依据：{_esc(basis)}；已排除保险公司主体、第三方财富管理、销售岗、小规模金融公司。
        排序：简历匹配度 → 公司规模 → 发布时间。
      </p>
      {table if jobs else '<p style="color:#888;">本次没有新增匹配岗位。</p>'}
      <p style="color:#aaa;font-size:12px;margin-top:18px;">
        由岗位匹配系统自动生成 · {datetime.now():%Y-%m-%d %H:%M}
      </p>
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
    msg["From"] = formataddr(("岗位匹配助手", _setting("MAIL_FROM", mail["from"])))
    msg["To"] = _setting("MAIL_TO", mail["to"])
    msg["Subject"] = f"{mail['subject_prefix']}{run_label}｜需要人工补登"
    msg.set_content(message)
    msg.add_alternative(html, subtype="html")

    auth = load_auth()
    if dry_run or not auth:
        OUT_DIR.mkdir(exist_ok=True)
        path = OUT_DIR / f"{datetime.now():%Y%m%d_%H%M%S}_{_safe_name(run_label)}_ALERT.eml"
        path.write_bytes(bytes(msg))
        return f"DRYRUN:{path}"

    ctx = ssl.create_default_context()
    with smtplib.SMTP_SSL(mail["smtp_host"], mail["smtp_port"], context=ctx, timeout=30) as s:
        s.login(_setting("MAIL_FROM", mail["from"]), auth)
        s.send_message(msg)
    return f"SENT:{_setting('MAIL_TO', mail['to'])}"


def send(jobs: list[dict], cfg: dict, run_label: str, dry_run: bool = False) -> str:
    mail = cfg["mail"]
    html = render_html(jobs, cfg, run_label)
    subject = f"{mail['subject_prefix']}{run_label}｜{len(jobs)} 条匹配岗位"

    msg = EmailMessage()
    msg["From"] = formataddr(("岗位匹配助手", _setting("MAIL_FROM", mail["from"])))
    msg["To"] = _setting("MAIL_TO", mail["to"])
    msg["Subject"] = subject
    msg.set_content("请使用支持 HTML 的邮件客户端查看。")
    msg.add_alternative(html, subtype="html")

    auth = load_auth()
    if dry_run or not auth:
        OUT_DIR.mkdir(exist_ok=True)
        path = OUT_DIR / f"{datetime.now():%Y%m%d_%H%M%S}_{_safe_name(run_label)}.eml"
        path.write_bytes(bytes(msg))
        return f"DRYRUN:{path}"

    ctx = ssl.create_default_context()
    with smtplib.SMTP_SSL(mail["smtp_host"], mail["smtp_port"], context=ctx, timeout=30) as s:
        s.login(_setting("MAIL_FROM", mail["from"]), auth)
        s.send_message(msg)
    return f"SENT:{_setting('MAIL_TO', mail['to'])}"
