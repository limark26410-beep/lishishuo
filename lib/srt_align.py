# -*- coding: utf-8 -*-
"""字幕 ASR 校正：用真实音频时间戳重排 SRT，解决 edge-tts 字幕与录音不同步。

原理：edge-tts 的 SRT 时间戳是按文本长度"估算"的，语速一变就偏。
本模块用 sherpa-onnx ASR 听真实音频，得到每句的精确起止时间，重写 SRT。

复用 talkcraft 的工具链（Python 3.11 venv + sherpa 1.10.46 + FireRed CTC 模型），
通过 subprocess 调用，避免与 lishishuo 主 venv（py3.14）依赖冲突。

回滚：删除 run.py 中 srt_align 调用段即可。
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

# ASR 环境（Windows：直接用当前 Python + 本仓库脚本，需已装 sherpa-onnx）
TALK_VENV_PY = sys.executable
TALK_TS_SCRIPT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "scripts", "timestamps_cpu.py")
ASR_MODEL_DIR = str(Path.home() / ".cache/koubo/sherpa-onnx-fire-red-asr2-ctc-zh_en-int8-2026-02-25")

# 音频重采样临时目录
TMP = os.path.join(tempfile.gettempdir(), "srt_align")


def _ts_str(sec: float) -> str:
    """秒 → SRT 时间格式 00:00:00,000"""
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = sec % 60
    return f"{h:02d}:{m:02d}:{int(s):02d},{int((s - int(s)) * 1000):03d}"


def _ensure_audio_wav(audio_path: str) -> str:
    """转成 16k 单声道 wav（ASR 需要）"""
    os.makedirs(TMP, exist_ok=True)
    wav = os.path.join(TMP, "align_in.wav")
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", audio_path,
         "-ar", "16000", "-ac", "1", wav],
        check=True, capture_output=True, text=True)
    return wav


def _cjk_len(s: str) -> int:
    """有效字数（去掉标点空白），用于语速估算"""
    return len(re.sub(r"[^\w\u4e00-\u9fff]", "", s)) or 1


def _script_sentences(script_text: str) -> list[str]:
    """稿子 → 按句切分的有序句子表（字幕文本的唯一来源）"""
    body = re.sub(r"\s+", "", script_text)
    return [x.strip() for x in re.split(r"(?<=[。！？])", body) if x.strip()]


def _repair_timeline(times: list, chars: list, rate: float, total: float) -> list:
    """时长合理性修复：把"不可能的时长"用相邻可信锚点按字数重分。

    背景（GL-20260911 实测）：分段 ASR 在段边界会把句子撑长或压扁
    （实测"他不是没想过离开"8 字撑到 6.1s，"路漫漫其修远兮"33 字压到 0.8s），
    下游连锁导致字幕整体滞后 4~5s——用户反馈"2分40秒字幕慢了"。
    这里用全局语速 rate 判定离群，再按字数比例重分，保证整条时间轴单调且合理。
    """
    n = len(times)
    ok = []
    for i, t in enumerate(times):
        if not t:
            ok.append(False)
            continue
        d = t[1] - t[0]
        ok.append(chars[i] * rate * 0.40 <= d <= chars[i] * rate * 2.40 + 0.6)

    for _ in range(3):
        i = 0
        while i < n:
            if ok[i]:
                i += 1
                continue
            j = i
            while j < n and not ok[j]:
                j += 1
            left = times[i - 1][1] if i > 0 else 0.0
            right = times[j][0] if j < n else total
            if right <= left:
                right = left + sum(chars[i:j]) * rate
            span = right - left
            tot = sum(chars[i:j]) or 1
            cur = left
            for k in range(i, j):
                d = span * chars[k] / tot
                times[k] = (cur, cur + d)
                ok[k] = True
                cur += d
            i = j

    # 单调校正
    for i in range(1, n):
        if times[i][0] < times[i - 1][1]:
            times[i] = (times[i - 1][1], max(times[i - 1][1] + 0.3, times[i][1]))
    return times


def _asr_once(wav_path: str, script_text: str, tag: str = "") -> list[dict]:
    """单次 ASR 调用（音频需已切好）"""
    os.makedirs(TMP, exist_ok=True)
    txt_path = os.path.join(TMP, f"align_script{tag}.txt")
    out_json = os.path.join(TMP, f"align_ts{tag}.json")
    Path(txt_path).write_text(script_text, encoding="utf-8")
    if os.path.exists(out_json):
        os.remove(out_json)

    r = subprocess.run(
        [TALK_VENV_PY, TALK_TS_SCRIPT, wav_path, txt_path, out_json,
         "--model-dir", ASR_MODEL_DIR],
        capture_output=True, text=True, timeout=900)
    if not os.path.exists(out_json):
        raise RuntimeError(f"ASR 失败: {r.stderr[-400:]}")

    data = json.loads(Path(out_json).read_text(encoding="utf-8"))
    return data.get("sentences", [])


def _find_split_points(wav: str, total: float, target_seg: float = 40.0) -> list:
    """用静音检测找"自然停顿点"，作为音频切片边界。

    固定按 40s 硬切会把句子拦腰截断（两侧都识别不全 → 字幕漏整句，
    实测每期漏句，导致"字幕和声音对不上"）。改为在目标时间附近找静音中点。
    """
    r = subprocess.run(
        ["ffmpeg", "-i", wav, "-af", "silencedetect=n=-35dB:d=0.25", "-f", "null", "-"],
        capture_output=True, text=True)
    starts = [float(x) for x in re.findall(r"silence_start: ([0-9.]+)", r.stderr)]
    ends = [float(x) for x in re.findall(r"silence_end: ([0-9.]+)", r.stderr)]
    mids = [(s + e) / 2 for s, e in zip(starts, ends)]

    points = [0.0]
    t = target_seg
    while t < total - 8:
        cand = [m for m in mids if abs(m - t) <= 12 and m > points[-1] + 10]
        points.append(min(cand, key=lambda m: abs(m - t)) if cand else t)
        t += target_seg
    points.append(total)
    return points


def _run_asr(audio_path: str, script_text: str,
             seg_seconds: float = 40.0,
             srt_entries: list = None) -> list[dict]:
    """分段 ASR：长音频按 seg_seconds 切片逐段识别，再合并时间戳。

    sherpa FireRed CTC 处理长音频会报 broadcast 维度错误（实测 >2 分钟必挂），
    因此按 40 秒切片。

    稿子→段落的分配策略（关键）：
    - 优先用原 SRT 的句子时间（虽为估算，但顺序与音频一致）
    - 无 SRT 时回退按字数比例
    按字数硬切会在语速不均时错位，导致末段对不上（实测踩过）。
    """
    if not os.path.exists(TALK_VENV_PY):
        raise RuntimeError(f"talkcraft venv 不存在: {TALK_VENV_PY}")
    if not os.path.exists(ASR_MODEL_DIR):
        raise RuntimeError(f"ASR 模型目录不存在: {ASR_MODEL_DIR}")

    wav = _ensure_audio_wav(audio_path)          # 16k 单声道
    total = _wav_duration(wav)

    # 句子表一律取自稿子（有序、完整）；按【字数比例】估算每句位置。
    # 实测（GL-20260911）：字数比例估算误差 ≤1.2s，而早期用的"旧 SRT 估算时间"
    # 到 2 分 40 秒已漂移 5.4s，把句子分错段 → 段内对齐崩坏 → 字幕整体滞后。
    sents = _script_sentences(script_text)
    if not sents:
        sents = [t for _s, _e, t in (srt_entries or []) if t.strip()] or [script_text]
    chars = [_cjk_len(s) for s in sents]
    tot_c = sum(chars) or 1
    rate = total / tot_c
    est = []          # 每句 (start, end) 的字数比例估算
    _c = 0
    for n in chars:
        est.append((total * _c / tot_c, total * (_c + n) / tot_c))
        _c += n

    # 短音频直接一次识别
    if total <= seg_seconds * 1.5:
        try:
            res = _asr_once(wav, "\n".join(sents))
        except Exception as e:
            raise RuntimeError(f"ASR 失败: {str(e)[:120]}")
        times = [(float(s["start"]), float(s["end"])) if i < len(res) else None
                 for i, s in enumerate(res)] + [None] * max(0, len(sents) - len(res))
        times = times[:len(sents)]
        times = _repair_timeline(times, chars, rate, total)
        return [{"text": t, "start": times[i][0], "end": times[i][1], "match": 1.0}
                for i, t in enumerate(sents)]

    # 长音频：静音点切片。段间【音频重叠 OV 秒】+【文本外扩 MARGIN 秒】——
    # 跨边界的句子在相邻段里是"完整的"，取 match 最高的那次即可避免边界崩坏。
    OV, MARGIN = 5.0, 12.0
    points = _find_split_points(wav, total, seg_seconds)
    n_seg = len(points) - 1
    print(f"    切分点（静音对齐）: {[f'{p:.0f}s' for p in points]}")

    best = [None] * len(sents)      # 每句的最佳候选 {match, start, end, interior}
    n_ok = 0
    for k in range(n_seg):
        p0, p1 = points[k], points[k + 1]
        ws, we = max(0.0, p0 - OV), min(total, p1 + OV)
        # 文本窗口比音频窗口宽，覆盖可能跨界的句子
        idxs = [i for i in range(len(sents))
                if est[i][0] < p1 + MARGIN and est[i][1] > p0 - MARGIN]
        if not idxs:
            continue
        chunk = os.path.join(TMP, f"seg_{k:02d}.wav")
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-i", wav,
             "-ss", f"{ws:.3f}", "-t", f"{we - ws:.3f}",
             "-ar", "16000", "-ac", "1", chunk],
            check=True, capture_output=True, text=True)
        try:
            res = _asr_once(chunk, "\n".join(sents[i] for i in idxs), tag=f"_{k:02d}")
        except Exception as e:
            print(f"    ⚠ 第{k+1}段 ASR 失败（{str(e)[:80]}），跳过")
            continue
        got = 0
        for j, gi in enumerate(idxs):
            if j >= len(res):
                break
            s = res[j]
            st, en = float(s["start"]) + ws, float(s["end"]) + ws
            # 越靠段内侧越可信：句子距音频窗口边缘的距离
            interior = min(st - ws, we - en)
            cand = {"match": float(s.get("match", 0)), "start": st, "end": en,
                    "interior": interior}
            cur = best[gi]
            if cur is None or (cand["match"], cand["interior"]) > (cur["match"], cur["interior"]):
                best[gi] = cand
            if cand["match"] >= 0.8:
                got += 1
        n_ok += got
        print(f"    段{k+1}/{n_seg}: {got}/{len(idxs)} 句可信（{p0:.0f}-{p1:.0f}s）")

    times = []
    n_fix = 0
    for i in range(len(sents)):
        c = best[i]
        if c and c["match"] >= 0.55 and c["interior"] > -0.5:
            times.append((c["start"], c["end"]))
        else:
            times.append(None)
            n_fix += 1
    times = _repair_timeline(times, chars, rate, total)
    if n_fix:
        print(f"    时长修复: {n_fix} 句用相邻锚点按字数重分")
    print(f"    ✓ 对齐完成: {len(sents)} 句 / 语速 {rate:.3f}s·字")
    return [{"text": t, "start": times[i][0], "end": times[i][1],
             "match": best[i]["match"] if best[i] else 0.0}
            for i, t in enumerate(sents)]


def _filter_sentences(sentences: list[dict]) -> list[dict]:
    """只保留识别质量达标的句子（match ≥ 0.8 或 ok=True）"""
    out = []
    for s in sentences:
        if s.get("ok", False) or s.get("match", 0) >= 0.8:
            txt = (s.get("text") or "").strip()
            if not txt:
                continue
            out.append({
                "text": txt,
                "start": float(s["start"]),
                "end": float(s["end"]),
                "match": s.get("match", 0),
            })
    return out


def _wav_duration(path: str) -> float:
    """wav 时长（秒）"""
    import wave
    try:
        with wave.open(path, "rb") as w:
            return w.getnframes() / float(w.getframerate())
    except Exception:
        r = subprocess.run(["ffmpeg", "-i", path], capture_output=True, text=True)
        m = re.search(r"Duration: (\d+):(\d+):(\d+\.?\d*)", r.stderr)
        if m:
            return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
        return 0.0


def align_srt(
    srt_path: str,
    audio_path: str,
    script_path: str,
    output_path: str,
    min_display_s: float = 0.8,
) -> str:
    """用 ASR 真实时间重写 SRT。

    策略：
    - 读稿子正文 → ASR 出每句真实起止
    - 读原 SRT 的文本内容（已按语义切好的字幕行）
    - 把 ASR 的句子时间分配给内容最接近的 SRT 条目
    - 无 ASR 结果时原样复制（不破坏出片）
    """
    print("\n  字幕 ASR 校正（真实音频对齐）...")

    # 读原 SRT 条目（拿时间用于分段 ASR；文本最终以稿子为准）
    try:
        raw = Path(srt_path).read_text(encoding="utf-8")
    except Exception:
        raw = ""
    orig_entries = []  # [text_joined]
    timed_entries = []  # [(start, end, text)] —— 供分段 ASR 用
    blocks = re.split(r"\n\s*\n", raw.strip())
    for b in blocks:
        lines = b.splitlines()
        if len(lines) >= 3 and "-->" in lines[1]:
            text = "".join(l for l in lines[2:] if l.strip()).strip()
            orig_entries.append(text)
            try:
                ts = lines[1].split("-->")
                def _t(x):
                    p = x.strip().replace(",", ".").split(":")
                    return float(p[0]) * 3600 + float(p[1]) * 60 + float(p[2])
                timed_entries.append((_t(ts[0]), _t(ts[1]), text))
            except Exception:
                pass

    if not orig_entries:
        print("  ⚠ 原 SRT 无条目，跳过校正")
        return srt_path

    # 读稿子（去掉首行标题，只留正文）
    script_text = ""
    try:
        lines = Path(script_path).read_text(encoding="utf-8").splitlines()
        # 跳过首行标题（形如"系列 第X期 主·副"）
        body = [l for l in lines if l.strip() and not re.match(r"^[\u4e00-\u9fff]{2,8}\s*第\s*\d+\s*期", l.strip())]
        script_text = "\n".join(body)
    except Exception:
        script_text = " ".join(orig_entries)

    if not script_text.strip():
        script_text = " ".join(orig_entries)

    # ASR
    try:
        asr_sentences = _run_asr(audio_path, script_text, srt_entries=timed_entries)
    except Exception as e:
        print(f"  ⚠ ASR 校正失败（{type(e).__name__}: {str(e)[:120]}），保留原字幕")
        return srt_path

    if not asr_sentences:
        print("  ⚠ ASR 无结果，保留原字幕")
        return srt_path

    # ── 用 ASR 时间轴 + 【稿子文本】（文本完整，时间真实）──
    # ASR 会漏字/错字，不能直接当字幕；只取其时间轴。
    # 字幕文本改为直接来自稿子按句切分——不依赖中间 SRT（它可能已被清理，
    # 或折行后条数不匹配），保证字幕完整不漏句。
    body_for_subs = re.sub(r"\s+", "", script_text)
    sents_from_script = [x.strip() for x in re.split(r"(?<=[。！？])", body_for_subs) if x.strip()]
    text_source = sents_from_script or orig_entries
    aligned = _map_text_to_asr_time(text_source, asr_sentences)
    if not aligned:
        print("  ⚠ 文本对齐失败，保留原字幕")
        return srt_path

    out_entries = []
    idx = 1
    prev_end = 0.0
    n_al = len(aligned)
    for k, (txt, start, end) in enumerate(aligned):
        # 时间单调递增修正
        if start < prev_end:
            start = prev_end
        # 连续覆盖：每条持续到下一句开始（旁白是连续朗读；
        # 分段边界处 ASR 会漏识别，造成时间锚点缺失与字幕空档）
        if k + 1 < n_al:
            nxt = aligned[k + 1][1]
            if nxt > start + 0.5:
                end = max(end, nxt - 0.12)
        if end - start < min_display_s:
            end = start + min_display_s
        prev_end = end
        out_entries.append(
            f"{idx}\n{_ts_str(start)} --> {_ts_str(end)}\n{txt}")
        idx += 1

    Path(output_path).write_text("\n\n".join(out_entries) + "\n", encoding="utf-8")
    avg = sum(s.get("match", 0) for s in asr_sentences) / max(len(asr_sentences), 1)
    print(f"  ✓ ASR 校正: {len(out_entries)} 条字幕（文本取自原稿，时间来自真实音频，"
          f"ASR 平均 match {avg:.2f}）")
    return output_path


def _map_text_to_asr_time(orig_entries: list[str],
                          asr_sentences: list[dict]) -> list[tuple]:
    """把原字幕文本映射到 ASR 真实时间轴。

    做法：把「原字幕全文」与「ASR 全文」做字符级对齐（difflib），
    用匹配块建立 原文字符位置 → ASR字符位置 的映射，
    再由 ASR 字符位置反查其所在句子的起止时间。

    返回 [(text, start, end), ...]；对齐失败返回 []。
    """
    import difflib

    srt_texts = [t.replace(" ", "") for t in orig_entries]
    asr_texts = [(s.get("text") or "").replace(" ", "") for s in asr_sentences]
    # 快路径：分段 ASR 现在直接以稿子句子为参考行，两边文本逐句相同 → 免 difflib
    if srt_texts and srt_texts == asr_texts:
        return [(orig_entries[i], float(asr_sentences[i]["start"]),
                 float(asr_sentences[i]["end"])) for i in range(len(orig_entries))]
    srt_full = "".join(srt_texts)
    asr_full = "".join(asr_texts)
    if not srt_full or not asr_full:
        return []

    # ASR 每个字符 → (start, end)
    char_time = []
    for s, t in zip(asr_sentences, asr_texts):
        char_time.extend([(float(s["start"]), float(s["end"]))] * len(t))
    if not char_time:
        return []

    # 字符级对齐：原文字符位置 → ASR 字符位置
    sm = difflib.SequenceMatcher(None, srt_full, asr_full, autojunk=False)
    pos_map = {}
    for a, b, size in sm.get_matching_blocks():
        for k in range(size):
            pos_map[a + k] = b + k
    if len(pos_map) < len(srt_full) * 0.5:
        # 匹配率过低（<50%），说明两边内容差异大，放弃
        return []

    def _time_at(char_idx: int, forward: bool = True):
        """从 char_idx 出发找最近的已映射位置，返回对应时间"""
        rng = range(char_idx, len(srt_full)) if forward else range(char_idx, -1, -1)
        for k in rng:
            if k in pos_map:
                b = min(pos_map[k], len(char_time) - 1)
                return char_time[b]
        return None

    out = []
    pos = 0
    for txt in srt_texts:
        n = len(txt)
        if n == 0:
            continue
        head = _time_at(pos, forward=True)
        tail = _time_at(pos + n - 1, forward=False)
        pos += n
        if head is None or tail is None:
            continue
        out.append((txt, head[0], tail[1]))
    return out
