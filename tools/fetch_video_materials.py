#!/Users/local/lishishuo/venv/bin/python3
# -*- coding: utf-8 -*-
"""自动抓取视频素材入库（YouTube 为主，非商用）。

用法：
  # 搜索候选（列标题/时长/频道）
  python tools/fetch_video_materials.py --search "古代中国战争 纪录片" --count 5

  # 下载入库（可按时间段截取，避免整段长纪录片占空间）
  python tools/fetch_video_materials.py --download <视频链接或ID> \
      --topic 战争 --start 00:01:30 --end 00:03:00 \
      --desc "古代士兵攻城场面（横屏，裁9:16时注意构图）"

  下载后自动写入主题目录 README.txt（每行「文件名：描述」，AI 选材读取）。
"""
import argparse
import os
import subprocess
import sys

import yt_dlp

DEFAULT_ROOT = os.path.expanduser("~/Desktop/历史说素材/视频/")


def _fmt_dur(sec: int) -> str:
    sec = int(sec or 0)
    return f"{sec // 60}分{sec % 60:02d}s"


def search(keyword: str, count: int, dur_max: int):
    opts = {
        "quiet": True, "no_warnings": True, "proxy": "",
        "extract_flat": "in_playlist", "skip_download": True,
        "playlist_items": f"1-{count}",
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(f"ytsearch{count}:{keyword}", download=False)
    entries = [e for e in (info.get("entries") or []) if e]
    print(f"搜索「{keyword}」命中 {len(entries)} 条:")
    print(f"{'#':>2} | {'时长':>8} | 标题 / 频道 / ID")
    print("-" * 80)
    for i, e in enumerate(entries):
        dur = e.get("duration") or 0
        if dur_max and dur > dur_max:
            continue
        print(f"{i+1:>2} | {_fmt_dur(dur):>8} | {e.get('title', '?')[:42]}")
        print(f"    频道: {e.get('channel', '?')[:30]} | ID: {e.get('id')}")
    return entries


def download(url: str, topic: str, start: str, end: str, desc: str,
             root: str, max_height: int):
    outdir = os.path.join(root, topic)
    os.makedirs(outdir, exist_ok=True)
    existing = [f for f in os.listdir(outdir)
                if f.lower().endswith((".mp4", ".mkv", ".webm"))]
    n = len(existing) + 1
    prefix = f"{topic}_{n:02d}"
    outtmpl = os.path.join(outdir, f"{prefix}.%(ext)s")

    # 注：yt-dlp download_sections 在本机不生效（会下全片），
    # 改为：下载完整（限高控大小）→ ffmpeg 本地截取 → 删原片
    opts = {
        "quiet": True, "no_warnings": True, "proxy": "",
        "format": f"bv*[height<={max_height}]+ba/b[height<={max_height}]",
        "merge_output_format": "mp4",
        "outtmpl": outtmpl,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)

    files = [f for f in os.listdir(outdir)
             if f.startswith(prefix) and f.lower().endswith((".mp4", ".mkv", ".webm"))]
    if not files:
        print("❌ 下载后未找到文件")
        sys.exit(1)
    full_path = os.path.join(outdir, files[0])

    # 时间段截取（ffmpeg 本地切，更可靠）
    if start and end:
        clip_tmp = os.path.join(outdir, f"{prefix}.clip.mp4")
        cmd = ["ffmpeg", "-y", "-loglevel", "error",
               "-ss", start, "-to", end, "-i", full_path,
               "-c", "copy", clip_tmp]
        subprocess.run(cmd, check=True, capture_output=True, text=True)
        os.remove(full_path)  # 删原片
        clip_path = os.path.join(outdir, f"{prefix}.mp4")
        os.replace(clip_tmp, clip_path)  # 临时名 → 正式名
        fname = f"{prefix}.mp4"
        print(f"  已按 {start}~{end} 截取片段")
    else:
        fname = files[0]

    desc = desc or f"搜索主题：{topic}"
    readme = os.path.join(outdir, "README.txt")
    with open(readme, "a", encoding="utf-8") as f:
        f.write(f"{fname}：{desc}\n")

    size_mb = os.path.getsize(os.path.join(outdir, fname)) / 1024 / 1024
    print(f"✅ 已入库: {os.path.join(outdir, fname)} ({size_mb:.1f}MB)")
    print(f"  标题: {(info.get('title') or '?')[:60]}")
    print(f"  README 已写入: {fname}：{desc}")
    return os.path.join(outdir, fname)


def main():
    ap = argparse.ArgumentParser(description="自动抓取视频素材入库（YouTube）")
    ap.add_argument("--search", help="搜索关键词")
    ap.add_argument("--count", type=int, default=5, help="搜索条数（默认5）")
    ap.add_argument("--dur-max", type=int, default=0, help="只列出时长≤该秒的视频（0=不限）")
    ap.add_argument("--download", help="视频链接或ID")
    ap.add_argument("--topic", default="素材", help="题材目录名（如 战争/历史）")
    ap.add_argument("--start", help="截取起点 HH:MM:SS（可选）")
    ap.add_argument("--end", help="截取终点 HH:MM:SS（可选）")
    ap.add_argument("--desc", help="画面描述（写进 README.txt 供 AI 选材）")
    ap.add_argument("--max-height", type=int, default=720, help="下载最高高度（默认720p控大小）")
    ap.add_argument("--root", default=DEFAULT_ROOT, help=f"素材库根（默认 {DEFAULT_ROOT}）")
    args = ap.parse_args()

    if args.search:
        search(args.search, args.count, args.dur_max)
    elif args.download:
        download(args.download, args.topic, args.start, args.end, args.desc, args.root, args.max_height)
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
