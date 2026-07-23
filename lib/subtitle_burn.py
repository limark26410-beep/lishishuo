"""
Subtitle rendering module v4
h264+colorkey subtitle burn, real-pixel positioning (1080x1920)
PlayResX=1080, PlayResY=1920, FontSize=60, MarginV=140
"""

import os, subprocess, tempfile, shutil, re
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont


FONT_PATH = "/Library/Fonts/AdobeHeitiStd-Regular.otf"
FONT_SIZE = 60
IMG_W = 1080
IMG_H = 1920
MARGIN_BOTTOM = 280       # bottom margin in real pixels
MARGIN_SIDE = int(FONT_SIZE * 1.5)
MAX_CHARS_PER_LINE = 14
MAX_LINES = 2
BAR_HEIGHT = 300           # fixed subtitle bar height (fits 2 lines)
PUNCTS = set('\uff0c\u3002\uff01\uff1f\u3001\uff1b\uff1a')


# ============================================================
# Text wrapping & entry processing
# ============================================================

def _wrap_text(text: str, max_chars: int = MAX_CHARS_PER_LINE) -> str:
    """Strict line wrap: <=max_chars/line, <=2 lines. Never overflow."""
    text = text.replace('\n', '').replace('\r', '').strip()
    if not text:
        return ''
    if len(text) <= max_chars:
        return text

    limit = max_chars * MAX_LINES  # 28

    # Should have been split by postprocess_srt if > 28
    # If somehow still > 28, truncate to 28 to enforce the rule
    if len(text) > limit:
        text = text[:limit]

    return _split_balanced(text, max_chars)


def _split_balanced(text: str, per_line: int) -> str:
    """Split at punctuation near midpoint. STRICT: both halves <= per_line."""
    if len(text) <= per_line:
        return text

    candidates = [i + 1 for i, ch in enumerate(text) if ch in PUNCTS]
    mid = len(text) // 2

    # Find punctuation where both halves <= per_line, closest to midpoint
    best, best_score = None, float('inf')
    for c in candidates:
        if c <= per_line and (len(text) - c) <= per_line:
            score = abs(c - mid)
            if score < best_score:
                best_score, best = score, c

    if best:
        return f"{text[:best]}\n{text[best:]}"

    # No perfect punctuation split -> hard break at per_line (never exceed)
    return f"{text[:per_line]}\n{text[per_line:per_line*2]}"


def _split_long_text(text: str, max_chars: int) -> list:
    """Split long text into chunks at punctuation boundaries"""
    chunks = []
    while len(text) > max_chars:
        split_at = max_chars
        for i in range(max_chars - 1, max_chars // 2, -1):
            if text[i] in PUNCTS:
                split_at = i + 1
                break
        chunks.append(text[:split_at])
        text = text[split_at:]
    if text:
        chunks.append(text)
    return chunks


# ============================================================
# SRT parsing & postprocessing
# ============================================================

def _parse_srt(srt_path: str) -> list:
    """Parse SRT, return [(start_sec, end_sec, text), ...]"""
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


def postprocess_srt(srt_path: str, output_path: str = None) -> str:
    """
    SRT postprocess: split long entries + smart line wrap
    - Entries >28 chars split by punctuation, time distributed by char count
    - No merging (edge-tts entries already have natural pauses)
    - <=14 chars/line, <=2 lines, zero character loss
    """
    if output_path is None:
        output_path = srt_path

    entries = _parse_srt(srt_path)
    if not entries:
        return output_path

    processed = []
    for start, end, text in entries:
        t = text.replace('\n', '').replace('\r', '').strip()
        if not t:
            continue
        dur = end - start
        if dur <= 0:
            continue

        if len(t) > 26:
            chunks = _split_long_text(t, 26)
            chars_per_sec = len(t) / dur if dur > 0 else 10
            chunk_start = start
            for chunk in chunks:
                chunk_dur = len(chunk) / chars_per_sec
                processed.append((chunk_start, chunk_start + chunk_dur, chunk))
                chunk_start += chunk_dur
        else:
            processed.append((start, end, t))

    lines_out = []
    idx = 1
    for start, end, text in processed:
        wrapped = _wrap_text(text)
        dur = end - start
        if dur <= 0 or not wrapped:
            continue

        def _fmt(sec: float) -> str:
            h = int(sec // 3600)
            m = int((sec % 3600) // 60)
            s = sec % 60
            return f"{h:02d}:{m:02d}:{s:06.3f}".replace(".", ",")

        lines_out.append(str(idx))
        lines_out.append(f"{_fmt(start)} --> {_fmt(end)}")
        lines_out.append(wrapped)
        lines_out.append("")
        idx += 1

    with open(output_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines_out))

    print(f"  SRT postprocess: {len(entries)}->{idx-1} entries -> {output_path}")
    return output_path


# ============================================================
# PNG rendering - fixed bar height, bottom-aligned
# ============================================================

def _render_png(text: str, idx: int = 0) -> str:
    """Render subtitle PNG: fixed BAR_HEIGHT, text bottom-aligned with MARGIN_BOTTOM"""
    font = ImageFont.truetype(FONT_PATH, FONT_SIZE)
    max_w = IMG_W - MARGIN_SIDE * 2
    lines = text.split('\n')

    # Shrink font if needed to fit width
    needs_shrink = any(font.getbbox(lt)[2] > max_w for lt in lines)
    if needs_shrink:
        max_tw = max(font.getbbox(lt)[2] for lt in lines)
        ratio = max_w / max_tw
        fs = max(int(FONT_SIZE * ratio), 36)
        font = ImageFont.truetype(FONT_PATH, fs)

    # Fixed-height PNG, black background (for colorkey)
    img = Image.new("RGBA", (IMG_W, BAR_HEIGHT), (0, 0, 0, 255))
    draw = ImageDraw.Draw(img)

    lh = int(font.size * 1.5)
    total_text_h = lh * len(lines)
    # Bottom-align: text starts at BAR_HEIGHT - total_text_h - padding
    padding = 20
    y_start = BAR_HEIGHT - total_text_h - padding

    for i, lt in enumerate(lines):
        tw = font.getbbox(lt)[2]
        x = (IMG_W - tw) // 2
        y = y_start + i * lh
        draw.text((x + 1, y + 1), lt, fill=(80, 80, 80, 255), font=font)
        draw.text((x, y), lt, fill=(255, 255, 255, 255), font=font)

    fd, out = tempfile.mkstemp(suffix=".png", prefix=f"s{idx}_")
    os.close(fd)
    img.save(out, "PNG")
    return out


# ============================================================
# Subtitle burn entry point
# ============================================================

def burn_subtitles_overlay(
    video_path: str,
    srt_path: str,
    output_path: str,
    encode_args: list = None,
    margin_bottom: int = None,
) -> str:
    """
    h264+colorkey subtitle burn (real-pixel positioning)
    1. Render all subtitle PNGs (fixed BAR_HEIGHT)
    2. Encode subtitle track as h264
    3. colorkey black->transparent + overlay at fixed Y
    Y = IMG_H - BAR_HEIGHT - margin_bottom = 1920 - 300 - 140(200) = 1480(1420)
    """
    mb = margin_bottom if margin_bottom is not None else MARGIN_BOTTOM
    print(f"  Subtitle burn (h264+colorkey, real-pixel pos, margin_bottom={mb})...")

    entries = _parse_srt(srt_path)
    if not entries:
        subprocess.run(
            ["ffmpeg", "-y", "-i", video_path, "-c", "copy", output_path],
            check=True, capture_output=True, text=True,
        )
        return output_path

    print(f"  SRT: {len(entries)} entries")

    tmp_dir = tempfile.mkdtemp(prefix="subburn_")
    text_cache = {}
    concat_lines = []
    render_count = 0

    for start, end, text in entries:
        dur = end - start
        if dur <= 0:
            continue
        if text not in text_cache:
            text_cache[text] = _render_png(text, idx=render_count)
            render_count += 1
        concat_lines.append(f"file '{text_cache[text]}'")
        concat_lines.append(f"duration {dur:.3f}")

    print(f"  Rendered {render_count} unique PNGs (BAR_HEIGHT={BAR_HEIGHT}px)")

    cp = os.path.join(tmp_dir, "c.txt")
    with open(cp, "w") as f:
        f.write("\n".join(concat_lines))

    sv = os.path.join(tmp_dir, "subs.mp4")
    print(f"  Encoding subtitle video (h264_videotoolbox)...")
    subprocess.run([
        "ffmpeg", "-y", "-f", "concat", "-safe", "0",
        "-i", cp,
        "-c:v", "h264_videotoolbox", "-b:v", "2000k",
        "-pix_fmt", "yuv420p", "-r", "25",
        sv,
    ], check=True, capture_output=True, text=True, timeout=600)
    print(f"  Subtitle video: {os.path.getsize(sv)/1024/1024:.1f}MB")

    # Overlay Y: subtitle bar sits at bottom with margin_bottom clearance
    ov_y = IMG_H - BAR_HEIGHT - mb  # 1920 - 300 - 200 = 1420 (ep12+) or 1920-300-140=1480 (ep1-11)
    enc = encode_args or [
        "-c:v", "h264_videotoolbox", "-b:v", "3000k",
        "-c:a", "copy",
    ]

    print(f"  Overlaying @ y={ov_y} (margin_bottom={MARGIN_BOTTOM})...")
    subprocess.run([
        "ffmpeg", "-y",
        "-i", video_path, "-i", sv,
        "-filter_complex",
        f"[1:v]colorkey=0x000000:similarity=0.1:blend=0.0[sub];"
        f"[0:v][sub]overlay=0:{ov_y}[v]",
        "-map", "[v]", "-map", "0:a",
        *enc, "-shortest", output_path,
    ], check=True, capture_output=True, text=True, timeout=900)

    print(f"  Done: {Path(output_path).name}")

    shutil.rmtree(tmp_dir, ignore_errors=True)
    for p in text_cache.values():
        try:
            os.remove(p)
        except OSError:
            pass
    return output_path
