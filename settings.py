# -*- coding: utf-8 -*-
"""配置加载：基础 config.json + 画像覆盖 profiles/<name>/config.json。

一个「画像」= 一份求职画像：简历提炼、搜索关键词、排除规则、打分权重、收件人。
默认不传画像时用基础 config.json（原金融数字化方向）；传了 --profile 之后，
画像文件里的键会递归覆盖基础配置，未覆盖的键（如公司规模提示、金融科技白名单）
继续沿用基础配置，避免两份配置各自漂移。

运行哪个画像由 --profile 或环境变量 JOB_PROFILE 决定。画像之间的运行时文件
（去重库、日志、邮件预览）各自隔离，两份日报互不干扰。
"""
from __future__ import annotations

import json
import os
from pathlib import Path

BASE = Path(__file__).resolve().parent


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _merge(base, override):
    """递归合并：dict 逐键合并；list / 标量整体替换（关键词表必须能整体覆盖）。"""
    if isinstance(base, dict) and isinstance(override, dict):
        out = dict(base)
        for key, value in override.items():
            out[key] = _merge(base[key], value) if key in base else value
        return out
    return override


def active_profile() -> str:
    return (os.environ.get("JOB_PROFILE") or "").strip()


def load_config(profile: str | None = None) -> dict:
    name = active_profile() if profile is None else (profile or "").strip()
    cfg = _read_json(BASE / "config.json")
    if not name:
        return cfg
    path = BASE / "profiles" / name / "config.json"
    if not path.exists():
        raise RuntimeError(f"画像不存在：{path}（应形如 profiles/<name>/config.json）")
    cfg = _merge(cfg, _read_json(path))
    cfg["_profile"] = name
    return cfg


def profile_paths(name: str) -> dict[str, Path]:
    """画像的运行时文件目录，互相之间不共享。"""
    return {
        "db": BASE / f"jobs-{name}.db",
        "log": BASE / "logs" / name,
        "outbox": BASE / "outbox" / name,
    }


def apply_profile_env(name: str) -> None:
    """把画像的运行时路径写进环境变量（store / mailer / run_daily 都读这些）。

    必须在 import store / match_engine 之前调用，因为它们是在 import 阶段
    读取路径和配置的。
    """
    if not name:
        return
    paths = profile_paths(name)
    os.environ["JOB_PROFILE"] = name
    os.environ.setdefault("JOB_DB_PATH", str(paths["db"]))
    os.environ.setdefault("JOB_LOG_DIR", str(paths["log"]))
    os.environ.setdefault("JOB_OUT_DIR", str(paths["outbox"]))
