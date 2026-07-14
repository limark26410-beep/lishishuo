"""
字幕渲染模块（替代方案）
在不依赖 libass 的情况下，用 PIL + ffmpeg overlay 烧录字幕
"""

import os, subprocess, tempfile, shutil
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont


FONT_PATH = "/Library/Fonts/AdobeHeitiStd-Regular.otf"
FONT_SIZE = 36
IMG_W = 1080
IMG_H = 1920
MARGIN_BOTTOM = 80


def _parse_srt(srt_path: str) -> list:
    """解析 SRT，返回 [(start_sec, end_sec, text), ...]"""
    def _ts(t: str) -> float:
        parts = t.replace(",", ".").split(":")
        return float(parts[0]) * 3600 + float(parts[1]) * 60 + float(parts[2])

    entries = []
    with open(srt_path, "r", encoding="utf-8") as f:
        content = f.read()

    for block in content.strip().split("\n\n"):
        lines = block.strip().split("\n")
        if len(lines) < 3 or "-->" not in lines[1]:
            continue
        start_str, end_str = lines[1].split(" --> ")
        text = "\n".join(lines[2:])
        entries.append((_ts(start_str), _ts(end_str), text.strip()))
    return entries


def _render_sub(text: str, width: int = IMG_W) -> str:
    """为单条字幕生成半透明底 PNG，返回临时文件路径"""
    font = ImageFont.truetype(FONT_PATH, FONT_SIZE)
    max_w = width - 200
    lines = []
    for para in text.split("\n"):
        line = ""
        for ch in para:
            test = line + ch
            tw = font.getbbox(test)[2]
            if tw > max_w and line:
                lines.append(line)
                line = ch
            else:
                line = test
        if line:
            lines.append(line)

    lh = int(FONT_SIZE * 1.5)
    total_h = len(lines) * lh + 40

    img = Image.new("RGBA", (width, total_h), (0, 0, 0, 160))
    draw = ImageDraw.Draw(img)

    for i, lt in enumerate(lines):
        tw = font.getbbox(lt)[2]
        x = (width - tw) // 2
        y = 20 + i * lh
        draw.text((x + 1, y + 1), lt, fill=(0, 0, 0, 200), font=font)
        draw.text((x, y), lt, fill=(255, 255, 255, 255), font=font)

    fd, out = tempfile.mkstemp(suffix=".png", prefix="sub_")
    os.close(fd)
    img.save(out, "PNG")
    return out


def burn_subtitles_overlay(
    video_path: str,
    srt_path: str,
    output_path: str,
    encode_args: list = None,
) -> str:
    """
    用 overlay 滤镜烧录字幕（无需 libass）
    1. 每条 SRT 条目生成一张 PNG
    2. concat demuxer 拼成字幕视频（保持透明通道）
    3. overlay 叠加到主视频
    """
    print("  Subtitle burn (PIL overlay fallback)...")

    entries = _parse_srt(srt_path)
    if not entries:
        subprocess.run(
            ["ffmpeg", "-y", "-i", video_path, "-c", "copy", output_path],
            check=True, capture_output=True, text=True,
        )
        return output_path

    print(f"  SRT: {len(entries)} entries")

    tmp_dir = tempfile.mkdtemp(prefix="subburn_")
    cache = {}  # text → png path
    concat = []

    for start, end, text in entries:
        dur = end - start
        if dur <= 0:
            continue
        if text not in cache:
            cache[text] = _render_sub(text)
        concat.append(f"file '{os.path.relpath(cache[text], tmp_dir)}'")
        concat.append(f"duration {dur:.3f}")

    if not concat:
        subprocess.run(
            ["ffmpeg", "-y", "-i", video_path, "-c", "copy", output_path],
            check=True, capture_output=True, text=True,
        )
        return output_path

    # 写 concat 文件
    cp = os.path.join(tmp_dir, "c.txt")
    with open(cp, "w") as f:
        f.write("\n".join(concat))

    # 生成带 Alpha 的字幕视频
    sv = os.path.join(tmp_dir, "subs.mov")
    subprocess.run([
        "ffmpeg", "-y", "-f", "concat", "-safe", "0",
        "-i", cp,
        "-c:v", "prores_ks",
        "-pix_fmt", "yuva444p10le",
        "-r", "25",
        sv,
    ], check=True, capture_output=True, text=True)

    # 获取视频尺寸
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "stream=width,height",
         "-of", "default=noprint_wrappers=1:nokey=1", video_path],
        capture_output=True, text=True, check=True,
    )
    dims = r.stdout.strip().split("\n")
    vw = int(dims[0]) if dims[0] else IMG_W
    vh = int(dims[1]) if len(dims) > 1 and dims[1] else IMG_H

    # Overlay：底部居中
    oy = vh - MARGIN_BOTTOM
    enc = encode_args or [
        "-c:v", "h264_videotoolbox", "-b:v", "3000k",
        "-c:a", "copy",
    ]

    subprocess.run([
        "ffmpeg", "-y",
        "-i", video_path,
        "-i", sv,
        "-filter_complex",
        f"[0:v][1:v]overlay=0:{oy}:format=auto[v]",
        "-map", "[v]", "-map", "0:a",
        *enc,
        "-shortest",
        output_path,
    ], check=True, capture_output=True, text=True)

    print(f"  ✅ Subtitles burned: {Path(output_path).name}")

    # 清理
    shutil.rmtree(tmp_dir, ignore_errors=True)
    for p in cache.values():
        try:
            os.remove(p)
        except OSError:
            pass
    return output_path
