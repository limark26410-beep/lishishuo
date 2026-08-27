#!/Users/local/lishishuo/venv/bin/python3
# -*- coding: utf-8 -*-
"""自动抓取视频素材入库（YouTube 为主，非商用）——CLI 包装。

用法：
  # 搜索候选（列标题/时长/频道）
  python tools/fetch_video_materials.py --search "古代中国战争 纪录片" --count 5

  # 下载入库（可按时间段截取）
  python tools/fetch_video_materials.py --download <视频链接或ID> \
      --topic 战争 --start 00:01:30 --end 00:03:00 \
      --desc "古代士兵攻城场面"

  核心逻辑在 lib/fetch_materials.py（流水线集成同用）。
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from lib.fetch_materials import DEFAULT_ROOT, search, download


def _d(sec):
    sec = int(sec or 0)
    return "%d分%02ds" % (sec // 60, sec % 60)


def main():
    ap = argparse.ArgumentParser(description="自动抓取视频素材入库（YouTube，lib 薄包装）")
    ap.add_argument("--search", help="搜索关键词")
    ap.add_argument("--count", type=int, default=5, help="搜索条数（默认5）")
    ap.add_argument("--dur-max", type=int, default=0, help="只列出时长≤该秒的视频（0=不限）")
    ap.add_argument("--download", help="视频链接或ID")
    ap.add_argument("--topic", default="素材", help="题材目录名（如 战争/历史）")
    ap.add_argument("--start", help="截取起点 HH:MM:SS（可选）")
    ap.add_argument("--end", help="截取终点 HH:MM:SS（可选）")
    ap.add_argument("--desc", help="画面描述（写进 README.txt 供 AI 选材）")
    ap.add_argument("--max-height", type=int, default=720, help="下载最高高度（默认720p控大小）")
    ap.add_argument("--root", default=DEFAULT_ROOT,
                    help="素材库根（默认 " + DEFAULT_ROOT + "）")
    args = ap.parse_args()

    if args.search:
        for e in search(args.search, args.count, args.dur_max):
            print("- %s | %s | %s | %s" % (
                e["title"][:42], _d(e["duration"]), e["channel"][:24], e["id"]))
    elif args.download:
        p = download(args.download, args.topic, args.start, args.end,
                      args.desc, args.root, args.max_height)
        print("已入库: " + p)
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
