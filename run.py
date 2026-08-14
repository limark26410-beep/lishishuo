#!/usr/bin/env python3
"""
文史长音频自动化流水线 · 主编排脚本
输入：script.txt + prompts.json
输出：final.mp4（带字幕、背景乐）

流程：
  1. 配音（edge-tts） ────┐  并行
  2. 生图（通义万相） ────┘
  3. 字幕解析 + 时间分配
  4. Ken Burns 动效（逐段生成）
  5. 视频拼接（concat 硬切 / xfade 转场）
  6. 混音（配音 + 背景乐）
  7. 烧字幕
  8. 清理临时文件

编码策略（config.yaml 控制）：
  encoder: libx264 | auto | h264_videotoolbox
  transition: concat（生产默认，秒出）| xfade（重编码，可选）
  preset: veryfast（生产默认）
"""

import os
import sys
import json
import time
import shutil
import subprocess
import threading
from pathlib import Path
from datetime import timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "lib"))

import yaml
from tts_utils import generate_tts, vtt_to_srt
import subtitle_burn
import gen_title
from pipeline_steps import (
    check_subtitles,
    overlay_title_card,
    archive_episode,
    make_review_pack,
)
from ffmpeg_utils import (
    build_ken_burns_clip,
    build_video_clip,
    concat_clips,
    xfade_concat,
    mix_audio,
    burn_subtitles,
    get_media_duration,
)


# ─────────── 工具 ───────────

def load_config(config_path: str) -> dict:
    with open(config_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _encode_args(cfg: dict, final: bool = False) -> list:
    """
    按平台和配置生成编码参数
    final=True 时强制走 libx264 + CRF（保证成片体积可控）
    中间步骤可用硬件编码器加速
    """
    v = cfg.get("video", {})
    opts264 = v.get("encoder_options", {}).get("libx264", {})

    if final:
        return ["-c:v", "libx264",
                "-crf", str(opts264.get("crf", 26)),
                "-preset", opts264.get("preset", "medium")]

    plat = sys.platform
    enc = v.get("encoder_platform", {}).get(plat, "libx264")
    opts = v.get("encoder_options", {}).get(enc, {})
    if enc == "libx264":
        return ["-c:v", "libx264",
                "-crf", str(opts.get("crf", 26)),
                "-preset", opts.get("preset", "medium")]
    return ["-c:v", enc, "-b:v", opts.get("bitrate", "3000k")]


def _time_str(sec: float) -> str:
    td = timedelta(seconds=int(sec))
    return str(td)


# ─────────── 步骤函数 ───────────

def step_tts(episode_dir: str, cfg: dict) -> dict:
    """Step 1: 配音生成"""
    print(f"\n{'='*60}")
    print("STEP 1: 配音 (TTS)")
    print(f"{'='*60}")

    script_path = os.path.join(episode_dir, "script.txt")
    audio_path = os.path.join(episode_dir, "audio.mp3")
    subs_vtt = os.path.join(episode_dir, "subs.vtt")
    subs_srt = os.path.join(episode_dir, "subs.srt")
    tts_cfg = cfg.get("tts", {})

    result = generate_tts(
        script_path=script_path,
        output_audio=audio_path,
        output_subs=subs_vtt,
        voice=tts_cfg.get("voice", "zh-CN-YunjianNeural"),
        rate=tts_cfg.get("rate", "-4%"),
    )
    vtt_to_srt(subs_vtt, subs_srt)
    result["subs_srt"] = subs_srt
    return result


def step_image_gen(episode_dir: str, cfg: dict) -> dict:
    """Step 2: 生图（通义万相）"""
    print(f"\n{'='*60}")
    print("STEP 2: 生图 (Tongyi Wanxiang)")
    print(f"{'='*60}")

    prompts_path = os.path.join(episode_dir, "prompts.json")
    images_dir = os.path.join(episode_dir, "images")
    os.makedirs(images_dir, exist_ok=True)

    with open(prompts_path, "r", encoding="utf-8") as f:
        sections = json.load(f)

    all_prompts = []
    for sec in sections:
        for p in sec.get("prompts", []):
            all_prompts.append({
                "section_id": sec["id"],
                "section_title": sec["title"],
                "prompt": p,
            })

    # 成本护栏
    img_cfg = cfg.get("image_gen", {})
    cost_guard = img_cfg.get("cost_guard", {})
    max_images = cost_guard.get("max_images_per_episode", 50)
    if len(all_prompts) > max_images:
        print(f"  ⚠ 提示词数量 ({len(all_prompts)}) 超过上限 ({max_images})，截断")
        all_prompts = all_prompts[:max_images]

    from tongyi_api import TongyiImageGen
    api_key = os.environ.get("DASHSCOPE_API_KEY", "")
    client = TongyiImageGen(api_key=api_key)

    result = client.batch_generate(
        prompts=[p["prompt"] for p in all_prompts],
        output_dir=images_dir,
        batch_size=img_cfg.get("tongyi", {}).get("batch_size", 5),
    )
    result["prompts_meta"] = all_prompts
    return result


def step_mix(episode_dir: str, tts_result: dict, img_result: dict, cfg: dict) -> str:
    """Step 3-7: 混剪合成"""
    print(f"\n{'='*60}")
    print("STEP 3: 混剪合成")
    print(f"{'='*60}")

    audio_path = tts_result["audio_path"]
    subs_srt = tts_result["subs_srt"]
    audio_dur = tts_result["duration_sec"]
    images_dir = os.path.join(episode_dir, "images")
    clips_dir = os.path.join(episode_dir, "clips")
    os.makedirs(clips_dir, exist_ok=True)

    video_cfg = cfg.get("video", {})
    ken_cfg = video_cfg.get("ken_burns", {})
    trans = video_cfg.get("transition", "concat")
    xfade_cfg = video_cfg.get("xfade", {})
    bgm_cfg = cfg.get("bgm", {})
    enc_cfg = cfg.get("video", {}).get("encoding", {})

    width = video_cfg.get("width", 1080)
    height = video_cfg.get("height", 1920)
    fps = video_cfg.get("fps", 25)

    # ── 视频模式分支（GL-20260814-02）：素材轮播截取 → concat → 复用后处理 ──
    # 图片模式代码一行不改，只在入口分流
    if video_cfg.get("mode") == "clip":
        return _video_step_mix(episode_dir, tts_result, cfg)

    # ── 3a. 解析图片结果 ──
    image_map = img_result.get("image_map", {})
    prompts_meta = img_result.get("prompts_meta", [])
    image_paths = []
    for idx in sorted(image_map.keys()):
        path = image_map[idx]
        if path and os.path.exists(path):
            image_paths.append(path)

    if not image_paths:
        raise RuntimeError("没有可用的生图结果，无法继续")

    num_images = len(image_paths)
    print(f"\n  可用图片: {num_images}")
    print(f"  音频时长: {_time_str(audio_dur)}")

    # ── 3b. 时间分配 ──
    if trans == "xfade":
        xfade_dur = xfade_cfg.get("duration", 1.0)
        total_overlap = (num_images - 1) * xfade_dur
    else:
        total_overlap = 0
    available_time = audio_dur - total_overlap
    base_duration = available_time / num_images
    clip_durations = [base_duration] * num_images

    print(f"\n  时间分配 ({num_images} 张图):")
    for i, d in enumerate(clip_durations):
        print(f"    [{i+1:02d}] {_time_str(d)} | {Path(image_paths[i]).name}")

    # ── 3c. Ken Burns 逐段生成 ──
    print(f"\n  生成 Ken Burns clips...")
    clip_paths = []
    for i, (img_path, dur) in enumerate(zip(image_paths, clip_durations)):
        clip_out = os.path.join(clips_dir, f"clip_{i+1:03d}.mp4")
        print(f"  [{i+1}/{num_images}] {Path(img_path).name} -> {_time_str(dur)}")
        build_ken_burns_clip(
            image_path=img_path,
            output_path=clip_out,
            duration=dur,
            cfg=cfg,
            width=width, height=height, fps=fps,
            zoom_end=ken_cfg.get("zoom_end", 1.08),
            pan_speed=ken_cfg.get("pan_speed", 0.0004),
            preview_scale=ken_cfg.get("preview_scale", 2),
        )
        clip_paths.append(clip_out)

    # ── 3d. 拼接 ──
    merged_video = os.path.join(episode_dir, "_merged_video.mp4")
    if trans == "xfade":
        print(f"\n  拼接 clips (xfade 交叉溶解)...")
        xfade_concat(
            clip_paths=clip_paths,
            output_path=merged_video,
            cfg=cfg,
            transition=xfade_cfg.get("transition", "fade"),
            duration=xfade_cfg.get("duration", 1.0),
            fps=fps,
        )
    else:
        print(f"\n  拼接 clips (concat 硬切)...")
        concat_clips(clip_paths=clip_paths, output_path=merged_video)

    # ── 3e. 混音 ──
    print(f"\n  混音...")
    audio_mixed = os.path.join(episode_dir, "_audio_mixed.mp4")
    bgm_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        bgm_cfg.get("path", "assets/bgm.mp3"),
    )
    mix_audio(
        video_path=merged_video,
        audio_path=audio_path,
        bgm_path=bgm_path,
        output_path=audio_mixed,
        bgm_volume=bgm_cfg.get("volume", 0.12),
        audio_bitrate=enc_cfg.get("audio_bitrate", "192k"),
    )

    # ── 3f. 字幕后处理 + 自动检查 ──
    print(f"\n  字幕折行处理...")
    subtitle_burn.configure(cfg)
    processed_srt = os.path.join(episode_dir, "subs_processed.srt")
    subtitle_burn.postprocess_srt(subs_srt, processed_srt)

    print(f"  自动检查（超长行 / 全文比对 / 行数）...")
    script_path = os.path.join(episode_dir, "script.txt")
    report = check_subtitles(processed_srt, script_path, cfg)
    print(f"    ✓ {report['entries']} 条 | 超长行 {report.get('over_length', 0)} "
          f"| 超行数 {report.get('over_lines', 0)} | 字数差 {report.get('char_diff', 0)}")

    # ── 3g. 烧字幕（v4 真分辨率方案）──
    sub_cfg = cfg.get("subtitle", {})
    print(f"\n  烧录字幕 (font={sub_cfg.get('font_size')}px, "
          f"margin_bottom={sub_cfg.get('margin_bottom')}px)...")
    burned = os.path.join(episode_dir, "_burned.mp4")
    _has_title = os.path.exists(os.path.join(episode_dir, "title_card.png"))
    subtitle_burn.burn_subtitles_overlay(
        video_path=audio_mixed,
        srt_path=processed_srt,
        output_path=burned,
        encode_args=_encode_args(cfg, final=not _has_title),
    )

    # ── 3h. 片头 overlay ──
    tc_cfg = cfg.get("title_card", {})
    tc_dur = int(tc_cfg.get("duration", 3))
    final_output = os.path.join(episode_dir, "final.mp4")
    title_png = os.path.join(episode_dir, "title_card.png")

    if os.path.exists(title_png):
        print(f"\n  片头叠加 ({tc_dur}秒)...")
        overlay_title_card(
            video_path=burned,
            title_card_png=title_png,
            output_path=final_output,
            duration=tc_dur,
            encode_args=_encode_args(cfg, final=True),
        )
    else:
        print(f"\n  ⚠ 未找到 title_card.png，跳过片头")
        shutil.move(burned, final_output)

    final_dur = get_media_duration(final_output)
    print(f"\n  ✓ final.mp4: {_time_str(final_dur)} | "
          f"{os.path.getsize(final_output)/1024/1024:.1f}MB")
    return final_output


def _video_step_mix(episode_dir: str, tts_result: dict, cfg: dict) -> str:
    """视频模式混剪：素材按顺序轮播截取 → concat → 复用后处理（混音/字幕/片头/编码）。

    素材目录 = cfg.video_source.root（run.py --video-dir 或 CLI/API 传入）。
    第一步不做段落切分：全片按整段音频均分到素材上（AI 选材是第二步）。
    素材不足（无素材 / 总时长 < 音频）→ 明确报错，不静默。
    """
    print(f"\n{'='*60}")
    print("STEP 3: 混剪合成（视频模式）")
    print(f"{'='*60}")

    audio_path = tts_result["audio_path"]
    subs_srt = tts_result["subs_srt"]
    audio_dur = tts_result["duration_sec"]
    clips_dir = os.path.join(episode_dir, "clips")
    os.makedirs(clips_dir, exist_ok=True)

    video_cfg = cfg.get("video", {})
    vs_cfg = cfg.get("video_source", {})
    width = video_cfg.get("width", 1080)
    height = video_cfg.get("height", 1920)
    fps = video_cfg.get("fps", 25)

    # ── 3v1. 素材清单（按顺序轮播，不选材）──
    from material_scanner import scan_material
    lib_root = os.path.expanduser(vs_cfg.get("root", "~/历史说素材/视频/"))
    materials = scan_material(lib_root)
    if not materials:
        raise RuntimeError(
            f"该题材缺素材：{lib_root} 下没有可用视频素材\n"
            f"请把素材按「题材/主题_编号.mp4」放进素材库（详见 使用说明.md）")

    total_dur = sum(m["duration_sec"] for m in materials)
    print(f"\n  可用素材: {len(materials)} 条 (总时长 {_time_str(total_dur)})")
    print(f"  音频时长: {_time_str(audio_dur)}")
    if total_dur < audio_dur:
        raise RuntimeError(
            f"素材总时长不足：素材 {total_dur:.0f}s < 音频 {audio_dur:.0f}s。\n"
            f"请补充素材或缩短稿子（每个题材池建议总时长 ≥ 10 分钟）")

    # ── 3v2. 时间分配：每段 = 音频 / N ──
    n = len(materials)
    base_duration = audio_dur / n
    print(f"\n  时间分配 ({n} 条素材, 每段 {_time_str(base_duration)}):")

    # ── 3v3. 逐条截取（本轮从头部开始截）──
    print(f"\n  截取视频片段...")
    clip_paths = []
    for i, m in enumerate(materials):
        clip_out = os.path.join(clips_dir, f"clip_{i+1:03d}.mp4")
        print(f"  [{i+1}/{n}] {Path(m['path']).name} "
              f"({m['duration_sec']}s {m['width']}x{m['height']})")
        build_video_clip(
            video_path=m["path"],
            output_path=clip_out,
            start=0.0,
            duration=base_duration,
            width=width, height=height, fps=fps,
            cfg=cfg,
        )
        clip_paths.append(clip_out)

    # ── 3v4. 拼接（concat 硬切，不走 xfade）──
    merged_video = os.path.join(episode_dir, "_merged_video.mp4")
    print(f"\n  拼接 clips (concat 硬切)...")
    concat_clips(clip_paths=clip_paths, output_path=merged_video)

    # ── 3v5. 复用后处理：混音 → 字幕 → 片头 → 编码 ──
    return _post_mix(episode_dir, merged_video, tts_result, cfg)


def _post_mix(episode_dir: str, merged_video: str, tts_result: dict, cfg: dict) -> str:
    """后处理（视频模式专用，与图片分支 3e-3h 同款逻辑）：
    混音 → 字幕折行检查 → 烧字幕 → 片头叠加 → 编码。
    图片分支代码保持不动，此函数只服务视频模式。
    """
    audio_path = tts_result["audio_path"]
    subs_srt = tts_result["subs_srt"]
    bgm_cfg = cfg.get("bgm", {})
    enc_cfg = cfg.get("video", {}).get("encoding", {})

    # ── 混音 ──
    print(f"\n  混音...")
    audio_mixed = os.path.join(episode_dir, "_audio_mixed.mp4")
    bgm_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        bgm_cfg.get("path", "assets/bgm.mp3"),
    )
    mix_audio(
        video_path=merged_video,
        audio_path=audio_path,
        bgm_path=bgm_path,
        output_path=audio_mixed,
        bgm_volume=bgm_cfg.get("volume", 0.12),
        audio_bitrate=enc_cfg.get("audio_bitrate", "192k"),
    )

    # ── 字幕后处理 + 自动检查 ──
    print(f"\n  字幕折行处理...")
    subtitle_burn.configure(cfg)
    processed_srt = os.path.join(episode_dir, "subs_processed.srt")
    subtitle_burn.postprocess_srt(subs_srt, processed_srt)

    print(f"  自动检查（超长行 / 全文比对 / 行数）...")
    script_path = os.path.join(episode_dir, "script.txt")
    report = check_subtitles(processed_srt, script_path, cfg)
    print(f"    ✓ {report['entries']} 条 | 超长行 {report.get('over_length', 0)} "
          f"| 超行数 {report.get('over_lines', 0)} | 字数差 {report.get('char_diff', 0)}")

    # ── 烧字幕（v4 真分辨率方案）──
    sub_cfg = cfg.get("subtitle", {})
    print(f"\n  烧录字幕 (font={sub_cfg.get('font_size')}px, "
          f"margin_bottom={sub_cfg.get('margin_bottom')}px)...")
    burned = os.path.join(episode_dir, "_burned.mp4")
    _has_title = os.path.exists(os.path.join(episode_dir, "title_card.png"))
    subtitle_burn.burn_subtitles_overlay(
        video_path=audio_mixed,
        srt_path=processed_srt,
        output_path=burned,
        encode_args=_encode_args(cfg, final=not _has_title),
    )

    # ── 片头 overlay ──
    tc_cfg = cfg.get("title_card", {})
    tc_dur = int(tc_cfg.get("duration", 3))
    final_output = os.path.join(episode_dir, "final.mp4")
    title_png = os.path.join(episode_dir, "title_card.png")

    if os.path.exists(title_png):
        print(f"\n  片头叠加 ({tc_dur}秒)...")
        overlay_title_card(
            video_path=burned,
            title_card_png=title_png,
            output_path=final_output,
            duration=tc_dur,
            encode_args=_encode_args(cfg, final=True),
        )
    else:
        print(f"\n  ⚠ 未找到 title_card.png，跳过片头")
        shutil.move(burned, final_output)

    final_dur = get_media_duration(final_output)
    print(f"\n  ✓ final.mp4: {_time_str(final_dur)} | "
          f"{os.path.getsize(final_output)/1024/1024:.1f}MB")
    return final_output


def cleanup(episode_dir: str, cfg: dict):
    cleanup_cfg = cfg.get("cleanup", {})
    import shutil

    if cleanup_cfg.get("clean_images", True):
        d = os.path.join(episode_dir, "images")
        if os.path.exists(d):
            shutil.rmtree(d)
            print(f"  🗑 清理: images/")

    if cleanup_cfg.get("clean_clips", True):
        d = os.path.join(episode_dir, "clips")
        if os.path.exists(d):
            shutil.rmtree(d)
            print(f"  🗑 清理: clips/")

    # 中间产物（保留 final.mp4 / audio.mp3 / script.txt / subs_processed.srt / 验收/）
    temp_files = [
        "_merged_video.mp4", "_audio_mixed.mp4", "_burned.mp4",
        "subs.vtt", "subs.srt", "title_clip.mp4", "title_card.png",
    ]
    freed = 0
    for fname in temp_files:
        fpath = os.path.join(episode_dir, fname)
        if os.path.exists(fpath):
            freed += os.path.getsize(fpath)
            os.remove(fpath)
    if freed:
        print(f"  🗑 清理中间文件，释放 {freed/1024/1024:.0f}MB")


# ─────────── 主入口 ───────────

def main():
    import argparse
    from concurrent.futures import ThreadPoolExecutor

    parser = argparse.ArgumentParser(description="文史长音频自动化流水线")
    parser.add_argument("--episode", "-e", default="001", help="期号 (默认: 001)")
    parser.add_argument(
        "--config", "-c",
        default=os.path.join(os.path.dirname(__file__), "config.yaml"),
        help="配置文件路径",
    )
    parser.add_argument("--skip-tts", action="store_true", help="跳过 TTS")
    parser.add_argument("--skip-images", action="store_true", help="跳过生图")
    parser.add_argument("--shuffle-images", action="store_true",
                        help="本地图片按本期随机打乱顺序（每期画面不同）")
    parser.add_argument("--no-cleanup", action="store_true", help="不清理临时文件")
    parser.add_argument("--dry-run", action="store_true", help="仅检查素材")
    # ── 新增 ──
    parser.add_argument("--title", help="片头主标题，如 '唐朝·盛世气象'（用·分隔主副标题）")
    parser.add_argument("--series", help="系列名，如 '决战五千年'（缺省则从稿子标题自动读取）")
    parser.add_argument("--title-bg", help="片头背景图路径（缺省用纯黑底）")
    parser.add_argument("--name", help="归档名，如 '16-唐朝'（缺省用期号）")
    parser.add_argument("--images-dir", help="本地图片目录（指定则不生图）")
    parser.add_argument("--video-dir", help="视频模式素材目录（指定则走视频混剪，不生成图片）")
    parser.add_argument("--no-archive", action="store_true", help="不归档到素材库")
    parser.add_argument("--only", choices=["subtitle", "title", "encode"],
                        help="只重跑某一步（需已有中间产物）")
    args = parser.parse_args()

    config_path = args.config
    if not os.path.exists(config_path):
        print(f"❌ 配置文件不存在: {config_path}")
        sys.exit(1)

    cfg = load_config(config_path)
    episode_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "episodes", args.episode)

    # 视频模式：--video-dir 指定素材库 → cfg.video.mode=clip + video_source.root
    video_mode = bool(getattr(args, "video_dir", None))
    if video_mode:
        cfg.setdefault("video", {})["mode"] = "clip"
        cfg.setdefault("video_source", {})["root"] = os.path.expanduser(args.video_dir)
        print(f"  ▶ 视频模式：素材库 {cfg['video_source']['root']}")

    if not os.path.exists(os.path.join(episode_dir, "script.txt")):
        print(f"❌ {episode_dir}/script.txt 不存在")
        sys.exit(1)

    print(f"\n{'#'*60}")
    print(f"# 文史长音频自动化流水线")
    print(f"# 期号: {args.episode}")
    print(f"# 目录: {episode_dir}")
    print(f"{'#'*60}")

    if args.dry_run:
        ok = os.path.exists(os.path.join(episode_dir, "prompts.json"))
        print(f"\n✔ script.txt: OK")
        print(f"{'✔' if ok else '❌'} prompts.json: {'OK' if ok else '不存在'}")
        print(f"   转场: {cfg.get('video', {}).get('transition', 'concat')}")
        print(f"   编码: {cfg.get('video', {}).get('encoder', 'libx264')}")
        return

    # 并行跑 TTS + 生图
    tts_result, img_result = [None], [None]
    errors = []

    def run_tts():
        if args.skip_tts:
            ap = os.path.join(episode_dir, "audio.mp3")
            ss = os.path.join(episode_dir, "subs.srt")
            sv = os.path.join(episode_dir, "subs.vtt")
            if os.path.exists(ap):
                dur = get_media_duration(ap)
                if (not os.path.exists(ss) or os.path.getsize(ss) == 0) and os.path.exists(sv):
                    print("  VTT→SRT 转换...")
                    vtt_to_srt(sv, ss)
                print(f"  ⏩ 使用已有音频: {dur:.1f}s")
                return {
                    "audio_path": ap, "subs_path": sv,
                    "subs_srt": ss if os.path.exists(ss) else None,
                    "duration_sec": dur,
                }
            raise RuntimeError("--skip-tts 但未找到 audio.mp3")
        return step_tts(episode_dir, cfg)

    def run_images():
        if video_mode:
            # 视频模式不生成图片（step_mix 走视频分支，不读 image_map）
            return {"image_map": {}, "prompts_meta": []}
        if args.skip_images:
            d = os.path.join(episode_dir, "images")
            if os.path.exists(d):
                existing = sorted([
                    os.path.join(d, f) for f in os.listdir(d)
                    if f.lower().endswith((".jpg", ".png", ".jpeg"))
                ])
                if existing:
                    # 图片顺序：默认按文件名；--shuffle-images 则按期号随机打乱
                    if getattr(args, "shuffle_images", False):
                        import random as _rd
                        seed = getattr(args, "name", None) or args.episode
                        _rd.Random(str(seed)).shuffle(existing)
                        print(f"  🔀 图片已按本期随机打乱: {len(existing)} 张")
                    print(f"  ⏩ 使用已有图片: {len(existing)} 张")
                    return {"image_map": {i: p for i, p in enumerate(existing)},
                            "prompts_meta": []}
                raise RuntimeError("--skip-images 但 images/ 为空")
            raise RuntimeError("--skip-images 但 images/ 不存在")
        return step_image_gen(episode_dir, cfg)

    with ThreadPoolExecutor(max_workers=2) as ex:
        ft = ex.submit(run_tts)
        fi = ex.submit(run_images)

        for name, future in [("tts", ft), ("images", fi)]:
            try:
                r = future.result()
                if name == "tts":
                    tts_result[0] = r
                else:
                    img_result[0] = r
            except Exception as e:
                errors.append(f"{name}: {e}")
                print(f"❌ {name} failed: {e}")

    if errors:
        print(f"\n❌ 并行步骤失败: {errors}")
        sys.exit(1)

    tts_result, img_result = tts_result[0], img_result[0]

    # 片头标题：优先命令行 --title，缺省则从稿子标题自动提取
    _title = args.title
    if not _title:
        try:
            _hdr = open(os.path.join(episode_dir, "script.txt"),
                        encoding="utf-8").readline().strip()
        except Exception:
            _hdr = ""
        # 形如"决战五千年 第4期 桂陵·马陵之战·孙庞斗智" → 取"第X期"之后的部分作标题
        import re as _re2
        m2 = _re2.match(r"^[\u4e00-\u9fff]{2,8}\s*第\s*\d+\s*期\s*(.+)$", _hdr)
        if m2:
            _title = m2.group(1).strip()
            print(f"  (片头标题自动取自稿子: {_title})")

    # 生成片头卡
    if _title:
        print(f"\n{'='*60}")
        print("STEP 2.5: 生成片头")
        print(f"{'='*60}")
        gen_title.configure(cfg)
        parts = [x.strip() for x in _title.split("·") if x.strip()]
        # 系列名：优先命令行 --series，其次稿子标题首词，再次配置，最后兜底
        series_name = getattr(args, "series", None)
        if not series_name:
            # 从稿子标题第一行取系列名（如"决战五千年 第1期 涿鹿之战..."→"决战五千年"）
            try:
                _first = open(os.path.join(episode_dir, "script.txt"),
                              encoding="utf-8").readline().strip()
            except Exception:
                _first = ""
            import re as _re
            m = _re.match(r"^([\u4e00-\u9fff]{2,8})\s*第", _first)
            if m:
                series_name = m.group(1)
        if not series_name:
            series_name = cfg.get("title_card", {}).get("series_name", "上下五千年")
        series = f"{series_name} · 第{int(args.episode)}期"
        try:
            _bg = getattr(args, "title_bg", None)
            if _bg and os.path.exists(_bg):
                print(f"  片头背景图: {_bg}")
            elif _bg:
                print(f"  ⚠ 片头背景图路径不存在: {_bg}，改用黑底")
            else:
                print(f"  片头: 纯黑底（未指定背景图）")
            gen_title.render(parts, series, episode_dir, bg_image=_bg)
            print(f"  ✓ 片头卡已生成 ({cfg.get('title_card',{}).get('duration',3)}秒)")
        except Exception as e:
            print(f"  ⚠ 片头生成失败: {e}")

    # 混剪
    if tts_result and img_result:
        final_path = step_mix(episode_dir, tts_result, img_result, cfg)
    else:
        print("❌ TTS 或生图结果缺失")
        sys.exit(1)

    # ── 归档到素材库 ──
    if not args.no_archive:
        ep_name = args.name or args.episode
        print(f"\n{'='*60}")
        print(f"STEP 4: 归档到素材库 [{ep_name}]")
        print(f"{'='*60}")
        try:
            res = archive_episode(
                episode_name=ep_name,
                cfg=cfg,
                final_video=final_path,
                audio=os.path.join(episode_dir, "audio.mp3"),
                srt=os.path.join(episode_dir, "subs_processed.srt"),
                script=os.path.join(episode_dir, "script.txt"),
                images_dir=(os.path.join(episode_dir, "images")
                            if cfg.get("output", {}).get("archive_images", False) else None),
            )
            for k, v in res.items():
                if v:
                    print(f"  ✓ {k}: {v}")
        except Exception as e:
            print(f"  ⚠ 归档失败: {e}")

    # ── 验收包 ──
    if cfg.get("review", {}).get("enabled", True):
        print(f"\n{'='*60}")
        print("STEP 5: 生成验收包")
        print(f"{'='*60}")
        review_dir = os.path.join(episode_dir, "验收")
        tc_dur = int(cfg.get("title_card", {}).get("duration", 3))
        shots = make_review_pack(final_path, review_dir, title_duration=tc_dur)
        print(f"  ✓ {len(shots)} 张截图 -> {review_dir}")
        print(f"\n  ⚠ 请人工确认：")
        print(f"     1. 拖到片尾，字幕和声音对得上")
        print(f"     2. 传手机用抖音预览，字幕没被 UI 挡住")

    # 清理
    if not args.no_cleanup:
        print(f"\n清理临时文件...")
        cleanup(episode_dir, cfg)

    print(f"\n{'='*60}")
    print(f"🎉 流水线完成！")
    print(f"   成片: {final_path}")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
