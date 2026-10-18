"""核心纯逻辑自检：格式归并、默认档位、容器选择、异常分支。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import extract  # noqa: E402

FAIL = []


def check(name, cond, extra=""):
    if cond:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name} {extra}")
        FAIL.append(name)


def F(fid, **kw):
    base = {"format_id": fid, "ext": "mp4", "vcodec": "none", "acodec": "none",
            "height": None, "fps": None, "tbr": 0}
    base.update(kw)
    return base


# ---------- 用例 1：典型 1080p 视频，h264 与 vp9 并存，音频 m4a + opus ----------
info1 = {
    "id": "abc12345678",
    "title": "测试视频",
    "duration": 600,
    "uploader": "某频道",
    "view_count": 123456,
    "upload_date": "20240102",
    "thumbnail": "http://x/t.jpg",
    "formats": [
        F("140", ext="m4a", acodec="mp4a.40.2", abr=128, tbr=129, filesize=9800000),
        F("251", ext="webm", acodec="opus", abr=160, tbr=161, filesize=12000000),
        F("137", height=1080, fps=30, vcodec="avc1.640028", tbr=4500, filesize=330000000),
        F("299", height=1080, fps=60, vcodec="avc1.64002a", tbr=6800, filesize=500000000),
        F("248", height=1080, fps=30, vcodec="vp9", tbr=3200, filesize=230000000),
        F("136", height=720, fps=30, vcodec="avc1.4d401f", tbr=2200, filesize=160000000),
        F("247", height=720, fps=30, vcodec="vp9", tbr=1800, filesize=130000000),
        F("135", height=480, fps=30, vcodec="avc1.4d401e", tbr=1100, filesize=82000000),
        F("134", height=360, fps=30, vcodec="avc1.4d401e", tbr=700, filesize=52000000),
        F("18", height=360, fps=30, vcodec="avc1.42001e", acodec="mp4a.40.2", tbr=800,
          filesize=60000000, ext="mp4"),
    ],
}
out1 = extract.normalize_info(info1)
vids = [o for o in out1["options"] if o["kind"] == "video"]
auds = [o for o in out1["options"] if o["kind"] == "audio"]

check("视频档位按高度去重（1080/720/480/360）", [v["height"] for v in vids] == [1080, 720, 480, 360],
      [v["height"] for v in vids])
check("1080p 选 H.264 而不是 VP9", vids[0]["selector"].startswith("299"), vids[0]["selector"])
check("1080p 优先 60fps 版本", "299" in vids[0]["selector"], vids[0]["selector"])
check("1080p 走 mp4 容器", vids[0]["ext"] == "mp4", vids[0]["ext"])
check("1080p 选了 aac 音轨", vids[0]["selector"] == "299+140", vids[0]["selector"])
check("体积 = 视频 + 音频", vids[0]["size"] == 500000000 + 9800000, vids[0]["size"])
check("默认档位 ≤1080p 且最高", [o["is_default"] for o in vids] == [True, False, False, False],
      [o["is_default"] for o in vids])
check("最高清晰度识别为 1080", out1["best_height"] == 1080, out1["best_height"])
check("时长格式化 10:00", out1["duration_text"] == "10:00", out1["duration_text"])
check("音频给出原始 + MP3 两档", [a["id"] for a in auds] == ["a-orig", "a-mp3"], [a["id"] for a in auds])
check("原始音频选 m4a 而不是 opus", auds[0]["ext"] == "m4a", auds[0]["ext"])
check("MP3 档容器是 mp3", auds[1]["ext"] == "mp3", auds[1]["ext"])
check("有字幕后端能识别", extract.normalize_info({**info1, "subtitles": {"zh": []}})["has_subtitle"] is True)


# ---------- 用例 2：只有 AV1 的 4K 视频，音频只有 opus ----------
info2 = {
    "title": "4K 视频", "duration": 120, "id": "x",
    "formats": [
        F("251", ext="webm", acodec="opus", abr=130, filesize=2000000),
        F("401", height=2160, fps=30, vcodec="av01.0.12M.08", tbr=18000, filesize=270000000),
        F("399", height=1080, fps=30, vcodec="av01.0.08M.08", tbr=4000, filesize=60000000),
    ],
}
out2 = extract.normalize_info(info2)
v2 = [o for o in out2["options"] if o["kind"] == "video"]
a2 = [o for o in out2["options"] if o["kind"] == "audio"]
check("4K 也列出来了", v2[0]["height"] == 2160, v2[0]["height"])
check("AV1 + opus 走 mkv 容器", v2[0]["ext"] == "mkv", v2[0]["ext"])
check("AV1 标注正确", "AV1" in v2[0]["note"], v2[0]["note"])
check("无 m4a 时原始音频退化为 webm", a2[0]["ext"] == "webm", a2[0]["ext"])
check("默认档位回落到 ≤1080p 的 1080", v2[1]["is_default"] is True)


# ---------- 用例 3：只有合体流（老视频） ----------
info3 = {
    "title": "老视频", "duration": 60, "id": "y",
    "formats": [
        F("18", height=360, fps=30, vcodec="avc1.42001e", acodec="mp4a.40.2", ext="mp4", filesize=7000000),
        F("17", height=144, fps=15, vcodec="avc1.4d400c", acodec="mp4a.40.2", ext="3gp", filesize=900000),
    ],
}
out3 = extract.normalize_info(info3)
v3 = [o for o in out3["options"] if o["kind"] == "video"]
check("合体流也能列档", [o["height"] for o in v3] == [360, 144], [o["height"] for o in v3])
check("合体流 selector 不带 +", "+" not in v3[0]["selector"], v3[0]["selector"])
check("合体流标注已含音轨", "已含音轨" in v3[0]["note"], v3[0]["note"])


# ---------- 用例 4：估算大小（无 filesize） ----------
info4 = {
    "title": "无体积信息", "duration": 100, "id": "z",
    "formats": [
        F("137", height=1080, fps=30, vcodec="avc1.640028", tbr=4000),
        F("140", ext="m4a", acodec="mp4a", abr=128, tbr=128),
    ],
}
out4 = extract.normalize_info(info4)
v4 = [o for o in out4["options"] if o["kind"] == "video"][0]
check("无 filesize 时按码率估算", v4["size"] > 0, v4["size"])
check("估算值带「约」字样", v4["size_text"].startswith("约"), v4["size_text"])


# ---------- 用例 5：异常分支 ----------
def expect_err(name, info, kw):
    try:
        extract.normalize_info(info)
        check(name, False, "没有抛错")
    except Exception as e:
        check(name, kw in str(e), str(e))


expect_err("直播拒绝", {"title": "live", "is_live": True, "formats": []}, "直播")
expect_err("空格式拒绝", {"title": "drm", "duration": 10, "formats": []}, "没有解析到")
expect_err("播放列表取第一个", {"_type": "playlist", "entries": []}, "播放列表")

pl = extract.normalize_info({
    "_type": "playlist", "playlist_count": 12,
    "entries": [{"title": "第一条", "duration": 30, "id": "p1",
                 "formats": [F("18", height=360, vcodec="avc1", acodec="mp4a", ext="mp4", filesize=100)]}],
})
check("播放列表只取第一条且记录总数", pl["title"] == "第一条" and pl["playlist_count"] == 12,
      (pl["title"], pl["playlist_count"]))

# ---------- 用例 6：selector 安全性（下游会拼进命令行） ----------
import re  # noqa: E402
SEL = re.compile(r"^[A-Za-z0-9_.\-+/]{1,80}$")
for o in out1["options"] + out2["options"] + out3["options"]:
    check(f"selector 合法 {o['selector']}", bool(SEL.match(o["selector"])))
check("selector 拒绝注入尝试", not SEL.match("137; rm -rf /"))
check("selector 拒绝换行注入", not SEL.match("137\n+x"))

print()
if FAIL:
    print(f"FAILED {len(FAIL)}: {FAIL}")
    sys.exit(1)
print("全部通过")
