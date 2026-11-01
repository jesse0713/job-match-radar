# -*- coding: utf-8 -*-
"""生成邮件预览：把 demo 岗位渲染成 HTML 并截图，供人工确认版式。"""
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

import match_engine
import mailer
from run_daily import DEMO_JOBS, normalize

BASE = Path(__file__).resolve().parent
CFG = match_engine.CFG

jobs = [normalize(j) for j in DEMO_JOBS]
ranked = match_engine.rank(jobs)
html = mailer.render_html(ranked, CFG, "午间 11:30")
doc = f"""<!doctype html><html><head><meta charset="utf-8"><title>邮件预览</title></head>
<body style="margin:0;padding:24px;background:#f2f4f7;">{html}</body></html>"""
p = BASE / "outbox" / "preview.html"
p.parent.mkdir(exist_ok=True)
p.write_text(doc, encoding="utf-8")
print(p)

# 顺带截图
try:
    from DrissionPage import ChromiumPage, ChromiumOptions
    page = ChromiumPage(ChromiumOptions().set_local_port(9333))
    page.get(p.as_uri())
    page.wait.load_start()
    import time
    time.sleep(1.2)
    shot = BASE / "outbox" / "preview.png"
    page.get_screenshot(path=str(BASE / "outbox"), name="preview")
    print("shot ok")
except Exception as e:
    print("shot fail:", e)
