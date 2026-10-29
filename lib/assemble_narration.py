# -*- coding: utf-8 -*-
"""旁白体成片拼装（GL-20261013）

与 assemble_ai_video.py 的区别
------------------------------
那条是「对话体」：每段视频等长、配音逐句贴上。
这条是「旁白体」：**旁白时长决定画面时长**，画面只做叙事配图。

    旁白音频（含字级时间戳）
        ↓ 决定
    每个镜头的精确时长  →  视频片段按需变速/裁切  →  拼接
        ↓
    配乐按段落情绪铺（悬念/平叙/高潮/收束）
        ↓
    字幕用旁白真实时间轴烧录

用法：./venv/bin/python lib/assemble_narration.py <项目目录> [输出路径]
依赖：项目目录下需有
  narration.json    旁白稿（segments: id/emotion/text/shots）
  旁白/segN.mp3     每段旁白音频
  旁白/时间轴.json  由生成旁白时写出（含句级/字级时间戳）
  ai_video/seg_XX.mp4  各镜视频素材
  首帧/SXX.png      静帧（片段不够长时可做 Ken Burns）
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "lib"))

W, H = 1920, 1080
FPS = 25
BGM_VOL = 0.26          # 配乐压到旁白之下（0.16 太小声，用户反馈听不见）
TAIL = 3.0              # 结尾留白（让金句落地）


def run(cmd, **kw):
    r = subprocess.run(cmd, capture_output=True, text=True, **kw)
    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg 失败: {r.stderr[-800:]}")
    return r


def dur_of(path: str) -> float:
    r = subprocess.run(["ffmpeg", "-i", path], capture_output=True, text=True)
    m = re.search(r"Duration: (\d+):(\d+):([\d.]+)", r.stderr)
    if not m:
        raise RuntimeError(f"探测时长失败: {path}")
    return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))


def norm_seg(src: str, out: str, target: float, lead: float = 0.8):
    """把 src 变成 target 秒。

    GL-20261013：素材改成 10 秒生成后，大多数情况是「裁」而不是「变速」——
    裁掉头尾取中段（画面最稳），远好过把 10 秒压成 4 秒（1.5 倍快放很假）。
    只有素材比目标短时才慢放，且有上限。
    """
    d = dur_of(src)
    scale = (f"scale={W}:{H}:force_original_aspect_ratio=increase,"
             f"crop={W}:{H},setsar=1,fps={FPS}")
    if d >= target + 0.15:
        # 裁中段：跳过头 lead 秒（开头常有生成瑕疵），取 target 秒
        start = min(lead, max(0.0, d - target - 0.2))
        run(["ffmpeg", "-y", "-ss", f"{start:.3f}", "-i", src,
             "-vf", scale, "-t", f"{target:.3f}", "-an",
             "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "20",
             "-preset", "medium", out])
        return "trim"
    # 素材偏短 → 慢放（上限 1.8 倍，再长就靠 Ken Burns 补）
    factor = max(1.0, min(target / d, 1.80))
    vf = f"{scale},setpts={factor:.6f}*PTS"
    run(["ffmpeg", "-y", "-i", src, "-vf", vf, "-t", f"{target:.3f}", "-an",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "20",
         "-preset", "medium", out])
    return "slow"


def kenburns(still: str, out: str, target: float, mode: str = "in"):
    """静帧电影感推拉。mode: in 推近 / out 拉远 / left 左移 / right 右移。

    为什么用它替代图生视频：CogVideoX 等 i2v 模型从静帧出发极易生成
    「原地跑/反复搓钱/原地踏步」这类循环动作（用户实测反馈）。
    旁白体本就是纪录片语法，静帧推拉既稳又没有 AI 动作瑕疵。
    """
    frames = int(target * FPS) + 1
    Z0, Z1 = 1.0, 1.16
    if mode == "in":
        z = f"'{Z0}+({Z1}-{Z0})*on/{frames}'"
        x, y = "'iw/2-(iw/zoom/2)'", "'ih/2-(ih/zoom/2)'"
    elif mode == "out":
        z = f"'{Z1}-({Z1}-{Z0})*on/{frames}'"
        x, y = "'iw/2-(iw/zoom/2)'", "'ih/2-(ih/zoom/2)'"
    elif mode == "left":
        z = f"'{Z1}'"
        x, y = f"'(iw-iw/zoom)*(1-on/{frames})'", "'ih/2-(ih/zoom/2)'"
    elif mode == "right":
        z = f"'{Z1}'"
        x, y = f"'(iw-iw/zoom)*on/{frames}'", "'ih/2-(ih/zoom/2)'"
    else:  # up / 默认
        z = f"'{Z1}'"
        x, y = "'iw/2-(iw/zoom/2)'", f"'(ih-ih/zoom)*on/{frames}'"
    vf = (f"scale={int(W*1.25)}:{int(H*1.25)},"
          f"zoompan=z={z}:x={x}:y={y}:"
          f"d={frames}:s={W}x{H}:fps={FPS},setsar=1")
    run(["ffmpeg", "-y", "-loop", "1", "-i", still, "-vf", vf,
         "-t", f"{target:.3f}", "-an",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "20",
         "-preset", "medium", out])


def _ts(sec: float) -> str:
    h, m = int(sec // 3600), int(sec % 3600 // 60)
    s = sec % 60
    return f"{h:02d}:{m:02d}:{s:06.3f}".replace(".", ",")


def build_srt(tl: dict, out: str, max_chars: int = 20):
    """用旁白真实字级时间戳生成字幕（不猜）。"""
    lines = []
    n = 0
    for seg in tl["segments"]:
        base = seg["start"]
        for sent in seg.get("sentences", []):
            words = sent.get("words") or []
            txt = (sent.get("t") or "").strip()
            if not txt:
                continue
            if not words:
                # 豆包会在 sentences 里额外返回一条「整段合并」的冗余条目（无字级时间戳）。
                # 若把它当独立句，会生成 0.4 秒的碎片字幕叠在段首 —— 直接跳过。
                continue
            # 豆包返回的 startTime/endTime 单位是【秒】，不是毫秒
            t0 = min(float(w.get("startTime", 0)) for w in words)
            t1 = max(float(w.get("endTime", 0)) for w in words)
            # 断行：只在超过上限时切；末尾若只剩标点/极短碎片，并回上一行
            if len(txt) <= max_chars:
                chunks = [txt]
            else:
                chunks = [txt[i:i + max_chars]
                          for i in range(0, len(txt), max_chars)]
                # 尾块太短（如只剩「」？这种）→ 并回上一块
                if len(chunks) > 1 and len(chunks[-1].strip("」』）】”。？！… ")) < 3:
                    chunks[-2] += chunks[-1]
                    chunks.pop()
            span = max(0.4, (t1 - t0)) / len(chunks)
            for k, c in enumerate(chunks):
                n += 1
                s = base + t0 + k * span
                e = base + t0 + (k + 1) * span
                lines += [str(n), f"{_ts(s)} --> {_ts(e)}", c, ""]
    Path(out).write_text("\n".join(lines), encoding="utf-8")
    print(f"  ✓ 字幕 {n} 条（旁白真实时间轴）")
    return n


def main():
    proj = Path(sys.argv[1]).expanduser().resolve() if len(sys.argv) > 1 \
        else Path.cwd()
    out_path = sys.argv[2] if len(sys.argv) > 2 else str(proj / "final.mp4")

    tl = json.loads((proj / "旁白" / "时间轴.json").read_text(encoding="utf-8"))
    # 镜头分配以 narration.json 为准（按【台词内容】对齐，不是时间均分）；
    # 段时长以时间轴（配音真实时长）为准。
    ncfg_path = proj / "narration.json"
    if ncfg_path.exists():
        _n = json.loads(ncfg_path.read_text(encoding="utf-8"))
        _byid = {seg["id"]: seg.get("shots") for seg in _n.get("segments", [])}
        for seg in tl["segments"]:
            if _byid.get(seg["id"]):
                seg["shots"] = _byid[seg["id"]]
    nar_total = tl["total"]
    work = proj / "_nar_build"
    work.mkdir(exist_ok=True)

    # ── 1. 计算时间轴：段时长 → 每镜时长 ──
    plan = []           # (镜头号, 时长, 段号)
    for seg in tl["segments"]:
        shots = seg["shots"]
        per = seg["dur"] / max(1, len(shots))
        for k, sh in enumerate(shots):
            if isinstance(sh, dict):          # 按台词内容对齐：{tag,from,to,mode,kb}
                idx = int(re.sub(r"\D", "", str(sh.get("tag", ""))) or 0)
                d = max(0.6, float(sh["to"]) - float(sh["from"]))
                mode = sh.get("mode", "still")   # still=静帧推拉（默认，稳）
                kb = sh.get("kb", "in")
            else:                             # 兼容旧格式：均分
                idx = int(re.sub(r"\D", "", str(sh)) or 0)
                d = per if k < len(shots) - 1 else seg["dur"] - per * (len(shots) - 1)
                mode, kb = "still", "in"
            plan.append((idx, d, seg["id"], mode, kb))
    # 结尾留白：并入最后一个镜头，做【一段连续推拉】+ 末尾淡出黑场。
    # （早前做法是给同一张图做两次推拉，观众看着就是"卡住了"）
    if plan and plan[-1][3] == "still":
        _i, _d, _s, _m, _k = plan[-1]
        plan[-1] = (_i, _d + TAIL, _s, _m, _k)
    else:
        plan.append((13, TAIL, 5, "still", "in"))
    total_video = sum(p[1] for p in plan)
    print(f"▶ 旁白 {nar_total:.2f}s | 画面 {total_video:.2f}s | {len(plan)} 段")

    # ── 2. 生成每段画面 ──
    print("① 按旁白时长重排画面…")
    segs = []
    for i, (idx, d, sid, mode, kb) in enumerate(plan, 1):
        vid = proj / "ai_video" / f"seg_{idx:02d}.mp4"
        still = proj / "首帧" / f"S{idx:02d}.png"
        out = work / f"v{i:02d}.mp4"
        if mode == "still" and still.exists():
            kenburns(str(still), str(out), d, kb)
            print(f"  [{i:02d}] S{idx:02d} 静帧推拉[{kb}] {d:.2f}s")
        elif vid.exists():
            how = norm_seg(str(vid), str(out), d)
            src_d = dur_of(str(vid))
            tag = "裁中段" if how == "trim" else f"慢放×{d/src_d:.2f}"
            print(f"  [{i:02d}] S{idx:02d} 视频{tag}  ({src_d:.1f}s→{d:.2f}s)")
        elif still.exists():
            kenburns(str(still), str(out), d, kb)
            print(f"  [{i:02d}] S{idx:02d} 缺视频→静帧[{kb}] {d:.2f}s")
        else:
            print(f"  ✗ S{idx:02d} 既无视频也无首帧，跳过")
            continue
        segs.append(out)

    # ── 3. 拼接 ──
    lst = work / "_concat.txt"
    lst.write_text("".join(f"file '{s.resolve()}'\n" for s in segs), encoding="utf-8")
    video = work / "_video.mp4"
    run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(lst),
         "-c", "copy", str(video)])
    print(f"  ✓ 画面拼接完成 {dur_of(str(video)):.2f}s")

    # ── 4. 旁白轨 ──
    nl = work / "_nar.txt"
    nl.write_text("".join(f"file '{Path(s['file']).resolve()}'\n"
                          for s in tl["segments"]), encoding="utf-8")
    nar = work / "_nar.mp3"
    run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(nl),
         "-c:a", "libmp3lame", "-q:a", "3", str(nar)])
    print(f"  ✓ 旁白轨 {dur_of(str(nar)):.2f}s")

    # ── 5. 配乐（按段落情绪）──
    bgm_root = Path(os.path.expanduser("~/Desktop/历史说素材/bgm"))
    bgm_parts = []
    for seg in tl["segments"]:
        d = seg["dur"]
        cands = sorted((bgm_root / seg["emotion"]).glob("*.mp3")) \
            if (bgm_root / seg["emotion"]).exists() else []
        src = cands[0] if cands else (ROOT / "assets" / "bgm.mp3")
        o = work / f"b{seg['id']}.mp3"
        run(["ffmpeg", "-y", "-stream_loop", "-1", "-i", str(src),
             "-t", f"{d:.3f}", "-af", f"volume={BGM_VOL},afade=t=in:d=1,"
             f"afade=t=out:st={max(0,d-1.2):.2f}:d=1.2",
             "-c:a", "libmp3lame", "-q:a", "4", str(o)])
        bgm_parts.append(o)
        print(f"  ✓ 段{seg['id']} 配乐 {seg['emotion']} → {d:.1f}s")
    # 结尾留白配乐
    tail_bgm = work / "btail.mp3"
    run(["ffmpeg", "-y", "-stream_loop", "-1",
         "-i", str(sorted((bgm_root / "收束").glob("*.mp3"))[0]
                  if (bgm_root / "收束").exists() else ROOT / "assets" / "bgm.mp3"),
         "-t", f"{TAIL:.3f}", "-af", f"volume={BGM_VOL},afade=t=out:st=0.5:d=2.5",
         "-c:a", "libmp3lame", "-q:a", "4", str(tail_bgm)])
    bgm_parts.append(tail_bgm)
    bl = work / "_bgm.txt"
    bl.write_text("".join(f"file '{b.resolve()}'\n" for b in bgm_parts), encoding="utf-8")
    bgm = work / "_bgm.mp3"
    run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(bl),
         "-c:a", "libmp3lame", "-q:a", "3", str(bgm)])

    # ── 6. 混音 ──
    vsec = dur_of(str(video))
    mixed = work / "_mixed.mp4"
    run(["ffmpeg", "-y", "-i", str(video), "-i", str(nar), "-i", str(bgm),
         "-filter_complex",
         f"[1:a]aresample=48000,apad,atrim=0:{vsec},volume=1.0[n];"
         f"[2:a]aresample=48000,apad,atrim=0:{vsec}[b];"
         f"[n][b]amix=inputs=2:duration=first:weights=1 1,"
         f"alimiter=limit=0.95,atrim=0:{vsec}[a]",
         "-map", "0:v", "-map", "[a]", "-c:v", "copy",
         "-c:a", "aac", "-b:a", "192k", str(mixed)])
    print(f"  ✓ 混音完成（旁白 + 配乐）")

    # ── 7. 字幕 ──
    srt = proj / "旁白字幕.srt"
    build_srt(tl, str(srt))
    ass = work / "_sub.ass"
    _srt_to_ass(str(srt), str(ass), fontsize=46, margin_v=80)

    final = work / "_final.mp4"
    vsec2 = dur_of(str(mixed))
    fade_st = max(0.0, vsec2 - 1.6)
    run(["ffmpeg", "-y", "-i", str(mixed),
         "-vf", f"ass='{ass}',fade=t=out:st={fade_st:.2f}:d=1.6",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "20",
         "-preset", "medium", "-c:a", "copy", str(final)])
    import shutil
    shutil.move(str(final), out_path)
    print(f"\n🎬 成片: {out_path}  ({dur_of(out_path):.2f}s)")


def _srt_to_ass(srt_path: str, ass_path: str, fontsize: int = 46, margin_v: int = 80):
    def _t(s: str) -> str:
        h, m, rest = s.split(":")
        sec, ms = rest.split(",")
        return f"{int(h)}:{int(m)}:{int(sec)}.{int(ms)//10:02d}"
    blocks = Path(srt_path).read_text(encoding="utf-8").strip().split("\n\n")
    header = ("[Script Info]\nScriptType: v4.00+\n"
              f"PlayResX: {W}\nPlayResY: {H}\nWrapStyle: 0\n"
              "ScaledBorderAndShadow: yes\n\n[V4+ Styles]\n"
              "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
              "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
              "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
              "Alignment, MarginL, MarginR, MarginV, Encoding\n"
              f"Style: Default,PingFang SC,{fontsize},&H00FFFFFF,&H000000FF,"
              "&H70000000,&H00000000,0,0,0,0,100,100,0,0,1,2.5,0,2,80,80,"
              f"{margin_v},1\n\n[Events]\n"
              "Format: Layer, Start, End, Style, Name, MarginL, MarginR, "
              "MarginV, Effect, Text\n")
    ev = []
    for b in blocks:
        ls = b.strip().split("\n")
        if len(ls) < 3 or "-->" not in ls[1]:
            continue
        t0, t1 = (x.strip() for x in ls[1].split("-->"))
        txt = "\\N".join(ls[2:]).replace("{", "｛").replace("}", "｝")
        ev.append(f"Dialogue: 0,{_t(t0)},{_t(t1)},Default,,0,0,0,,{txt}")
    Path(ass_path).write_text(header + "\n".join(ev) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
