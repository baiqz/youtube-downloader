"""YouTube 视频下载器 —— 本地服务。

只监听 127.0.0.1。启动后自动打开浏览器。
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import re
import subprocess
import sys
import threading
import time
import webbrowser

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from flask import Flask, Response, jsonify, request, send_file, send_from_directory

from core import auth, config, downloader, jobs, probe

app = Flask(__name__, static_folder=os.path.join(config.ROOT, "static"))
app.json.ensure_ascii = False

# 允许的 selector 字符集：格式 id 只可能是数字/字母，组合用 + / 连接
_SELECTOR_RE = re.compile(r"^[A-Za-z0-9_.\-+/]{1,80}$")

_WAITQ: collections.deque = collections.deque()
_SCHED_LOCK = threading.Lock()


# ----------------------------- 调度 -----------------------------

def _run_job(job, url: str, opt: dict) -> None:
    try:
        job.check_cancel()
        result = downloader.run(job, url, opt, config.load())
        job.finish(result)
        if config.load().get("open_folder_on_done"):
            downloader.reveal(result["path"])
    except jobs.Cancelled as e:
        job.fail(e)
    except Exception as e:
        job.fail(e)


def _scheduler() -> None:
    while True:
        time.sleep(0.4)
        try:
            cfg = config.load()
            limit = max(1, int(cfg.get("concurrency") or 1))
            running = [j for j in jobs.recent(50) if j.stage in ("downloading", "merging")]
            with _SCHED_LOCK:
                while _WAITQ and len(running) < limit:
                    job, url, opt = _WAITQ.popleft()
                    if job.cancelled:
                        continue
                    threading.Thread(target=_run_job, args=(job, url, opt), daemon=True).start()
                    running.append(job)
        except Exception:
            pass


threading.Thread(target=_scheduler, daemon=True).start()


# ----------------------------- 页面 -----------------------------

@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.get("/api/health")
def health():
    cfg = config.load()
    try:
        import yt_dlp
        ytv = yt_dlp.version.__version__
    except Exception:
        ytv = ""
    stats = auth.cookie_stats()
    return jsonify({
        "yt_dlp": ytv,
        "ffmpeg": bool(cfg.get("ffmpeg") and os.path.isfile(cfg["ffmpeg"])),
        "node": bool(cfg.get("node") and os.path.isfile(cfg["node"])),
        "browser": bool(cfg.get("browser") and os.path.isfile(cfg["browser"])),
        "proxy": cfg.get("proxy") or "",
        "out_dir": config.out_dir(),
        "cookies": stats,
        "version": "1.0.0",
    })


# ----------------------------- 设置 -----------------------------

@app.get("/api/settings")
def get_settings():
    cfg = config.load()
    cfg["out_dir_effective"] = config.out_dir()
    return jsonify(cfg)


@app.post("/api/settings")
def post_settings():
    patch = request.get_json(force=True, silent=True) or {}
    clean = {}
    for k, v in patch.items():
        if k not in config.DEFAULTS:
            continue
        if k == "concurrency":
            try:
                v = max(1, min(6, int(v)))
            except (TypeError, ValueError):
                continue
        clean[k] = v
    cfg = config.save(clean)
    cfg["out_dir_effective"] = config.out_dir()
    return jsonify(cfg)


# ----------------------------- 登录态 -----------------------------

@app.get("/api/auth/status")
def auth_status():
    return jsonify(auth.status())


@app.post("/api/auth/login")
def auth_login():
    body = request.get_json(force=True, silent=True) or {}
    headless = bool(body.get("headless"))
    try:
        return jsonify(auth.start_login(headless=headless))
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400


@app.post("/api/auth/cancel")
def auth_cancel():
    return jsonify(auth.cancel_login())


@app.post("/api/auth/clear")
def auth_clear():
    return jsonify(auth.clear_cookies())


# ----------------------------- 解析 -----------------------------

@app.post("/api/info")
def api_info():
    body = request.get_json(force=True, silent=True) or {}
    url = (body.get("url") or "").strip()
    if not url:
        return jsonify({"error": "先把 YouTube 链接粘进来"}), 400
    if not probe.looks_like_url(url):
        return jsonify({"error": "这不像一个 YouTube 链接"}), 400
    try:
        info = probe.fetch_info(url)
        return jsonify(info)
    except subprocess.TimeoutExpired:
        return jsonify({"error": "解析超时（网络太慢或被墙）。检查代理设置，或稍后重试。"}), 504
    except Exception as e:
        return jsonify({"error": str(e)}), 400


# ----------------------------- 下载 -----------------------------

@app.post("/api/download")
def api_download():
    body = request.get_json(force=True, silent=True) or {}
    url = probe.normalize_url((body.get("url") or "").strip())
    opt = body.get("option") or {}
    if not url:
        return jsonify({"error": "缺少链接"}), 400
    if not isinstance(opt, dict):
        return jsonify({"error": "参数不对"}), 400

    selector = str(opt.get("selector") or "").strip()
    if not _SELECTOR_RE.match(selector):
        return jsonify({"error": "清晰度参数不合法，重新解析一次再选"}), 400

    if jobs.active_count() >= 12:
        return jsonify({"error": "排队太多了，等几个下完再点"}), 429

    clean_opt = {
        "selector": selector,
        "kind": "audio" if opt.get("kind") == "audio" else "video",
        "ext": str(opt.get("ext") or "mp4")[:8],
        "label": str(opt.get("label") or "")[:40],
        "height": opt.get("height") or 0,
    }
    payload = {
        "url": url,
        "video_id": str(body.get("video_id") or "")[:20],
        "title": str(body.get("title") or "")[:200],
        "thumbnail": str(body.get("thumbnail") or "")[:400],
        "label": clean_opt["label"],
        "option": clean_opt,
    }
    job = jobs.create("download", payload)
    with _SCHED_LOCK:
        _WAITQ.append((job, url, clean_opt))
    return jsonify({"id": job.id})


@app.get("/api/jobs")
def api_jobs():
    return jsonify({"jobs": [j.snapshot() for j in jobs.recent(20)]})


@app.get("/api/jobs/<job_id>")
def api_job(job_id):
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "任务不存在"}), 404
    return jsonify(job.snapshot())


@app.get("/api/jobs/<job_id>/events")
def api_job_events(job_id):
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "任务不存在"}), 404

    def gen():
        last = 0.0
        while True:
            snap = job.wait_change(last)
            last = snap["updated"]
            yield f"data: {json.dumps(snap, ensure_ascii=False)}\n\n"
            if snap["stage"] in ("done", "error"):
                break

    return Response(gen(), mimetype="text/event-stream", headers={
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",
    })


@app.post("/api/jobs/<job_id>/cancel")
def api_job_cancel(job_id):
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "任务不存在"}), 404
    job.cancel()
    if job.proc is not None:
        downloader._kill(job.proc)
    return jsonify({"ok": True})


# ----------------------------- 文件 -----------------------------

def _allowed_roots() -> list[str]:
    roots = [config.out_dir(), config.OUT_DIR, config.TMP_DIR]
    return [os.path.realpath(r) for r in roots if r]


@app.get("/api/file")
def api_file():
    path = request.args.get("path", "")
    if not path:
        return jsonify({"error": "缺少 path"}), 400
    real = os.path.realpath(path)
    if not any(real == r or real.startswith(r + os.sep) for r in _allowed_roots()):
        return jsonify({"error": "这个路径不在下载目录里"}), 403
    if not os.path.isfile(real):
        return jsonify({"error": "文件不见了，可能被移走或删除"}), 404
    as_dl = request.args.get("download") == "1"
    return send_file(real, as_attachment=as_dl, download_name=os.path.basename(real), conditional=True)


@app.post("/api/reveal")
def api_reveal():
    body = request.get_json(force=True, silent=True) or {}
    path = (body.get("path") or "").strip()
    if not path:
        return jsonify({"error": "缺少 path"}), 400
    real = os.path.realpath(path)
    if not any(real == r or real.startswith(r + os.sep) for r in _allowed_roots()):
        return jsonify({"error": "路径不在下载目录里"}), 403
    downloader.reveal(real)
    return jsonify({"ok": True})


@app.post("/api/open-folder")
def api_open_folder():
    d = config.out_dir()
    downloader.reveal(d)
    return jsonify({"ok": True, "dir": d})


# ----------------------------- 启动 -----------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8799)
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()

    config.ensure_dirs()
    url = f"http://127.0.0.1:{args.port}/"
    print("=" * 56)
    print("  YouTube 视频下载器")
    print(f"  界面地址：{url}")
    print(f"  保存目录：{config.out_dir()}")
    print("  关闭这个窗口即停止服务")
    print("=" * 56)
    if not args.no_browser:
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    app.run(host="127.0.0.1", port=args.port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
