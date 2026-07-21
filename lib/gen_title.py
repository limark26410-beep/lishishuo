#!/usr/bin/env python3
"""
片头标题生成器 · 固定模板
====================================
从第四期（春秋战国）片头提取的固定参数：
  主标题 fs=69 → y_top=846 → center y≈877
  副标题 fs=67 → y_top=937 → center y≈967
  金线    y=1131, 400×3px, 金色 #D4AF37
  间隔: 底(T1)→顶(T2)=29px, 底(T2)→金线=133px

注: FFmpeg drawtext 的 y 是文本顶部（非基线）
"""
import subprocess, os, sys
from PIL import Image, ImageDraw, ImageFont

FONT = '/System/Library/Fonts/PingFang.ttc'

# ===== 模板常量（写死，不准改）=====
MAIN_FS = 69
MAIN_Y = 846          # FFmpeg drawtext y (文本顶部)
SUB_FS = 67
SUB_Y = 937            # FFmpeg drawtext y (文本顶部)
GL_Y = 1131            # 金线顶部y坐标
GL_W = 400             # 金线宽度
GL_H = 3               # 金线高度
DUR = 6                # 片头时长（秒）
W, H = 1080, 1920
FPS = 25
COLOR = "0xD4AF37"     # 金色


def render(main_text: str, subtitle_text: str, out_dir: str):
    os.makedirs(out_dir, exist_ok=True)
    clip_path = os.path.join(out_dir, "title_clip.mp4")
    card_path = os.path.join(out_dir, "title_card.png")
    gl_x = (W - GL_W) // 2

    # ----- FFmpeg -----
    parts = [
        f"color=c=0x0a0503:s={W}x{H}:d={DUR}:r={FPS}[bg]",
        f"[bg]drawtext=text='{main_text}':fontfile={FONT}:fontsize={MAIN_FS}:fontcolor={COLOR}:x=(w-text_w)/2:y={MAIN_Y}[t1]",
        f"[t1]drawtext=text='{subtitle_text}':fontfile={FONT}:fontsize={SUB_FS}:fontcolor={COLOR}:x=(w-text_w)/2:y={SUB_Y}[t2]",
        f"[t2]drawbox=x={gl_x}:y={GL_Y}:w={GL_W}:h={GL_H}:color={COLOR}:t=fill[vout]",
    ]
    fc = ";".join(parts)
    cmd = ["ffmpeg", "-y", "-filter_complex", fc, "-map", "[vout]",
           "-c:v", "h264_videotoolbox", "-b:v", "3000k",
           "-pix_fmt", "yuv420p", "-t", str(DUR), clip_path]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode:
        print("FFmpeg FAILED:", r.stderr[:500], file=sys.stderr)
        return None, None

    # ----- PNG 卡 -----
    img = Image.new("RGBA", (W, H), (10, 5, 3, 255))
    draw = ImageDraw.Draw(img)
    fm = ImageFont.truetype(FONT, MAIN_FS)
    bm = fm.getbbox(main_text)
    draw.text(((W - (bm[2] - bm[0])) // 2, MAIN_Y), main_text, (212, 175, 55, 255), font=fm)
    fs = ImageFont.truetype(FONT, SUB_FS)
    bs = fs.getbbox(subtitle_text)
    draw.text(((W - (bs[2] - bs[0])) // 2, SUB_Y), subtitle_text, (212, 175, 55, 255), font=fs)
    for y in range(GL_H):
        for x in range(GL_W):
            draw.point((gl_x + x, GL_Y + y), (212, 175, 55, 255))
    img.save(card_path)

    sz_mb = os.path.getsize(clip_path) / 1024 / 1024
    sz_kb = os.path.getsize(card_path) / 1024
    print(f"  OK: title_clip.mp4 ({sz_mb:.1f}MB)")
    print(f"  OK: title_card.png ({sz_kb:.0f}KB)")
    return clip_path, card_path


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="片头标题生成器 · 固定模板")
    p.add_argument("--main", required=True, help="主标题 (如: 大一统时代)")
    p.add_argument("--sub", required=True, help="副标题 (如: 上下五千年·第五期)")
    p.add_argument("--output-dir", required=True, help="输出目录")
    a = p.parse_args()
    render(a.main, a.sub, a.output_dir)
