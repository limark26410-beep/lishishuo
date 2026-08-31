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
from tts_utils import (
    generate_tts,
    generate_tts_segmented,
    generate_tts_doubao,
    vtt_to_srt,
)
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


def _deep_merge(base: dict, override: dict) -> None:
    """递归合并：override 的值覆盖 base（dict 递归、其他直接替换）"""
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v


def _tag_emotions_safe(segments: list):
    """段落情绪标注（图片/视频模式共用，失败返回 None 不阻断）"""
    try:
        from emotion_tag import tag_emotions
        return tag_emotions(segments, os.environ.get("DEEPSEEK_API_KEY", ""))
    except Exception as e:  # noqa: BLE001
        print(f"  ⚠ 情绪标注失败（{type(e).__name__}: {e}）")
        return None


def _build_bgm_map(bgm_cfg: dict) -> dict:
    """扫描配乐库根目录下的情绪子目录，返回 {情绪: 乐文件路径}。

    规范：{library_root}/{悬念|平叙|高潮|收束}/*.mp3；每情绪取第一个文件
    （素材库有备选后可按期轮换）。库不存在/为空 → 返回空 dict（混音走单 BGM 兜底）。
    """
    root = os.path.expanduser(bgm_cfg.get("library_root", "~/历史说素材/bgm/"))
    if not os.path.isdir(root):
        return {}
    from emotion_tag import EMOTIONS
    m = {}
    for emo in EMOTIONS:
        d = os.path.join(root, emo)
        if not os.path.isdir(d):
            continue
        files = sorted(
            f for f in os.listdir(d)
            if f.lower().endswith((".mp3", ".wav", ".m4a", ".flac"))
        )
        if files:
            m[emo] = os.path.join(d, files[0])
    return m


def apply_canvas(cfg: dict, canvas) -> None:
    """GL-20260828：应用画布（成片画幅）——覆盖 video 尺寸 + 生图尺寸。

    canvas 取 config.yaml canvas.presets 的键（portrait/landscape）；None → 用 default。
    未知画布 → 警告并回退默认。竖屏（portrait）= 现状尺寸，行为不变。
    """
    canvas_cfg = cfg.get("canvas", {})
    presets = canvas_cfg.get("presets", {})
    if not canvas:
        canvas = canvas_cfg.get("default", "portrait")
    preset = presets.get(canvas)
    if preset is None:
        if canvas:
            print(f"  ⚠ 未知画布 '{canvas}'（可用: {', '.join(presets) or '无'}），回退默认")
        canvas = canvas_cfg.get("default", "portrait")
        preset = presets.get(canvas) or {}
    w = int(preset.get("width", 1080))
    h = int(preset.get("height", 1920))
    cfg.setdefault("video", {})["width"] = w
    cfg.setdefault("video", {})["height"] = h
    if preset.get("image_size"):
        cfg.setdefault("image", {}).setdefault("tongyi", {})["size"] = preset["image_size"]
    # 画布自带字幕参数（横屏字号/行宽/位置与竖屏不同）→ merge 进 cfg.subtitle
    if preset.get("subtitle"):
        cfg.setdefault("subtitle", {}).update(preset["subtitle"])
    # GL-20260828：横屏成片时关闭 crop_vertical（16:9 源正好适配，不裁上下；
    # 原字幕问题由横屏压暗条处理，见第二步）
    if canvas == "landscape":
        cfg.setdefault("video", {}).setdefault("crop_vertical", {})["enabled"] = False
    print(f"  ▶ 画布: {canvas}（{w}x{h}，生图 {preset.get('image_size', '默认')}）")


def _cv_path(episode_dir: str, name: str, canvas) -> str:
    """GL-20260828：中间文件按画布区分（portrait/None 无后缀=兼容现状，landscape 加 _landscape）"""
    if canvas in (None, "portrait"):
        return os.path.join(episode_dir, name)
    base, ext = os.path.splitext(name)
    return os.path.join(episode_dir, f"{base}_{canvas}{ext}")


def apply_style_preset(cfg: dict, style) -> None:
    """GL-20260817-03：把风格预设 deep-merge 进 cfg（预设优先）。

    style 取 config.yaml style.presets 的键（story/short）；None → 用 style.default。
    未知风格 → 警告并回退默认。故事风格（story）预设为空 = 不覆盖任何值 = 固化现状。
    """
    style_cfg = cfg.get("style", {})
    presets = style_cfg.get("presets", {})
    if not style:
        style = style_cfg.get("default", "")
    preset = presets.get(style)
    if preset is None:
        if style:
            print(f"  ⚠ 未知风格 '{style}'（可用: {', '.join(presets) or '无'}），回退默认")
        style = style_cfg.get("default", "")
        preset = presets.get(style) or {}
    if preset:
        _deep_merge(cfg, preset)
        print(f"  ▶ 风格: {style}（预设 {len(preset)} 组旋钮已应用）")
    else:
        print(f"  ▶ 风格: {style}（无覆盖 = 现状配方）")


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
    """Step 1: 配音生成（GL-20260831：支持豆包语音引擎，失败自动回退 edge-tts）"""
    print(f"\n{'='*60}")
    print("STEP 1: 配音 (TTS)")
    print(f"{'='*60}")

    script_path = os.path.join(episode_dir, "script.txt")
    audio_path = os.path.join(episode_dir, "audio.mp3")
    subs_vtt = os.path.join(episode_dir, "subs.vtt")
    subs_srt = os.path.join(episode_dir, "subs.srt")
    tts_cfg = cfg.get("tts", {})
    engine = tts_cfg.get("engine", "edge")  # edge | doubao

    # GL-20260817-05 5b-3：段落级语速——配置 tts.emotion_rates 时按段落分次 TTS
    emotion_rates = tts_cfg.get("emotion_rates") or {}
    if emotion_rates and engine == "edge":
        from script_seg import split_segments
        from emotion_tag import tag_emotions
        with open(script_path, encoding="utf-8") as f:
            body = f.read()
        segments = split_segments(body)
        emotions = tag_emotions(segments, os.environ.get("DEEPSEEK_API_KEY", ""))
        if segments and emotions and len(segments) == len(emotions):
            print(f"  ▶ 段落级语速：{emotion_rates}")
            result = generate_tts_segmented(
                segments=segments,
                emotions=emotions,
                output_audio=audio_path,
                output_subs=subs_vtt,
                voice=tts_cfg.get("voice", "zh-CN-YunjianNeural"),
                base_rate=tts_cfg.get("rate", "-4%"),
                emotion_rates=emotion_rates,
            )
            vtt_to_srt(subs_vtt, subs_srt)
            result["subs_srt"] = subs_srt
            return result
        print("  ⚠ 段落/情绪切分失败，退回整篇 TTS")

    if engine == "doubao":
        voice = tts_cfg.get("doubao", {}).get("voice", "zh_female_vv_uranus_bigtts")
        rate = tts_cfg.get("rate", "-4%")
        try:
            result = generate_tts_doubao(
                script_path=script_path,
                output_audio=audio_path,
                output_subs=subs_vtt,
                voice=voice,
                rate=rate,
            )
            # 豆包 SRT 就是标准 SRT；vtt_to_srt 幂等重编号，走同一管线
            vtt_to_srt(subs_vtt, subs_srt)
            result["subs_srt"] = subs_srt
            result["engine"] = "doubao"
            return result
        except Exception as e:
            print(f"  ⚠ 豆包引擎失败（{type(e).__name__}: {str(e)[:150]}），自动回退 edge-tts")
            # 清掉可能残留的半个文件，避免下游用旧音频
            for p in (audio_path, subs_vtt, subs_srt):
                if os.path.exists(p):
                    os.remove(p)

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

    # 成本护栏（GL-20260817-05 键名兼容：config 顶层是 image，旧 image_gen 兼容）
    img_cfg = cfg.get("image", cfg.get("image_gen", {}))
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


def step_mix(episode_dir: str, tts_result: dict, img_result: dict, cfg: dict, canvas=None, also_nosub=False) -> str:
    """Step 3-7: 混剪合成"""
    print(f"\n{'='*60}")
    print("STEP 3: 混剪合成")
    print(f"{'='*60}")

    audio_path = tts_result["audio_path"]
    subs_srt = tts_result["subs_srt"]
    audio_dur = tts_result["duration_sec"]
    images_dir = os.path.join(episode_dir, "images")
    clips_dir = _cv_path(episode_dir, "clips", canvas)
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
        return _video_step_mix(episode_dir, tts_result, cfg, canvas=canvas, also_nosub=also_nosub)

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

    # ── 3c. Ken Burns 逐段生成（5b-2：按段落情绪调速）──
    print(f"\n  生成 Ken Burns clips...")
    # 图片 → 段落情绪映射（稿子分段 + 情绪标注；失败则全部用全局参数）
    seg_emotions = None
    bounds = None
    try:
        from script_seg import split_segments, segment_audio_bounds
        script_path = os.path.join(episode_dir, "script.txt")
        with open(script_path, encoding="utf-8") as f:
            body = f.read()
        segments = split_segments(body)
        if segments:
            bounds = segment_audio_bounds(segments, subs_srt)
            seg_emotions = _tag_emotions_safe(segments)
            if not (bounds and seg_emotions and len(bounds) == len(seg_emotions)):
                bounds, seg_emotions = None, None
    except Exception as e:  # noqa: BLE001
        print(f"  ⚠ 段落切分失败（{type(e).__name__}: {e}），Ken Burns 用全局参数")
        bounds, seg_emotions = None, None

    kb_emo = ken_cfg.get("emotions", {})
    clip_paths = []
    for i, (img_path, dur) in enumerate(zip(image_paths, clip_durations)):
        clip_out = os.path.join(clips_dir, f"clip_{i+1:03d}.mp4")
        # 图片起点 → 所属段落情绪 → 动效参数
        emotion = None
        if seg_emotions and bounds:
            start_t = sum(clip_durations[:i]) + dur / 2  # 用图片中点对齐段落
            for b, e in zip(bounds, seg_emotions):
                if start_t < b[1]:
                    emotion = e["emotion"]
                    break
            else:
                emotion = seg_emotions[-1]["emotion"]
        if emotion:
            ep = kb_emo.get(emotion, {})
            zoom_end = ep.get("zoom_end", ken_cfg.get("zoom_end", 1.08))
            pan_speed = ep.get("pan_speed", ken_cfg.get("pan_speed", 0.0004))
        else:
            zoom_end = ken_cfg.get("zoom_end", 1.08)
            pan_speed = ken_cfg.get("pan_speed", 0.0004)
        print(f"  [{i+1}/{num_images}] {Path(img_path).name} -> {_time_str(dur)}"
              + (f" [{emotion} zoom={zoom_end}]" if emotion else ""))
        build_ken_burns_clip(
            image_path=img_path,
            output_path=clip_out,
            duration=dur,
            cfg=cfg,
            width=width, height=height, fps=fps,
            zoom_end=zoom_end,
            pan_speed=pan_speed,
            preview_scale=ken_cfg.get("preview_scale", 2),
        )
        clip_paths.append(clip_out)

    # ── 3d. 拼接 ──
    merged_video = _cv_path(episode_dir, "_merged_video.mp4", canvas)
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
    audio_mixed = _cv_path(episode_dir, "_audio_mixed.mp4", canvas)
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

    # GL-20260828：图片分支无字幕版（混音后导出）
    if also_nosub:
        tc_cfg = cfg.get("title_card", {})
        tc_dur = int(tc_cfg.get("duration", 3))
        nosub_out = _cv_path(episode_dir, "final_nosub.mp4", canvas)
        title_png0 = _cv_path(episode_dir, "title_card.png", canvas)
        if os.path.exists(title_png0):
            overlay_title_card(
                video_path=audio_mixed, title_card_png=title_png0,
                output_path=nosub_out, duration=tc_dur,
                encode_args=_encode_args(cfg, final=True))
        else:
            subprocess.run(["ffmpeg", "-y", "-loglevel", "error",
                            "-i", audio_mixed, "-c", "copy", nosub_out],
                           check=True, capture_output=True, text=True)
        print(f"  ✓ 无字幕版: {os.path.basename(nosub_out)}")

    # ── 3f. 字幕后处理 + 自动检查 ──
    print(f"\n  字幕折行处理...")
    subtitle_burn.configure(cfg)
    processed_srt = _cv_path(episode_dir, "subs_processed.srt", canvas)
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
    burned = _cv_path(episode_dir, "_burned.mp4", canvas)
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
    final_output = _cv_path(episode_dir, "final.mp4", canvas)
    title_png = _cv_path(episode_dir, "title_card.png", canvas)

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


def _video_step_mix(episode_dir: str, tts_result: dict, cfg: dict, canvas=None, also_nosub=False) -> str:
    """视频模式混剪（GL-20260814-03：AI 智能选材版）。

    段落即选材单位：稿子分段 → 段落音频时长（subs.srt 聚合）→
    选材计划（material_plan.json 已有则直接消费，否则内部调 AI 语义选材，
    失败降级顺序轮播）→ 逐段截取（clip_dur=段音频+0.5s 转场余量）→
    concat → 复用后处理（混音/字幕/片头/编码）。
    素材不足（无素材 / 池总时长 < 音频）→ 明确报错，不静默。
    """
    print(f"\n{'='*60}")
    print("STEP 3: 混剪合成（视频模式·AI选材）")
    print(f"{'='*60}")

    audio_path = tts_result["audio_path"]
    subs_srt = tts_result["subs_srt"]
    audio_dur = tts_result["duration_sec"]
    clips_dir = _cv_path(episode_dir, "clips", canvas)
    os.makedirs(clips_dir, exist_ok=True)

    video_cfg = cfg.get("video", {})
    vs_cfg = cfg.get("video_source", {})
    xfade_cfg = video_cfg.get("xfade", {})
    width = video_cfg.get("width", 1080)
    height = video_cfg.get("height", 1920)
    fps = video_cfg.get("fps", 25)

    # ── 3v1. 段落切分 + 段落音频时长 ──
    from script_seg import split_segments, segment_audio_durations
    script_path = os.path.join(episode_dir, "script.txt")
    with open(script_path, encoding="utf-8") as f:
        body = f.read()
    segments = split_segments(body)
    if not segments:
        raise RuntimeError("稿子为空，无法分段")
    seg_durs = segment_audio_durations(segments, subs_srt)
    if len(seg_durs) != len(segments):
        seg_durs = [audio_dur / len(segments)] * len(segments)
    print(f"\n  段落: {len(segments)} 段")
    for s, d in zip(segments, seg_durs):
        print(f"    [{s['index']}] {_time_str(d)} | {s['text'][:26]}…")

    # ── 3v1b. 段落情绪标注（5a 分段配乐 + 5b-1 混合转场共用，失败不阻断）──
    seg_emotions = _tag_emotions_safe(segments)

    # ── 3v2. 素材清单 + 池时长校验 ──
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
        print(f"  ⚠ 素材总时长 {_time_str(total_dur)} < 音频 {_time_str(audio_dur)}："
              f"将循环复用素材出片，画面可能重复；正式生产建议素材池 ≥ 音频时长")

    # ── 3v3. 选材计划（material_plan.json 已有→直接消费；否则 AI 选材/降级轮播）──
    plan = _resolve_video_plan(episode_dir, segments, materials)
    print(f"\n  选材计划 ({len(plan)} 段):")
    for p in plan:
        print(f"    [{p['index']}] {p['material']} @{p['clip_start']}s | "
              f"{(p.get('reason') or '')[:32]}")

    # ── 3v4. 逐段截取（clip_dur = 段音频时长 + 转场余量；xfade 时余量=xfade_dur）──
    print(f"\n  截取视频片段...")
    clip_paths = []
    # xfade 模式下每段多截 xfade_dur（重叠区消耗），最后一段不加余量（后面无转场）
    if video_cfg.get("transition", "concat") == "xfade":
        _xfade_dur = float(xfade_cfg.get("duration", 1.0))
        _tail_dur = 0.0
    else:
        _xfade_dur = 0.5
        _tail_dur = 0.5
    for i, p in enumerate(plan):
        # GL-20260814-04 越界防御：外部手拼 plan 的 index 越界/缺失 → 跳过该段不截取
        idx = p.get("index")
        try:
            idx = int(idx)
        except (TypeError, ValueError):
            idx = -1
        if idx < 0 or idx >= len(seg_durs):
            print(f"  ⚠ 选材计划段 index 越界/缺失（{p.get('index')}），跳过该段不截取")
            continue
        mat = _material_by_name(materials, p.get("material") or "") \
            or _material_by_path(materials, p.get("material_path") or "")
        if mat is None:
            # 素材找不到：顺序补一条（复用兜底逻辑），不中断
            mat = materials[i % len(materials)]
            print(f"  ⚠ 选材计划素材不存在: {p.get('material')}，顺序补用 {mat['name']}")
        is_last = (i == len(plan) - 1)
        clip_dur = seg_durs[idx] + (_tail_dur if is_last else _xfade_dur)
        start = float(p.get("clip_start") or 0)
        avail = mat["duration_sec"]
        # GL-20260817-02：不再截短 clip_dur，段落超长交给 build_video_clip 内部循环填充
        if start + clip_dur > avail:
            start = 0.0
            print(f"    ⚠ 段 {p['index']}: 起点+时长超出素材"
                  f"({clip_dur:.1f}s>{avail:.1f}s)，起点回退 0，交由循环填充")
        clip_out = os.path.join(clips_dir, f"clip_{i+1:03d}.mp4")
        print(f"  [{i+1}/{len(plan)}] {mat['name']} "
              f"{_time_str(clip_dur)} @{start:.1f}s")
        # GL-20260828：去原视频字幕（横屏素材上下双端裁剪，config video.crop_vertical 可调）
        crop_cfg = video_cfg.get("crop_vertical", {}) or {}
        crop_keep = crop_cfg.get("keep") if crop_cfg.get("enabled", True) else None
        build_video_clip(
            video_path=mat["path"],
            output_path=clip_out,
            start=start,
            duration=clip_dur,
            width=width, height=height, fps=fps,
            cfg=cfg,
            crop_keep=crop_keep,
        )
        clip_paths.append(clip_out)

    # ── 3v5. 拼接（concat 硬切 / xfade 交叉溶解 / mixed 混合，读 video.transition）──
    merged_video = _cv_path(episode_dir, "_merged_video.mp4", canvas)
    transition_mode = video_cfg.get("transition", "concat")
    if transition_mode == "xfade":
        print(f"\n  拼接 clips (xfade 交叉溶解)...")
        xfade_concat(
            clip_paths=clip_paths,
            output_path=merged_video,
            cfg=cfg,
            transition=xfade_cfg.get("transition", "fade"),
            duration=float(xfade_cfg.get("duration", 1.0)),
            fps=fps,
        )
    elif transition_mode == "mixed" and seg_emotions:
        # GL-20260817-05 5b-1：与高潮段相邻的衔接硬切，其余 xfade 溶解
        from ffmpeg_utils import mixed_concat
        print(f"\n  拼接 clips (mixed 混合转场)...")
        mixed_concat(
            clip_paths=clip_paths,
            emotions=seg_emotions,
            output_path=merged_video,
            cfg=cfg,
            transition=xfade_cfg.get("transition", "fade"),
            duration=float(xfade_cfg.get("duration", 1.0)),
            fps=fps,
        )
    else:
        print(f"\n  拼接 clips (concat 硬切)...")
        concat_clips(clip_paths=clip_paths, output_path=merged_video)

    # ── 3v6. 复用后处理：混音 → 字幕 → 片头 → 编码 ──
    # GL-20260817-04：段落情绪标注 → 分段配乐（只对视频模式生效；图片模式保持单 BGM）
    seg_audio = None
    try:
        from script_seg import segment_audio_bounds
        bounds = segment_audio_bounds(segments, subs_srt)
        if seg_emotions and len(bounds) == len(seg_emotions) == len(segments):
            seg_audio = [
                {"start": b[0], "end": b[1], "emotion": e["emotion"]}
                for b, e in zip(bounds, seg_emotions)
            ]
    except Exception as e:  # noqa: BLE001 标注失败不阻断，退回单 BGM
        print(f"  ⚠ 分段配乐准备失败（{type(e).__name__}: {e}），退回单 BGM")
        seg_audio = None
    return _post_mix(episode_dir, merged_video, tts_result, cfg, seg_audio=seg_audio, canvas=canvas, also_nosub=also_nosub)


def _resolve_video_plan(episode_dir: str, segments: list, materials: list) -> list:
    """选材计划：material_plan.json 已有 → 直接消费（预览确认路径）；
    否则内部调 AI 选材（失败降级顺序轮播）。计划永远写回文件保证可消费。"""
    plan_path = os.path.join(episode_dir, "material_plan.json")
    if os.path.exists(plan_path):
        try:
            plan = json.load(open(plan_path, encoding="utf-8"))
            if isinstance(plan, list) and plan:
                print("  选材计划: 使用 material_plan.json（预览确认路径）")
                for p in plan:
                    if not p.get("text"):
                        for s in segments:
                            if s["index"] == p.get("index"):
                                p["text"] = s["text"]
                                break
                    if not p.get("material_path"):
                        m = _material_by_name(materials, p.get("material") or "")
                        if m:
                            p["material_path"] = m["path"]
                return plan
        except Exception as e:
            print(f"  ⚠ material_plan.json 读取失败（{e}），重新选材")

    from ai_script_gen import recommend_material
    api_key = os.environ.get("DEEPSEEK_API_KEY", "")
    try:
        plan = recommend_material(segments, materials, api_key)
        print(f"  ✓ AI 选材完成: {len(plan)} 段")
    except Exception as e:
        print(f"  ⚠ AI 选材失败（{e}），降级为顺序轮播")
        plan = _fallback_video_plan(segments, materials)
    try:
        with open(plan_path, "w", encoding="utf-8") as f:
            json.dump(plan, f, ensure_ascii=False, indent=1)
    except Exception:
        pass
    return plan


def _fallback_video_plan(segments: list, materials: list) -> list:
    """降级：按顺序轮播分配（每段一条，用完循环）。

    GL-20260831：按 mtime 倒序（新素材优先）——刚抓的素材先用上，
    避免轮播总选素材库前几个旧文件导致画面不变。
    """
    mats = sorted(materials, key=lambda m: m.get("mtime", 0), reverse=True)
    plan = []
    for i, s in enumerate(segments):
        m = mats[i % len(mats)]
        plan.append({
            "index": s["index"],
            "text": s["text"],
            "material": os.path.basename(m["path"]),
            "material_path": m["path"],
            "clip_start": 0.0,
            "reason": "顺序轮播（AI 选材不可用）",
        })
    return plan


def _material_by_name(materials: list, name: str):
    """按文件名/去掉扩展名的名字找素材"""
    if not name:
        return None
    name = os.path.basename(str(name))
    for m in materials:
        if m["name"] == name or os.path.basename(m["path"]) == name:
            return m
    return None


def _material_by_path(materials: list, path: str):
    """按绝对路径找素材"""
    if not path:
        return None
    p = os.path.abspath(str(path))
    for m in materials:
        if os.path.abspath(m["path"]) == p:
            return m
    return None


def _post_mix(episode_dir: str, merged_video: str, tts_result: dict, cfg: dict,
              seg_audio: list = None, canvas: str = None,
              also_nosub: bool = False) -> str:
    """后处理（视频模式专用，与图片分支 3e-3h 同款逻辑）：
    混音 → 字幕折行检查 → 烧字幕 → 片头叠加 → 编码。
    图片分支代码保持不动，此函数只服务视频模式。
    seg_audio: [{start,end,emotion}] 分段配乐用（None=单 BGM）
    """
    audio_path = tts_result["audio_path"]
    subs_srt = tts_result["subs_srt"]
    bgm_cfg = cfg.get("bgm", {})
    enc_cfg = cfg.get("video", {}).get("encoding", {})

    # ── 混音 ──
    print(f"\n  混音...")
    audio_mixed = _cv_path(episode_dir, "_audio_mixed.mp4", canvas)
    bgm_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        bgm_cfg.get("path", "assets/bgm.mp3"),
    )
    bgm_map = _build_bgm_map(bgm_cfg) if seg_audio else None
    # GL-20260817-04：分段配乐用 seg_volume（比单 BGM 高，无人声时明显可闻）
    mix_volume = (bgm_cfg.get("seg_volume", 0.18) if seg_audio
                  else bgm_cfg.get("volume", 0.12))
    mix_audio(
        video_path=merged_video,
        audio_path=audio_path,
        bgm_path=bgm_path,
        output_path=audio_mixed,
        bgm_volume=mix_volume,
        audio_bitrate=enc_cfg.get("audio_bitrate", "192k"),
        seg_audio=seg_audio,
        bgm_map=bgm_map,
    )

    # GL-20260828：无字幕版（烧字幕前导出：混音视频 + 片头 + 编码）
    if also_nosub:
        tc_cfg = cfg.get("title_card", {})
        tc_dur = int(tc_cfg.get("duration", 3))
        nosub_out = _cv_path(episode_dir, "final_nosub.mp4", canvas)
        title_png0 = _cv_path(episode_dir, "title_card.png", canvas)
        if os.path.exists(title_png0):
            overlay_title_card(
                video_path=audio_mixed, title_card_png=title_png0,
                output_path=nosub_out, duration=tc_dur,
                encode_args=_encode_args(cfg, final=True))
        else:
            subprocess.run(["ffmpeg", "-y", "-loglevel", "error",
                            "-i", audio_mixed, "-c", "copy", nosub_out],
                           check=True, capture_output=True, text=True)
        print(f"  ✓ 无字幕版: {os.path.basename(nosub_out)}")

    # ── 字幕后处理 + 自动检查 ──
    print(f"\n  字幕折行处理...")
    subtitle_burn.configure(cfg)
    processed_srt = _cv_path(episode_dir, "subs_processed.srt", canvas)
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
    burned = _cv_path(episode_dir, "_burned.mp4", canvas)
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
    final_output = _cv_path(episode_dir, "final.mp4", canvas)
    title_png = _cv_path(episode_dir, "title_card.png", canvas)

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

def _load_env_if_exists():
    """加载 .env 到环境变量（不覆盖已存在的），供 AI 出稿/选材/生图子进程使用"""
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    try:
        if os.path.exists(env_path):
            for ln in open(env_path, encoding="utf-8").read().splitlines():
                ln = ln.strip()
                if ln and not ln.startswith("#") and "=" in ln:
                    _k, _v = ln.split("=", 1)
                    _k = _k.strip()
                    if _k and _k not in os.environ:
                        os.environ[_k] = _v.strip()
    except Exception:
        pass


def main():
    import argparse
    from concurrent.futures import ThreadPoolExecutor

    _load_env_if_exists()

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
    parser.add_argument("--fetch-keyword", default=None,
                        help="GL-20260827：抓视频素材关键词（自动搜YouTube→下载入库→走视频模式）")
    parser.add_argument("--fetch-source", default="youtube",
                        choices=["youtube", "bilibili", "pexels"],
                        help="抓素材平台：youtube / bilibili / pexels（默认youtube）")
    parser.add_argument("--fetch-url", default=None,
                        help="GL-20260828：直接按视频链接下载入库（抖音/其他，绕过搜索）")
    parser.add_argument("--fetch-topic", default=None, help="抓取素材题材目录（默认=关键词首词）")
    parser.add_argument("--fetch-count", type=int, default=3, help="抓取片段数（默认3）")
    parser.add_argument("--fetch-clip-seconds", type=int, default=90,
                        help="每片段截取秒数（默认90）")
    parser.add_argument("--no-archive", action="store_true", help="不归档到素材库")
    parser.add_argument("--style", default=None,
                        help="风格预设（config.yaml style.presets 的键，如 story/short；缺省用 style.default）")
    parser.add_argument("--rate", default=None,
                        help="手动语速（GL-20260818 D3：显式指定则优先于风格预设，如 --rate +5%%）")
    parser.add_argument("--engine", default=None, choices=["edge", "doubao"],
                        help="GL-20260831：配音引擎（edge=微软 / doubao=豆包语音；缺省用 config tts.engine）")
    parser.add_argument("--also-nosub", action="store_true",
                        help="GL-20260828：每个画幅同时出无字幕版（final_nosub.mp4）")
    parser.add_argument("--canvas", default=None,
                        help="成片画幅（config canvas.presets 的键：portrait/landscape；缺省用 canvas.default）")
    parser.add_argument("--only", choices=["subtitle", "title", "encode"],
                        help="只重跑某一步（需已有中间产物）")
    args = parser.parse_args()

    config_path = args.config
    if not os.path.exists(config_path):
        print(f"❌ 配置文件不存在: {config_path}")
        sys.exit(1)

    cfg = load_config(config_path)

    # GL-20260817-03：应用风格预设（deep-merge 进 cfg，之后全链路照跑；故事=不覆盖=现状）
    apply_style_preset(cfg, getattr(args, "style", None))

    # GL-20260828：应用画布（成片画幅：竖屏/横屏），覆盖 video 尺寸 + 生图尺寸
    # dual 时交给混剪循环各自应用，这里不预应用
    if getattr(args, "canvas", None) != "dual":
        apply_canvas(cfg, getattr(args, "canvas", None))

    # GL-20260818 D3：手动语速优先——显式 --rate 覆盖风格预设的语速
    if getattr(args, "rate", None):
        from tts_utils import normalize_rate
        cfg.setdefault("tts", {})["rate"] = normalize_rate(args.rate)
        print(f"  ▶ 手动语速优先: {cfg['tts']['rate']}（覆盖风格预设）")

    # GL-20260831：显式 --engine 覆盖配置里的配音引擎
    if getattr(args, "engine", None):
        cfg.setdefault("tts", {})["engine"] = args.engine
        print(f"  ▶ 配音引擎: {args.engine}")

    episode_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "episodes", args.episode)

    # 视频模式：--video-dir 指定素材库 → cfg.video.mode=clip + video_source.root
    video_mode = bool(getattr(args, "video_dir", None))
    if video_mode:
        cfg.setdefault("video", {})["mode"] = "clip"
        cfg.setdefault("video_source", {})["root"] = os.path.expanduser(args.video_dir)
        print(f"  ▶ 视频模式：素材库 {cfg['video_source']['root']}")

    # GL-20260827：抓视频素材——先自动搜 YouTube 下载入库，再走视频模式（流程其余不变）
    fetch_kw = getattr(args, "fetch_keyword", None)
    if fetch_kw:
        from lib.fetch_materials import fetch_by_keyword
        topic = getattr(args, "fetch_topic", None) or fetch_kw.split()[0]
        count = getattr(args, "fetch_count", None) or 3
        clip_sec = getattr(args, "fetch_clip_seconds", None) or 90
        lib_root = os.path.expanduser(
            cfg.get("video_source", {}).get("root", "~/Desktop/历史说素材/视频/"))
        print(f"  ▶ 抓取素材：「{fetch_kw}」{count} 个片段 [{getattr(args, 'fetch_source', 'youtube')}]"
              f" → {lib_root}/{topic}")
        fetch_by_keyword(fetch_kw, topic, count, clip_sec, root=lib_root,
                         source=getattr(args, "fetch_source", "youtube"),
                         progress=lambda m: print(f"    {m}"))
        cfg.setdefault("video", {})["mode"] = "clip"
        cfg.setdefault("video_source", {})["root"] = os.path.join(lib_root, topic)
        video_mode = True
        print(f"  ▶ 视频模式：素材库 {cfg['video_source']['root']}（刚抓取）")
        # 抓了新素材 → 清掉旧选材计划（强制重新选材用新素材，否则重跑同期限会复用旧画面）
        _mp = os.path.join(episode_dir, "material_plan.json")
        if os.path.exists(_mp):
            os.remove(_mp)
            print("  ⚠ 已清空旧选材计划（抓了新素材，将重新选材）")

    # GL-20260828：直接按链接下载单条素材（抖音等，绕过搜索）
    fetch_url = getattr(args, "fetch_url", None)
    if fetch_url:
        from lib.fetch_materials import download
        topic = getattr(args, "fetch_topic", None) or "素材"
        lib_root = os.path.expanduser(
            cfg.get("video_source", {}).get("root", "~/Desktop/历史说素材/视频/"))
        print(f"  ▶ 链接下载: {fetch_url[:60]}… → {lib_root}/{topic}")
        download(fetch_url, topic, None, None, f"链接素材 {fetch_url[:40]}", lib_root)
        cfg.setdefault("video", {})["mode"] = "clip"
        cfg.setdefault("video_source", {})["root"] = os.path.join(lib_root, topic)
        video_mode = True
        print(f"  ▶ 视频模式：素材库 {cfg['video_source']['root']}（链接下载）")
        _mp = os.path.join(episode_dir, "material_plan.json")
        if os.path.exists(_mp):
            os.remove(_mp)
            print("  ⚠ 已清空旧选材计划（链接下载新素材，将重新选材）")

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
        # 系列期数：优先从稿子标题提取（"十大名将 第1期 白起·不败战神"→1；
        # "决战五千年 第4期 桂陵·马陵之战·孙庞斗智"→4），缺省回退流水线期号
        import re as _re3
        _ep_num = None
        try:
            _hdr3 = open(os.path.join(episode_dir, "script.txt"),
                         encoding="utf-8").readline().strip()
            _m3 = _re3.match(r"^[\u4e00-\u9fff]{2,8}\s*第\s*(\d+)\s*期", _hdr3)
            if _m3:
                _ep_num = int(_m3.group(1))
                print(f"  (系列期数自动取自稿子: 第{_ep_num}期)")
        except Exception:
            pass
        if _ep_num is None:
            _ep_num = int(args.episode)
        series = f"{series_name} · 第{_ep_num}期"
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

    # 混剪（GL-20260828：dual = 竖屏+横屏各出一版；单画布照常）
    if tts_result and img_result:
        import copy as _copy
        cv_default = cfg.get("canvas", {}).get("default", "portrait")
        raw_cv = getattr(args, "canvas", None)
        canvases = (["portrait", "landscape"] if raw_cv == "dual"
                    else [raw_cv or cv_default])
        for cv in canvases:
            cfg_cv = _copy.deepcopy(cfg)
            apply_canvas(cfg_cv, cv)
            final_path = step_mix(episode_dir, tts_result, img_result,
                                  cfg_cv, canvas=cv,
                                  also_nosub=getattr(args, "also_nosub", False))
            print(f"  ✓ [{cv}] 成片: {final_path}")
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
            # GL-20260828：dual 时横屏成片也归档（文件名带 _横屏 后缀）
            if raw_cv == "dual" and final_path != final_path.replace("_landscape", ""):
                ls_path = os.path.join(episode_dir, "final_landscape.mp4")
                if os.path.exists(ls_path):
                    res2 = archive_episode(
                        episode_name=f"{ep_name}_横屏",
                        cfg=cfg, final_video=ls_path,
                        srt=os.path.join(episode_dir, "subs_processed_landscape.srt"),
                        script=os.path.join(episode_dir, "script.txt"),
                    )
                    if res2.get("video"):
                        print(f"  ✓ 横屏归档: {res2['video']}")
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
