# -*- coding: utf-8 -*-
"""关键词字卡叠加：稿子提取关键词 → 句级 SRT 时间段 → 金字卡 PNG → colorkey 叠加。

与 subtitle_burn 同机制（PNG + h264 透明轨 + colorkey overlay）。
零 AI 调用，纯本地计算。

回滚：删除 run.py 中 keyword_cards 相关调用段即可。
"""
from __future__ import annotations

import os
import re
import subprocess
import tempfile

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    Image = ImageDraw = ImageFont = None

# 字卡样式（与片头金字呼应）
GOLD = (212, 175, 55)
GOLD_LIGHT = (245, 214, 123)
BAR_H = 260          # 字卡高度
MARGIN_TOP = 560     # 距顶部（避开底部字幕区）
FONT_SIZE = 120

# 关键词规则：哪些词值得弹字卡
KEYWORD_PATTERNS = [
    r"[0-9一二三四五六七八九十百千万]+万",       # 四十万、二十四万
    r"[0-9一二三四五六七八九十]+[余]?战",        # 七十余战、九战
    r"白起|韩信|霍去病|李靖|卫青|岳飞|徐达|郭子仪|戚继光|李牧",  # 名将
    r"秦始皇|汉武帝|唐太宗|宋太祖|明太祖|康熙|忽必烈|隋文帝|汉文帝|武则天",  # 帝王
    r"长平之战|垓下|漠北|虎牢关|郾城|鄱阳湖|雅克萨|萨尔浒|封狼居胥",  # 战役/典故
]

# 忽略词（太常见，弹了反而吵）
STOPWORDS = {"中国", "历史", "天下", "皇帝", "大军", "名将", "战争", "国家"}


def _parse_srt(srt_path: str) -> list[tuple[float, float, str]]:
    """解析 SRT → [(start, end, text)]"""
    entries = []
    try:
        with open(srt_path, encoding="utf-8") as f:
            lines = f.read().splitlines()
        i = 0
        while i < len(lines):
            if "-->" in lines[i]:
                ts = lines[i].split("-->")
                def _t(s: str) -> float:
                    p = s.strip().replace(",", ".").split(":")
                    return float(p[0]) * 3600 + float(p[1]) * 60 + float(p[2])
                start, end = _t(ts[0]), _t(ts[1])
                text = []
                i += 1
                while i < len(lines) and lines[i].strip():
                    text.append(lines[i].strip())
                    i += 1
                entries.append((start, end, "".join(text)))
            i += 1
    except Exception:
        pass
    return entries


def _extract_keywords(text: str) -> list[str]:
    """从一句字幕提取关键词。"""
    kws = []
    for pat in KEYWORD_PATTERNS:
        for m in re.findall(pat, text):
            if m not in STOPWORDS:
                kws.append(m)
    return list(dict.fromkeys(kws))


def _render_card_png(text: str, out_path: str) -> None:
    """渲染金字卡 PNG（金色描边大字 + 底部金线）。"""
    if Image is None:
        return
    font = None
    for fp in ("/Library/Fonts/AdobeHeitiStd-Regular.otf",
               "/System/Library/Fonts/PingFang.ttc",
               "/Library/Fonts/Arial Unicode.ttf"):
        if os.path.exists(fp):
            try:
                font = ImageFont.truetype(fp, FONT_SIZE)
                break
            except Exception:
                continue
    if font is None:
        font = ImageFont.load_default()

    # 估算尺寸
    img = Image.new("RGBA", (10, 10))
    d = ImageDraw.Draw(img)
    bbox = d.textbbox((0, 0), text, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    pad_x, pad_y = 60, 30
    W, H = tw + pad_x * 2, th + pad_y * 2 + 24  # +金线

    card = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    dc = ImageDraw.Draw(card)
    # 金字（三层：黑描边 + 金底 + 高光）
    x0 = pad_x - bbox[0]
    y0 = pad_y - bbox[1]
    for dx, dy in ((-4, 0), (4, 0), (0, -4), (0, 4), (-3, -3), (3, 3), (-3, 3), (3, -3)):
        dc.text((x0 + dx, y0 + dy), text, font=font, fill=(40, 25, 5))
    dc.text((x0, y0), text, font=font, fill=GOLD)
    # 高光层（偏上）
    dc.text((x0, y0 - 2), text, font=font, fill=GOLD_LIGHT)
    # 底部金线
    ly = H - 18
    dc.rectangle([W * 0.2, ly, W * 0.8, ly + 5], fill=GOLD)
    card.save(out_path)


def overlay_keyword_cards(
    video_path: str,
    srt_path: str,
    output_path: str,
    encode_args: list = None,
    max_cards: int = 6,
) -> str:
    """在视频上按句级时间叠加关键词字卡。

    流程：解析 SRT → 每句提关键词 → 渲染字卡 PNG → h264 透明轨 → colorkey overlay。
    字卡出现时机：句子的前 35% 时段（念到关键词时），停留 min(句长, 2.2s)。
    """
    print("\n  关键词字卡叠加...")
    entries = _parse_srt(srt_path)
    if not entries:
        subprocess.run(["ffmpeg", "-y", "-i", video_path, "-c", "copy", output_path],
                       check=True, capture_output=True, text=True)
        return output_path

    # 收集所有关键词卡（去重，限数）
    cards: list[tuple[float, float, str]] = []
    seen = set()
    for start, end, text in entries:
        for kw in _extract_keywords(text):
            if kw in seen or len(cards) >= max_cards:
                continue
            seen.add(kw)
            dur = min(end - start, 2.2)
            card_start = start + (end - start) * 0.3  # 句子念到 30% 时出现
            cards.append((card_start, card_start + dur, kw))

    if not cards:
        print("  无匹配关键词，跳过字卡")
        subprocess.run(["ffmpeg", "-y", "-i", video_path, "-c", "copy", output_path],
                       check=True, capture_output=True, text=True)
        return output_path

    print(f"  字卡 {len(cards)} 个: {[c[2] for c in cards]}")

    tmp_dir = tempfile.mkdtemp(prefix="kwcards_")
    png_cache = {}
    concat_lines = []
    for start, end, kw in cards:
        if kw not in png_cache:
            png = os.path.join(tmp_dir, f"kw_{len(png_cache):03d}.png")
            _render_card_png(kw, png)
            png_cache[kw] = png
        concat_lines.append(f"file '{png_cache[kw]}'")
        concat_lines.append(f"duration {max(end - start, 0.1):.3f}")

    # 视频总时长（补尾帧）
    probe = subprocess.run(["ffmpeg", "-i", video_path], capture_output=True, text=True)
    m = re.search(r"Duration: (\d+):(\d+):(\d+\.\d+)", probe.stderr)
    total = 0
    if m:
        total = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))

    # 首尾补黑帧，保证字卡轨对齐视频时间轴
    if cards:
        first_start = cards[0][0]
        last_end = cards[-1][1]
        if first_start > 0:
            concat_lines.insert(0, f"file '{png_cache[cards[0][2]]}'")
            concat_lines.insert(1, f"duration {first_start:.3f}")
        if last_end < total:
            concat_lines.append(f"file '{png_cache[cards[-1][2]]}'")
            concat_lines.append(f"duration {max(total - last_end, 0.1):.3f}")

    cp = os.path.join(tmp_dir, "c.txt")
    with open(cp, "w") as f:
        f.write("\n".join(concat_lines))

    sv = os.path.join(tmp_dir, "cards.mp4")
    subprocess.run([
        "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", cp,
        "-c:v", "libx264", "-crf", "23", "-preset", "veryfast",
        "-pix_fmt", "yuv420p", "-r", "25", sv,
    ], check=True, capture_output=True, text=True, timeout=600)

    # colorkey 黑底透明 + overlay（顶部区域，避开底部字幕）
    cmd = [
        "ffmpeg", "-y",
        "-i", video_path, "-i", sv,
        "-filter_complex",
        f"[1:v]colorkey=0x000000:similarity=0.1:blend=0.0[card];"
        f"[0:v][card]overlay=0:{MARGIN_TOP}[v]",
        "-map", "[v]", "-map", "0:a",
    ]
    cmd += (encode_args or ["-c:v", "libx264", "-crf", "26", "-preset", "medium",
                            "-c:a", "copy", "-pix_fmt", "yuv420p"])
    cmd += [output_path]
    subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=900)
    print(f"  ✓ 字卡叠加完成: {len(cards)} 个关键词")
    return output_path
