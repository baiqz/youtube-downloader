"""解析视频信息：跑一次 yt-dlp -J，转成前端要的结构。"""
from __future__ import annotations

import json
import os
import subprocess
import sys

from . import config, extract
from .downloader import base_cmd, friendly_error

_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def looks_like_url(s: str) -> bool:
    s = (s or "").strip()
    if not s:
        return False
    if s.startswith("http://") or s.startswith("https://"):
        return True
    # 允许直接粘 youtu.be/xxx 这种
    return s.startswith("youtu.be/") or s.startswith("www.youtube.com/")


def normalize_url(s: str) -> str:
    s = (s or "").strip()
    if not s:
        return s
    if s.startswith("http://") or s.startswith("https://"):
        return s
    if s.startswith("youtu.be/") or s.startswith("www.youtube.com/"):
        return "https://" + s
    # 纯视频 ID
    if len(s) == 11 and all(c.isalnum() or c in "-_" for c in s):
        return "https://www.youtube.com/watch?v=" + s
    return s


def fetch_info(url: str, cfg: dict | None = None, timeout: int = 150) -> dict:
    cfg = cfg or config.load()
    url = normalize_url(url)
    args = base_cmd(cfg) + ["--skip-download", "--playlist-items", "1", "-J", url]
    p = subprocess.run(args, capture_output=True, timeout=timeout, creationflags=_NO_WINDOW)
    out = p.stdout.decode("utf-8", "ignore").strip()
    err = p.stderr.decode("utf-8", "ignore").strip()

    if p.returncode != 0 or not out:
        raise RuntimeError(friendly_error(err or out))

    try:
        raw = json.loads(out)
    except json.JSONDecodeError:
        raise RuntimeError("解析结果格式异常，重试一次或换个链接")

    info = extract.normalize_info(raw)
    info["url"] = url
    return info
