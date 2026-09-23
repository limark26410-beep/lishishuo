"""
TTS 配音工具模块
调用 edge-tts Python API + 自定义 DNS 解析器，输出 audio.mp3 + subs.vtt

背景（工单 2026-08-13 TTS 网络根治）：
- Shadowrocket 虚拟网卡 utun3 把系统 DNS 改成假 DNS（198.18.0.2）
- speech.platform.bing.com 被劫持成假 IP 198.18.0.19，微软真实服务器连不上
- 方案：aiohttp AsyncResolver 走公共 DNS（114.114.114.114 / 223.5.5.5）绕过假 DNS
"""

import asyncio
import json
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


def normalize_rate(rate) -> str:
    """规范化 edge-tts rate 参数（GL-20260817-03 语速修复）。

    edge-tts 要求格式 `^[+-]\\d+%$`（必须带正负号，如 +28% / -4%）；
    纯数字 `28%` 会被 edge-tts 判 Invalid rate 而中断流水线。
    本函数：无符号自动补 `+`（`28%`→`+28%`，语义=加速）；非法输入给清晰中文报错。
    注意：2 倍速 = `+100%`（`+200%` 会被微软服务端封顶成 2 倍速，效果相同但值不规范）。
    """
    if rate is None:
        return "-4%"
    r = str(rate).strip()
    if not r:
        return "-4%"
    r = r.replace(" ", "").replace("％", "%")
    if r.endswith("%"):
        r = r[:-1]
    sign = ""
    if r and r[0] in "+-":
        sign, r = r[0], r[1:]
    if not r or not r.isdigit():
        raise ValueError(
            f"语速格式不对：'{rate}'。须为带正负号的百分比，如 +28%（加速）或 -4%（减速）；"
            f"2 倍速写 +100%")
    n = int(r)
    if n > 500:
        raise ValueError(f"语速数值过大：'{rate}'（上限 +500%）。2 倍速写 +100%")
    if not sign:
        sign = "+"  # 无符号默认按加速处理（28% → +28%）
    return f"{sign}{n}%"  # 始终带 %：'28'（无%）也按 +28% 处理


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
    rate = normalize_rate(rate)  # GL-20260817-03：语速规范化（28%→+28%，非法中文报错）
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


# ─────────── 分段 TTS（GL-20260817-05 5b-3 段落级语速） ───────────

def _audio_duration_sec(path: str) -> float:
    """ffmpeg 解析音频时长"""
    r = subprocess.run(["ffmpeg", "-i", path], capture_output=True, text=True)
    m = re.search(r'Duration: (\d+):(\d+):(\d+\.?\d*)', r.stderr)
    if not m:
        raise RuntimeError(f"无法解析音频时长: {path}")
    h, mi, s = float(m.group(1)), float(m.group(2)), float(m.group(3))
    return h * 3600 + mi * 60 + s


def _tts_with_retry(text: str, out_audio: str, out_subs: str,
                    voice: str, rate: str, seg_no: int = 0) -> float:
    """单段 TTS（带网络重试），返回音频时长秒数"""
    import time as _t
    last_err = None
    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            audio_bytes = _tts_once(text, out_audio, out_subs, voice, rate)
            if audio_bytes > 0:
                break
            raise RuntimeError("empty audio")
        except (TimeoutError, aiohttp.ClientError, ConnectionError,
                OSError, EdgeTTSException, RuntimeError) as e:
            last_err = e
            if attempt < _MAX_RETRIES:
                wait = attempt * 8
                print(f"  ⚠ 段{seg_no} 网络超时（第{attempt}次），"
                      f"{wait}秒后重试…（{type(e).__name__}）")
                _t.sleep(wait)
                continue
    else:
        raise RuntimeError(f"edge-tts failed: {last_err}")
    return _audio_duration_sec(out_audio)


def _shift_srt(srt_path: str, offset: float) -> str:
    """SRT 时间轴整体平移 offset 秒，返回新内容"""
    def _add(t: str) -> str:
        h, mi, s, ms = int(t[0:2]), int(t[3:5]), int(t[6:8]), int(t[9:12])
        total = h * 3600 + mi * 60 + s + ms / 1000 + offset
        hh = int(total // 3600)
        mm = int((total % 3600) // 60)
        ss = int(total % 60)
        mss = int(round((total - int(total)) * 1000))
        if mss == 1000:
            ss += 1
            mss = 0
        return f"{hh:02d}:{mm:02d}:{ss:02d},{mss:03d}"

    out = []
    for line in open(srt_path, encoding="utf-8"):
        m = re.match(r"^(\d+):(\d+):(\d+),(\d+)\s*-->\s*(\d+):(\d+):(\d+),(\d+)\s*$", line.strip())
        if m:
            out.append(f"{_add(line.strip().split(' --> ')[0])} --> "
                       f"{_add(line.strip().split(' --> ')[1])}\n")
        else:
            out.append(line)
    return "".join(out)


def _concat_audios(files: list, out_path: str) -> None:
    """concat 拼接同参数 mp3（edge-tts 各段输出码率一致）"""
    if len(files) == 1:
        import shutil
        shutil.copyfile(files[0], out_path)
        return
    import tempfile
    list_path = os.path.join(tempfile.mkdtemp(prefix="ttscon_"), "files.txt")
    with open(list_path, "w", encoding="utf-8") as f:
        for p in files:
            f.write(f"file '{Path(p).resolve()}'\n")
    subprocess.run(
        ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", list_path,
         "-c:a", "copy", out_path],
        check=True, capture_output=True, text=True)


def _merge_srts(seg_srts: list) -> str:
    """多段 SRT 内容合并 + 重编号（段间空行分隔）"""
    blocks = []
    for s in seg_srts:
        s = s.strip()
        if s:
            blocks.append(s)
    if not blocks:
        return ""
    # 重编号
    numbered = []
    idx = 1
    for b in blocks:
        lines = b.split("\n")
        out_lines = []
        for line in lines:
            if re.match(r"^\d+$", line.strip()):
                out_lines.append(str(idx))
                idx += 1
            else:
                out_lines.append(line)
        numbered.append("\n".join(out_lines))
    return "\n\n".join(numbered) + "\n"


def generate_tts_segmented(
    segments: list,
    emotions: list,
    output_audio: str,
    output_subs: str,
    voice: str = "zh-CN-YunjianNeural",
    base_rate: str = "-4%",
    emotion_rates: dict = None,
) -> dict:
    """分段 TTS（5b-3 段落级语速）：每段按情绪 rate 独立生成，
    音频 concat 拼接、SRT 时间轴逐段累加。返回与 generate_tts 同结构。

    segments: [{index, text}]；emotions: [{index, emotion}]；
    emotion_rates: {情绪: rate}（如 {高潮: "+20%"}，缺省用 base_rate）。
    """
    import tempfile, shutil
    emotion_rates = emotion_rates or {}
    tmpdir = tempfile.mkdtemp(prefix="tts_seg_")
    try:
        seg_audios, seg_srts = [], []
        offset = 0.0
        n = len(segments)
        for i, (seg, emo) in enumerate(zip(segments, emotions)):
            text = (seg.get("text") or "").strip()
            if not text:
                continue
            rate = normalize_rate(emotion_rates.get(emo["emotion"], base_rate))
            seg_mp3 = os.path.join(tmpdir, f"seg_{i:03d}.mp3")
            seg_srt = os.path.join(tmpdir, f"seg_{i:03d}.srt")
            dur = _tts_with_retry(text, seg_mp3, seg_srt, voice, rate, i + 1)
            seg_srts.append(_shift_srt(seg_srt, offset))
            seg_audios.append(seg_mp3)
            print(f"  [段{i+1}/{n}] {emo['emotion']} rate={rate} {dur:.1f}s")
            offset += dur
        if not seg_audios:
            raise RuntimeError("分段 TTS：所有段落均为空")
        _concat_audios(seg_audios, output_audio)
        merged = _merge_srts(seg_srts)
        with open(output_subs, "w", encoding="utf-8") as f:
            f.write(merged)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    print(f"  TTS done（分段 {len(seg_audios)} 段）: "
          f"{Path(output_audio).name} -> {offset:.1f}s")
    print(f"  Subs: {Path(output_subs).name}")
    return {
        "audio_path": output_audio,
        "subs_path": output_subs,
        "duration_sec": offset,
    }


# ─────────── 豆包语音引擎（GL-20260831：第二个配音引擎） ───────────
#
# 豆包语音合成大模型 2.0（火山引擎「豆包语音」产品，非方舟 API）
# - 接口: POST https://openspeech.bytedance.com/api/v3/tts/unidirectional
# - 鉴权: X-Api-Key: <豆包语音 API Key>（控制台 https://console.volcengine.com/speech/new/setting/apikeys）
#          X-Api-Resource-Id: seed-tts-2.0
# - 特性: 支持 enable_subtitle 返回逐字时间戳（可直接生成 SRT）、speech_rate 语速(-50~100)、emotion 情感
# - Key 存 .env: DOUBAO_TTS_KEY；未配置/失败时由上层回退 edge-tts

_DOUBAO_TTS_URL = "https://openspeech.bytedance.com/api/v3/tts/unidirectional"
_DOUBAO_RESOURCE_ID = "seed-tts-2.0"
_DOUBAO_SAMPLE_RATE = 24000
# 单次请求文本上限（超过则按句子分批，避免 40402003 超限）
_DOUBAO_MAX_CHARS = 500


def _doubao_api_key() -> str:
    # 兜底：从 .env 加载密钥（同事各填各的 key，无需 export）
    try:
        from paths import load_env
        load_env()
    except Exception:
        pass
    key = os.environ.get("DOUBAO_TTS_KEY", "").strip()
    if not key:
        raise RuntimeError("未配置 DOUBAO_TTS_KEY（豆包语音 API Key），请在 .env 填写")
    return key


def _edge_rate_to_doubao(rate: str) -> int:
    """edge 语速（如 +28% / -4%）→ 豆包 speech_rate（-50~100，100=2倍速）。
    超出豆包上限时截断（+120% → 100）。"""
    r = normalize_rate(rate)  # 复用规范化（+28% / -4%）
    n = int(r[:-1])           # 去掉尾 %
    return max(-50, min(100, n))


def _doubao_synth_once(text: str, voice: str, speech_rate: int,
                       session: "requests.Session" = None,
                       max_retries: int = 3) -> tuple:
    """单次调用豆包语音合成（带重试）。
    返回 (audio_bytes, sentences)；sentences = [{text, words:[{word,startTime,endTime}]}]
    失败抛 RuntimeError（由上层回退 edge-tts）。"""
    import base64
    import requests  # 延迟导入：仅豆包引擎使用

    key = _doubao_api_key()
    payload = {
        "user": {"uid": "lishishuo"},
        "req_params": {
            "text": text,
            "speaker": voice,
            "audio_params": {
                "format": "mp3",
                "sample_rate": _DOUBAO_SAMPLE_RATE,
                "enable_subtitle": True,
                "speech_rate": speech_rate,
            },
        },
    }
    headers = {
        "X-Api-Key": key,
        "X-Api-Resource-Id": _DOUBAO_RESOURCE_ID,
        "X-Api-Connect-Id": f"ls-{os.getpid()}-{int(__import__('time').time()*1000)}",
        "Content-Type": "application/json",
    }
    last_err = None
    for attempt in range(1, max_retries + 1):
        try:
            resp = (session or requests).post(
                _DOUBAO_TTS_URL, headers=headers, json=payload, timeout=120)
            if resp.status_code != 200:
                raise RuntimeError(f"豆包 HTTP {resp.status_code}: {resp.text[:200]}")
            audio = bytearray()
            sentences = []
            for line in resp.text.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                code = obj.get("code")
                if code == 0 and obj.get("data"):
                    audio.extend(base64.b64decode(obj["data"]))
                elif obj.get("sentence"):
                    sentences.append(obj["sentence"])
                elif code == 20000000:
                    break  # 会话结束
                elif code not in (0,):
                    raise RuntimeError(f"豆包业务错误 {code}: {obj.get('message','')[:200]}")
            if not audio:
                raise RuntimeError("豆包返回空音频")
            return bytes(audio), sentences
        except Exception as e:  # 网络/超时/业务错误统一重试
            last_err = e
            if attempt < max_retries:
                wait = attempt * 6
                print(f"  ⚠ 豆包调用失败（第{attempt}次）：{str(e)[:100]}，{wait}秒后重试…")
                __import__("time").sleep(wait)
    raise RuntimeError(f"豆包语音合成失败: {last_err}")


def _split_long_text(text: str, max_chars: int = _DOUBAO_MAX_CHARS) -> list:
    """按句子切分长文本，每段 ≤ max_chars（按。！？；\n 切，兜底按长度硬切）。"""
    import re as _re
    if len(text) <= max_chars:
        return [text]
    parts = []
    cur = ""
    # 先按行切（稿子本身按句分行）
    for line in _re.split(r"\n+", text):
        line = line.strip()
        if not line:
            continue
        # 行内再按句末标点切
        segs = _re.findall(r"[^。！？；]*[。！？；]?|[^。！？；]+$", line)
        for s in segs:
            s = s.strip()
            if not s:
                continue
            if len(cur) + len(s) > max_chars and cur:
                parts.append(cur)
                cur = s
            else:
                cur += s
    if cur:
        parts.append(cur)
    return parts or [text]


def _sentences_to_srt(sentences: list) -> str:
    """豆包逐字时间戳 → SRT 内容。
    优先用 sentence.words 首末字时间；无 words 的句子用前后时间戳句子估算。"""
    def _ts(sec: float) -> str:
        sec = max(0.0, sec)
        h = int(sec // 3600); m = int((sec % 3600) // 60)
        s = int(sec % 60); ms = int(round((sec - int(sec)) * 1000))
        if ms == 1000:
            s += 1; ms = 0
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

    # 预扫描每个有 words 句子的起止，建立索引
    n = len(sentences)
    timed_idx = [i for i, s in enumerate(sentences) if s.get("words")]

    blocks = []
    idx = 1
    last_end = 0.0
    next_timed_ptr = 0
    for i, s in enumerate(sentences):
        text = (s.get("text") or "").strip()
        if not text:
            continue
        words = s.get("words") or []
        if not words:
            # 无逐字时间戳的句子（聚合句/重复句）跳过——字幕以有 words 的句子为准
            continue
        start = words[0]["startTime"]
        end = words[-1]["endTime"]
        last_end = end
        if next_timed_ptr < len(timed_idx) and timed_idx[next_timed_ptr] == i:
            next_timed_ptr += 1
        blocks.append(f"{idx}\n{_ts(start)} --> {_ts(end)}\n{text}\n")
        idx += 1
    return "\n".join(blocks)


def generate_tts_doubao(
    script_path: str,
    output_audio: str,
    output_subs: str,
    voice: str = "zh_female_vv_uranus_bigtts",
    rate: str = "-4%",
) -> dict:
    """豆包语音引擎整篇合成：长文按句分批 → 音频 concat → 逐字时间戳 SRT。
    返回与 generate_tts 同结构 {audio_path, subs_path, duration_sec}。"""
    script_path = str(script_path); output_audio = str(output_audio); output_subs = str(output_subs)
    ensure_dir(output_audio)
    with open(script_path, encoding="utf-8") as f:
        text = f.read().strip()
    if not text:
        raise RuntimeError("稿子为空")
    speech_rate = _edge_rate_to_doubao(rate)
    print(f"  Running: doubao-tts --speaker {voice} --speech_rate {speech_rate}")

    import tempfile, shutil, time as _t
    tmpdir = tempfile.mkdtemp(prefix="doubao_")
    try:
        seg_audios = []
        offset = 0.0
        all_sentences = []
        parts = _split_long_text(text)
        for i, part in enumerate(parts):
            audio, sentences = _doubao_synth_once(part, voice, speech_rate)
            seg_path = os.path.join(tmpdir, f"seg_{i:03d}.mp3")
            with open(seg_path, "wb") as f:
                f.write(audio)
            dur = _audio_duration_sec(seg_path)
            seg_audios.append(seg_path)
            # 本段句子时间戳是相对本段起点的 → 平移到全局
            for s in sentences:
                s2 = dict(s)
                words = []
                for w in s.get("words", []):
                    w2 = dict(w)
                    w2["startTime"] = round(w2["startTime"] + offset, 3)
                    w2["endTime"] = round(w2["endTime"] + offset, 3)
                    words.append(w2)
                s2["words"] = words
                all_sentences.append(s2)
            print(f"  [段{i+1}/{len(parts)}] {dur:.1f}s（{len(sentences)} 句字幕）")
            offset += dur
            _t.sleep(0.3)  # 避免并发限流
        if not seg_audios:
            raise RuntimeError("豆包：所有段落均为空")
        _concat_audios(seg_audios, output_audio)
        srt = _sentences_to_srt(all_sentences)
        with open(output_subs, "w", encoding="utf-8") as f:
            f.write(srt)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    print(f"  TTS done（豆包 {len(seg_audios)} 段）: {Path(output_audio).name} -> {offset:.1f}s")
    print(f"  Subs: {Path(output_subs).name}")
    return {"audio_path": output_audio, "subs_path": output_subs, "duration_sec": offset}


# 豆包 2.0 推荐音色（通用/视频配音场景，中文）——网页音色列表用
DOUBAO_VOICES = [
    {"name": "zh_female_vv_uranus_bigtts",      "label": "vivi 2.0 · 女声 · 通用自然（推荐）"},
    {"name": "zh_male_dayi_saturn_bigtts",      "label": "大壹 · 男声 · 视频配音"},
    {"name": "zh_female_santongyongns_saturn_bigtts", "label": "流畅女声 · 视频配音"},
    {"name": "zh_male_ruyayichen_saturn_bigtts", "label": "儒雅逸辰 · 男声 · 视频配音"},
    {"name": "zh_female_gaolengyujie_uranus_bigtts", "label": "高冷御姐 · 女声 · 视频配音"},
    {"name": "zh_female_jitangnv_saturn_bigtts", "label": "鸡汤女 · 女声 · 视频配音"},
    {"name": "zh_male_shenyeboke_emo_v2_mars_bigtts", "label": "深夜播客 · 男声 · 磁性"},
    {"name": "zh_female_wanwanxiaohe_mars_bigtts", "label": "湾湾小何 · 女声 · 台湾腔"},
]


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
