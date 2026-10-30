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
import shutil
import statistics
import subprocess
import sys
import tempfile

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _d in (r"C:\ffmpeg\bin", r"C:\MV_WT\tools\ffmpeg"):
    if not shutil.which("ffmpeg") and os.path.exists(os.path.join(_d, "ffmpeg.exe")):
        os.environ["PATH"] = _d + os.pathsep + os.environ.get("PATH", "")
TALK_VENV_PY = sys.executable
TALK_TS_SCRIPT = os.path.join(REPO_ROOT, "scripts", "timestamps_cpu.py")
ASR_MODEL_DIR = os.path.expanduser(
    "~/.cache/koubo/sherpa-onnx-fire-red-asr2-ctc-zh_en-int8-2026-02-25")
FFMPEG = shutil.which("ffmpeg") or "ffmpeg"
WINDOW = 60.0          # 每个校验窗口秒数
EDGE = 0.6             # 窗口边缘丢弃带（音频被截断的条目不计）


def _t(s):
    h, m, r = s.split(":")
    return int(h) * 3600 + int(m) * 60 + float(r.replace(",", "."))


def read_srt(path):
    """读字幕。text = 单行（给 ASR 当参考行，不能带换行）；
    raw = 保留原始换行（给掩码渲染用——烧录是按原样换行渲染的，
    用单行去渲染会得到不同字形，造成假性不匹配）。"""
    out = []
    for blk in open(path, encoding="utf-8").read().strip().split("\n\n"):
        L = blk.split("\n")
        if len(L) >= 3 and "-->" in L[1]:
            a, b = L[1].split("-->")
            lines = [x for x in L[2:]]
            txt = "".join(lines).strip()
            if txt:
                out.append({"start": _t(a), "end": _t(b),
                            "text": txt, "raw": "\n".join(lines)})
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


def verify_pixels(media, srt_path, tmpdir, fps=25.0):
    """逐帧比对"屏幕上的字幕"与"SRT 该显示的字幕"，并量化漂移。

    25fps 逐帧 + 缩图：漂移数字直接就是真实帧级偏差（正确烧录应 ≈0.00s）。
    返回 (stats, switches)；switches = [(条目序号, SRT切换点, 屏幕实际切换点, 偏差)]
    """
    import numpy as np
    from PIL import Image

    SB = _load_burn()
    entries = read_srt(srt_path)
    if not entries:
        raise RuntimeError("字幕为空")

    # 每条字幕渲染成参考掩码（缩到 1/4 尺寸，比对快且抗压缩噪点）
    masks = []
    keep = []
    for i, e in enumerate(entries):
        try:
            p = SB._render_png(e.get("raw") or e["text"], 0)
        except Exception:
            continue
        g = np.array(Image.open(p).convert("L")) >= 200
        os.unlink(p)
        masks.append(g[::4, ::4])
        keep.append(i)
    masks = np.array(masks)
    mh, mw = masks.shape[1], masks.shape[2]
    ov_y = SB.IMG_H - SB.BAR_HEIGHT - SB.MARGIN_BOTTOM

    # 原始分辨率取帧 → 归一化相关匹配
    # 不用"白像素阈值+IoU"：背景画面很亮时（雪/天空/白墙）背景像素也被算进掩码，
    # 会污染比对、把切换点检迟到 0.4~1.2 秒（实测邯郸第4条、南京第18条误报）。
    # 归一化相关对背景亮度/对比度整体偏移不变，只在"文字笔画形状"上给分。
    fs = SB.IMG_W * SB.BAR_HEIGHT
    proc = subprocess.Popen(
        [FFMPEG, "-v", "error", "-i", media, "-vf",
         f"crop={SB.IMG_W}:{SB.BAR_HEIGHT}:0:{ov_y},format=gray",
         "-r", str(fps), "-f", "rawvideo", "-pix_fmt", "gray", "-"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    N = masks.shape[1] * masks.shape[2]
    M2 = masks.reshape(len(masks), -1).astype(np.float32)
    msum = M2.sum(axis=1)
    msum2 = (M2 * M2).sum(axis=1)
    mvar = np.maximum(msum2 - msum ** 2 / N, 1e-6)
    on = []
    CH = 250
    n = 0
    while True:
        buf = proc.stdout.read(CH * fs)
        if not buf:
            break
        cnt = len(buf) // fs
        if cnt == 0:
            break
        B = np.frombuffer(buf[:cnt * fs], dtype=np.uint8).reshape(
            cnt, SB.BAR_HEIGHT, SB.IMG_W)
        F = B[:, ::4, ::4].reshape(cnt, -1).astype(np.float32)
        fsum = F.sum(axis=1)
        fvar = np.maximum((F * F).sum(axis=1) - fsum ** 2 / N, 1e-6)
        dot = F @ M2.T
        cov = dot - np.outer(fsum, msum) / N
        corr = cov / np.sqrt(np.outer(fvar, mvar))
        order = np.argsort(-corr, axis=1)
        best = order[:, 0]
        bestv = corr[np.arange(cnt), best]
        second = corr[np.arange(cnt), order[:, 1]]
        # 判定只看【排序与领先优势】，不看绝对分数：
        # 短句 + 亮背景时相关值可能只有 0.29，但仍是最高分且领先次席 0.24，
        # 用绝对阈值会把这帧判成"没匹配上"，导致切换点被检迟 1 秒多（实测南京第18条）。
        for i in range(cnt):
            on.append(((n + i) / fps, keep[int(best[i])], float(bestv[i]),
                       float(bestv[i] - second[i])))
        n += cnt
    proc.stdout.close()
    proc.wait()
    nf = n

    def expected(t):
        for j, e in enumerate(entries):
            if e["start"] <= t < e["end"]:
                return j
        return None

    judged = [(t, k, s, m) for t, k, s, m in on
              if m > 0.10 and expected(t) is not None]
    hit = sum(1 for t, k, s, m in judged if k == expected(t))
    switches = []
    for j in range(1, len(entries)):
        T = entries[j]["start"]
        # 切到第 j 条 = 连续 3 帧（0.12s）argmax 都是 j
        # 同样要求领先优势够大，否则相邻两条文本相近时会误判成"提前切换"
        seg = [(t, k) for t, k, _s, m in on
               if T - 4.5 <= t <= T + 1.5 and m > 0.10]
        sw = None
        for i in range(len(seg) - 2):
            if seg[i][1] == j and seg[i + 1][1] == j and seg[i + 2][1] == j:
                sw = seg[i][0]
                break
        if sw is not None:
            switches.append((j, T, sw, T - sw))

    drifts = [d for _j, _T, _sw, d in switches]
    # 用【中位数】+【前后半段趋势】判定，不看单个最大值：
    # 个别条目的检测会被"亮背景画面"干扰（白阈值把背景算进掩码）而延后约0.8s，
    # 那是测量噪声（25fps 逐帧复核实为 0.000s）；真正的病是漂移随片长持续增长。
    _mid = len(drifts) // 2
    stats = {
        "frames": nf,
        "judged": len(judged),
        "hit": hit,
        "hit_rate": hit / len(judged) if judged else 0.0,
        "switches": len(switches),
        "drift_median": statistics.median(drifts) if drifts else 0.0,
        "drift_first_half": statistics.median(drifts[:_mid]) if _mid else 0.0,
        "drift_second_half": statistics.median(drifts[_mid:]) if drifts[_mid:] else 0.0,
        "drift_max": max(drifts, key=abs) if drifts else 0.0,
    }
    stats["drift_growth"] = stats["drift_second_half"] - stats["drift_first_half"]
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
        print(f"  切换点 {px['switches']} 个 ｜ 漂移中位 {px['drift_median']:+.2f}s "
              f"（前半 {px['drift_first_half']:+.2f}s → 后半 {px['drift_second_half']:+.2f}s，"
              f"增长 {px['drift_growth']:+.2f}s）｜ 最大 {px['drift_max']:+.2f}s")
        if sw:
            step = max(1, len(sw) // 6)
            print("  漂移抽样：")
            for j, T, s, d in sw[::step][:7]:
                flag = "✅" if abs(d) <= 0.6 else "⚠️"
                print(f"    {flag} 第{j+1}条 SRT {T:7.2f}s → 屏幕 {s:7.2f}s  偏差 {d:+.2f}s")
        # 判定：恒定偏差（5fps抽样量化+帧对齐）容忍到 0.6s；
        # 但"漂移随片长增长">0.8s 或命中率低 → 字幕轨时间轴有问题。
        # 旧 concat 烧录：命中率 20%、漂移从 +0.4s 长到 +3.9s，必然抓住。
        if (abs(px["drift_median"]) > 0.6 or px["drift_growth"] > 0.8
                or px["hit_rate"] < 0.85):
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
