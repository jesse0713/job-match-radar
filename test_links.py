# -*- coding: utf-8 -*-
"""岗位链接、采集解析与邮件渲染的回归测试。

运行：
    python -m unittest test_links -v
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import collect
import links
import mailer

CFG = {
    "candidate_profile": {"summary": "多年金融业务 + 数字化复合履历"},
    "mail": {"subject_prefix": "【岗位匹配】"},
}

ROOT = Path(__file__).resolve().parent
SKIP_DIRS = {
    ".git", ".smartlink-worktree", "chrome-profile", "logs", "outbox", "__pycache__",
}
TEXT_SUFFIXES = {".py", ".json", ".md", ".ps1", ".txt", ".html"}
# 拆开写，避免守卫测试把自己的源码也算成残留
BANNED_TERMS = ("lie" + "pin", "猎" + "聘")


class TestBossLinks(unittest.TestCase):
    def test_boss_deep_link(self):
        job = {
            "source": "boss",
            "raw_id": "bd152cb9de9131791HB43d6_EFtQ",
            "url": "https://www.zhipin.com/job_detail/bd152cb9de9131791HB43d6_EFtQ.html",
        }
        web, app = links.build(job)
        self.assertEqual(web, "https://m.zhipin.com/job_detail/bd152cb9de9131791HB43d6_EFtQ.html")
        # 邮件按钮必须是 https 跳转页，否则客户端会把 bosszp:// 直接抹掉
        self.assertTrue(app.startswith("https://d.bosszhipin.com/dl/openprotocol?"))
        self.assertIn("openTag=1", app)
        inner = parse_qs(urlparse(app).query)["url"][0]
        u = urlparse(inner)
        self.assertEqual((u.scheme, u.netloc, u.path), ("bosszp", "bosszhipin.app", "/openwith"))
        q = parse_qs(u.query)
        self.assertEqual(q["type"], ["newwebview"])
        self.assertEqual(q["fullScreen"], ["1"])
        self.assertEqual(
            q["url"][0], "https://m.zhipin.com/job_detail/bd152cb9de9131791HB43d6_EFtQ.html"
        )
        # 未验证过的 jobview 类型不能出现在链接里，避免 APP 收到后被忽略
        self.assertNotIn("jobview", inner)

    def test_boss_dom_fallback_parses_id_from_url(self):
        job = {
            "source": "boss",
            "raw_id": "",
            "url": "https://www.zhipin.com/job_detail/597bf1d7127308471HJ529y5FFNZ.html?securityId=x",
        }
        web, app = links.build(job)
        self.assertIn("597bf1d7127308471HJ529y5FFNZ", web)
        inner = parse_qs(urlparse(app).query)["url"][0]
        self.assertIn("type=newwebview", inner)

    def test_boss_app_link_goes_through_official_bridge(self):
        job = {
            "source": "boss",
            "raw_id": "bd152cb9de9131791HB43d6_EFtQ",
            "encrypt_boss_id": "boss123",
            "title": "金融行业解决方案总监",
            "url": "https://www.zhipin.com/job_detail/bd152cb9de9131791HB43d6_EFtQ.html",
        }
        links.apply(job, "https://example.com/smartlink.html")
        self.assertTrue(job["native_url"].startswith("https://d.bosszhipin.com/dl/openprotocol?"))
        self.assertIn("openTag=1", job["native_url"])
        self.assertTrue(job["native_scheme"].startswith("bosszp://bosszhipin.app/openwith"))
        inner = parse_qs(urlparse(job["native_url"]).query)["url"][0]
        # jobview 在外部传参会被 APP 判「数据异常」，所以默认仍走 WebView
        self.assertIn("type=newwebview", inner)
        self.assertIn("bd152cb9de9131791HB43d6_EFtQ", inner)
        self.assertNotIn("jobview", inner)

    def test_jobview_helper_is_not_used(self):
        """记录结论：jobview 只认 APP 内部 ID，外部拿不到，所以不能进邮件。"""
        job = {
            "source": "boss",
            "raw_id": "bd152cb9de9131791HB43d6_EFtQ",
            "encrypt_boss_id": "boss123",
            "url": "https://www.zhipin.com/job_detail/bd152cb9de9131791HB43d6_EFtQ.html",
        }
        self.assertIn("type=jobview", links._boss_jobview(job, job["raw_id"]))
        links.apply(job, "")
        self.assertNotIn("jobview", job["native_scheme"])

    def test_apply_sets_smart_url(self):
        job = {
            "source": "boss",
            "raw_id": "bd152cb9de9131791HB43d6_EFtQ",
            "title": "金融行业解决方案总监",
            "url": "https://www.zhipin.com/job_detail/bd152cb9de9131791HB43d6_EFtQ.html",
        }
        links.apply(job, "https://example.com/smartlink.html")
        self.assertTrue(job["smart_url"].startswith("https://example.com/smartlink.html?s=boss"))
        self.assertTrue(job["app_url"].startswith("https://d.bosszhipin.com/dl/openprotocol?"))

    def test_boss_h5_share_url(self):
        job = {
            "source": "boss",
            "raw_id": "bd152cb9de9131791HB43d6_EFtQ",
            "url": "https://www.zhipin.com/job_detail/bd152cb9de9131791HB43d6_EFtQ.html",
        }
        links.apply(job)
        self.assertEqual(
            job["boss_h5_url"],
            "https://m.zhipin.com/mpa/html/weijd/weijd-job/bd152cb9de9131791HB43d6_EFtQ/",
        )

    def test_smartlink_round_trip(self):
        base = "https://example.com/smartlink.html"
        smart = links.build_smartlink(
            base, "boss", "bd152cb9de9131791HB43d6_EFtQ",
            "https://m.zhipin.com/job_detail/bd152cb9de9131791HB43d6_EFtQ.html",
            "金融行业解决方案总监",
        )
        u = urlparse(smart)
        q = parse_qs(u.query)
        self.assertEqual(u.scheme, "https")
        self.assertEqual(q["s"], ["boss"])
        self.assertEqual(q["id"], ["bd152cb9de9131791HB43d6_EFtQ"])
        self.assertEqual(
            q["u"][0], "https://m.zhipin.com/job_detail/bd152cb9de9131791HB43d6_EFtQ.html"
        )
        self.assertEqual(q["t"], ["金融行业解决方案总监"])

    def test_invalid_ids_are_dropped(self):
        web, app = links.build(
            {"source": "boss", "raw_id": "javascript:alert(1)", "url": "javascript:alert(1)"}
        )
        self.assertEqual((web, app), ("", ""))

    def test_unknown_source_falls_back_to_original(self):
        job = {"source": "demo", "raw_id": "d1", "url": "https://example.com/1"}
        web, app = links.build(job)
        self.assertEqual(web, "https://example.com/1")
        self.assertEqual(app, "")


class TestCollectParsing(unittest.TestCase):
    def test_search_url(self):
        url = collect.search_url("金融 解决方案", 2)
        u = urlparse(url)
        q = parse_qs(u.query)
        self.assertEqual(u.netloc, "www.zhipin.com")
        self.assertEqual(u.path, "/web/geek/jobs")
        self.assertEqual(q["query"], ["金融 解决方案"])
        self.assertEqual(q["city"], [collect.BOSS_CITY_ALL])
        self.assertEqual(q["page"], ["2"])

    def test_api_url_never_used_as_document(self):
        # 只拼接口地址给页面内 fetch 用；不是给 page.get() 打开
        url = collect.api_url("售前")
        self.assertTrue(url.startswith("/wapi/zpgeek/search/joblist.json"))
        self.assertIn("scene=1", url)

    def test_classify_page(self):
        self.assertEqual(collect.classify_page("https://www.zhipin.com/web/user/", "", 0), "login")
        self.assertEqual(collect.classify_page("https://passport.zhipin.com/x", "", 0), "login")
        self.assertEqual(collect.classify_page("https://www.zhipin.com/web/geek/job", "扫码登录", 0), "login")
        self.assertEqual(collect.classify_page("https://www.zhipin.com/web/geek/job", "安全验证", 0), "verify")
        # 页头的「登录/注册」不能作为掉登录依据：搜索页加载中也会短暂出现
        self.assertEqual(
            collect.classify_page(
                "https://www.zhipin.com/web/geek/jobs?query=x",
                "首页 | 职位 | 公司 | 登录/注册 | 热门职位: 售前技术支持 | " + "岗位" * 80,
                0,
            ),
            "ok",
        )
        self.assertEqual(
            collect.classify_page(
                "https://www.zhipin.com/web/passport/zp/security.html?code=37&name=e331459e",
                "正在加载中...", 0,
            ),
            "security",
        )
        self.assertEqual(collect.classify_page("https://www.zhipin.com/web/geek/job", "短", 0), "blank")
        self.assertEqual(collect.classify_page("https://www.zhipin.com/web/geek/job", "岗位列表" * 40, 0), "ok")
        self.assertEqual(collect.classify_page("https://www.zhipin.com/web/user/", "x" * 200, 30), "ok")

    def test_boss_json_carries_ids_for_native_link(self):
        job = collect._boss_from_json({
            "encryptJobId": "bd152cb9de9131791HB43d6_EFtQ",
            "encryptBossId": "boss123",
            "jobId": 123456,
            "bossId": 7890,
            "jobName": "金融行业解决方案总监",
            "brandName": "阿里云",
            "salaryDesc": "60-90K",
            "cityName": "北京",
            "brandScaleName": "10000人以上",
        })
        self.assertEqual(job["encrypt_boss_id"], "boss123")
        self.assertEqual(job["job_id_num"], "123456")
        self.assertEqual(job["boss_id_num"], "7890")
        self.assertEqual(job["scale_text"], "10000人以上")
        links.apply(job, "https://example.com/smartlink.html")
        self.assertTrue(job["native_url"])

    def test_job_from_card(self):
        job = collect.job_from_card({
            "title": "金融行业解决方案架构师",
            "company": "阿里云",
            "salary": "60-90K·16薪",
            "area": "北京·朝阳",
            "href": "https://www.zhipin.com/job_detail/597bf1d7127308471HJ529y5FFNZ.html?lid=x",
            "tags": ["银行", "大模型"],
            "comp_tags": ["互联网", "已上市", "10000人以上"],
        })
        self.assertEqual(job["source"], "boss")
        self.assertEqual(job["raw_id"], "597bf1d7127308471HJ529y5FFNZ")
        self.assertEqual(job["industry"], "互联网")
        self.assertEqual(job["scale_text"], "已上市 10000人以上")
        self.assertEqual(job["tags"], ["银行", "大模型"])
        self.assertTrue(job["url"].startswith("https://www.zhipin.com/job_detail/"))

    def test_salary_icon_font_is_decoded(self):
        # BOSS 搜索页的薪资用私有区字符混淆：\ue031=0 … \ue03a=9
        self.assertEqual(collect.decode_salary("\ue035\ue031-\ue038\ue031K"), "40-70K")
        self.assertEqual(collect.decode_salary("\ue039-\ue032\ue034K"), "8-13K")
        self.assertEqual(collect.decode_salary("面议"), "面议")
        job = collect.job_from_card({
            "title": "金融产品经理",
            "salary": "\ue035\ue031-\ue038\ue031K·14薪",
        })
        self.assertEqual(job["salary_text"], "40-70K·14薪")

    def test_handle_api_codes(self):
        self.assertRaises(collect.BossLoginRequired, collect._handle_api, {"code": 7}, "x")
        self.assertRaises(collect.BossBlocked, collect._handle_api,
                          {"code": 37, "message": "您的环境存在异常."}, "x")
        jobs = collect._handle_api(
            {"code": 0, "zpData": {"jobList": [
                {"encryptJobId": "abc123", "jobName": "金融解决方案", "brandName": "阿里云"}
            ]}},
            "x",
        )
        self.assertEqual(jobs[0]["raw_id"], "abc123")
        self.assertEqual(jobs[0]["company"], "阿里云")

    def test_card_without_title_is_rejected(self):
        job = collect.job_from_card({"title": "", "company": "x"})
        self.assertEqual(job["title"], "")


class TestMailer(unittest.TestCase):
    def test_score_and_app_link_are_rendered(self):
        job = {
            "source": "boss",
            "raw_id": "bd152cb9de9131791HB43d6_EFtQ",
            "title": "金融行业解决方案总监",
            "company": "阿里云",
            "salary_text": "60-90K·16薪",
            "city": "北京",
            "score": 86.0,
            "parts": {"role": 26, "domain": 30, "ai": 8, "seniority": 12, "salary": 10, "scale": 6},
            "reasons": ["岗位族：solution（命中「解决方案」）", "业务域：银行、金融"],
            "web_url": "https://m.zhipin.com/job_detail/bd152cb9de9131791HB43d6_EFtQ.html",
            "app_url": (
                "bosszp://bosszhipin.app/openwith?type=newwebview&fullScreen=1&url="
                "https%3A%2F%2Fm.zhipin.com%2Fjob_detail%2Fbd152cb9de9131791HB43d6_EFtQ.html"
            ),
        }
        html = mailer.render_html([job], CFG, "午间 12:30")
        self.assertIn("匹配度", html)
        self.assertIn(">86<", html)
        self.assertIn("最高 86 分", html)
        self.assertIn("BOSS直聘", html)
        self.assertIn("打开 BOSS APP 看 JD", html)
        self.assertIn("手机网页版", html)
        self.assertIn("构成：岗位 26 · 业务 30", html)

    def test_app_button_never_points_at_login_page(self):
        """回归：按钮曾直接指向 weijd-job 网页，点了只会开浏览器并要求登录。"""
        job = {
            "source": "boss",
            "raw_id": "bd152cb9de9131791HB43d6_EFtQ",
            "title": "金融行业解决方案总监",
            "company": "阿里云",
            "score": 86.0,
            "web_url": "https://m.zhipin.com/job_detail/bd152cb9de9131791HB43d6_EFtQ.html",
            "boss_h5_url": "https://m.zhipin.com/mpa/html/weijd/weijd-job/bd152cb9de9131791HB43d6_EFtQ/",
            "native_url": "https://d.bosszhipin.com/dl/openprotocol?openTag=1&url=bosszp%3A%2F%2Fbosszhipin.app%2Fopenwith",
            "smart_url": "https://example.com/smartlink.html?s=boss&id=x",
        }
        html = mailer.render_html([job], CFG, "午间 12:30")
        m = re.search(r'<a href="([^"]+)"[^>]*>打开 BOSS APP 看 JD</a>', html)
        self.assertIsNotNone(m, "邮件里应保留「打开 BOSS APP 看 JD」按钮")
        self.assertTrue(m.group(1).startswith("https://d.bosszhipin.com/dl/openprotocol?"))
        self.assertIn("openTag=1", m.group(1))
        self.assertNotIn("weijd-job", m.group(1))
        self.assertNotIn("example.com/smartlink.html", m.group(1))

    def test_scheme_is_first_button_for_system_mail(self):
        """系统邮件 / Gmail 能把 bosszp:// 交给系统，一次点击就进 APP。"""
        job = {
            "source": "boss",
            "raw_id": "bd152cb9de9131791HB43d6_EFtQ",
            "title": "金融行业解决方案总监",
            "company": "阿里云",
            "score": 86.0,
            "web_url": "https://m.zhipin.com/job_detail/bd152cb9de9131791HB43d6_EFtQ.html",
            "boss_h5_url": "https://m.zhipin.com/mpa/html/weijd/weijd-job/bd152cb9de9131791HB43d6_EFtQ/",
            "native_scheme": (
                "bosszp://bosszhipin.app/openwith?type=newwebview&fullScreen=1&url="
                "https%3A%2F%2Fm.zhipin.com%2Fjob_detail%2Fbd152cb9de9131791HB43d6_EFtQ.html"
            ),
            "native_url": "https://d.bosszhipin.com/dl/openprotocol?openTag=1&url=bosszp%3A%2F%2Fbosszhipin.app%2Fopenwith",
        }
        html = mailer.render_html([job], CFG, "午间 12:30")
        m = re.search(r'<a href="([^"]+)"[^>]*>打开 BOSS APP 看 JD</a>', html)
        self.assertIsNotNone(m)
        self.assertTrue(m.group(1).startswith("bosszp://bosszhipin.app/openwith"))
        # 改用系统「邮件」后只保留两个入口：进 APP、手机网页版
        self.assertNotIn("QQ 邮箱用这个", html)
        self.assertIn("手机网页版", html)

    def test_boss_h5_page_keeps_score_card(self):
        job = {
            "source": "boss",
            "raw_id": "bd152cb9de9131791HB43d6_EFtQ",
            "title": "金融行业解决方案总监",
            "company": "阿里云",
            "salary_text": "60-90K·16薪",
            "city": "北京",
            "score": 86.0,
            "parts": {"role": 26, "domain": 30, "ai": 8, "seniority": 12, "salary": 10, "scale": 6},
            "reasons": ["岗位族：solution（命中「解决方案」）"],
            "web_url": "https://m.zhipin.com/job_detail/bd152cb9de9131791HB43d6_EFtQ.html",
            "boss_h5_url": "https://m.zhipin.com/mpa/html/weijd/weijd-job/bd152cb9de9131791HB43d6_EFtQ/",
        }
        html = mailer.render_html([job], CFG, "午间 12:30")
        self.assertIn("m.zhipin.com/mpa/html/weijd/weijd-job/", html)
        self.assertIn("匹配度", html)
        self.assertIn(">86<", html)
        self.assertIn("构成：", html)

    def test_native_url_is_preferred_over_smart_link(self):
        job = {
            "source": "boss",
            "raw_id": "bd152cb9de9131791HB43d6_EFtQ",
            "title": "金融行业解决方案总监",
            "company": "阿里云",
            "score": 86.0,
            "web_url": "https://m.zhipin.com/job_detail/bd152cb9de9131791HB43d6_EFtQ.html",
            "smart_url": "https://example.com/smartlink.html?s=boss&id=x",
            "native_url": "https://d.bosszhipin.com/dl/openprotocol?url=bosszp%3A%2F%2Fbosszhipin.app%2Fopenwith",
        }
        html = mailer.render_html([job], CFG, "午间 12:30")
        self.assertIn("d.bosszhipin.com/dl/openprotocol", html)
        self.assertNotIn("example.com/smartlink.html", html)

    def test_javascript_href_is_blocked(self):
        job = {
            "source": "demo", "title": "x", "company": "y", "score": 50,
            "url": "javascript:alert(1)", "web_url": "javascript:alert(1)", "app_url": "",
        }
        html = mailer.render_html([job], CFG, "午间 12:30")
        self.assertNotIn("javascript:", html)
        self.assertIn('href="#"', html)

    def test_notes_and_empty_list(self):
        html = mailer.render_html([], CFG, "午间 12:30", notes=["BOSS 本次返回 0 条"])
        self.assertIn("数据源提示", html)
        self.assertIn("本次没有新增匹配岗位", html)


class TestSourcePurity(unittest.TestCase):
    def test_no_legacy_source_left(self):
        hits: list[str] = []
        for path in ROOT.rglob("*"):
            if not path.is_file() or path.suffix.lower() not in TEXT_SUFFIXES:
                continue
            if any(part in SKIP_DIRS for part in path.relative_to(ROOT).parts):
                continue
            if path.name in {"resume_text.txt"}:
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue
            low = text.lower()
            for term in BANNED_TERMS:
                if term.lower() in low:
                    hits.append(f"{path.relative_to(ROOT)}: {term}")
                    break
        self.assertEqual(hits, [], "发现旧站点残留：" + "; ".join(hits))


class TestLegalProfile(unittest.TestCase):
    """法务合规画像：配置合并、排除规则、城市白名单、打分。

    这几条是分支最容易踩坑的地方：基础画像「传统金融机构一律排除」「薪资 50K」
    直接套上去，会把该画像的目标岗位全部挡掉，而且不容易从日报里看出来。
    """

    def setUp(self):
        import match_engine
        import settings

        self.m = match_engine
        self.settings = settings
        self._orig = match_engine.CFG
        match_engine.CFG = settings.load_config("legal")

    def tearDown(self):
        self.m.CFG = self._orig

    def test_overrides_apply_and_base_keys_survive(self):
        cfg = self.m.CFG
        self.assertEqual(cfg["search"]["min_salary_k"], 25)
        self.assertIn("北京", cfg["search"]["city_allow"])
        self.assertEqual(cfg["mail"]["env_to_key"], "MAIL_TO_LEGAL")
        self.assertFalse(cfg["exclude"]["exclude_finance_entity"])
        # 没覆盖的基础键必须还在，否则白名单 / 规模提示会静默失效
        self.assertTrue(cfg["exclude"]["title_sales"])
        self.assertTrue(cfg["fintech_companies"])
        self.assertTrue(cfg["company_scale_hint"]["10000+"])

    def test_bank_entity_is_not_excluded(self):
        job = {"title": "法务经理", "company": "某某银行股份有限公司",
               "salary_text": "30-40K", "city": "北京"}
        self.assertIsNone(self.m.hard_exclude(job))

    def test_law_firm_is_excluded(self):
        job = {"title": "授薪律师", "company": "北京市某某律师事务所",
               "salary_text": "20-30K", "city": "北京"}
        self.assertIn("律师事务所", self.m.hard_exclude(job) or "")

    def test_city_whitelist(self):
        job = {"title": "法务总监", "company": "某消费金融公司",
               "salary_text": "35-50K", "city": "广州"}
        self.assertIn("城市不在范围", self.m.hard_exclude(job) or "")

    def test_chengdu_is_in_range(self):
        job = {"title": "法务经理", "company": "某消费金融公司",
               "salary_text": "30-45K", "city": "成都·高新区"}
        self.assertIsNone(self.m.hard_exclude(job))

    def test_junior_legal_titles_blocked(self):
        for title in ("法务专员", "律师助理", "合规专员"):
            job = {"title": title, "company": "某金融科技有限公司",
                   "salary_text": "30-50K", "city": "上海"}
            self.assertIsNotNone(self.m.hard_exclude(job), title)

    def test_on_point_job_scores_high(self):
        job = {"title": "法务总监（消费金融）", "company": "马上消费金融股份有限公司",
               "salary_text": "35-55K", "city": "北京·朝阳",
               "tags": ["不良资产", "合规"]}
        result = self.m.score_job(job)
        self.assertGreaterEqual(result["score"], 70, result)


if __name__ == "__main__":
    unittest.main()
