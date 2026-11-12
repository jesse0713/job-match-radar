# -*- coding: utf-8 -*-
"""BOSS 直聘登录态体检 / 重新扫码登录（只处理 BOSS，不再管其他招聘站）。

采集脚本报「0 条 / 登录态失效」时先跑这个：

    python login.py --check     # 只体检：不动窗口、不扫码、不打接口
    python login.py             # 需要时把窗口抬到眼前，等你扫码，成功后保存并关窗口
    python login.py --keep-open # 登录成功后窗口留着，自己再翻翻
    python login.py --timeout 600

判定逻辑和采集脚本完全一致：打开一次真实搜索页，看它是正常出岗位、
被重定向到 /web/user/（掉登录），还是卡在安全验证。掉登录时不会再请求
wapi 接口——之前正是「掉登录后还直接打开 joblist.json」把账号越推越远。

登录态存在 chrome-profile/（等同账号凭据，别外发、别提交）。
"""
from __future__ import annotations

import argparse
import json
import sys
import time

import collect

STATE_CN = {
    "ok": "已登录，搜索正常",
    "login": "需要扫码登录",
    "security": "环境校验中（__zp_stoken__ 刷新）",
    "verify": "卡在安全验证",
    "blank": "页面白屏未渲染",
    "err": "页面异常",
}


def bring_onscreen(page) -> None:
    """窗口挪回主屏并置前，避免用户找不到二维码。"""
    for action in (
        lambda: page.set.window.normal(),
        lambda: page.set.window.location(80, 40),
        lambda: page.set.window.size(1400, 920),
    ):
        try:
            action()
        except Exception:
            pass
    try:
        page.latest_tab.run_cdp("Page.bringToFront")
    except Exception:
        pass


def _shot(tab, name: str = "login_qr") -> str:
    """把当前窗口截图存到 outbox/，窗口被挡住时也能看到二维码。"""
    try:
        out = collect.BASE / "outbox"
        out.mkdir(exist_ok=True)
        path = tab.get_screenshot(path=str(out), name=name)
        return str(path or "")
    except Exception:
        return ""


def check(page) -> int:
    st = collect.login_state(page)
    print(f"BOSS：{STATE_CN.get(st['state'], st['state'])}")
    print(f"  当前页面：{st.get('url', '')}")
    print(f"  岗位卡片：{st.get('cards', 0)} 张")
    last = collect.last_login_ok()
    if last:
        print(f"  上次搜索成功：{last}")

    ok = st["state"] == "ok"
    if ok:
        try:
            jobs = collect.search(page, collect.PROBE_KEYWORD)
            print(f"  接口校验：{len(jobs)} 条（关键词「{collect.PROBE_KEYWORD}」）")
        except collect.BossLoginRequired as e:
            ok = False
            print(f"  接口校验失败：登录态失效（{e}）")
        except collect.BossBlocked as e:
            print(f"  接口校验：能登录，但搜索被风控挡了（code={e.code} {e.message}）。")
            print("  这不是扫码问题；等 30–60 分钟或换网络出口再跑，脚本会自动退避。")
        except collect.BossVerifyRequired as e:
            ok = False
            print(f"  接口校验失败：环境校验 / 安全验证未完成（{e}）")

    print("\n" + json.dumps(st, ensure_ascii=False, indent=2))
    if st["state"] == "security":
        print("\nBOSS 正在刷新 __zp_stoken__（环境校验页）。等 1–2 分钟再跑 --check；"
              "这不是掉登录，不需要扫码。")
        return 3
    if st["state"] == "verify":
        print("\n当前卡在安全验证，请在已打开的窗口里完成滑块后再跑 --check。")
        return 3
    if not ok:
        print("\n处理：运行 python login.py 扫码；若显示「安全验证」，在弹出的窗口里拖一下滑块。")
        return 2
    return 0


def relogin(page, timeout: int, keep_open: bool) -> int:
    # 已经登录就别再弹二维码：先做一次只读体检
    st = collect.login_state(page)
    if st["state"] == "ok":
        print("BOSS 登录态正常，不需要扫码。")
        print(f"  当前页面：{st.get('url', '')}")
        try:
            jobs = collect.search(page, collect.PROBE_KEYWORD)
            print(f"  搜索校验：{len(jobs)} 条")
        except collect.BossBlocked as e:
            print(f"  搜索接口暂时风控（code={e.code} {e.message}），登录本身没问题；"
                  "脚本会自动退避，稍后再跑即可。")
            if not keep_open:
                collect.close_browser(page)
                print("窗口已正常关闭。")
            return 0
        except (collect.BossLoginRequired, collect.BossVerifyRequired) as e:
            print(f"  搜索校验未通过：{e}，继续走扫码流程。")
        else:
            collect.mark_login_ok()
            if not keep_open:
                collect.close_browser(page)
                print("窗口已正常关闭。")
            return 0

    if st["state"] == "security":
        print("\nBOSS 正在刷新 __zp_stoken__（环境校验页），先等 1–2 分钟再跑 python login.py；"
              "这不是掉登录。")
        if not keep_open:
            collect.close_browser(page)
        return 3
    if st["state"] == "verify":
        print("\n当前卡在安全验证，请在已打开的窗口里完成滑块后再跑 python login.py --check。")
        return 3

    tab = page.latest_tab
    tab.get(collect.BOSS_LOGIN_URL)
    time.sleep(2.5)

    print("\n请在弹出的 Chrome 窗口里用 BOSS APP 扫码登录。")
    print("  · 扫码后不用管这个窗口，检测到登录成功会自动保存并关闭。")
    print("  · 如果出现滑块 / 验证码，直接在窗口里完成即可。")
    shot = _shot(tab)
    if shot:
        print(f"  · 二维码截图：{shot}")

    deadline = time.time() + timeout
    last_beat = 0.0
    verified = False
    jobs: list[dict] = []

    while time.time() < deadline:
        time.sleep(3)
        try:
            url = tab.url or ""
        except Exception:
            continue

        # BOSS 每天会刷新 __zp_stoken__：搜索页会先跳到 security.html，
        # 脚本算出新 token 再跳回。先等它自己完成，不要当成掉登录。
        if "security.html" in url:
            if time.time() - last_beat > 20:
                print(f"[{time.strftime('%H:%M:%S')}] BOSS 环境校验中（会自动完成，不要关窗口）")
                last_beat = time.time()
            st = collect.login_state(page, tab=tab)
            if st["state"] == "ok":
                try:
                    jobs = collect.search(page, collect.PROBE_KEYWORD)
                    verified = True
                except collect.BossBlocked as e:
                    print(f"\n登录成功，但搜索接口仍返回风控（code={e.code} {e.message}）。")
                    print("可以等 30–60 分钟再跑；脚本会自动重试和退避。")
                    verified = True
                except (collect.BossLoginRequired, collect.BossVerifyRequired):
                    continue
                break
            continue

        if "verify" in url:
            if time.time() - last_beat > 20:
                print(f"[{time.strftime('%H:%M:%S')}] 卡在安全验证，请在窗口里拖动滑块")
                last_beat = time.time()
            continue

        if "/web/user" in url or "passport" in url:
            if time.time() - last_beat > 20:
                print(f"[{time.strftime('%H:%M:%S')}] 等待扫码…")
                last_beat = time.time()
            continue

        # URL 已经离开登录页，说明扫码 / 验证已完成，做一次真实搜索校验
        st = collect.login_state(page, tab=tab)
        if st["state"] == "ok":
            try:
                jobs = collect.search(page, collect.PROBE_KEYWORD)
                verified = True
            except (collect.BossLoginRequired, collect.BossVerifyRequired):
                # 登录其实还没完成，回到登录页继续等
                tab.get(collect.BOSS_LOGIN_URL)
                time.sleep(2)
                continue
            except collect.BossBlocked as e:
                print(f"\n登录成功，但搜索接口被风控挡了（code={e.code} {e.message}）。")
                print("这不是扫码问题；等 30–60 分钟或换网络再跑采集，脚本会自动退避。")
                verified = True
            break

        print(f"[{time.strftime('%H:%M:%S')}] 登录页已跳转，但搜索页仍要求登录，重新打开二维码")
        tab.get(collect.BOSS_LOGIN_URL)
        time.sleep(2)
        _shot(tab)

    if not verified:
        print(f"\n等了 {timeout} 秒仍未完成登录。窗口先留着，扫完可以再跑 python login.py --check 复查。")
        return 1

    collect.mark_login_ok()
    print(f"\n登录成功，搜索校验：{len(jobs)} 条。")
    if keep_open:
        print("窗口保持打开（--keep-open）。")
    else:
        collect.close_browser(page)
        print("登录态已保存到 chrome-profile，窗口已正常关闭。")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="只体检，不扫码、不关窗口")
    ap.add_argument("--keep-open", action="store_true", help="成功后不关窗口")
    ap.add_argument("--timeout", type=int, default=300, help="等扫码的最长秒数")
    args = ap.parse_args()

    collect.ensure_browser(onscreen=not args.check)
    page = collect.attach()

    if not args.check:
        bring_onscreen(page)
        return relogin(page, args.timeout, args.keep_open)
    return check(page)


if __name__ == "__main__":
    sys.exit(main())
