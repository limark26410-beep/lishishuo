"""
FFmpeg 工具模块
Ken Burns 静图动效 + concat/xfade 拼接 + 烧字幕 + 混音

编码器策略：
- macOS 本地开发 → h264_videotoolbox（GPU 硬件加速，需要 brew install ffmpeg）
- Linux 生产     → libx264 + veryfast（CPU 软编码，多核友好）
- 通过 config.encoder 切换，自动探测平台
"""

import subprocess
import os
import math
import sys
import tempfile
from pathlib import Path


# ─────────── 工具函数 ───────────

def get_media_duration(path: str) -> float:
    """获取音视频时长（秒）"""
    r = subprocess.run(
        ["ffmpeg", "-i", path],
        capture_output=True, text=True
    )
    import re
    m = re.search(r'Duration: (\d+):(\d+):(\d+\.?\d*)', r.stderr)
    if not m:
        raise RuntimeError(f"Could not parse duration from ffmpeg for {path}")
    h, min_, s = float(m.group(1)), float(m.group(2)), float(m.group(3))
    return h * 3600 + min_ * 60 + s


def _detect_platform_encoder(cfg: dict) -> tuple:
    """
    根据 config + 平台自动选择编码器参数
    返回 (codec, encoder_args_list)
    """
    video_cfg = cfg.get("video", {})
    encoder = video_cfg.get("encoder", "libx264")

    # 探测平台，自动切换
    platform = sys.platform
    platform_map = video_cfg.get("encoder_platform", {})
    if encoder == "auto":
        encoder = platform_map.get(platform, "libx264")

    opts = video_cfg.get("encoder_options", {}).get(encoder, {})
    base = ["-pix_fmt", video_cfg.get("pixel_format", "yuv420p")]

    if encoder == "h264_videotoolbox":
        return encoder, [
            "-c:v", "h264_videotoolbox",
            "-b:v", opts.get("bitrate", "3000k"),
        ] + base
    else:
        return encoder, [
            "-c:v", "libx264",
            "-preset", opts.get("preset", "veryfast"),
            "-crf", str(opts.get("crf", 22)),
        ] + base


def _build_encoder_args(cfg: dict) -> list:
    """返回编码器参数列表（不含 output 路径）"""
    _, args = _detect_platform_encoder(cfg)
    return args


# ─────────── Ken Burns ───────────

def build_ken_burns_clip(
    image_path: str,
    output_path: str,
    duration: float,
    cfg: dict = None,
    width: int = 1080,
    height: int = 1920,
    fps: int = 25,
    zoom_end: float = 1.08,
    pan_speed: float = 0.0004,
    preview_scale: int = 2,
) -> str:
    """
    单张静图做 Ken Burns 慢推近效果
    先放大 preview_scale 倍再 zoompan，避免推近时糊边
    """
    output_path = str(output_path)
    duration_sec = max(duration, 3.0)
    frames = int(duration_sec * fps)

    scale_w = width * preview_scale
    scale_h = height * preview_scale
    z_start = 1.0
    z_end = zoom_end
    z_step = (z_end - z_start) / frames if frames > 0 else 0

    vf = (
        f"scale={scale_w}:{scale_h},"
        f"zoompan=z='{z_start}+(on-1)*{z_step}':"
        f"d={frames}:"
        f"x='iw/2-(iw/zoom/2)':"
        f"y='ih/2-(ih/zoom/2)':"
        f"s={width}x{height}:fps={fps}"
    )

    encoder_args = _build_encoder_args(cfg or {})

    cmd = (["ffmpeg", "-y", "-loop", "1", "-i", str(image_path),
            "-vf", vf, "-t", f"{duration_sec:.2f}"]
           + encoder_args
           + ["-an", output_path])

    subprocess.run(cmd, check=True, capture_output=True, text=True)

    actual_dur = get_media_duration(output_path)
    print(f"  Ken Burns: {Path(output_path).name} -> {actual_dur:.1f}s")
    return output_path


# ─────────── 硬切拼接（concat，不转码） ───────────

def concat_clips(
    clip_paths: list,
    output_path: str,
) -> str:
    """
    concat demuxer 硬切拼接
    不重新编码，秒出
    适合长音频多图的默认转场策略
    """
    if len(clip_paths) == 0:
        raise ValueError("No clips to concatenate")
    if len(clip_paths) == 1:
        subprocess.run(
            ["ffmpeg", "-y", "-i", clip_paths[0], "-c", "copy", output_path],
            check=True, capture_output=True, text=True
        )
        return output_path

    # 写临时 concat 文件
    tmp_dir = tempfile.mkdtemp(prefix="concat_")
    list_path = os.path.join(tmp_dir, "files.txt")
    with open(list_path, "w") as f:
        for p in clip_paths:
            f.write(f"file '{Path(p).resolve()}'\n")

    cmd = [
        "ffmpeg", "-y", "-f", "concat", "-safe", "0",
        "-i", list_path, "-c", "copy", "-an", output_path
    ]
    subprocess.run(cmd, check=True, capture_output=True, text=True)

    actual_dur = get_media_duration(output_path)
    print(f"  Concat: {len(clip_paths)} clips -> {actual_dur:.1f}s")

    try:
        import shutil
        shutil.rmtree(tmp_dir, ignore_errors=True)
    except OSError:
        pass

    return output_path


# ─────────── 交叉溶解拼接（xfade，重编码） ───────────

def xfade_concat(
    clip_paths: list,
    output_path: str,
    cfg: dict = None,
    transition: str = "fade",
    duration: float = 1.0,
    fps: int = 25,
) -> str:
    """
    相邻片段交叉溶解拼接（xfade）
    逐对合并，每轮 2 段输入，避免链式 filter_complex 爆内存
    需要重编码，用于关键转场
    """
    import tempfile as tmpmod

    if len(clip_paths) == 0:
        raise ValueError("No clips to concatenate")
    if len(clip_paths) == 1:
        subprocess.run(
            ["ffmpeg", "-y", "-i", clip_paths[0], "-c", "copy", output_path],
            check=True, capture_output=True, text=True
        )
        return output_path

    tmp_dir = tmpmod.mkdtemp(prefix="xfade_")
    current = clip_paths[0]
    remaining = clip_paths[1:]
    step = 0
    encoder_args = _build_encoder_args(cfg or {})

    while remaining:
        step += 1
        next_clip = remaining.pop(0)
        dur_current = get_media_duration(current)
        dur_next = get_media_duration(next_clip)
        offset = dur_current - duration
        total = dur_current + dur_next - duration
        tmp_out = os.path.join(tmp_dir, f"t_{step:03d}.mp4") if remaining else output_path

        filter_c = (
            f"[0:v]format=pix_fmts=yuv420p[v0];"
            f"[v0][1:v]xfade=transition={transition}:"
            f"duration={duration}:offset={offset:.2f},"
            f"format=pix_fmts=yuv420p[out]"
        )

        print(f"  xfade step {step}: "
              f"{os.path.basename(current)} + {os.path.basename(next_clip)} "
              f"→ {os.path.basename(tmp_out)} ({total:.1f}s)")

        cmd = (["ffmpeg", "-y", "-i", current, "-i", next_clip,
                "-filter_complex", filter_c, "-map", "[out]"]
               + encoder_args + [tmp_out])
        subprocess.run(cmd, check=True, capture_output=True, text=True)

        if step > 1:
            os.remove(current)
        current = tmp_out

    actual_dur = get_media_duration(output_path)
    print(f"  xfade done: {Path(output_path).name} -> {actual_dur:.1f}s")
    try:
        import shutil
        shutil.rmtree(tmp_dir, ignore_errors=True)
    except OSError:
        pass
    return output_path


# ─────────── 混音 ───────────

def mix_audio(
    video_path: str,
    audio_path: str,
    bgm_path: str,
    output_path: str,
    bgm_volume: float = 0.12,
    audio_bitrate: str = "192k",
) -> str:
    """混入配音 + 背景古风乐，BGM 循环铺满"""
    video_dur = get_media_duration(video_path)

    if not os.path.exists(bgm_path):
        # 无 BGM，仅配音
        cmd = [
            "ffmpeg", "-y",
            "-i", video_path, "-i", audio_path,
            "-c:v", "copy", "-c:a", "aac", "-b:a", audio_bitrate,
            "-shortest", output_path
        ]
        subprocess.run(cmd, check=True, capture_output=True, text=True)
    else:
        bgm_dur = get_media_duration(bgm_path)
        loop_count = math.ceil(video_dur / bgm_dur)
        cmd = [
            "ffmpeg", "-y",
            "-i", video_path, "-i", audio_path, "-i", bgm_path,
            "-filter_complex",
            f"[2:a]volume={bgm_volume},aloop=loop={loop_count}:size=2e9[bg];"
            f"[1:a][bg]amix=inputs=2:duration=first:dropout_transition=0[a]",
            "-map", "0:v", "-map", "[a]",
            "-c:v", "copy", "-c:a", "aac", "-b:a", audio_bitrate,
            "-shortest", output_path
        ]
        subprocess.run(cmd, check=True, capture_output=True, text=True)

    print(f"  Audio mix: {Path(output_path).name}")
    return output_path


# ─────────── 烧字幕 ───────────

def burn_subtitles(
    video_path: str,
    subtitle_path: str,
    output_path: str,
    cfg: dict = None,
    font: str = "AdobeHeitiStd-Regular",
    font_size: int = 18,
    font_color: str = "&H00FFFFFF",
    outline_color: str = "&H90000000",
    border_style: int = 3,
    margin_v: int = 40,
) -> str:
    """
    烧录字幕到视频
    需要 ffmpeg 编译时带 --enable-libass
    否则回退到 copy（不烧字幕）
    """
    sub_cfg = (cfg or {}).get("video", {}).get("subtitle", {})
    font = sub_cfg.get("font", font)
    platform = sys.platform
    if platform == "linux":
        font = sub_cfg.get("font_linux", "Noto Sans CJK SC")

    # 检测 subtitles 滤镜是否可用
    try:
        check = subprocess.run(
            ["ffmpeg", "-filters"],
            capture_output=True, text=True, check=True
        )
        has_subtitle_filter = "subtitles" in check.stdout
    except Exception:
        has_subtitle_filter = False

    if not has_subtitle_filter:
        print("  ⚠ subtitles 滤镜不可用（ffmpeg 缺 --enable-libass）")
        print("  → 使用 PIL overlay 方案替代")
        from subtitle_burn import burn_subtitles_overlay
        return burn_subtitles_overlay(
            video_path, subtitle_path, output_path,
            encode_args=_build_encoder_args(cfg or {}),
        )

    style = (
        f"FontName={font},"
        f"FontSize={sub_cfg.get('font_size', font_size)},"
        f"PrimaryColour={sub_cfg.get('font_color', font_color)},"
        f"OutlineColour={sub_cfg.get('outline_color', outline_color)},"
        f"BorderStyle={sub_cfg.get('border_style', border_style)},"
        f"Alignment=2,"
        f"WrapStyle=1,"
        f"MarginV={sub_cfg.get('margin_v', margin_v)},"
        f"MarginL=40,"
        f"MarginR=40"
    )

    encoder_args = _build_encoder_args(cfg or {})
    cmd = (["ffmpeg", "-y", "-i", video_path,
            "-vf", f"subtitles={subtitle_path}:force_style='{style}'"]
           + encoder_args
           + ["-c:a", "copy", output_path])

    subprocess.run(cmd, check=True, capture_output=True, text=True)
    print(f"  Burn subtitles: {Path(output_path).name}")
    return output_path
