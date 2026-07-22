"""
TTS 配音工具模块
包装 edge-tts CLI 调用，输出 audio.mp3 + subs.vtt
"""

import subprocess
import os
import re
from pathlib import Path


def generate_tts(
    script_path: str,
    output_audio: str,
    output_subs: str,
    voice: str = "zh-CN-YunjianNeural",
    rate: str = "-4%",
) -> dict:
    """
    调用 edge-tts 生成配音音频 + 字幕文件
    返回 {audio_path, subs_path, duration_sec}
    """
    script_path = str(script_path)
    output_audio = str(output_audio)
    output_subs = str(output_subs)

    ensure_dir(output_audio)

    cmd = [
        "edge-tts",
        "--file", script_path,
        "--voice", voice,
        f"--rate={rate}",
        "--write-media", output_audio,
        "--write-subtitles", output_subs,
    ]

    print(f"  Running: edge-tts --voice {voice} --rate {rate}")
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)

    if result.returncode != 0:
        raise RuntimeError(f"edge-tts failed: {result.stderr}")

    # 获取音频时长
    import re
    dur_result = subprocess.run(
        ["ffmpeg", "-i", output_audio],
        capture_output=True, text=True
    )
    m = re.search(r'Duration: (\d+):(\d+):(\d+\.?\d*)', dur_result.stderr)
    if not m:
        raise RuntimeError(f"Could not parse duration from ffmpeg for {output_audio}")
    h, min_, s = float(m.group(1)), float(m.group(2)), float(m.group(3))
    duration_sec = h * 3600 + min_ * 60 + s

    print(f"  TTS done: {Path(output_audio).name} -> {duration_sec:.1f}s")
    print(f"  Subs: {Path(output_subs).name}")

    return {
        "audio_path": output_audio,
        "subs_path": output_subs,
        "duration_sec": duration_sec,
    }


def vtt_to_srt(vtt_path: str, srt_path: str) -> str:
    """
    VTT 字幕转 SRT 格式
    VTT 和 SRT 时间线格式几乎一样，只需
    去掉 VTT header 和调整时间格式
    """
    srt_path = str(srt_path)

    with open(vtt_path, "r", encoding="utf-8") as f:
        content = f.read()

    # 去掉 VTT header（第一行 WEBVTT 及空行）
    lines = content.split("\n")
    if lines and lines[0].strip().upper().startswith("WEBVTT"):
        lines = lines[1:]
    while lines and lines[0].strip() == "":
        lines = lines[1:]

    # 将 VTT 时间格式中的 . 替换为 , (SRT 用逗号)
    # VTT: 00:01:23.456 --> 00:01:25.789
    srt_lines = []
    counter = 1
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if not line:
            i += 1
            continue
        # 检测是否是时间行（包含 -->）
        if "-->" in line:
            # 写入序号
            srt_lines.append(str(counter))
            counter += 1
            # 转换时间格式 . → ,
            time_line = line.replace(".", ",")
            srt_lines.append(time_line)
            i += 1
            # 收集所有后续文本行直到空行
            texts = []
            while i < len(lines) and lines[i].strip():
                texts.append(lines[i].strip())
                i += 1
            if texts:
                srt_lines.extend(texts)
            srt_lines.append("")  # 空行
        else:
            i += 1

    with open(srt_path, "w", encoding="utf-8") as f:
        f.write("\n".join(srt_lines))

    print(f"  VTT→SRT: {len(srt_lines)} lines -> {Path(srt_path).name}")
    return srt_path


def ensure_dir(path: str):
    """确保文件所在目录存在"""
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
