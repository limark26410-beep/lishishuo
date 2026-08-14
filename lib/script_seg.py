# -*- coding: utf-8 -*-
"""稿子段落切分 + 段落音频时长聚合（视频混剪第二步：AI 选材基础）

- split_segments(): 稿子正文 → 段落列表（段落即选材单位）
- segment_audio_durations(): 从 TTS 产出的 subs.srt 聚合每段音频时长，
  AI 选材的截取时长最终以此为准（段音频时长 + 0.5s 转场余量）
"""

import re

_SRT_TIME_RE = re.compile(
    r"(\d+):(\d+):(\d+),(\d+)\s*-->\s*(\d+):(\d+):(\d+),(\d+)")


def split_segments(script_body: str) -> list:
    """把稿子正文切成段落列表。

    规则：按空行分段；无空行时按单换行分段；过滤空段。
    返回: [{"index": 0, "text": "段落文本（不含空行）"}, ...]
    每段保留完整句子，不从中截断（语义匹配靠整段）。
    """
    if not script_body:
        return []
    raw = (script_body or "").strip().split("\n")
    # 段落分隔：空行
    paras = []
    cur = []
    has_blank = any(not ln.strip() for ln in raw)
    for ln in raw:
        if not ln.strip():
            if cur:
                paras.append("\n".join(cur).strip())
                cur = []
            continue
        cur.append(ln.strip())
    if cur:
        paras.append("\n".join(cur).strip())

    if not has_blank:
        # 无空行：按单换行分段
        paras = [ln.strip() for ln in raw if ln.strip()]

    paras = [p for p in paras if p]
    return [{"index": i, "text": p} for i, p in enumerate(paras)]


def _parse_srt(srt_path: str) -> list:
    """解析 srt 文件，返回 [{"start": 秒, "end": 秒, "text": 文本}, ...]"""
    cues = []
    try:
        with open(srt_path, "r", encoding="utf-8") as f:
            lines = f.read().splitlines()
    except Exception:
        return []
    i = 0
    n = len(lines)
    while i < n:
        ln = lines[i].strip()
        m = _SRT_TIME_RE.search(ln)
        if m:
            g = m.groups()
            start = (int(g[0]) * 3600 + int(g[1]) * 60 + int(g[2])
                     + int(g[3]) / 1000.0)
            end = (int(g[4]) * 3600 + int(g[5]) * 60 + int(g[6])
                   + int(g[7]) / 1000.0)
            text_lines = []
            i += 1
            while i < n and lines[i].strip():
                text_lines.append(lines[i].strip())
                i += 1
            cues.append({"start": start, "end": end,
                         "text": " ".join(text_lines)})
        else:
            i += 1
    return cues


def _norm(text: str) -> str:
    """去全部空白，用于文本长度/顺序匹配"""
    return re.sub(r"\s+", "", text or "")


def segment_audio_durations(segments: list, subs_srt: str) -> list:
    """从 subs.srt 聚合出每段的音频时长（秒），与 segments 等长。

    规则：
    - cue 按顺序遍历，段落文本（归一化后）的字符长度锚定段落边界
      （TTS 字幕由整稿生成，cue 顺序与稿子一致）
    - 段音频起点 = 该段第一句 cue.start；终点 = 该段最后一句 cue.end
    - 匹配失败（cue 文本与段落对不上/长度漂移）→ 退化为按字数占比
      分配总时长（最后一句 cue.end），并打印警告
    """
    if not segments:
        return []
    cues = _parse_srt(subs_srt)
    if not cues:
        print("  ⚠ 无字幕 cue，段落时长按字数占比分配")
        return _fallback(segments, 0.0)

    total_audio = cues[-1]["end"]
    seg_lens = [len(_norm(s["text"])) for s in segments]
    if not any(seg_lens):
        return _fallback(segments, total_audio)

    durations = []
    cue_idx = 0
    ok = True
    for need in seg_lens:
        consumed = 0
        start_t = None
        end_t = None
        while cue_idx < len(cues) and consumed < need:
            c = cues[cue_idx]
            if start_t is None:
                start_t = c["start"]
            end_t = c["end"]
            consumed += len(_norm(c["text"]))
            cue_idx += 1
        if consumed < need or start_t is None:
            ok = False
            break
        durations.append(end_t - start_t)

    if not ok or len(durations) != len(segments):
        print("  ⚠ 字幕与段落匹配不精确，段落时长按字数占比分配（总和=音频总时长）")
        return _fallback(segments, total_audio)

    # 时长平滑：保证总和 = 音频总时长（±0.1s 内的浮点差）
    diff = sum(durations) - total_audio
    if abs(diff) > 0.001 and durations:
        durations[0] = max(0.0, durations[0] - diff)

    return durations


def _fallback(segments: list, total_audio: float) -> list:
    """按字数占比分配总时长"""
    total_han = sum(len(_norm(s["text"])) for s in segments)
    if not total_han:
        per = total_audio / len(segments) if segments else 0.0
        return [per] * len(segments)
    return [total_audio * len(_norm(s["text"])) / total_han for s in segments]


if __name__ == "__main__":
    import sys as _sys
    path = _sys.argv[1] if len(_sys.argv) > 1 else "episodes/039/script.txt"
    body = open(path, encoding="utf-8").read()
    segs = split_segments(body)
    print(f"段落数: {len(segs)}")
    for s in segs:
        print(f"  [{s['index']}] {s['text'][:40]}…")
    srt = path.replace("script.txt", "subs.srt")
    if __import__("os").path.exists(srt):
        durs = segment_audio_durations(segs, srt)
        print(f"段落时长: {[round(d, 1) for d in durs]}")
        print(f"时长和: {sum(durs):.1f}s")
