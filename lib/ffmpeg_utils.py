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


def probe_video_size(path: str):
    """探测视频分辨率 (w, h)，失败返回 None"""
    r = subprocess.run(["ffmpeg", "-i", path], capture_output=True, text=True)
    import re
    m = re.search(r"Stream.*?Video:.*?(\d{3,5})x(\d{3,5})", r.stderr)
    if not m:
        return None
    return int(m.group(1)), int(m.group(2))


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

    # 智能适配任意尺寸/比例的图片（含用户自拍的横图、方图）：
    #   scale=...:force_original_aspect_ratio=increase 按比例放大到刚好填满
    #   crop 居中裁掉多余部分 —— 不变形、不留黑边
    vf = (
        f"scale={scale_w}:{scale_h}:force_original_aspect_ratio=increase,"
        f"crop={scale_w}:{scale_h},"
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


# ─────────── 视频片段截取（视频模式） ───────────

def build_video_clip(
    video_path: str,
    output_path: str,
    start: float,
    duration: float,
    width: int = 1080,
    height: int = 1920,
    fps: int = 25,
    cfg: dict = None,
    crop_keep: float = None,
) -> str:
    """
    截取视频片段 + 画幅适配 + 静音，输出规整竖屏片段
    - 输入侧 -ss 快速 seek（起点可能偏移 1 个 GOP，轮播素材无碍）
    - scale + crop 等比放大裁剪居中，不拉伸不变形
    - crop_keep（GL-20260828 去原视频字幕）：横屏素材先垂直双端裁剪
      （保留画面中间 crop_keep 比例，上下字幕带裁出画面），None=不裁
    - -an 静音（方案定稿默认静音，只留 TTS 配音 + BGM）
    - 编码参数对齐 build_ken_burns_clip（libx264/平台编码器、yuv420p、fps）
    """
    duration_sec = max(float(duration), 1.0)
    vf_pre = ""
    if crop_keep and 0 < crop_keep < 1:
        size = probe_video_size(video_path)
        if size and size[0] > size[1]:
            # 横屏：先裁上下（保留中间 crop_keep），再放大填满竖屏
            yoff = (1 - crop_keep) / 2
            vf_pre = f"crop=iw:ih*{crop_keep}:0:ih*{yoff:.3f},"
    vf = (
        vf_pre
        + f"scale={width}:{height}:force_original_aspect_ratio=increase,"
        + f"crop={width}:{height},setsar=1,fps={fps}"
    )
    encoder_args = _build_encoder_args(cfg or {})
    avail = get_media_duration(video_path)
    # GL-20260817-02：起点+时长超出素材全长 → 从素材头循环填充（-stream_loop 必须放 -i 前）
    if start + duration_sec > avail:
        start = 0.0
        print(f"  ⚠ 段落时长 {duration_sec:.1f}s > 素材全长 {avail:.1f}s，"
              f"单素材循环填充（画面会重复）")
        cmd = (["ffmpeg", "-y", "-stream_loop", "-1", "-ss", "0",
                "-i", str(video_path), "-t", f"{duration_sec:.2f}",
                "-vf", vf]
               + encoder_args
               + ["-an", str(output_path)])
    else:
        cmd = (["ffmpeg", "-y", "-ss", f"{start:.2f}", "-i", str(video_path),
                "-t", f"{duration_sec:.2f}", "-vf", vf]
               + encoder_args
               + ["-an", str(output_path)])
    subprocess.run(cmd, check=True, capture_output=True, text=True)

    actual_dur = get_media_duration(output_path)
    print(f"  Video clip: {Path(output_path).name} -> {actual_dur:.1f}s "
          f"(start={start:.1f}s)")
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


# ─────────── 混合转场（GL-20260817-05 5b-1） ───────────

def mixed_concat(
    clip_paths: list,
    emotions: list,
    output_path: str,
    cfg: dict = None,
    transition: str = "fade",
    duration: float = 1.0,
    fps: int = 25,
) -> str:
    """混合转场：与高潮段相邻的衔接硬切，其余 xfade 溶解。

    规则：衔接在 clip i 与 i+1 之间，若其中任一段情绪是「高潮」→ 硬切（增强冲击）；
    否则 xfade 溶解（抒情过渡）。实现：按硬切点切块，块内 xfade 链式、块间 concat。
    emotions: [{index, emotion}, ...] 与段落等长（长度不足按平叙兜底）。
    """
    if len(clip_paths) <= 1:
        subprocess.run(["ffmpeg", "-y", "-i", clip_paths[0], "-c", "copy",
                        output_path], check=True, capture_output=True, text=True)
        return output_path

    def _emo(i):
        return emotions[i].get("emotion", "平叙") if i < len(emotions) else "平叙"

    hard_cuts = set()
    for i in range(len(clip_paths) - 1):
        if "高潮" in (_emo(i), _emo(i + 1)):
            hard_cuts.add(i)

    blocks, cur = [], [clip_paths[0]]
    for i in range(1, len(clip_paths)):
        if (i - 1) in hard_cuts:
            blocks.append(cur)
            cur = [clip_paths[i]]
        else:
            cur.append(clip_paths[i])
    blocks.append(cur)

    n_join = len(clip_paths) - 1
    print(f"  混合转场: 硬切 {len(hard_cuts)} 处 / xfade {n_join - len(hard_cuts)} 处"
          f"（→ {len(blocks)} 块）")

    tmp_dir = tempfile.mkdtemp(prefix="mixed_")
    try:
        block_files = []
        for bi, block in enumerate(blocks):
            if len(block) == 1:
                block_files.append(block[0])
            else:
                tmp = os.path.join(tmp_dir, f"block_{bi:03d}.mp4")
                xfade_concat(block, tmp, cfg=cfg, transition=transition,
                             duration=duration, fps=fps)
                block_files.append(tmp)
        if len(block_files) == 1:
            subprocess.run(["ffmpeg", "-y", "-i", block_files[0], "-c", "copy",
                            output_path], check=True, capture_output=True, text=True)
        else:
            concat_clips(block_files, output_path)
    finally:
        import shutil
        shutil.rmtree(tmp_dir, ignore_errors=True)
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
    seg_audio: list = None,
    bgm_map: dict = None,
) -> str:
    """混入配音 + 背景乐。

    单 BGM 模式（seg_audio/bgm_map 为空）：BGM 循环铺满全程（现状逻辑）。
    分段配乐模式（GL-20260817-04）：seg_audio=[{start,end,emotion},...]，
    bgm_map={情绪: 文件路径}——按段落起止切配音、对应情绪乐段铺满该段，
    分段混音后 concat，再 mux 回视频。情绪缺素材 → 用 bgm_path 兜底。
    """
    video_dur = get_media_duration(video_path)

    if not seg_audio or not bgm_map or not os.path.exists(bgm_path) and not any(
            os.path.exists(p) for p in bgm_map.values()):
        # 无 BGM 或未启用分段配乐 → 现状单 BGM / 仅配音
        return _mix_audio_single(video_path, audio_path, bgm_path, output_path,
                                 bgm_volume, audio_bitrate)

    # ── 分段配乐：逐段混音 → concat ──
    import tempfile
    tmpdir = tempfile.mkdtemp(prefix="mixseg_")
    try:
        seg_files = []
        for i, seg in enumerate(seg_audio):
            start = float(seg.get("start") or 0)
            end = float(seg.get("end") or start)
            dur = max(end - start, 0.3)
            emotion = seg.get("emotion") or "平叙"
            # 情绪乐段 → 缺素材用兜底 BGM
            bgm = bgm_map.get(emotion) or bgm_path
            if not os.path.exists(bgm):
                bgm = bgm_path
            if not os.path.exists(bgm):
                # 连兜底都没有：该段只留配音
                seg_v = os.path.join(tmpdir, f"seg_{i:02d}_voice.wav")
                _cut_audio(audio_path, start, dur, seg_v)
                seg_files.append(seg_v)
                continue
            seg_v = os.path.join(tmpdir, f"seg_{i:02d}_voice.wav")
            seg_m = os.path.join(tmpdir, f"seg_{i:02d}.wav")
            _cut_audio(audio_path, start, dur, seg_v)
            bgm_dur = get_media_duration(bgm)
            loop_count = max(1, math.ceil(dur / bgm_dur))
            cmd = [
                "ffmpeg", "-y",
                "-i", seg_v, "-i", bgm,
                "-filter_complex",
                f"[1:a]volume={bgm_volume},aloop=loop={loop_count}:size=2e9[bg];"
                f"[0:a][bg]amix=inputs=2:duration=first:dropout_transition=0:normalize=0,alimiter=limit=0.95:level=false[a]",
                "-map", "[a]", "-c:a", "pcm_s16le", seg_m,
            ]
            subprocess.run(cmd, check=True, capture_output=True, text=True)
            seg_files.append(seg_m)
            print(f"  [配乐 {i+1}/{len(seg_audio)}] {emotion} {dur:.1f}s"
                  f" ← {Path(bgm).name}")

        # concat 段音频 → 整段混音音频
        mixed = os.path.join(tmpdir, "mixed.wav")
        _concat_audio(seg_files, mixed)

        cmd = [
            "ffmpeg", "-y",
            "-i", video_path, "-i", mixed,
            "-c:v", "copy", "-c:a", "aac", "-b:a", audio_bitrate,
            "-shortest", output_path
        ]
        subprocess.run(cmd, check=True, capture_output=True, text=True)
    finally:
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)

    print(f"  Audio mix (分段配乐): {Path(output_path).name}")
    return output_path


def _mix_audio_single(video_path, audio_path, bgm_path, output_path,
                      bgm_volume, audio_bitrate) -> str:
    """现状单 BGM 逻辑（无 BGM 时仅配音）"""
    video_dur = get_media_duration(video_path)
    if not os.path.exists(bgm_path):
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
            f"[1:a][bg]amix=inputs=2:duration=first:dropout_transition=0:normalize=0,alimiter=limit=0.95:level=false[a]",
            "-map", "0:v", "-map", "[a]",
            "-c:v", "copy", "-c:a", "aac", "-b:a", audio_bitrate,
            "-shortest", output_path
        ]
        subprocess.run(cmd, check=True, capture_output=True, text=True)

    print(f"  Audio mix: {Path(output_path).name}")
    return output_path


def _cut_audio(audio_path: str, start: float, dur: float, out: str) -> None:
    """切一段音频（wav 无损中间格式）"""
    cmd = ["ffmpeg", "-y", "-ss", f"{start:.3f}", "-t", f"{dur:.3f}",
           "-i", audio_path, "-vn", "-c:a", "pcm_s16le", out]
    subprocess.run(cmd, check=True, capture_output=True, text=True)


def _concat_audio(files: list, out: str) -> None:
    """concat 多个同规格 wav（demuxer 直连）"""
    if len(files) == 1:
        import shutil
        shutil.copyfile(files[0], out)
        return
    list_path = out + ".txt"
    with open(list_path, "w", encoding="utf-8") as f:
        for p in files:
            f.write(f"file '{p}'\n")
    cmd = ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", list_path,
           "-c:a", "pcm_s16le", out]
    subprocess.run(cmd, check=True, capture_output=True, text=True)
    os.remove(list_path)


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
