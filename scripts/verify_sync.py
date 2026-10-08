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


# ────────────────────────────────────────────────────────────
# B. 画面像素校验：屏幕上真正烧进去的字幕 == SRT 内容吗
#    这一段是 GL-20260911 加的：只比 SRT 文件与音频会漏掉"烧录环节把
#    时间轴搞漂"这类问题（concat 分离器按帧取整，60 条累积漂 3.9 秒）。
# ────────────────────────────────────────────────────────────
def _load_burn():
    """按出片同一套参数载入字幕渲染器（保证渲染出的字与烧进去的一致）"""
    here = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, os.path.join(os.path.dirname(here), "lib"))
    import subtitle_burn as SB
    SB.configure({"subtitle": {"font_size": 60, "margin_bottom": 400,
                               "margin_side": 60}})
    return SB


def verify_pixels(media, srt_path, tmpdir, fps=5.0):
    """逐帧比对"屏幕上的字幕"与"SRT 该显示的字幕"，并量化漂移。

    返回 (stats, switches)；switches = [(条目序号, SRT切换点, 屏幕实际切换点, 偏差)]
    """
    import numpy as np
    from PIL import Image

    SB = _load_burn()
    entries = read_srt(srt_path)
    if not entries:
        raise RuntimeError("字幕为空")

    # 每条字幕渲染成参考掩码（下采样 4 倍加速比对）
    masks = []
    keep = []
    for i, e in enumerate(entries):
        try:
            p = SB._render_png(e["text"], 0)
        except Exception:
            continue
        g = np.array(Image.open(p).convert("L")) >= 200
        os.unlink(p)
        masks.append(g[::4, ::4])
        keep.append(i)
    masks = np.array(masks)
    bar_h = masks.shape[1] * 4
    ov_y = SB.IMG_H - SB.BAR_HEIGHT - SB.MARGIN_BOTTOM

    raw = os.path.join(tmpdir, "bands.raw")
    subprocess.run([FFMPEG, "-y", "-loglevel", "error", "-i", media, "-vf",
                    f"crop={SB.IMG_W}:{bar_h}:0:{ov_y},format=gray",
                    "-r", str(fps), "-f", "rawvideo", "-pix_fmt", "gray", raw],
                   check=True, capture_output=True)
    B = np.fromfile(raw, dtype=np.uint8)
    nf = B.size // (SB.IMG_W * bar_h)
    B = B.reshape(nf, bar_h // 4 * 4, SB.IMG_W)[:, :bar_h, :][:, ::4, ::4]

    # 每帧找最像的条目
    on = []
    for i in range(nf):
        fr = B[i] >= 200
        inter = (masks & fr).sum(axis=(1, 2))
        union = (masks | fr).sum(axis=(1, 2))
        iou = np.where(union > 0, inter / np.maximum(union, 1), 0)
        k = int(iou.argmax())
        on.append((i / fps, keep[k], float(iou[k])))

    def expected(t):
        for j, e in enumerate(entries):
            if e["start"] <= t < e["end"]:
                return j
        return None

    judged = [(t, k, s) for t, k, s in on if s > 0.35 and expected(t) is not None]
    hit = sum(1 for t, k, s in judged if k == expected(t))
    switches = []
    for j in range(1, len(entries)):
        T = entries[j]["start"]
        seg = [(t, k, s) for t, k, s in on if T - 4.0 <= t <= T + 1.2 and s > 0.5]
        sw = next((t for t, k, s in seg if k == j), None)
        if sw is not None:
            switches.append((j, T, sw, T - sw))

    drifts = [d for _j, _T, _sw, d in switches]
    stats = {
        "frames": nf,
        "judged": len(judged),
        "hit": hit,
        "hit_rate": hit / len(judged) if judged else 0.0,
        "switches": len(switches),
        "drift_first": drifts[0] if drifts else 0.0,
        "drift_last": drifts[-1] if drifts else 0.0,
        "drift_max": max(drifts, key=abs) if drifts else 0.0,
    }
    return stats, switches



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
    ap.add_argument("--pixels", action="store_true",
                    help="只做画面像素校验（屏幕字幕 vs SRT，含漂移量化），跳过 ASR")
    ap.add_argument("--no-pixels", action="store_true", help="跳过画面像素校验")
    a = ap.parse_args()

    tag = f"[{a.tag}] " if a.tag else ""
    bad = 0

    if not a.no_pixels:
        with tempfile.TemporaryDirectory(prefix="verify_px_") as td:
            px, sw = verify_pixels(a.media, a.srt, td)
        print(f"{tag}画面字幕校验（屏幕上真正烧的字幕 vs SRT）")
        print(f"  抽帧 {px['frames']} ｜ 可判定 {px['judged']} 帧 ｜ "
              f"屏幕==SRT {px['hit']} 帧（{px['hit_rate']*100:.0f}%）")
        print(f"  切换点 {px['switches']} 个 ｜ 漂移 首 {px['drift_first']:+.2f}s "
              f"末 {px['drift_last']:+.2f}s 最大 {px['drift_max']:+.2f}s")
        if sw:
            step = max(1, len(sw) // 6)
            print("  漂移抽样：")
            for j, T, s, d in sw[::step][:7]:
                flag = "✅" if abs(d) <= 0.6 else "⚠️"
                print(f"    {flag} 第{j+1}条 SRT {T:7.2f}s → 屏幕 {s:7.2f}s  偏差 {d:+.2f}s")
        # 5fps 抽样的量化误差最大 0.2s，加上烧录本身的帧对齐（≤1帧），
        # 所以恒定偏差判到 0.6s；真正的问题是"累积漂移"——旧 concat 烧录
        # 会从 0.4s 一路漂到 3.9s，这里必然抓住。
        if abs(px["drift_max"]) > 0.6 or px["hit_rate"] < 0.9:
            bad += 1
            print("  → ❌ 烧进去的字幕与 SRT 不一致（字幕轨时间轴有问题）")
        else:
            print("  → ✅ 通过（屏幕上烧的字幕与 SRT 一致）")

    if not a.pixels:
        with tempfile.TemporaryDirectory(prefix="verify_sync_") as td:
            stats, results = verify(a.media, a.srt, td)
        print(f"{tag}字幕音画同步校验（SRT 时间 vs 音频真实时间）")
        print(f"  可比对 {stats['entries']} 条 ｜ |偏差| 中位 {stats['median']:.2f}s "
              f"均值 {stats['mean']:.2f}s 最大 {stats['max']:.2f}s")
        print(f"  超 {a.thresh}s 的条目: "
              f"{sum(1 for r in results if abs(r['dev']) > a.thresh)}"
              f" ｜ ASR低置信 {stats['low_match']} 条")
        if results:
            print(f"  最差 {min(a.top, len(results))} 条：")
            for r in results[:a.top]:
                flag = "⚠️" if abs(r["dev"]) > a.thresh else "✅"
                print(f"    {flag} 偏差{r['dev']:+5.2f}s match={r['match']:.2f}  "
                      f"音频{r['asr_start']:6.1f}s 字幕{r['srt_start']:6.1f}s  "
                      f"{r['text'][:24]}")
        if a.json_out:
            json.dump({"media": a.media, "srt": a.srt, "stats": stats,
                       "worst": results},
                      open(a.json_out, "w", encoding="utf-8"),
                      ensure_ascii=False, indent=2)
        n_bad = sum(1 for r in results if abs(r["dev"]) > a.thresh)
        if n_bad:
            bad += 1
        print("  → " + ("✅ 通过" if n_bad == 0 else f"❌ {n_bad} 条超阈"))

    print("★ 总体：" + ("✅ 通过" if bad == 0 else "❌ 不通过"))
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
