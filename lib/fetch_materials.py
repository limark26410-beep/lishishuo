# -*- coding: utf-8 -*-
"""自动抓取视频素材（YouTube 为主，非商用）——流水线集成版。

核心函数：
- search(keyword, count, dur_max)           搜索候选
- download(url, topic, start, end, ...)     下载单条入库
- fetch_by_keyword(keyword, topic, ...)     按关键词自动抓 N 个片段（流水线用）

工具脚本 tools/fetch_video_materials.py 是本模块的 CLI 包装。
"""
import os
import re
import subprocess

import yt_dlp

DEFAULT_ROOT = os.path.expanduser("~/Desktop/历史说素材/视频/")


def _fmt_dur(sec: int) -> str:
    sec = int(sec or 0)
    return f"{sec // 60}分{sec % 60:02d}s"


def _is_av1(path: str) -> bool:
    """检测视频是否为 AV1 编码（macOS 播放器不兼容）"""
    r = subprocess.run(["ffmpeg", "-i", path], capture_output=True, text=True)
    low = (r.stderr or "").lower()
    return "av1" in low or "av01" in low


def _to_h264(path: str) -> None:
    """AV1 → H.264 转码（保证任何播放器可打开）"""
    tmp = path + ".h264.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", path,
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
         "-c:a", "aac", "-b:a", "128k", tmp],
        check=True, capture_output=True, text=True)
    os.remove(path)
    os.replace(tmp, path)


def search(keyword: str, count: int, dur_max: int = 0) -> list:
    """YouTube 搜索，返回候选列表 [{title, duration, channel, id}]"""
    opts = {
        "quiet": True, "no_warnings": True, "proxy": "",
        "extract_flat": "in_playlist", "skip_download": True,
        "playlist_items": f"1-{count}",
        "socket_timeout": 15, "retries": 1,  # 网络挂时快速失败不卡死
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(f"ytsearch{count}:{keyword}", download=False)
    entries = []
    for e in (info.get("entries") or []):
        if not e:
            continue
        dur = e.get("duration") or 0
        if dur_max and dur > dur_max:
            continue
        entries.append({
            "title": e.get("title", "?"),
            "duration": dur,
            "channel": e.get("channel", "?"),
            "id": e.get("id"),
        })
    return entries


def download(url: str, topic: str, start: str, end: str, desc: str,
             root: str = DEFAULT_ROOT, max_height: int = 720) -> str:
    """下载单条视频入库（截取可选），返回入库文件路径。

    注：yt-dlp download_sections 在本机不生效 → 下载完整（限高控大小）
    → ffmpeg 本地截取 → 删原片；排除 AV1 编码保证播放器兼容。
    """
    outdir = os.path.join(root, topic)
    os.makedirs(outdir, exist_ok=True)
    existing = [f for f in os.listdir(outdir)
                if f.lower().endswith((".mp4", ".mkv", ".webm"))]
    n = len(existing) + 1
    prefix = f"{topic}_{n:02d}"
    outtmpl = os.path.join(outdir, f"{prefix}.%(ext)s")

    opts = {
        "quiet": True, "no_warnings": True, "proxy": "",
        "format": (f"bv*[height<={max_height}][vcodec!=av01]"
                   f"+ba/b[height<={max_height}][vcodec!=av01]"),
        "merge_output_format": "mp4",
        "outtmpl": outtmpl,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)

    files = [f for f in os.listdir(outdir)
             if f.startswith(prefix) and f.lower().endswith((".mp4", ".mkv", ".webm"))]
    if not files:
        raise RuntimeError("下载后未找到文件")
    full_path = os.path.join(outdir, files[0])

    # 兜底：AV1 → H.264
    if _is_av1(full_path):
        print("  ⚠ 检测到 AV1 编码，转 H.264…")
        _to_h264(full_path)

    if start and end:
        clip_tmp = os.path.join(outdir, f"{prefix}.clip.mp4")
        cmd = ["ffmpeg", "-y", "-loglevel", "error",
               "-ss", start, "-to", end, "-i", full_path,
               "-c", "copy", clip_tmp]
        subprocess.run(cmd, check=True, capture_output=True, text=True)
        os.remove(full_path)
        os.replace(clip_tmp, os.path.join(outdir, f"{prefix}.mp4"))
        fname = f"{prefix}.mp4"
    else:
        fname = files[0]

    desc = desc or f"搜索主题：{topic}"
    readme = os.path.join(outdir, "README.txt")
    with open(readme, "a", encoding="utf-8") as f:
        f.write(f"{fname}：{desc}\n")

    size_mb = os.path.getsize(os.path.join(outdir, fname)) / 1024 / 1024
    print(f"  ✅ {fname} ({size_mb:.1f}MB) | {(info.get('title') or '?')[:50]}")
    return os.path.join(outdir, fname)


def fetch_by_keyword(keyword: str, topic: str, count: int = 3,
                     clip_seconds: int = 90, root: str = DEFAULT_ROOT,
                     max_height: int = 720, max_duration: int = 1200,
                     progress=None) -> list:
    """按关键词自动抓 N 个片段入库（流水线集成用）。

    - 关键词按逗号/分号拆成多个搜索词，逐词搜索合并候选（AI 生成的关键词
      往往又长又碎，单次搜索命中差）
    - 优先选时长 ≤ max_duration 的视频（避免长片大下载）；无短片时降级取
      最短的长片（提示可能较大），不直接报错
    - 每个视频从 20% 处截取 clip_seconds 秒（避开片头/片尾）
    """
    if progress is None:
        progress = print

    terms = [t.strip() for t in re.split(r"[,，;；]", keyword) if t.strip()][:3]
    if not terms:
        terms = [keyword[:60]]

    # 过滤歌曲/歌词/翻唱类（画面是歌词字幕，混剪会叠字幕）
    _SONG_HINTS = ("歌词", "翻唱", "Lyrics", "lyrics", "cover", "Cover",
                   "MV", "mv", "动态歌词", "remix", "Remix", "OST",
                   "主题曲", "片尾曲", "music video", "Music Video")
    all_entries, seen = [], set()
    for t in terms:
        try:
            for e in search(t, count=count * 4):  # 先不限时长收集候选
                if any(h in (e.get("title") or "") for h in _SONG_HINTS):
                    continue  # 跳过歌曲/歌词视频
                if e.get("id") and e["id"] not in seen:
                    seen.add(e["id"])
                    all_entries.append(e)
        except Exception as ex:
            progress(f"  ⚠ 搜索「{t}」失败: {type(ex).__name__}，换下一组词")
    if not all_entries:
        raise RuntimeError(f"搜索「{keyword}」全部无结果（网络/关键词问题），换个关键词试试")

    short = [e for e in all_entries if (e.get("duration") or 0) <= max_duration]
    if short:
        usable = short
        progress(f"🔍 关键词拆分为 {len(terms)} 组，合并候选 {len(all_entries)} 条"
                 f"（其中 {max_duration // 60} 分钟内 {len(short)} 条）")
    else:
        usable = sorted(all_entries, key=lambda e: e.get("duration") or 0)[:count]
        progress(f"⚠ 无 {max_duration // 60} 分钟内的视频，降级取最短的 "
                 f"{len(usable)} 个（长片下载量大）")

    downloaded = []
    for i, e in enumerate(usable[:count]):
        vid = e["id"]
        dur = e.get("duration") or 0
        if dur < clip_seconds + 30:
            start, end = None, None  # 太短：整段入库
        else:
            s = int(dur * 0.2)
            start = f"{s // 3600:02d}:{(s % 3600) // 60:02d}:{s % 60:02d}"
            end_sec = s + clip_seconds
            end = (f"{end_sec // 3600:02d}:{(end_sec % 3600) // 60:02d}:"
                   f"{end_sec % 60:02d}")
        desc = f"{keyword} 相关画面（横屏16:9，裁9:16注意构图）"
        progress(f"  ⬇ 下载片段 {i + 1}/{min(count, len(usable))}: "
                 f"{e['title'][:36]}… ({dur // 60}分)")
        path = download(vid, topic, start, end, desc, root, max_height)
        downloaded.append(path)
    return downloaded
