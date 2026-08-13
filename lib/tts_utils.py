"""
TTS 配音工具模块
调用 edge-tts Python API + 自定义 DNS 解析器，输出 audio.mp3 + subs.vtt

背景（工单 2026-08-13 TTS 网络根治）：
- Shadowrocket 虚拟网卡 utun3 把系统 DNS 改成假 DNS（198.18.0.2）
- speech.platform.bing.com 被劫持成假 IP 198.18.0.19，微软真实服务器连不上
- 方案：aiohttp AsyncResolver 走公共 DNS（114.114.114.114 / 223.5.5.5）绕过假 DNS
"""

import asyncio
import os
import re
import subprocess
from pathlib import Path

import aiohttp
from aiohttp.resolver import AsyncResolver
from edge_tts import Communicate, SubMaker
from edge_tts.exceptions import EdgeTTSException

import edge_tts.communicate as _edge_comm


def _patch_edge_tts_connector_owner():
    """edge-tts 7.2.8 多段流式 bug 补丁：

    __stream() 用 async with ClientSession(connector=self.connector) 时
    connector_owner 默认 True，段结束 session.close() 会把共享 connector 一起关掉，
    长文本第 2 段报 Session is closed。
    强制 connector_owner=False：session 不拥有外部传入的 connector，close 时不会关闭它。
    """
    if getattr(_edge_comm, "_connector_owner_patched", False):
        return

    _OrigClientSession = _edge_comm.aiohttp.ClientSession

    class _PatchedClientSession(_OrigClientSession):
        def __init__(self, *args, connector=None, connector_owner=True, **kwargs):
            if connector is not None:
                connector_owner = False
            super().__init__(*args, connector=connector,
                             connector_owner=connector_owner, **kwargs)

    _edge_comm.aiohttp.ClientSession = _PatchedClientSession
    _edge_comm._connector_owner_patched = True


_patch_edge_tts_connector_owner()

# 公共 DNS：绕过 Shadowrocket 假 DNS（198.18.0.2），解析微软真实 IP
_DNS_SERVERS = ["114.114.114.114", "223.5.5.5"]
_MAX_RETRIES = 4


def _tts_once(
    text: str,
    output_audio: str,
    output_subs: str,
    voice: str,
    rate: str,
) -> int:
    """单次 TTS（无重试），返回音频字节数。resolver/connector 必须在事件循环内创建（Python 3.14 限制）"""

    async def _run():
        resolver = AsyncResolver(nameservers=_DNS_SERVERS)
        connector = aiohttp.TCPConnector(resolver=resolver)

        com = Communicate(
            text,
            voice,
            rate=rate,
            connector=connector,
            connect_timeout=30,
            receive_timeout=300,
        )
        submaker = SubMaker()
        audio = bytearray()
        async for chunk in com.stream():
            if chunk["type"] == "audio":
                audio.extend(chunk["data"])
            elif chunk["type"] in ("WordBoundary", "SentenceBoundary"):
                submaker.feed(chunk)
        with open(output_audio, "wb") as f:
            f.write(bytes(audio))
        # SubMaker 生成 SRT；vtt_to_srt() 对 SRT 输入幂等（重编号），无需转 VTT
        with open(output_subs, "w", encoding="utf-8") as f:
            f.write(submaker.get_srt())
        return len(audio)

    return asyncio.run(_run())


def generate_tts(
    script_path: str,
    output_audio: str,
    output_subs: str,
    voice: str = "zh-CN-YunjianNeural",
    rate: str = "-4%",
) -> dict:
    """
    调用 edge-tts 生成配音音频 + 字幕文件（自定义 DNS 直连，绕过 Shadowrocket 劫持）
    返回 {audio_path, subs_path, duration_sec}
    """
    script_path = str(script_path)
    output_audio = str(output_audio)
    output_subs = str(output_subs)

    ensure_dir(output_audio)

    with open(script_path, "r", encoding="utf-8") as f:
        text = f.read().strip()

    print(f"  Running: edge-tts --voice {voice} --rate {rate}")

    import time as _t

    # 网络波动常见（微软语音服务），自动重试
    last_err = None
    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            audio_bytes = _tts_once(text, output_audio, output_subs, voice, rate)
            if audio_bytes > 0:
                break
            raise RuntimeError("empty audio")
        except (TimeoutError, aiohttp.ClientError, ConnectionError,
                OSError, EdgeTTSException, RuntimeError) as e:
            last_err = e
            if attempt < _MAX_RETRIES:
                wait = attempt * 8
                print(f"  ⚠ 网络超时（第{attempt}次），{wait}秒后重试…（{type(e).__name__}: {str(e)[:120]}）")
                _t.sleep(wait)
                continue
    else:
        raise RuntimeError(f"edge-tts failed: {last_err}")

    if attempt > 1:
        print(f"  ✓ 第 {attempt} 次尝试成功")

    # 获取音频时长
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
