"""
Subtitle rendering module v5
h264+colorkey subtitle burn, real-pixel positioning (1080x1920)
参数全部来自 config.yaml，不再硬编码。
"""

import os, sys, subprocess, tempfile, shutil, re
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont


# ── 默认值（可被 configure() 覆盖）──
FONT_PATH = "/Library/Fonts/AdobeHeitiStd-Regular.otf"
FONT_SIZE = 60
IMG_W = 1080
IMG_H = 1920
MARGIN_BOTTOM = 1000
MARGIN_SIDE = 90
MAX_CHARS_PER_LINE = 14
MAX_LINES = 2
BAR_HEIGHT = 300
OUTLINE_W = 3
PUNCTS = set('\uff0c\u3002\uff01\uff1f\u3001\uff1b\uff1a')

# 禁拆词表：折行时不得在词中间断行（十大谋士系列人名/成语/CTA 用语）
# 按长度降序匹配，避免"手无缚鸡之力"被"之力"这类短词抢先命中
NO_SPLIT_TERMS = sorted(set([
    # 系列与 CTA 用语
    "完整合集", "关注不迷路", "十大谋士", "下期预告", "评论区",
    "不迷路",
    # 人物（十大谋士 + 常见相关人名）
    "姜子牙", "管仲", "张良", "诸葛亮", "范蠡", "郭嘉", "荀彧",
    "王猛", "刘伯温", "姚广孝", "齐桓公", "鲍叔牙", "刘邦",
    "周文王", "姜尚", "周武王", "曹操", "刘备", "孙权",
    # 常见四字成语 / 别称
    "运筹帷幄", "手无缚鸡之力", "出师未捷", "功成身退", "王佐之才",
    "功盖诸葛", "一统江山", "黑衣宰相", "愿者上钩", "鬼才早逝",
    "决胜千里", "鞠躬尽瘁", "死而后已", "三顾茅庐",
]), key=len, reverse=True)


def _pick_font(sub: dict) -> str:
    """按平台优先顺序选第一个存在的字体（三平台）"""
    order = {
        "darwin": ("font_path", "font_path_linux", "font_path_windows"),
        "linux": ("font_path_linux", "font_path", "font_path_windows"),
        "win32": ("font_path_windows", "font_path", "font_path_linux"),
    }.get(sys.platform, ("font_path", "font_path_linux", "font_path_windows"))
    for k in order:
        fp = sub.get(k)
        if fp and os.path.exists(fp):
            return fp
    return ""


def configure(cfg: dict):
    """从 config.yaml 注入参数，取代硬编码"""
    global FONT_PATH, FONT_SIZE, IMG_W, IMG_H, MARGIN_BOTTOM
    global MARGIN_SIDE, MAX_CHARS_PER_LINE, MAX_LINES, BAR_HEIGHT, OUTLINE_W
    sub = (cfg or {}).get("subtitle", {})
    vid = (cfg or {}).get("video", {})
    fp = _pick_font(sub)
    if fp:
        FONT_PATH = fp
    FONT_SIZE = int(sub.get("font_size", FONT_SIZE))
    MARGIN_BOTTOM = int(sub.get("margin_bottom", MARGIN_BOTTOM))
    BAR_HEIGHT = int(sub.get("bar_height", BAR_HEIGHT))
    MAX_CHARS_PER_LINE = int(sub.get("max_chars_per_line", MAX_CHARS_PER_LINE))
    MAX_LINES = int(sub.get("max_lines", MAX_LINES))
    OUTLINE_W = int(sub.get("outline_width", OUTLINE_W))
    MARGIN_SIDE = int(FONT_SIZE * 1.5)
    IMG_W = int(vid.get("width", IMG_W))
    IMG_H = int(vid.get("height", IMG_H))


# ============================================================
# Text wrapping & entry processing
# ============================================================

def _wrap_text(text: str, max_chars: int = None) -> str:
    """
    折行：保证每行 <= max_chars、最多 MAX_LINES 行、零丢字。
    调用前应先用 _split_long_text 把过长文本拆成多条，
    这里只处理 <= max_chars*MAX_LINES 的文本。
    """
    mc = max_chars or MAX_CHARS_PER_LINE
    text = text.replace('\n', '').replace('\r', '').strip()
    if not text:
        return ''
    if len(text) <= mc:
        return text
    return _split_balanced(text, mc)


def _find_safe_cut(text: str, lo: int, hi: int) -> int:
    """在 [lo, hi] 内找最佳硬断点：优先靠近中点，且不切开禁拆词（人名/成语/CTA）。"""
    spans = []
    for term in NO_SPLIT_TERMS:
        start = 0
        while True:
            i = text.find(term, start)
            if i < 0:
                break
            spans.append((i, i + len(term)))
            start = i + 1
    mid = (lo + hi) / 2.0
    for cut in sorted(range(lo, hi + 1), key=lambda c: abs(c - mid)):
        if not any(s < cut < e for s, e in spans):
            return cut
    return hi


def _split_balanced(text: str, per_line: int) -> str:
    """
    在标点处靠近中点断行；找不到合适标点就硬断（避开禁拆词）。
    硬性保证：每行长度 <= per_line。
    """
    if len(text) <= per_line:
        return text

    # 候选断点：标点之后
    candidates = [i + 1 for i, ch in enumerate(text) if ch in PUNCTS]
    mid = len(text) / 2

    best, best_score = None, float('inf')
    for c in candidates:
        # 两行都必须不超限
        if c <= per_line and (len(text) - c) <= per_line:
            score = abs(c - mid)
            if score < best_score:
                best_score, best = score, c

    if best:
        return f"{text[:best]}\n{text[best:]}"

    # 没有合适标点：找不拆词的硬断点（两行都不超限）
    lo = max(1, len(text) - per_line)
    hi = min(per_line, len(text) - 1)
    cut = _find_safe_cut(text, lo, hi)
    first, second = text[:cut], text[cut:]
    if len(second) > per_line:
        second = second[:per_line]      # 理论上不会走到，防御性截断
    return f"{first}\n{second}"


def _split_long_text(text: str, max_chars: int) -> list:
    """
    把过长文本拆成多块，每块 <= max_chars（默认 = 每行上限 * 最大行数）。
    优先在标点处拆；标点不够就按字数硬拆。零丢字。
    """
    text = text.replace('\n', '').replace('\r', '').strip()
    if not text:
        return []
    if len(text) <= max_chars:
        return [text]

    chunks = []
    rest = text
    while len(rest) > max_chars:
        window = rest[:max_chars]
        # 在窗口内找最靠后的标点
        cut = -1
        for i in range(len(window) - 1, 0, -1):
            if window[i] in PUNCTS:
                cut = i + 1
                break
        # 标点太靠前（小于一半）就不用，改硬拆，避免碎块；硬拆也避开禁拆词
        if cut < max_chars // 2:
            cut = _find_safe_cut(rest, max_chars // 2, max_chars)
        chunks.append(rest[:cut])
        rest = rest[cut:]
    if rest:
        # 尾块太短就并进上一块（若并完仍不超限）
        if chunks and len(rest) <= 4 and len(chunks[-1]) + len(rest) <= max_chars:
            chunks[-1] += rest
        else:
            chunks.append(rest)
    return chunks


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

        if len(t) > 28:
            chunks = _split_long_text(t, 28)
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
) -> str:
    """
    h264+colorkey subtitle burn (real-pixel positioning)
    1. Render all subtitle PNGs (fixed BAR_HEIGHT)
    2. Encode subtitle track as h264
    3. colorkey black->transparent + overlay at fixed Y
    Y = IMG_H - BAR_HEIGHT - MARGIN_BOTTOM = 1920 - 300 - 140 = 1480
    """
    print("  Subtitle burn (h264+colorkey, real-pixel pos)...")

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

    # Overlay Y: subtitle bar sits at bottom with MARGIN_BOTTOM clearance
    ov_y = IMG_H - BAR_HEIGHT - MARGIN_BOTTOM  # 1920 - 300 - 140 = 1480
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
        *enc, output_path,
    ], check=True, capture_output=True, text=True, timeout=900)

    print(f"  Done: {Path(output_path).name}")

    shutil.rmtree(tmp_dir, ignore_errors=True)
    for p in text_cache.values():
        try:
            os.remove(p)
        except OSError:
            pass
    return output_path
