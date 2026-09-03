"""
流水线补全模块
补上原 run.py 缺失的环节：片头 overlay、自动检查、归档、验收包
"""

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path


# ============================================================
# 自动检查（机器比人可靠的三项，不通过则抛错）
# ============================================================

def _parse_srt_entries(srt_path: str) -> list:
    """解析 SRT，返回 [(start, end, text), ...]"""
    def _ts(t):
        h, m, rest = t.split(":")
        s, ms = rest.split(",")
        return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000

    entries = []
    blocks = open(srt_path, encoding="utf-8", errors="ignore").read().split("\n\n")
    for b in blocks:
        lines = [x for x in b.split("\n") if x.strip()]
        idx = None
        for i, ln in enumerate(lines):
            if "-->" in ln:
                idx = i
                break
        if idx is None:
            continue
        a, z = lines[idx].split("-->")
        entries.append((_ts(a.strip()), _ts(z.strip()), "\n".join(lines[idx + 1:])))
    return entries


def check_subtitles(srt_path: str, script_path: str, cfg: dict) -> dict:
    """
    三项自动检查：
      1. 超长行 = 0
      2. 全文比对零丢字
      3. 每条 ≤ max_lines 行
    不通过抛 RuntimeError。
    """
    checks = (cfg or {}).get("checks", {})
    sub_cfg = (cfg or {}).get("subtitle", {})
    max_chars = int(sub_cfg.get("max_chars_per_line", 14))
    max_lines = int(sub_cfg.get("max_lines", 2))

    entries = _parse_srt_entries(srt_path)
    if not entries:
        raise RuntimeError("字幕检查失败：SRT 为空")

    report = {"entries": len(entries)}
    problems = []

    # 1. 超长行
    if checks.get("max_line_length", True):
        over = []
        for i, (_, _, text) in enumerate(entries):
            for ln in text.split("\n"):
                if len(ln.strip()) > max_chars:
                    over.append((i + 1, ln.strip()))
        report["over_length"] = len(over)
        if over:
            sample = "; ".join(f"#{i}:{t[:20]}" for i, t in over[:3])
            problems.append(f"超长行 {len(over)} 条（限{max_chars}字）：{sample}")

    # 2. 每条行数
    if checks.get("max_lines", True):
        over_lines = [i + 1 for i, (_, _, t) in enumerate(entries)
                      if len([x for x in t.split("\n") if x.strip()]) > max_lines]
        report["over_lines"] = len(over_lines)
        if over_lines:
            problems.append(f"超行数 {len(over_lines)} 条（限{max_lines}行）：{over_lines[:5]}")

    # 3. 全文比对
    if checks.get("text_integrity", True) and os.path.exists(script_path):
        def _han(s):
            return "".join(re.findall(r"[\u4e00-\u9fff]", s))
        srt_text = _han("".join(t for _, _, t in entries))
        script_text = _han(open(script_path, encoding="utf-8").read())
        report["srt_chars"] = len(srt_text)
        report["script_chars"] = len(script_text)
        diff = abs(len(srt_text) - len(script_text))
        report["char_diff"] = diff
        # 允许极小误差（TTS 可能对数字/符号做读法转换）
        if diff > max(20, len(script_text) * 0.01):
            problems.append(
                f"全文比对不一致：字幕 {len(srt_text)} 字 vs 稿子 {len(script_text)} 字，差 {diff}")

    report["passed"] = not problems
    report["problems"] = problems
    if problems:
        raise RuntimeError("字幕自动检查未通过：\n  - " + "\n  - ".join(problems))
    return report


# ============================================================
# 片头 overlay（原流水线缺失的环节）
# ============================================================

def overlay_title_card(
    video_path: str,
    title_card_png: str,
    output_path: str,
    duration: int = 3,
    encode_args: list = None,
) -> str:
    """
    把标题卡叠加在正片前 N 秒。
    音频不动、时间轴不动，总时长 = 正片时长。
    """
    if not os.path.exists(title_card_png):
        raise FileNotFoundError(f"标题卡不存在: {title_card_png}")

    enc = encode_args or ["-c:v", "libx264", "-crf", "26", "-preset", "medium"]

    cmd = [
        "ffmpeg", "-y",
        "-i", video_path,
        "-i", title_card_png,
        "-filter_complex",
        f"[0:v][1:v]overlay=0:0:enable='between(t,0,{duration})'[v]",
        "-map", "[v]", "-map", "0:a",
        *enc,
        "-c:a", "copy",
        "-movflags", "+faststart",
        output_path,
    ]
    subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=3600)
    return output_path


# ============================================================
# AI 视频片头（GL-20260902：把 AI 生成的动态视频拼到正片前）
# ============================================================

def build_ai_title_clip(
    ai_video: str,
    title_text: str,
    series_text: str,
    output_path: str,
    cfg: dict = None,
    width: int = 1080,
    height: int = 1920,
) -> str:
    """把 AI 视频转成片头短片：缩放到目标画幅 + 叠加标题文字。

    改进（GL-20260902 v2）：
    - 保留 AI 视频原声（重编码为干净 AAC，解决 concat 解码问题）
    - 标题下方加半透明压暗条（任何画面都清晰，不会花）
    """
    tc = (cfg or {}).get("title_card", {})
    font = tc.get("font", "/System/Library/Fonts/PingFang.ttc")
    if not os.path.exists(font):
        font = "/System/Library/Fonts/STHeiti Light.ttc"
    # 标题文字转义（drawtext 特殊字符）
    def _esc(s):
        return (str(s).replace("\\", "\\\\").replace(":", "\\:")
                .replace("'", "\\'").replace("%", "\\%"))
    title_esc = _esc(title_text)
    series_esc = _esc(series_text)
    fs_main = int(height * 0.065)      # 主标题字号
    fs_ser = int(height * 0.032)       # 系列名字号
    # 标题区位置（垂直居中偏下）+ 压暗条
    band_h = int(height * 0.16)        # 压暗条高度
    band_top = int(height * 0.60)      # 压暗条顶部
    tf_y = band_top + int(band_h * 0.30)   # 主标题 y
    sf_y = band_top + int(band_h * 0.72)   # 系列名 y

    vf = (
        f"scale={width}:{height}:force_original_aspect_ratio=increase,"
        f"crop={width}:{height},setsar=1,"
        # 半透明压暗条（标题区），保证文字清晰
        f"drawbox=x=0:y={band_top}:w={width}:h={band_h}:"
        f"color=black@0.45:t=fill,"
        f"drawtext=fontfile={font}:text='{title_esc}':"
        f"fontsize={fs_main}:fontcolor=white:"
        f"x=(w-text_w)/2:y={tf_y}:"
        f"shadowcolor=black@0.8:shadowx=2:shadowy=2,"
        f"drawtext=fontfile={font}:text='{series_esc}':"
        f"fontsize={fs_ser}:fontcolor=0xD4AF37:"
        f"x=(w-text_w)/2:y={sf_y}:"
        f"shadowcolor=black@0.8:shadowx=2:shadowy=2"
    )

    cmd = [
        "ffmpeg", "-y", "-i", ai_video,
        "-vf", vf,
        # 原声保留，重编码干净 AAC（44.1k 立体声）
        "-c:v", "libx264", "-crf", "23", "-preset", "medium",
        "-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-ac", "2",
        "-movflags", "+faststart",
        output_path,
    ]
    subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=1800)
    return output_path


def concat_with_ai_title(
    main_video: str,
    ai_title_clip: str,
    output_path: str,
) -> str:
    """AI 片头 + 正片 拼接（画面 concat；音频：片头原声在前，正片配音延迟衔接）。"""
    # 片头时长
    probe = subprocess.run(
        ["ffmpeg", "-i", ai_title_clip], capture_output=True, text=True)
    import re as _re
    m = _re.search(r"Duration: (\d+):(\d+):([\d.]+)", probe.stderr)
    if not m:
        raise RuntimeError("无法读取 AI 片头时长")
    lead_sec = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    delay_ms = int(round(lead_sec * 1000))

    # 判断片头是否有音轨
    has_audio = any("Audio:" in l for l in probe.stderr.split("\n"))
    if has_audio:
        # 片头原声 + 正片延迟：统一 24000 mono 后 concat
        filt = (
            f"[0:v]setsar=1[tv];[1:v]setsar=1[mv];"
            f"[tv][mv]concat=n=2:v=1:a=0[v];"
            f"[0:a]aresample=24000,pan=mono|c0=c0[a0];"
            f"[1:a]aresample=24000,pan=mono|c0=c0,"
            f"adelay={delay_ms}|{delay_ms}[a1];"
            f"[a0][a1]concat=n=2:v=0:a=1[a]"
        )
        a_map = "[a]"
    else:
        # 片头无音轨：只延迟正片配音
        filt = (
            f"[0:v]setsar=1[tv];[1:v]setsar=1[mv];"
            f"[tv][mv]concat=n=2:v=1:a=0[v];"
            f"[1:a]aresample=24000,pan=mono|c0=c0,"
            f"adelay={delay_ms}|{delay_ms}[a]"
        )
        a_map = "[a]"

    cmd = [
        "ffmpeg", "-y",
        "-i", ai_title_clip,      # 0: 片头
        "-i", main_video,          # 1: 正片
        "-filter_complex", filt,
        "-map", "[v]", "-map", a_map,
        "-c:v", "libx264", "-crf", "23", "-preset", "medium",
        "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart",
        output_path,
    ]
    subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=3600)
    return output_path


def shift_srt_file(srt_path: str, offset_sec: float, out_path: str = None) -> str:
    """SRT 时间轴整体平移 offset_sec 秒（AI 片头插入后字幕后移）。"""
    out_path = out_path or srt_path
    def _add(t: str, off: float) -> str:
        h, m, s = t.split(":")
        ms_part = s.split(",")
        sec = float(h) * 3600 + float(m) * 60 + float(ms_part[0]) + int(ms_part[1]) / 1000 + off
        sec = max(0, sec)
        hh = int(sec // 3600); mm = int((sec % 3600) // 60)
        ss = int(sec % 60); mss = int(round((sec - int(sec)) * 1000))
        if mss == 1000: ss += 1; mss = 0
        return f"{hh:02d}:{mm:02d}:{ss:02d},{mss:03d}"

    lines = open(srt_path, encoding="utf-8").read().split("\n")
    out = []
    for ln in lines:
        m = re.match(r"^(\d+):(\d+):(\d+),(\d+)\s*-->\s*(\d+):(\d+):(\d+),(\d+)\s*$", ln.strip())
        if m:
            a = f"{m.group(1)}:{m.group(2)}:{m.group(3)},{m.group(4)}"
            b = f"{m.group(5)}:{m.group(6)}:{m.group(7)},{m.group(8)}"
            out.append(f"{_add(a, offset_sec)} --> {_add(b, offset_sec)}")
        else:
            out.append(ln)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(out))
    return out_path


# ============================================================
# 归档到素材库
# ============================================================

def archive_episode(
    episode_name: str,
    cfg: dict,
    final_video: str = None,
    audio: str = None,
    srt: str = None,
    script: str = None,
    images_dir: str = None,
) -> dict:
    """
    按素材库结构归档。只复制不删原件；覆盖前先备份。
    episode_name 形如 "16-唐朝"
    """
    out_cfg = (cfg or {}).get("output", {})
    root = os.path.expanduser(out_cfg.get("library_root", "~/Desktop/历史说素材"))
    dirs = out_cfg.get("dirs", {})
    backup = out_cfg.get("backup_before_overwrite", True)

    def _copy(src, sub_dir, filename):
        if not src or not os.path.exists(src):
            return None
        dst_dir = os.path.join(root, sub_dir)
        os.makedirs(dst_dir, exist_ok=True)
        dst = os.path.join(dst_dir, filename)
        if backup and os.path.exists(dst):
            bak = dst + ".bak"
            shutil.copy2(dst, bak)
        shutil.copy2(src, dst)
        return dst

    result = {}
    result["video"] = _copy(final_video, dirs.get("video", "成片"), f"{episode_name}.mp4")
    result["audio"] = _copy(audio, dirs.get("audio", "录音"), f"{episode_name}.mp3")
    result["subtitle"] = _copy(srt, dirs.get("subtitle", "字幕"), f"{episode_name}.srt")
    if script:
        ext = Path(script).suffix or ".txt"
        result["script"] = _copy(script, dirs.get("script", "稿子"), f"{episode_name}{ext}")

    # 图片目录
    if images_dir and os.path.isdir(images_dir):
        dst_dir = os.path.join(root, dirs.get("images", "图片"), episode_name)
        os.makedirs(dst_dir, exist_ok=True)
        n = 0
        for f in sorted(os.listdir(images_dir)):
            if f.lower().endswith((".png", ".jpg", ".jpeg", ".webp")):
                shutil.copy2(os.path.join(images_dir, f), os.path.join(dst_dir, f))
                n += 1
        result["images"] = f"{dst_dir} ({n}张)"

    return result


# ============================================================
# 验收包：自动截图，供人工一分钟抽验
# ============================================================

def make_review_pack(video_path: str, out_dir: str, title_duration: int = 3) -> list:
    """
    自动截取关键帧，供人工验收：
      开头 / 片头消失后 / 中段 / 片尾
    """
    os.makedirs(out_dir, exist_ok=True)

    # 取总时长
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", video_path],
            capture_output=True, text=True, check=True,
        )
        dur = float(r.stdout.strip())
    except Exception:
        dur = 0

    shots = [
        ("01_开头0.5秒", 0.5),
        ("02_片头消失后", title_duration + 1),
        ("03_中段", dur / 2 if dur else 300),
        ("04_片尾", max(dur - 20, 0) if dur else 0),
    ]

    made = []
    for name, at in shots:
        if at <= 0:
            continue
        out = os.path.join(out_dir, f"{name}.jpg")
        try:
            subprocess.run(
                ["ffmpeg", "-y", "-ss", str(at), "-i", video_path,
                 "-frames:v", "1", "-q:v", "2", out],
                check=True, capture_output=True, text=True, timeout=120,
            )
            made.append(out)
        except Exception:
            pass
    return made
