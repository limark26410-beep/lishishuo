#!/usr/bin/env python3
"""历史说 · 视频混剪 SDK CLI —— 一条命令出片

用法：
    python -m lishishuo \\
        --script 稿子.txt \\
        --素材库 ~/历史说素材/视频/商品/ \\
        --duration 3 \\
        --output ./output/

参数：
    --script   稿子文件（必填）
    --素材库   素材库目录（缺省用 config.yaml 的 video_source.root）
    --video-dir 素材库目录（英文别名）
    --duration 目标时长（分钟，缺省按稿子字数估算：约250字/分钟）
    --output   成片输出目录（缺省不拷贝，成片留在 episodes/期号/final.mp4）
    --title    片头标题（用·分隔主副标题）
    --series   系列名
    --name     归档名（缺省用期号）

本质：run.py 流水线的入口壳——写 episode 目录 → 调 run_pipeline 同款逻辑
（视频模式）→ 成片拷到 --output。日志输出到 stdout，方便外部脚本捕获。
"""

import argparse
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

BASE_DIR = Path(__file__).parent.resolve()
sys.path.insert(0, str(BASE_DIR / "lib"))


def estimate_duration_min(body: str) -> int:
    """按稿子字数估目标时长（约 250 字/分钟）"""
    han = len(re.findall(r"[\u4e00-\u9fff]", body))
    return max(1, round(han / 250))


def next_episode_id() -> str:
    eps_dir = BASE_DIR / "episodes"
    mx = 0
    if eps_dir.exists():
        for d in eps_dir.iterdir():
            if d.is_dir() and d.name.isdigit():
                mx = max(mx, int(d.name))
    return f"{mx + 1:03d}"


def main():
    ap = argparse.ArgumentParser(
        description="历史说 · 视频混剪 CLI（一条命令出片，SDK 形态 A）",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    ap.add_argument("--script", required=True, help="稿子文件路径（必填，支持 txt/md/docx）")
    ap.add_argument("--素材库", dest="video_dir", default="",
                    help="素材库目录（缺省用 config 的 video_source.root）")
    ap.add_argument("--video-dir", dest="video_dir_alias", default="",
                    help="素材库目录（英文别名）")
    ap.add_argument("--duration", type=int, default=0,
                    help="目标时长（分钟），缺省按稿子字数估算")
    ap.add_argument("--output", default="", help="成片输出目录（缺省留在 episodes/）")
    ap.add_argument("--title", default="", help="片头标题，如 '李白·诗仙传奇'")
    ap.add_argument("--series", default="", help="系列名")
    ap.add_argument("--name", default="", help="归档名，如 '17-李白'")
    ap.add_argument("--config", default=str(BASE_DIR / "config.yaml"),
                    help="配置文件路径")
    ap.add_argument("--no-cleanup", action="store_true", help="保留中间文件")
    args = ap.parse_args()

    video_dir = args.video_dir or args.video_dir_alias

    # 稿子
    script_path = Path(os.path.expanduser(args.script))
    if not script_path.exists():
        print(f"❌ 稿子文件不存在: {script_path}")
        sys.exit(1)
    body = script_path.read_text(encoding="utf-8", errors="ignore").strip()
    if not body:
        print("❌ 稿子为空")
        sys.exit(1)

    # 素材库（缺省用 config）
    if not video_dir:
        import yaml
        try:
            cfg = yaml.safe_load(open(args.config, encoding="utf-8")) or {}
            video_dir = cfg.get("video_source", {}).get("root", "~/历史说素材/视频/")
        except Exception:
            video_dir = "~/历史说素材/视频/"
        video_dir = os.path.expanduser(video_dir)
        print(f"  素材库（config 缺省）: {video_dir}")

    duration = args.duration or estimate_duration_min(body)
    print(f"▶ 期号估算时长: {duration} 分钟（{len(re.findall(r'[\\u4e00-\\u9fff]', body))} 字）")

    # 写 episode 目录
    episode = next_episode_id()
    ep_dir = BASE_DIR / "episodes" / episode
    ep_dir.mkdir(parents=True, exist_ok=True)
    (ep_dir / "script.txt").write_text(body, encoding="utf-8")
    print(f"▶ 期号 {episode} · 已写 script.txt")

    # 组 run.py 命令（视频模式）
    cmd = [sys.executable, str(BASE_DIR / "run.py"), "-e", episode,
           "--video-dir", video_dir]
    if args.title:
        cmd += ["--title", args.title]
    if args.series:
        cmd += ["--series", args.series]
    if args.name:
        cmd += ["--name", args.name]
    if args.output:
        cmd += ["--no-archive"]  # 有 --output 时成片直接拷到指定目录
    if args.no_cleanup:
        cmd += ["--no-cleanup"]

    print("▶ " + " ".join(str(c) for c in cmd))
    r = subprocess.run(cmd, cwd=str(BASE_DIR))
    if r.returncode != 0:
        print(f"❌ 流水线失败（退出码 {r.returncode}）")
        sys.exit(r.returncode)

    final = ep_dir / "final.mp4"
    if not final.exists():
        print(f"❌ 未找到成片 {final}")
        sys.exit(1)

    if args.output:
        out_dir = Path(os.path.expanduser(args.output))
        out_dir.mkdir(parents=True, exist_ok=True)
        dst = out_dir / f"{args.name or episode}.mp4"
        shutil.copy2(final, dst)
        print(f"✅ 成片已输出: {dst} ({os.path.getsize(dst)/1024/1024:.1f}MB)")
    else:
        print(f"✅ 成片: {final}")

    # 附：脚本产物说明（供外部脚本解析）
    print(f"EPISODE={episode}")
    print(f"FINAL={final if not args.output else out_dir / (args.name or episode + '.mp4')}")


if __name__ == "__main__":
    main()
