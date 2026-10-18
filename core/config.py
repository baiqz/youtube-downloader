"""配置与路径。所有可调参数集中在 DEFAULTS，前端通过 /api/settings 读写。"""
from __future__ import annotations

import json
import os
import shutil
import sys
import threading

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT, "data")
OUT_DIR = os.path.join(ROOT, "downloads")
TMP_DIR = os.path.join(ROOT, "data", "tmp")
SETTINGS_FILE = os.path.join(DATA_DIR, "settings.json")
COOKIES_FILE = os.path.join(DATA_DIR, "cookies.txt")
EDGE_PROFILE = os.path.join(DATA_DIR, "edge-profile")
AUTH_STATE = os.path.join(DATA_DIR, "auth_state.json")

_lock = threading.RLock()


def _find(exe: str, cands: list[str]) -> str:
    """按候选路径找可执行文件，最后回退 PATH。"""
    for c in cands:
        if c and os.path.isfile(c):
            return c
    return shutil.which(exe) or ""


def find_ffmpeg() -> str:
    exe = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
    return _find("ffmpeg", [
        os.environ.get("FFMPEG_DIR", "") and os.path.join(os.environ["FFMPEG_DIR"], exe),
        os.path.join(ROOT, "bin", exe),
        r"C:\Users\Administrator\.workbuddy\binaries\ffmpeg\ffmpeg.exe",
        "/usr/local/bin/ffmpeg",
        "/opt/homebrew/bin/ffmpeg",
    ])


def find_node() -> str:
    return _find("node", [
        r"C:\Users\Administrator\.workbuddy\binaries\node\versions\22.22.2-6\node.exe",
        r"C:\Program Files\nodejs\node.exe",
    ])


def find_browser() -> str:
    return _find("msedge", [
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    ])


DEFAULTS: dict = {
    # 拉 YouTube 需要能出海的代理；留空 = 直连。首次运行在设置里填自己的地址
    "proxy": "",
    # 下载保存目录（绝对路径，空则用内置 downloads/）
    "out_dir": "",
    # 同时下载数
    "concurrency": 2,
    # 解析/下载时附加的 yt-dlp 参数（高级）
    "extra_args": "",
    # 下载完自动在文件夹里高亮该文件
    "open_folder_on_done": False,
    "ffmpeg": find_ffmpeg(),
    "node": find_node(),
    "browser": find_browser(),
}


def ensure_dirs() -> None:
    for d in (DATA_DIR, OUT_DIR, TMP_DIR):
        os.makedirs(d, exist_ok=True)


def load() -> dict:
    ensure_dirs()
    with _lock:
        data = dict(DEFAULTS)
        # 每次启动重新探测，避免换了机器后路径失效
        data["ffmpeg"] = find_ffmpeg() or data["ffmpeg"]
        data["node"] = find_node() or data["node"]
        data["browser"] = find_browser() or data["browser"]
        if os.path.isfile(SETTINGS_FILE):
            try:
                with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                    saved = json.load(f)
                if isinstance(saved, dict):
                    for k, v in saved.items():
                        if k in DEFAULTS:
                            data[k] = v
            except Exception:
                pass
        return data


def save(patch: dict) -> dict:
    ensure_dirs()
    with _lock:
        cur = load()
        for k, v in (patch or {}).items():
            if k in DEFAULTS:
                cur[k] = v
        cur["ffmpeg"] = find_ffmpeg() or cur.get("ffmpeg", "")
        cur["node"] = find_node() or cur.get("node", "")
        cur["browser"] = find_browser() or cur.get("browser", "")
        # 只持久化用户可改的项，自动探测的不落盘
        persist = {k: cur[k] for k in DEFAULTS if k not in ("ffmpeg", "node", "browser")}
        tmp = SETTINGS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(persist, f, ensure_ascii=False, indent=2)
        os.replace(tmp, SETTINGS_FILE)
        return cur


def out_dir() -> str:
    d = (load().get("out_dir") or "").strip()
    if not d:
        return OUT_DIR
    d = os.path.expanduser(d)
    os.makedirs(d, exist_ok=True)
    return d


def cookie_args() -> list[str]:
    """cookies 存在就带上。没有也可以跑，只是遇到风控会失败。"""
    if os.path.isfile(COOKIES_FILE) and os.path.getsize(COOKIES_FILE) > 50:
        return ["--cookies", COOKIES_FILE]
    return []


def proxy() -> str:
    return (load().get("proxy") or "").strip()
