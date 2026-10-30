# -*- coding: utf-8 -*-
"""字幕时间戳：用 sherpa-onnx FireRed CTC 对齐「参考文本 → 音频真实时间」。

替代 Mac 上 talkcraft 的 timestamps_cpu.py，接口保持一致：

    python scripts/timestamps_cpu.py <wav> <ref.txt> <out.json> --model-dir <dir>

ref.txt 每行一句（srt_align 传入的就是稿子按句切分的结果）。
输出 {"sentences": [{"text", "start", "end", "match"}, ...]}，与参考行一一对应。

做法：
1. sherpa-onnx 识别音频，拿到每个 token 的文本与时间戳；
2. 把识别出的字符序列与参考文本做字符级 difflib 对齐；
3. 每一句取首/尾匹配字符的时间，match = 匹配字数 / 参考字数。
识别只用来取时间轴，输出文本始终用参考行（避免 ASR 错字进字幕）。
"""
from __future__ import annotations

import argparse
import array
import json
import os
import re
import sys
import wave
from difflib import SequenceMatcher
from pathlib import Path

from pypinyin import Style, lazy_pinyin

DEFAULT_MODEL = str(
    Path.home() / ".cache/koubo/sherpa-onnx-fire-red-asr2-ctc-zh_en-int8-2026-02-25")

# 只保留真正读出来的字：汉字 + 英文字母 + 数字
_KEEP = re.compile(r"[\u3400-\u9fffA-Za-z0-9]")

_PY_CACHE: dict[str, str] = {}


def _is_keep(ch: str) -> bool:
    return bool(_KEEP.match(ch))


def _tok(ch: str) -> str:
    """对齐用的最小单位：汉字转拼音（避免 金/斤 这类同音误识别拆散句子），
    英文/数字转小写。"""
    t = _PY_CACHE.get(ch)
    if t is None:
        if "\u3400" <= ch <= "\u9fff":
            t = lazy_pinyin(ch, style=Style.NORMAL)[0]
        else:
            t = ch.lower()
        _PY_CACHE[ch] = t
    return t


def _read_wav(path: str):
    """读 16k 单声道 wav，返回 (sample_rate, float samples)。"""
    with wave.open(path, "rb") as w:
        sr = w.getframerate()
        nch = w.getnchannels()
        sw = w.getsampwidth()
        raw = w.readframes(w.getnframes())
    if sw != 2:
        raise RuntimeError(f"只支持 16-bit wav，收到 sampwidth={sw}: {path}")
    a = array.array("h")
    a.frombytes(raw)
    if sys.byteorder == "big":
        a.byteswap()
    if nch > 1:
        a = array.array(
            "h",
            [sum(a[i:i + nch]) // nch for i in range(0, len(a), nch)],
        )
    return sr, [x / 32768.0 for x in a]


def _recognize(sr: int, samples: list, model_dir: str):
    import sherpa_onnx

    model = os.path.join(model_dir, "model.int8.onnx")
    tokens = os.path.join(model_dir, "tokens.txt")
    if not os.path.exists(model):
        raise RuntimeError(f"ASR 模型不存在: {model}")
    if not os.path.exists(tokens):
        raise RuntimeError(f"ASR tokens 不存在: {tokens}")

    rec = sherpa_onnx.OfflineRecognizer.from_fire_red_asr_ctc(
        model=model, tokens=tokens, num_threads=4,
    )
    stream = rec.create_stream()
    stream.accept_waveform(sr, samples)
    rec.decode_stream(stream)
    return stream.result


def _asr_chars(result) -> list[tuple[str, float, float]]:
    """识别 token → [(字符, 起, 止), ...]，跳过标点/空白/特殊符号。"""
    toks = list(getattr(result, "tokens", []) or [])
    tss = list(getattr(result, "timestamps", []) or [])
    out: list[tuple[str, float, float]] = []
    prev_end = 0.0
    for i, tok in enumerate(toks):
        if i >= len(tss):
            break
        ts = float(tss[i])
        text = str(tok).replace("▁", " ").strip()
        if not text or (text.startswith("<") and text.endswith(">")):
            prev_end = max(prev_end, ts)
            continue
        chars = [c for c in text if _is_keep(c)]
        if not chars:
            prev_end = max(prev_end, ts)
            continue
        # timestamp 视为该 token 的结束时间；起点取「上一个 token 结束」与
        # 「终点往前 0.30s」中较晚者——长停顿时不会被拉回上一句。
        start = max(prev_end, ts - 0.30)
        if start >= ts:
            start = max(0.0, ts - 0.12)
        step = (ts - start) / len(chars)
        for j, c in enumerate(chars):
            out.append((c.lower(), start + j * step, start + (j + 1) * step))
        prev_end = ts
    return out


def _norm(text: str) -> list[str]:
    return [c.lower() for c in text if _is_keep(c)]


def _pick_cluster(blocks):
    """从 difflib 匹配块里挑「连续的一段」。

    一句话里个别字被 ASR 听错时，difflib 会在句子后段再找到一个同字，
    产生一个离得很远的孤立匹配块；若把它算进句尾，会把字幕拉长十几秒。
    这里按参考位置/识别位置的双向间隔聚类，取匹配字数最多的那一簇。
    """
    if not blocks:
        return []
    clusters = []
    cur = [blocks[0]]
    cur_a = blocks[0].a + blocks[0].size
    cur_b = blocks[0].b + blocks[0].size
    for b in blocks[1:]:
        if b.a - cur_a <= 6 and b.b - cur_b <= 6:
            cur.append(b)
        else:
            clusters.append(cur)
            cur = [b]
        cur_a = b.a + b.size
        cur_b = b.b + b.size
    clusters.append(cur)
    # 匹配字数最多的一簇；并列取最靠前的
    return max(clusters, key=lambda c: (sum(x.size for x in c), -c[0].a))


def align(wav: str, ref_text: str, model_dir: str) -> dict:
    sr, samples = _read_wav(wav)
    total = len(samples) / float(sr or 1)
    result = _recognize(sr, samples, model_dir)
    asr = _asr_chars(result)

    sentences = [ln.strip() for ln in ref_text.splitlines() if ln.strip()]
    if not sentences:
        return {"sentences": []}

    asr_tokens = [_tok(c) for c, _s, _e in asr]
    out = []
    cursor = 0          # 已消费到的 ASR 字符位置（单调推进）
    anchor_end = 0.0    # 最近一条「高置信匹配」的结束时间
    for text in sentences:
        chars = _norm(text)
        n = len(chars)
        ref = [_tok(c) for c in chars]
        if n == 0:
            out.append({"text": text, "start": anchor_end, "end": anchor_end,
                        "match": 1.0})
            continue

        # 只在 cursor 之后的有限窗口里找这一句：跳过窗口开头不属于本句的
        # 前一句残留，也避免把本句匹配到后面重复出现的同名片段。
        win_len = max(80, n * 3)
        window = asr_tokens[cursor:cursor + win_len]
        sm = SequenceMatcher(None, ref, window, autojunk=False)
        blocks = [b for b in sm.get_matching_blocks() if b.size > 0]
        cluster = _pick_cluster(blocks)
        matched = sum(b.size for b in cluster)
        match = matched / n

        if matched >= max(1, int(n * 0.5)):
            positions = []
            for b in cluster:
                for k in range(b.size):
                    positions.append(cursor + b.b + k)
            start = asr[positions[0]][1]
            end = asr[positions[-1]][2]
            cursor = min(len(asr), positions[-1] + 1)
            if start < anchor_end:
                start = anchor_end
            if end <= start:
                end = start + max(0.30, n * 0.16)
            anchor_end = end
        else:
            # 这一句在窗口内没有足够匹配（多出现在切片边界外）：
            # 给一个按字数比例的占位时间，match=0，下游会换用相邻切片的结果。
            # 注意：占位时间不推进 anchor_end，避免把后一句真正的起点顶后。
            start = max(anchor_end, total * cursor / max(len(asr_tokens), 1))
            end = start + max(0.30, n * 0.18)

        out.append({
            "text": text,
            "start": round(start, 3),
            "end": round(end, 3),
            "match": round(match, 4),
        })
    return {"sentences": out}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("wav")
    ap.add_argument("ref")
    ap.add_argument("out")
    ap.add_argument("--model-dir", default=DEFAULT_MODEL)
    a = ap.parse_args()

    ref_text = Path(a.ref).read_text(encoding="utf-8")
    data = align(a.wav, ref_text, a.model_dir)
    Path(a.out).write_text(
        json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"aligned {len(data['sentences'])} sentences -> {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
