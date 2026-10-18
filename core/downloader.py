"""调 yt-dlp 真正下载：进度解析、双流进度映射、取消、产物定位。"""
from __future__ import annotations

import os
import queue
import re
import shlex
import shutil
import subprocess
import sys
import threading

from . import config, jobs

_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

_PCT = re.compile(r"\[download\]\s+([\d.]+)%\s+of\s+~?\s*([\d.]+\s*[KMG]?iB)", re.I)
_PCT_ONLY = re.compile(r"\[download\]\s+([\d.]+)%")
_SPEED = re.compile(r"at\s+([\d.]+\s*[KMG]?iB/s)", re.I)
_ETA = re.compile(r"ETA\s+(\d+:\d+(?::\d+)?)", re.I)
_DEST = re.compile(r"\[download\]\s+Destination:\s*(.+)$")

# 下载阶段占总进度的 0~92%，剩下给合并/转码
_DL_SPAN = 0.92


def base_cmd(cfg: dict | None = None) -> list[str]:
    cfg = cfg or config.load()
    args = [sys.executable, "-m", "yt_dlp", "--no-playlist", "--newline", "--no-warnings",
            "--no-update", "--progress", "--windows-filenames", "--no-mtime",
            # 代理出口对 YouTube 单线程只有 100 kB/s 量级，多分片是唯一解
            "--concurrent-fragments", "4", "--retries", "5", "--fragment-retries", "10"]
    node = cfg.get("node") or ""
    if node and os.path.isfile(node):
        # 新版 YouTube 解析签名需要 JS 运行时，没有会只给到 360p
        args += ["--js-runtimes", f"node:{node}"]
    ff = cfg.get("ffmpeg") or ""
    if ff and os.path.isfile(ff):
        args += ["--ffmpeg-location", os.path.dirname(ff)]
    if cfg.get("proxy"):
        args += ["--proxy", cfg["proxy"]]
    extra = (cfg.get("extra_args") or "").strip()
    if extra:
        try:
            args += shlex.split(extra)
        except ValueError:
            pass
    args += config.cookie_args()
    return args


def _filename_tpl(outdir: str, kind: str, height: int = 0) -> str:
    if kind == "audio":
        name = "%(title).90B [%(id)s][音频].%(ext)s"
    elif height:
        name = "%(title).90B [%(id)s][%(height)sp].%(ext)s"
    else:
        # 没有分辨率信息（直链等）时别在文件名里留个 NA
        name = "%(title).90B [%(id)s].%(ext)s"
    return os.path.join(outdir, name)


def build_cmd(url: str, opt: dict, outdir: str, tmpdir: str, pathfile: str, cfg: dict | None = None) -> list[str]:
    cfg = cfg or config.load()
    args = base_cmd(cfg) + [
        "-f", opt["selector"],
        "-o", _filename_tpl(outdir, opt.get("kind", "video"), int(opt.get("height") or 0)),
        "--paths", f"temp:{tmpdir}",
        # 最终文件路径单独写到文件里，避免和进度输出混在 stdout
        "--print-to-file", "after_move:filepath", pathfile,
    ]
    if opt.get("kind") == "audio":
        if (opt.get("ext") or "").lower() == "mp3":
            args += ["-x", "--audio-format", "mp3", "--audio-quality", "192K"]
        # 原始音频不转码，直接落地
    else:
        args += ["--merge-output-format", opt.get("ext") or "mp4"]
    args.append(url)
    return args


def friendly_error(text: str) -> str:
    e = (text or "").strip()
    low = e.lower()
    if "sign in to confirm" in low or "not a bot" in low:
        return ("YouTube 要求登录验证。点右上角「连接 YouTube」登录一次，"
                "之后所有下载都会带着登录态走。")
    if "unable to connect to proxy" in low or "tunnel connection failed" in low:
        return "代理连不上。到「设置」里确认代理地址和端口，并确认代理软件在运行。"
    if "private video" in low:
        return "这是私有视频，无法下载。"
    if "video unavailable" in low:
        return "视频不可用（已删除、地区限制或需要会员）。"
    if "members-only" in low or "join this channel" in low:
        return "会员专属视频，需要对应账号权限。"
    if "requested format is not available" in low:
        return "这个清晰度此刻不可用，重新解析一次再选。"
    tail = [ln for ln in e.splitlines() if ln.strip()][-3:]
    return "下载失败：" + " / ".join(tail) if tail else "下载失败"


def _kill(proc) -> None:
    if proc is None or proc.poll() is not None:
        return
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           capture_output=True, creationflags=_NO_WINDOW)
        else:
            proc.kill()
    except Exception:
        pass


def _read_path(pathfile: str) -> str:
    if not os.path.isfile(pathfile):
        return ""
    try:
        with open(pathfile, "r", encoding="utf-8", errors="ignore") as f:
            lines = [ln.strip() for ln in f if ln.strip()]
        return lines[-1] if lines else ""
    except OSError:
        return ""


def _fmt_bytes(n: float) -> str:
    if not n:
        return ""
    if n >= 1024 ** 3:
        return f"{n / 1024 ** 3:.2f} GB"
    if n >= 1024 ** 2:
        return f"{n / 1024 ** 2:.1f} MB"
    return f"{max(1, n // 1024)} KB"


def run(job, url: str, opt: dict, cfg: dict | None = None) -> dict:
    """阻塞执行一次下载；由调用方放在线程里跑。"""
    cfg = cfg or config.load()
    outdir = config.out_dir()
    os.makedirs(outdir, exist_ok=True)
    tmpdir = os.path.join(config.TMP_DIR, job.id)
    os.makedirs(tmpdir, exist_ok=True)
    pathfile = os.path.join(tmpdir, "_final.txt")

    n_streams = opt["selector"].count("+") + 1
    job.set_stage("downloading", "下载中", "正在连接 YouTube…")
    job.set_progress(0.0)

    cmd = build_cmd(url, opt, outdir, tmpdir, pathfile, cfg)
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, encoding="utf-8", errors="ignore",
                            bufsize=1, creationflags=_NO_WINDOW)
    job.proc = proc

    q: queue.Queue = queue.Queue()

    def reader():
        try:
            for line in proc.stdout:  # type: ignore[union-attr]
                q.put(line)
        finally:
            q.put(None)

    threading.Thread(target=reader, daemon=True).start()

    stream_idx = 0
    last_pct = -1.0
    tail: list[str] = []
    try:
        while True:
            try:
                line = q.get(timeout=0.4)
            except queue.Empty:
                if job.cancelled:
                    _kill(proc)
                    raise jobs.Cancelled("已取消")
                continue
            if line is None:
                break
            line = line.rstrip()
            if not line:
                continue
            tail.append(line)
            tail = tail[-30:]

            m_dest = _DEST.search(line)
            if m_dest:
                stream_idx = min(stream_idx, n_streams - 1)
                if stream_idx:
                    job.note(f"开始下载第 {stream_idx + 1}/{n_streams} 路流")
                continue

            m = _PCT.search(line) or _PCT_ONLY.search(line)
            if m:
                pct = float(m.group(1))
                if pct + 5 < last_pct:  # 掉头说明换了一条流
                    stream_idx = min(stream_idx + 1, n_streams - 1)
                last_pct = pct
                overall = (stream_idx + pct / 100.0) / n_streams * _DL_SPAN
                sp = _SPEED.search(line)
                et = _ETA.search(line)
                total = m.group(2) if m.lastindex and m.lastindex >= 2 else ""
                job.set_progress(overall,
                                 speed=sp.group(1) if sp else "",
                                 eta=et.group(1) if et else "",
                                 total=total)
                continue

            if "[Merger]" in line or "[ExtractAudio]" in line or "[VideoConvertor]" in line:
                job.set_stage("merging", "合并音轨", "正在合并音视频…")
                job.set_progress(max(job.progress, _DL_SPAN))
                continue

            if line.startswith("ERROR"):
                tail.append(line)

            if job.cancelled:
                _kill(proc)
                raise jobs.Cancelled("已取消")
    finally:
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            _kill(proc)

    final = _read_path(pathfile)

    if job.cancelled:
        shutil.rmtree(tmpdir, ignore_errors=True)
        raise jobs.Cancelled("已取消")

    if not final or not os.path.isfile(final):
        # 兜底：扫输出目录里刚生成、且 id 对得上的文件
        vid = job.payload.get("video_id") or ""
        cands = []
        for name in os.listdir(outdir):
            p = os.path.join(outdir, name)
            if os.path.isfile(p) and (not vid or vid in name):
                cands.append(p)
        if cands:
            final = max(cands, key=os.path.getmtime)

    if not final or not os.path.isfile(final):
        if proc.returncode != 0:
            raise RuntimeError(friendly_error("\n".join(tail)))
        raise RuntimeError("下载结束但没找到成品文件")

    shutil.rmtree(tmpdir, ignore_errors=True)
    size = os.path.getsize(final)
    job.set_progress(1.0, speed="", eta="", total=_fmt_bytes(size))
    return {
        "path": final,
        "name": os.path.basename(final),
        "size": size,
        "size_text": _fmt_bytes(size),
        "folder": os.path.dirname(final),
        "label": opt.get("label") or "",
    }


def reveal(path: str) -> None:
    """在资源管理器里高亮这个文件。"""
    try:
        if os.name == "nt":
            if os.path.isdir(path):
                subprocess.Popen(["explorer", os.path.normpath(path)])
            else:
                subprocess.Popen(["explorer", f"/select,{os.path.normpath(path)}"])
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-R", path])
        else:
            subprocess.Popen(["xdg-open", os.path.dirname(path)])
    except Exception:
        pass
