# -*- coding: utf-8 -*-
"""自动抓取视频素材（YouTube / B站，非商用）——流水线集成版。

核心函数：
- search(keyword, count, dur_max, source)  搜索候选（youtube / bilibili）
- download(url, topic, start, end, ...)    下载单条入库
- fetch_by_keyword(keyword, topic, ...)    按关键词自动抓 N 个片段（流水线用）

工具脚本 tools/fetch_video_materials.py 是本模块的 CLI 包装。
"""
import os
import re
import subprocess
import time

import requests

import yt_dlp

DEFAULT_ROOT = os.path.expanduser("~/Desktop/历史说素材/视频/")

# B 站 buvid cookie 缓存（防 412 反爬）
_BVID_COOKIE = {"v": ""}


def _get_buvid_cookie() -> str:
    """取 B 站 buvid3 cookie（防 412 反爬），失败返回空串。

    用公共 DNS（114.114.114.114）拿真实 IP + --resolve 直连 B 站主页，
    绕过代理工具的假 DNS（198.18.x.x 劫持）。cookie 缓存复用。
    """
    if _BVID_COOKIE["v"]:
        return _BVID_COOKIE["v"]
    try:
        r = subprocess.run(
            ["nslookup", "www.bilibili.com", "114.114.114.114"],
            capture_output=True, text=True, timeout=10)
        ips = re.findall(r"Address: (\d+\.\d+\.\d+\.\d+)", r.stdout)
        if not ips:
            return ""
        r2 = subprocess.run(
            ["curl", "-s", "--max-time", "8",
             "--resolve", f"www.bilibili.com:443:{ips[0]}",
             "-D", "-", "-o", "/dev/null", "https://www.bilibili.com/"],
            capture_output=True, text=True, timeout=15)
        m = re.search(r"[Ss]et-[Cc]ookie: (buvid3=[^;]+)", r2.stdout)
        if m:
            _BVID_COOKIE["v"] = m.group(1)
    except Exception:
        pass
    return _BVID_COOKIE["v"]


# B 站请求需完整浏览器头（UA/Accept/Referer 可过 412；buvid cookie 有则叠加）
_BILI_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/126.0.0.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Referer": "https://www.bilibili.com/",
}


def _bili_headers() -> dict:
    """B 站请求头：完整浏览器头 + buvid cookie（如有）"""
    h = dict(_BILI_HEADERS)
    ck = _get_buvid_cookie()
    if ck:
        h["Cookie"] = ck
    return h


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


from urllib.parse import urlparse

def _pexels_api_key() -> str:
    """Pexels API key（pexels.com/api 免费申请）"""
    return os.environ.get("PEXELS_API_KEY", "") or _read_env_key("PEXELS_API_KEY")


def _read_env_key(name: str) -> str:
    """读 .env 里的 key"""
    env = os.path.expanduser("~/.env")
    for cand in ("~/.env", "~/Desktop/历史说素材/.env", "/Users/local/lishishuo/.env"):
        p = os.path.expanduser(cand)
        if os.path.exists(p):
            for ln in open(p, encoding="utf-8"):
                if ln.strip().startswith(name):
                    return ln.split("=", 1)[-1].strip()
    return ""


def _pexels_search(keyword: str, count: int, dur_max: int) -> list:
    """Pexels 视频搜索（无版权素材库，需 PEXELS_API_KEY）"""
    key = _pexels_api_key()
    if not key:
        raise RuntimeError("未配置 PEXELS_API_KEY（https://www.pexels.com/api/ 免费申请）")
    r = requests.get(
        "https://api.pexels.com/videos/search",
        params={"query": keyword, "per_page": max(count, 5),
                "orientation": "landscape"},
        headers={"Authorization": key}, timeout=20)
    r.raise_for_status()
    entries = []
    for v in (r.json().get("videos") or []):
        dur = v.get("duration") or 0
        if dur_max and dur > dur_max:
            continue
        url = ""
        for f in (v.get("video_files") or []):
            if f.get("link") and (f.get("height") or 9999) <= 720:
                url = f["link"]
                break
        if not url:
            continue
        entries.append({"title": (v.get("url") or "Pexels素材"),
                        "duration": dur, "channel": "Pexels", "id": url})
    return entries


def _pexels_download(url: str, out_path: str) -> None:
    """直接下载 Pexels 直链 mp4"""
    r = requests.get(url, stream=True, timeout=120)
    r.raise_for_status()
    with open(out_path, "wb") as f:
        for chunk in r.iter_content(1024 * 256):
            if chunk:
                f.write(chunk)


# ── GL-20260902：Pexels 图片素材（图片来源 → Pexels 素材库） ──

def _pexels_search_photos(keyword: str, count: int) -> list:
    """Pexels 图片搜索（返回图片直链列表）"""
    key = _pexels_api_key()
    if not key:
        raise RuntimeError("未配置 PEXELS_API_KEY（https://www.pexels.com/api/ 免费申请）")
    r = requests.get(
        "https://api.pexels.com/v1/search",
        params={"query": keyword, "per_page": max(1, count),
                "orientation": "landscape"},
        headers={"Authorization": key}, timeout=20)
    r.raise_for_status()
    urls = []
    for p in (r.json().get("photos") or []):
        # 取 1080p 左右的原图直链（src.large 是压缩版，原图 original 可能过大）
        src = p.get("src") or {}
        url = src.get("large2x") or src.get("large") or src.get("original")
        if url:
            urls.append(url)
    return urls[:count]


def fetch_pexels_photos(keyword: str, count: int, out_dir: str) -> list:
    """按关键词搜索并下载 Pexels 图片到 out_dir，返回下载文件路径列表。
    用于「图片来源 → Pexels 素材库」：搜索 → 下载 → 图片模式混剪。
    文件名用 时间戳+序号 避免与目录已有图片冲突（换图场景）。"""
    import tempfile, shutil, time as _t
    key = _pexels_api_key()
    if not key:
        raise RuntimeError("未配置 PEXELS_API_KEY（https://www.pexels.com/api/ 免费申请）")
    os.makedirs(out_dir, exist_ok=True)
    urls = _pexels_search_photos(keyword, count)
    if not urls:
        raise RuntimeError(f"Pexels 未找到「{keyword}」相关图片")
    downloaded = []
    stamp = _t.strftime("%Y%m%d%H%M%S")
    for i, url in enumerate(urls, 1):
        ext = os.path.splitext(urlparse(url).path)[1] or ".jpg"
        if ext.lower() not in (".jpg", ".jpeg", ".png", ".webp"):
            ext = ".jpg"
        out_path = os.path.join(out_dir, f"px_{stamp}_{i:02d}{ext}")
        try:
            r = requests.get(url, stream=True, timeout=60)
            r.raise_for_status()
            with open(out_path, "wb") as f:
                for chunk in r.iter_content(1024 * 256):
                    if chunk:
                        f.write(chunk)
            downloaded.append(out_path)
        except Exception as e:
            print(f"  ⚠ Pexels 第{i}张下载失败: {e}")
    if not downloaded:
        raise RuntimeError("Pexels 图片全部下载失败")
    print(f"  ✓ Pexels 图片下载: {len(downloaded)} 张 → {out_dir}")
    return downloaded


def search(keyword: str, count: int, dur_max: int = 0,
           source: str = "youtube") -> list:
    """搜索候选。source: youtube / bilibili。

    返回 [{title, duration, channel, id}]。B 站用 bilisearch 完整提取
    （extract_flat 下 B 站条目信息不全）+ Referer/buvid cookie 防 412。
    """
    if source == "pexels":
        return _pexels_search(keyword, count, dur_max)
    headers = {}
    if source == "bilibili":
        headers = _bili_headers()
    is_bili = source == "bilibili"
    opts = {
        "quiet": True, "no_warnings": True, "proxy": "",
        "skip_download": True,
        "socket_timeout": 15, "retries": 1,  # 网络挂时快速失败不卡死
        "http_headers": headers or None,
    }
    if not is_bili:
        # YouTube：extract_flat 快搜（条目信息足够）
        opts["extract_flat"] = "in_playlist"
        opts["playlist_items"] = f"1-{count}"
    query = (f"bilisearch:{keyword}" if is_bili
             else f"ytsearch{count}:{keyword}")
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(query, download=False)
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
             root: str = DEFAULT_ROOT, max_height: int = 720,
             source: str = "youtube") -> str:
    """下载单条视频入库（截取可选），返回入库文件路径。

    注：yt-dlp download_sections 在本机不生效 → 下载完整（限高控大小）
    → ffmpeg 本地截取 → 删原片；排除 AV1 编码保证播放器兼容。
    B 站下载带 Referer（防盗链）。
    """
    headers = {}
    if source == "bilibili":
        headers = _bili_headers()
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
        "http_headers": headers or None,
    }
    # Pexels：直链下载（不经 yt-dlp）
    if source == "pexels":
        _pexels_download(url, os.path.join(outdir, f"{prefix}.mp4"))
        full_path = os.path.join(outdir, f"{prefix}.mp4")
        info = {"title": url}
    else:
        # B 站 412 偶发反爬 → 自动重试（强制刷新 cookie）
        last_err = None
        for attempt in range(1, 4):
            try:
                with yt_dlp.YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(url, download=True)
                break
            except Exception as e:
                last_err = e
                if "412" in str(e) and attempt < 3:
                    _BVID_COOKIE["v"] = ""  # 强制重新取 buvid cookie
                    print(f"  ⚠ B站 412（第{attempt}次），{attempt * 5}秒后重试…")
                    time.sleep(attempt * 5)
                    opts["http_headers"] = _bili_headers()
                    continue
                raise
        else:
            raise RuntimeError(f"B站下载失败（多次重试仍 412）: {last_err}")

    if source == "pexels":
        files = [os.path.basename(full_path)]
    else:
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
                     source: str = "youtube", progress=None) -> list:
    """按关键词自动抓 N 个片段入库（流水线集成用）。

    - source: youtube / bilibili（B 站用完整 URL + Referer 下载）
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
            for e in search(t, count=count * 4, source=source):  # 先不限时长收集候选
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
        # B 站要完整 URL（裸 BV ID yt-dlp 不认）；YouTube 用 ID 即可
        url = (f"https://www.bilibili.com/video/{vid}" if source == "bilibili"
               else vid)
        path = download(url, topic, start, end, desc, root, max_height,
                        source=source)
        downloaded.append(path)
    return downloaded
