"""把 yt-dlp 的 format 列表归纳成用户能看懂的清晰度档位。

yt-dlp 一个视频能吐 30~60 个 format（同一分辨率还有不同编码、不同 fps、
纯视频/纯音频/音视频合体）。直接摊给用户没法选，这里收敛成：
  - 每个 height 一档（2160p / 1440p / 1080p / 720p / 480p / 360p ...）
  - 每档挑一个「最稳」的具体 format（≤1080p 优先 H.264，兼容性最好）
  - 音频单独给两个档（原始 M4A / 转 MP3）
每档都带上精确的 format selector，下载时原样回传，避免二次解析结果漂移。
"""
from __future__ import annotations

import re

# 视频编码 → 展示名
_VCODEC_LABEL = [
    ("avc1", "H.264"),
    ("h264", "H.264"),
    ("vp9", "VP9"),
    ("vp09", "VP9"),
    ("av01", "AV1"),
    ("hev1", "H.265"),
    ("hvc1", "H.265"),
]


def _vcodec_kind(fmt: dict) -> str:
    """返回 avc1 / vp9 / av01 / other 之类的族名。"""
    v = (fmt.get("vcodec") or "").lower()
    if not v or v == "none":
        return ""
    for key, _label in _VCODEC_LABEL:
        if v.startswith(key):
            return key
    return "other"


def _vcodec_label(fmt: dict) -> str:
    k = _vcodec_kind(fmt)
    for key, label in _VCODEC_LABEL:
        if k == key:
            return label
    return (fmt.get("vcodec") or "").split(".")[0] or "未知"


def _size(fmt: dict) -> int:
    """优先真实文件大小，其次估算；都没有返回 0。"""
    for k in ("filesize", "filesize_approx"):
        v = fmt.get(k)
        if isinstance(v, (int, float)) and v > 0:
            return int(v)
    return 0


def _est_size(fmt: dict, duration: float) -> int:
    """按码率估大小：tbr 单位 kbit/s。"""
    if _size(fmt):
        return _size(fmt)
    tbr = fmt.get("tbr") or 0
    if tbr and duration:
        return int(tbr * 1000 / 8 * duration)
    return 0


def _is_video(fmt: dict) -> bool:
    vc = (fmt.get("vcodec") or "none").lower()
    return vc not in ("", "none") and not fmt.get("has_drm")


def _is_audio_only(fmt: dict) -> bool:
    vc = (fmt.get("vcodec") or "none").lower()
    ac = (fmt.get("acodec") or "none").lower()
    return vc in ("", "none") and ac not in ("", "none")


def _is_muxed(fmt: dict) -> bool:
    """音视频已合体（如 18 = 360p mp4），不需要合并。"""
    vc = (fmt.get("vcodec") or "none").lower()
    ac = (fmt.get("acodec") or "none").lower()
    return vc not in ("", "none") and ac not in ("", "none")


def pick_best_audio(formats: list[dict]) -> dict | None:
    """挑一条最好的纯音频：优先 m4a（能被 mp4 容器接住），其次码率最高。"""
    audio = [f for f in formats if _is_audio_only(f)]
    if not audio:
        return None
    m4a = [f for f in audio if (f.get("ext") or "").lower() == "m4a"]
    pool = m4a or audio
    return max(pool, key=lambda f: (f.get("abr") or 0, f.get("tbr") or 0, _size(f)))


def pick_video_for_height(cands: list[dict]) -> dict:
    """同一分辨率里挑一条：H.264 优先（兼容性），其次 fps 高、码率高。"""
    h264 = [f for f in cands if _vcodec_kind(f).startswith("avc")]
    pool = h264 or cands
    return max(pool, key=lambda f: (f.get("fps") or 0, f.get("tbr") or 0, _size(f)))


def _fmt_bytes(n: int) -> str:
    if not n:
        return ""
    if n >= 1024 ** 3:
        return f"{n / 1024 ** 3:.2f} GB"
    if n >= 1024 ** 2:
        return f"{n / 1024 ** 2:.0f} MB"
    return f"{max(1, n // 1024)} KB"


def build_options(info: dict) -> dict:
    """核心：info → {title, options[], ...}。纯函数，方便单测。"""
    duration = float(info.get("duration") or 0)
    formats = [f for f in (info.get("formats") or []) if isinstance(f, dict)]
    best_audio = pick_best_audio(formats)
    audio_is_m4a = bool(best_audio and (best_audio.get("ext") or "") == "m4a")

    # 按 height 归组（只留纯视频流；合体流单独降级处理）
    by_height: dict[int, list[dict]] = {}
    muxed_by_height: dict[int, list[dict]] = {}
    for f in formats:
        h = f.get("height")
        if not isinstance(h, int) or h <= 0:
            continue
        if _is_muxed(f):
            muxed_by_height.setdefault(h, []).append(f)
        elif _is_video(f):
            by_height.setdefault(h, []).append(f)

    options: list[dict] = []
    heights = sorted(set(by_height) | set(muxed_by_height), reverse=True)
    for h in heights:
        sep = by_height.get(h) or []
        if sep:
            v = pick_video_for_height(sep)
            vid_id = v.get("format_id") or ""
            if best_audio and vid_id:
                selector = f"{vid_id}+{best_audio.get('format_id')}"
            else:
                selector = vid_id
            size = _est_size(v, duration) + (_est_size(best_audio, duration) if best_audio else 0)
            approx = not (_size(v) and (_size(best_audio) if best_audio else True))
            codec = _vcodec_label(v)
            fps = v.get("fps") or 0
            # 视频是 H.264 + 音频是 AAC → mp4；否则 mkv 更宽容
            merge = "mp4" if _vcodec_kind(v).startswith("avc") and audio_is_m4a else "mkv"
            note = " · ".join(x for x in [codec, f"{int(fps)}fps" if fps else ""] if x)
        else:
            v = max(muxed_by_height[h], key=lambda f: (f.get("fps") or 0, f.get("tbr") or 0))
            selector = v.get("format_id") or ""
            size = _est_size(v, duration)
            approx = not _size(v)
            codec = _vcodec_label(v)
            merge = (v.get("ext") or "mp4").lower()
            note = " · ".join(x for x in [codec, "已含音轨"] if x)

        if not selector:
            continue
        options.append({
            "id": f"v{h}",
            "kind": "video",
            "height": h,
            "label": f"{h}p",
            "ext": merge,
            "size": size,
            "size_text": ("约 " if approx and size else "") + _fmt_bytes(size),
            "note": note,
            "selector": selector,
            "is_default": False,
        })

    # 音视频合体流与分离流同高度时，保留分离流（画质更好），不重复列

    # 音频档
    if best_audio:
        a_id = best_audio.get("format_id") or ""
        a_ext = (best_audio.get("ext") or "m4a").lower()
        abr = best_audio.get("abr") or 0
        a_size = _est_size(best_audio, duration)
        if a_id:
            options.append({
                "id": "a-orig",
                "kind": "audio",
                "label": "仅音频 · 原始",
                "ext": a_ext,
                "size": a_size,
                "size_text": _fmt_bytes(a_size),
                "note": (f"{int(abr)}kbps" if abr else "") + (" · " if abr else "") + "不转码",
                "selector": a_id,
                "is_default": False,
            })
        options.append({
            "id": "a-mp3",
            "kind": "audio",
            "label": "仅音频 · MP3",
            "ext": "mp3",
            "size": a_size,
            "size_text": _fmt_bytes(a_size),
            "note": "192kbps · 通用格式",
            "selector": a_id + "/bestaudio" if a_id else "bestaudio",
            "is_default": False,
        })

    # 兜底：一个带 height 的视频流都没有（直链、部分站点、特殊视频），
    # 至少给一档「原始文件」，别让用户看着空白页干瞪眼
    if not any(o["kind"] == "video" for o in options):
        # 注意不能按 vcodec 过滤：直链（generic extractor）连编码信息都没有，
        # 只有个 format_id。这里只要求「有 format_id 且不是 DRM」。
        cand = [f for f in formats if f.get("format_id") and not f.get("has_drm")]
        if cand:
            f = max(cand, key=lambda x: (_size(x) or 0, x.get("tbr") or 0))
            options.append({
                "id": "raw",
                "kind": "video",
                "height": 0,
                "label": "原始文件",
                "ext": (f.get("ext") or "mp4").lower(),
                "size": _est_size(f, duration),
                "size_text": _fmt_bytes(_est_size(f, duration)),
                "note": "未提供分辨率信息，按原始文件下载",
                "selector": f["format_id"],
                "is_default": True,
            })

    # 默认选 1080p 以下最高的一档（兼顾画质与体积）
    preferred = [o for o in options if o["kind"] == "video" and 0 < o["height"] <= 1080]
    if not preferred:
        preferred = [o for o in options if o["kind"] == "video"]
    if preferred:
        preferred[0]["is_default"] = True
    elif options:
        options[0]["is_default"] = True

    has_sub = bool(info.get("subtitles")) or bool(info.get("automatic_captions"))
    return {
        "options": options,
        "best_height": max((o["height"] for o in options if o["kind"] == "video" and o["height"]), default=0),
        "has_subtitle": has_sub,
    }


def normalize_info(raw: dict) -> dict:
    """把 -J 的原始 info 提炼成前端要的字段；playlist 自动取第一个。"""
    info = raw or {}
    playlist_count = 0
    if info.get("_type") == "playlist" or info.get("entries"):
        entries = info.get("entries") or []
        playlist_count = info.get("playlist_count") or len(entries)
        first = next((e for e in entries if isinstance(e, dict) and e), None)
        if first is None:
            raise ValueError("这个播放列表里没有可下载的视频")
        info = first

    if info.get("is_live") or info.get("live_status") in ("is_live", "is_upcoming"):
        raise ValueError("这是直播 / 直播预告，无法按文件下载")

    built = build_options(info)
    if not built["options"]:
        raise ValueError("没有解析到可下载的格式（可能是会员专享、地区限制或 DRM 加密）")

    vlen = int(info.get("duration") or 0)
    return {
        "id": info.get("id") or "",
        "title": info.get("title") or "未命名视频",
        "uploader": info.get("uploader") or info.get("channel") or "",
        "duration": vlen,
        "duration_text": f"{vlen // 3600}:{vlen % 3600 // 60:02d}:{vlen % 60:02d}" if vlen >= 3600
        else f"{vlen // 60}:{vlen % 60:02d}",
        "thumbnail": info.get("thumbnail") or "",
        "view_count": info.get("view_count") or 0,
        "upload_date": _fmt_date(info.get("upload_date") or ""),
        "playlist_count": playlist_count,
        "has_subtitle": built["has_subtitle"],
        "best_height": built["best_height"],
        "options": built["options"],
        "webpage_url": info.get("webpage_url") or "",
    }


def _fmt_date(s: str) -> str:
    m = re.match(r"^(\d{4})(\d{2})(\d{2})$", s or "")
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else ""
