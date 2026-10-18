"""登录态管理：启动 Edge 登录向导、读状态、判断 cookies 是否可用。"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import threading
import time

from . import config

_LOCK = threading.RLock()
_proc: subprocess.Popen | None = None
_LOG = os.path.join(config.DATA_DIR, "login.log")

AUTH_NAMES = {"SID", "SAPISID", "__Secure-3PSID", "__Secure-1PSID", "HSID", "SSID", "LOGIN_INFO"}


def _free_port(start: int = 9411) -> int:
    for p in range(start, start + 40):
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", p))
                return p
            except OSError:
                continue
    return start


def cookie_stats() -> dict:
    """统计 cookies.txt：条数、是否含登录态、文件年龄。"""
    f = config.COOKIES_FILE
    if not os.path.isfile(f) or os.path.getsize(f) < 50:
        return {"exists": False, "count": 0, "logged_in": False, "age_hours": None}
    count = 0
    logged = False
    try:
        with open(f, "r", encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                if not line.strip() or line.startswith("# ") or line.strip() == "# Netscape HTTP Cookie File":
                    continue
                parts = line.rstrip("\n").split("\t")
                if len(parts) < 7:
                    continue
                count += 1
                if parts[5] in AUTH_NAMES and parts[6].strip():
                    logged = True
    except OSError:
        pass
    age = (time.time() - os.path.getmtime(f)) / 3600.0
    return {"exists": True, "count": count, "logged_in": logged, "age_hours": round(age, 1)}


def read_state() -> dict:
    if not os.path.isfile(config.AUTH_STATE):
        return {"state": "idle", "message": ""}
    try:
        with open(config.AUTH_STATE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"state": "idle", "message": ""}


def _write_state(state: str, message: str) -> None:
    try:
        os.makedirs(config.DATA_DIR, exist_ok=True)
        with open(config.AUTH_STATE, "w", encoding="utf-8") as f:
            json.dump({"state": state, "message": message, "updated": time.time()}, f, ensure_ascii=False)
    except OSError:
        pass


def running() -> bool:
    with _LOCK:
        return _proc is not None and _proc.poll() is None


def start_login(headless: bool = False, timeout: int = 900) -> dict:
    """拉起登录向导。已经跑着就直接返回当前状态。"""
    global _proc
    with _LOCK:
        if running():
            return {"ok": True, "already": True, **read_state()}

        cfg = config.load()
        browser = cfg.get("browser") or ""
        node = cfg.get("node") or ""
        if not browser or not os.path.isfile(browser):
            raise RuntimeError("没找到 Edge/Chrome，请先安装，或手动把 cookies.txt 放到 data 目录")
        if not node or not os.path.isfile(node):
            raise RuntimeError("没找到 Node.js 运行时（登录向导需要它读浏览器 cookie）")

        os.makedirs(config.DATA_DIR, exist_ok=True)
        port = _free_port()
        script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "edge_login.mjs")
        cmd = [node, script,
               "--browser", browser,
               "--profile", config.EDGE_PROFILE,
               "--out", config.COOKIES_FILE,
               "--state", config.AUTH_STATE,
               "--port", str(port),
               "--timeout", str(timeout)]
        if cfg.get("proxy"):
            cmd += ["--proxy", cfg["proxy"]]
        if headless:
            cmd += ["--headless"]

        _write_state("starting", "正在启动浏览器…")
        logf = open(_LOG, "w", encoding="utf-8", errors="ignore")
        _proc = subprocess.Popen(cmd, stdout=logf, stderr=subprocess.STDOUT,
                                 creationflags=0x08000000 if os.name == "nt" else 0)
        return {"ok": True, "already": False, **read_state()}


def status() -> dict:
    st = read_state()
    alive = running()
    # 进程已经退出但状态还停在等待中 —— 多为用户关窗
    if not alive and st.get("state") in ("starting", "waiting"):
        st = {"state": "closed", "message": "登录窗口已关闭"}
    stats = cookie_stats()
    return {
        "process_alive": alive,
        "state": st.get("state", "idle"),
        "message": st.get("message", ""),
        "cookies": stats,
    }


def cancel_login() -> dict:
    global _proc
    with _LOCK:
        if _proc and _proc.poll() is None:
            try:
                if os.name == "nt":
                    subprocess.run(["taskkill", "/F", "/T", "/PID", str(_proc.pid)],
                                   capture_output=True, creationflags=0x08000000)
                else:
                    _proc.kill()
            except Exception:
                pass
        _write_state("idle", "已取消")
        return {"ok": True}


def clear_cookies() -> dict:
    """断开连接：删掉 cookies 文件，登录态仍留在独立浏览器 profile 里。"""
    try:
        if os.path.isfile(config.COOKIES_FILE):
            os.remove(config.COOKIES_FILE)
    except OSError:
        pass
    _write_state("idle", "已断开连接")
    return {"ok": True}
