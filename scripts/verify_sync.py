#!/usr/bin/env python3
"""字幕音画同步校验器（独立复核，不依赖出片流程内的对齐结果）。

做法：把成片音频按窗口切出，用【烧进视频的字幕文本】当参考行做强制对齐，
再逐条比较"字幕写的时间"与"音频里真实的时间"。

    python scripts/verify_sync.py 成片.mp4 字幕.srt
    python scripts/verify_sync.py 成片.mp4 字幕.srt --tag 开封
    python scripts/verify_sync.py 成片.mp4 字幕.srt --json report.json

退出码：0 = 全部通过（|偏差| 全部 ≤ 阈值）；1 = 有超阈条目。
"""
import argparse
import json
import os
import re
import statistics
import subprocess
import sys
import tempfile

TALK_VENV_PY = "/Users/local/.dsh/skills/video-talkcraft/.venv311/bin/python"
TALK_TS_SCRIPT = "/Users/local/.dsh/skills/video-talkcraft/scripts/timestamps_cpu.py"
ASR_MODEL_DIR = os.path.expanduser(
    "~/.cache/koubo/sherpa-onnx-fire-red-asr2-ctc-zh_en-int8-2026-02-25")
FFMPEG = "/usr/local/bin/ffmpeg"
WINDOW = 60.0          # 每个校验窗口秒数
EDGE = 0.6             # 窗口边缘丢弃带（音频被截断的条目不计）


def _t(s):
    h, m, r = s.split(":")
    return int(h) * 3600 + int(m) * 60 + float(r.replace(",", "."))


def read_srt(path):
    out = []
    for blk in open(path, encoding="utf-8").read().strip().split("\n\n"):
        L = blk.split("\n")
        if len(L) >= 3 and "-->" in L[1]:
            a, b = L[1].split("-->")
            txt = "".join(L[2:]).strip()
            if txt:
                out.append({"start": _t(a), "end": _t(b), "text": txt})
    return out


def duration(media):
    r = subprocess.run([FFMPEG, "-i", media], capture_output=True, text=True)
    m = re.search(r"Duration: (\d+):(\d+):(\d+\.?\d*)", r.stderr)
    if not m:
        raise RuntimeError(f"读不到时长: {media}")
    return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))


def verify(media, srt_path, tmpdir, timeout_note=True):
    entries = read_srt(srt_path)
    if not entries:
        raise RuntimeError("字幕为空")
    total = duration(media)
    results = []
    w0 = 0.0
    while w0 < total - 2:
        w1 = min(total, w0 + WINDOW)
        sel = [e for e in entries if e["end"] > w0 + EDGE and e["start"] < w1 - EDGE]
        if not sel:
            w0 = w1
            continue
        wav = os.path.join(tmpdir, f"w_{int(w0)}.wav")
        subprocess.run([FFMPEG, "-y", "-loglevel", "error", "-ss", f"{w0:.3f}",
                        "-t", f"{w1 - w0:.3f}", "-i", media, "-ar", "16000",
                        "-ac", "1", wav], check=True, capture_output=True)
        reff = os.path.join(tmpdir, f"ref_{int(w0)}.txt")
        open(reff, "w", encoding="utf-8").write("\n".join(e["text"] for e in sel))
        outj = os.path.join(tmpdir, f"o_{int(w0)}.json")
        subprocess.run([TALK_VENV_PY, TALK_TS_SCRIPT, wav, reff, outj,
                        "--model-dir", ASR_MODEL_DIR],
                       capture_output=True, text=True)
        if not os.path.exists(outj):
            w0 = w1
            continue
        data = json.load(open(outj, encoding="utf-8"))
        for i, s in enumerate(data.get("sentences", [])):
            st, en = s["start"] + w0, s["end"] + w0
            if st <= w0 + EDGE or en >= w1 - EDGE:   # 窗口边缘不可比
                continue
            if i >= len(sel):
                break
            results.append({
                "text": sel[i]["text"],
                "srt_start": sel[i]["start"],
                "asr_start": st,
                "dev": sel[i]["start"] - st,
                "match": float(s.get("match", 0)),
            })
        w0 = w1

    devs = [abs(r["dev"]) for r in results]
    stats = {
        "entries": len(results),
        "median": statistics.median(devs) if devs else 0.0,
        "mean": statistics.mean(devs) if devs else 0.0,
        "max": max(devs) if devs else 0.0,
        "over_0.8": sum(1 for d in devs if d > 0.8),
        "low_match": sum(1 for r in results if r["match"] < 0.8),
    }
    results.sort(key=lambda r: -abs(r["dev"]))
    return stats, results


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("media", help="成片 mp4（或任意含配音的音视频）")
    ap.add_argument("srt", help="烧进成片的那份字幕")
    ap.add_argument("--tag", default="", help="报告前缀（如 开封）")
    ap.add_argument("--thresh", type=float, default=0.8, help="判定阈值秒数")
    ap.add_argument("--json", dest="json_out", default="", help="写出 JSON 报告")
    ap.add_argument("--top", type=int, default=6, help="打印最差几条")
    a = ap.parse_args()

    tag = f"[{a.tag}] " if a.tag else ""
    with tempfile.TemporaryDirectory(prefix="verify_sync_") as td:
        stats, results = verify(a.media, a.srt, td)

    print(f"{tag}字幕音画同步校验")
    print(f"  可比对 {stats['entries']} 条 ｜ |偏差| 中位 {stats['median']:.2f}s "
          f"均值 {stats['mean']:.2f}s 最大 {stats['max']:.2f}s")
    print(f"  超 {a.thresh}s 的条目: {sum(1 for r in results if abs(r['dev']) > a.thresh)}"
          f" ｜ ASR低置信 {stats['low_match']} 条")
    if results:
        print(f"  最差 {min(a.top, len(results))} 条：")
        for r in results[:a.top]:
            flag = "⚠️" if abs(r["dev"]) > a.thresh else "✅"
            print(f"    {flag} 偏差{r['dev']:+5.2f}s match={r['match']:.2f}  "
                  f"音频{r['asr_start']:6.1f}s 字幕{r['srt_start']:6.1f}s  {r['text'][:24]}")

    if a.json_out:
        json.dump({"media": a.media, "srt": a.srt, "stats": stats, "worst": results},
                  open(a.json_out, "w", encoding="utf-8"), ensure_ascii=False, indent=2)

    bad = sum(1 for r in results if abs(r["dev"]) > a.thresh)
    print("  → " + ("✅ 通过" if bad == 0 else f"❌ {bad} 条超阈"))
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
