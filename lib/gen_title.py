#!/usr/bin/env python3
"""
Title card generator v3 - Episode 4 style template
===================================================
Layout (top to bottom, vertically centered):
  Main title  fs=69  GOLD  centered  (1-2 lines, 25px gap)
  Series line fs=36  WHITE centered  "上下五千年 · 第X期"
  Gold bar    400x3px GOLD  close below series line

Title clip fixed at 6 seconds (changing breaks -6s subtitle sync).
"""

import subprocess, os, sys
from PIL import Image, ImageDraw, ImageFont

FONT = '/System/Library/Fonts/PingFang.ttc'

# Template constants
MAIN_FS = 69          # main title (GOLD)
SERIES_FS = 36        # series line (WHITE, ep8+ use 36, ep7 used 42)
LINE_GAP = 25         # main title line spacing
SERIES_GAP = 50       # main -> series gap
GLINE_GAP = 18        # series -> gold bar gap
GL_W = 400            # gold bar width
GL_H = 3              # gold bar height
DUR = 3               # 3s title card (抖音优化)
W, H = 1080, 1920
FPS = 25
GOLD = "0xD4AF37"
WHITE = "0xE8E8E0"
BG = "0x0a0503"


def configure(cfg: dict):
    """从 config.yaml 注入片头参数"""
    global MAIN_FS, SERIES_FS, LINE_GAP, SERIES_GAP, GLINE_GAP
    global GL_W, GL_H, DUR, W, H, FPS
    tc = (cfg or {}).get("title_card", {})
    vid = (cfg or {}).get("video", {})
    DUR = int(tc.get("duration", DUR))
    MAIN_FS = int(tc.get("main_font_size", MAIN_FS))
    SERIES_FS = int(tc.get("series_font_size", SERIES_FS))
    LINE_GAP = int(tc.get("line_gap", LINE_GAP))
    SERIES_GAP = int(tc.get("series_gap", SERIES_GAP))
    GLINE_GAP = int(tc.get("gold_line_gap", GLINE_GAP))
    GL_W = int(tc.get("gold_line_width", GL_W))
    GL_H = int(tc.get("gold_line_height", GL_H))
    W = int(vid.get("width", W))
    H = int(vid.get("height", H))
    FPS = int(vid.get("fps", FPS))

# PIL colors
GOLD_RGBA = (212, 175, 55, 255)
WHITE_RGBA = (232, 232, 224, 255)
BG_RGBA = (10, 5, 3, 255)


def render(main_texts, series_text, out_dir, bg_image=None):
    """
    main_texts: list[str] - 1-2 main title lines
      e.g. ["两晋南北朝", "乱世与融合"]
    series_text: str - series line
      e.g. "上下五千年 · 第七期"
    bg_image: str | None - 片头背景图路径。传了就用"图片+压暗文字条+文字"，
              不传就用纯黑底。
    """
    os.makedirs(out_dir, exist_ok=True)
    clip_path = os.path.join(out_dir, "title_clip.mp4")
    card_path = os.path.join(out_dir, "title_card.png")
    gl_x = (W - GL_W) // 2

    # Calculate Y positions (vertically centered block)
    n_main = len(main_texts)
    if n_main < 1 or n_main > 2:
        raise ValueError("main_texts must have 1 or 2 lines")

    block_h = (MAIN_FS * n_main + LINE_GAP * (n_main - 1)
               + SERIES_GAP + SERIES_FS + GLINE_GAP + GL_H)
    block_top = (H - block_h) // 2

    main_ys = []
    y = block_top
    for i in range(n_main):
        main_ys.append(y)
        y += MAIN_FS + LINE_GAP

    series_y = block_top + MAIN_FS * n_main + LINE_GAP * (n_main - 1) + SERIES_GAP
    gl_y = series_y + SERIES_FS + GLINE_GAP

    # 文字块的上下范围（用于局部压暗，让文字在鲜艳背景上也清晰）
    band_top = max(0, block_top - 40)
    band_bot = min(H, gl_y + GL_H + 40)
    band_h = band_bot - band_top

    # FFmpeg filter_complex
    label = "bg"
    use_bg = bool(bg_image and os.path.exists(bg_image))
    if use_bg:
        # 背景图作为 -i 输入(input 0)：缩放填满不变形 + 文字区域压暗条
        parts = [
            f"[0:v]scale={W}:{H}:force_original_aspect_ratio=increase,"
            f"crop={W}:{H},setsar=1,"
            f"drawbox=x=0:y={band_top}:w={W}:h={band_h}:"
            f"color=black@0.45:t=fill[{label}]"
        ]
    else:
        parts = [f"color=c={BG}:s={W}x{H}:d={DUR}:r={FPS}[{label}]"]

    for i, (text, y_top) in enumerate(zip(main_texts, main_ys)):
        new_label = f"t{i}"
        parts.append(
            f"[{label}]drawtext=text='{text}':fontfile={FONT}:"
            f"fontsize={MAIN_FS}:fontcolor={GOLD}:"
            f"borderw=3:bordercolor=black@0.8:"
            f"x=(w-text_w)/2:y={y_top}[{new_label}]"
        )
        label = new_label

    # Series line - WHITE
    ser_label = "tseries"
    parts.append(
        f"[{label}]drawtext=text='{series_text}':fontfile={FONT}:"
        f"fontsize={SERIES_FS}:fontcolor={WHITE}:"
        f"borderw=2:bordercolor=black@0.8:"
        f"x=(w-text_w)/2:y={series_y}[{ser_label}]"
    )
    label = ser_label

    # Gold bar - GOLD
    parts.append(
        f"[{label}]drawbox=x={gl_x}:y={gl_y}:w={GL_W}:h={GL_H}:"
        f"color={GOLD}:t=fill[vout]"
    )

    fc = ";".join(parts)
    cmd = ["ffmpeg", "-y"]
    if use_bg:
        cmd += ["-loop", "1", "-t", str(DUR), "-i", bg_image]
    cmd += ["-filter_complex", fc, "-map", "[vout]",
            "-c:v", "h264_videotoolbox", "-b:v", "3000k",
            "-pix_fmt", "yuv420p", "-t", str(DUR), clip_path]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode:
        # videotoolbox 失败时回退 libx264（不改黑底，只换编码器）
        cmd2 = ["ffmpeg", "-y"]
        if use_bg:
            cmd2 += ["-loop", "1", "-t", str(DUR), "-i", bg_image]
        cmd2 += ["-filter_complex", fc, "-map", "[vout]",
                 "-c:v", "libx264", "-crf", "20",
                 "-pix_fmt", "yuv420p", "-t", str(DUR), clip_path]
        r = subprocess.run(cmd2, capture_output=True, text=True)
        if r.returncode:
            print("FFmpeg failed:", r.stderr[:1000], file=sys.stderr)
            return None, None

    # PNG card (preview) —— 有背景图就铺上去
    if use_bg:
        try:
            bg = Image.open(bg_image).convert("RGBA")
            # 缩放填满 + 居中裁剪
            scale = max(W / bg.width, H / bg.height)
            bg = bg.resize((int(bg.width*scale)+1, int(bg.height*scale)+1))
            left = (bg.width - W)//2; top = (bg.height - H)//2
            bg = bg.crop((left, top, left+W, top+H))
            img = bg
            # 文字区域压暗条
            band = Image.new("RGBA", (W, band_h), (0,0,0,115))
            img.paste(band, (0, band_top), band)
        except Exception:
            img = Image.new("RGBA", (W, H), BG_RGBA)
    else:
        img = Image.new("RGBA", (W, H), BG_RGBA)
    draw = ImageDraw.Draw(img)

    for text, y_top in zip(main_texts, main_ys):
        fm = ImageFont.truetype(FONT, MAIN_FS)
        bm = fm.getbbox(text)
        draw.text(((W - (bm[2] - bm[0])) // 2, y_top), text, GOLD_RGBA, font=fm)

    fs = ImageFont.truetype(FONT, SERIES_FS)
    bs = fs.getbbox(series_text)
    draw.text(((W - (bs[2] - bs[0])) // 2, series_y), series_text, WHITE_RGBA, font=fs)

    for y in range(GL_H):
        for x in range(GL_W):
            draw.point((gl_x + x, gl_y + y), GOLD_RGBA)
    img.save(card_path)

    sz_mb = os.path.getsize(clip_path) / 1024 / 1024
    sz_kb = os.path.getsize(card_path) / 1024
    print(f"  title_clip.mp4 ({sz_mb:.1f}MB)")
    print(f"  title_card.png ({sz_kb:.0f}KB)")
    print(f"  main_ys={[int(y) for y in main_ys]}, series_y={series_y}, gl_y={gl_y}")
    print(f"  main=GOLD(fs{MAIN_FS}) | series=WHITE(fs{SERIES_FS})")
    return clip_path, card_path


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="Title card generator v3 - Episode 4 style")
    p.add_argument("--main", nargs="+", required=True,
                   help="Main title 1-2 lines (--main 两晋南北朝 乱世与融合)")
    p.add_argument("--series", required=True, help="Series line (--series '上下五千年 · 第七期')")
    p.add_argument("--output-dir", required=True)
    a = p.parse_args()
    render(a.main, a.series, a.output_dir)
