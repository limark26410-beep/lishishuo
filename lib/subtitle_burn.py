"""
字幕渲染模块
用 HEVC with alpha + overlay 滤镜烧录字幕（无需 libass）
"""

import os, subprocess, tempfile, shutil
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont


FONT_PATH = "/Library/Fonts/AdobeHeitiStd-Regular.otf"
FONT_SIZE = 60
IMG_W = 1080
IMG_H = 1920
MARGIN_BOTTOM = 80
# 防止长句换行后断词尴尬：边缘留一个半字符的余量
MARGIN_SIDE = int(FONT_SIZE * 1.5)  # ~90px


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
        a, b = lines[1].split(" --> ")
        entries.append((_ts(a), _ts(b), "\n".join(lines[2:]).strip()))
    return entries


def _render_png(text: str, idx: int = 0) -> str:
    """渲染单条字幕 PNG，返回临时路径"""
    font = ImageFont.truetype(FONT_PATH, FONT_SIZE)
    max_w = IMG_W - MARGIN_SIDE * 2
    # 去 SRT 换行，合成一行
    one_line = text.replace("\n", "").replace("\r", "")
    tw = font.getbbox(one_line)[2]
    if tw > max_w:
        # 超宽就等比缩字号到能塞下
        ratio = max_w / tw
        fs = int(FONT_SIZE * ratio)
        fs = max(fs, 36)  # 最小 36，不能再小了
        font = ImageFont.truetype(FONT_PATH, fs)
        tw = font.getbbox(one_line)[2]

    lines = [one_line]
    lh = int(font.size * 1.5)
    total_h = lh + 40
    img = Image.new("RGBA", (IMG_W, total_h), (0, 0, 0, 160))
    draw = ImageDraw.Draw(img)
    for i, lt in enumerate(lines):
        tw = font.getbbox(lt)[2]
        x = (IMG_W - tw) // 2
        y = 20 + i * lh
        draw.text((x + 1, y + 1), lt, fill=(0, 0, 0, 200), font=font)
        draw.text((x, y), lt, fill=(255, 255, 255, 255), font=font)

    fd, out = tempfile.mkstemp(suffix=".png", prefix=f"s{idx}_")
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
    用 HEVC with alpha + overlay 烧录字幕
    1. 渲染所有字幕 PNG（相同文本缓存复用）
    2. concat 拼成字幕视频（HEVC + alpha，快）
    3. overlay 叠加到主视频
    """
    print("  Subtitle burn (HEVC alpha overlay)...")

    entries = _parse_srt(srt_path)
    if not entries:
        subprocess.run(
            ["ffmpeg", "-y", "-i", video_path, "-c", "copy", output_path],
            check=True, capture_output=True, text=True,
        )
        return output_path

    print(f"  SRT: {len(entries)} entries")

    tmp_dir = tempfile.mkdtemp(prefix="subburn_")
    text_cache = {}  # text → png_path
    concat_lines = []
    render_count = 0

    for i, (start, end, text) in enumerate(entries):
        dur = end - start
        if dur <= 0:
            continue

        if text not in text_cache:
            text_cache[text] = _render_png(text, idx=render_count)
            render_count += 1

        concat_lines.append(f"file '{text_cache[text]}'")
        concat_lines.append(f"duration {dur:.3f}")

    print(f"  Rendered {render_count} unique PNGs for {len(entries)} entries")

    # 写 concat 文件
    cp = os.path.join(tmp_dir, "c.txt")
    with open(cp, "w") as f:
        f.write("\n".join(concat_lines))

    # 生成带 Alpha 的字幕视频（HEVC 比 ProRes 快 100 倍）
    sv = os.path.join(tmp_dir, "subs.mov")
    print(f"  Encoding subtitle video (HEVC + alpha)...")
    # ProRes 4444 supports alpha channel
    subprocess.run([
        "ffmpeg", "-y", "-f", "concat", "-safe", "0",
        "-i", cp,
        "-c:v", "prores_ks",
        "-profile:v", "4444",
        "-vendor", "apl0",
        "-pix_fmt", "yuva444p10le",
        "-r", "25",
        sv,
    ], check=True, capture_output=True, text=True, timeout=600)
    print(f"  Subtitle video: {os.path.getsize(sv)/1024/1024:.1f}MB")

    # 获取视频尺寸
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "stream=width,height",
         "-of", "default=noprint_wrappers=1:nokey=1", video_path],
        capture_output=True, text=True, check=True,
    )
    dims = r.stdout.strip().split("\n")
    vh = int(dims[1]) if len(dims) > 1 and dims[1] else IMG_H

    # Overlay：字幕底部固定位置
    ov_y = vh - MARGIN_BOTTOM
    enc = encode_args or [
        "-c:v", "h264_videotoolbox", "-b:v", "3000k",
        "-c:a", "copy",
    ]

    print(f"  Overlaying subtitles...")
    subprocess.run([
        "ffmpeg", "-y",
        "-i", video_path,
        "-i", sv,
        "-filter_complex",
        f"[0:v][1:v]overlay=0:{ov_y}:format=auto[v]",
        "-map", "[v]", "-map", "0:a",
        *enc,
        "-shortest",
        output_path,
    ], check=True, capture_output=True, text=True, timeout=900)

    print(f"  ✅ Subtitles burned: {Path(output_path).name}")

    # 清理
    shutil.rmtree(tmp_dir, ignore_errors=True)
    for p in text_cache.values():
        try:
            os.remove(p)
        except OSError:
            pass
    return output_path
