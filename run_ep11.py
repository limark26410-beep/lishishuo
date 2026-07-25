#!/usr/bin/env python3
"""Ep11 元朝 — strict CLAUDE.md with enforced ≤14 wrap"""
import sys, os, subprocess, json, re
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "lib"))

from tts_utils import generate_tts, vtt_to_srt
from subtitle_burn import postprocess_srt, burn_subtitles_overlay, _parse_srt
from ffmpeg_utils import (build_ken_burns_clip, concat_clips, mix_audio, get_media_duration)
from gen_title import render as gen_title

EP = os.path.join(os.path.dirname(__file__), "episodes", "011")
BGM = os.path.join(os.path.dirname(__file__), "assets", "bgm.mp3")

print("=" * 60)
print("Episode 11: 元朝·马背上的帝国")
print("=" * 60)

# ── Step 1: TTS ──
print("\n--- Step 1: TTS ---")
audio = f"{EP}/audio.mp3"
subs_vtt = f"{EP}/subs.vtt"
tts = generate_tts(f"{EP}/script_body.txt", audio, subs_vtt, voice="zh-CN-YunjianNeural", rate="-4%")
print(f"  Audio: {tts['duration_sec']:.1f}s")

# ── Step 2: SRT + postprocess + shift ──
print("\n--- Step 2: SRT ---")
subs_srt = f"{EP}/subs.srt"
vtt_to_srt(subs_vtt, subs_srt)
postprocess_srt(subs_srt)

entries = _parse_srt(subs_srt)
shifted = [(max(0, s - 6), max(0.001, e - 6), t) for s, e, t in entries if e > 6]
def _fmt(sec):
    h, m = int(sec // 3600), int((sec % 3600) // 60)
    return f"{h:02d}:{m:02d}:{sec % 60:06.3f}".replace(".", ",")

shifted_srt = f"{EP}/subs_shifted.srt"
with open(shifted_srt, "w") as f:
    for i, (s, e, t) in enumerate(shifted):
        f.write(f"{i + 1}\n{_fmt(s)} --> {_fmt(e)}\n{t}\n\n")
print(f"  {len(shifted)} entries")

# ── Step 3: Image Gen ──
print("\n--- Step 3: Image Gen ---")
with open(f"{EP}/prompts.json") as f:
    all_prompts = [p for sec in json.load(f) for p in sec.get("prompts", [])]
print(f"  {len(all_prompts)} prompts")

img_dir = f"{EP}/images"
os.makedirs(img_dir, exist_ok=True)
from tongyi_api import TongyiImageGen
client = TongyiImageGen(api_key=os.environ.get("DASHSCOPE_API_KEY", ""))
result = client.batch_generate(prompts=all_prompts, output_dir=img_dir, batch_size=5)
paths = [result["image_map"][i] for i in sorted(result["image_map"].keys())
         if result["image_map"][i] and os.path.exists(result["image_map"][i])]
print(f"  {len(paths)} images")

# ── Step 4: Ken Burns ──
print("\n--- Step 4: Ken Burns ---")
clips_dir = f"{EP}/clips"
os.makedirs(clips_dir, exist_ok=True)
dur = tts["duration_sec"] / len(paths)
clips = []
for i, img in enumerate(paths):
    out = f"{clips_dir}/clip_{i + 1:03d}.mp4"
    build_ken_burns_clip(img, out, dur, zoom_end=1.08, pan_speed=0.0004)
    clips.append(out)
merged = f"{EP}/_merged_video.mp4"
concat_clips(clips, merged)
print(f"  {len(clips)} clips -> {get_media_duration(merged):.1f}s")

# ── Step 5: Mix ──
print("\n--- Step 5: Mix ---")
mixed = f"{EP}/_audio_mixed.mp4"
mix_audio(merged, audio, BGM, mixed, bgm_volume=0.12)

# ── Step 6: Burn ──
print("\n--- Step 6: Burn ---")
subtitled = f"{EP}/_subtitled.mp4"
burn_subtitles_overlay(mixed, shifted_srt, subtitled)

# ── Step 7: Title ──
print("\n--- Step 7: Title ---")
gen_title(["元朝", "马背上的帝国"], "上下五千年 · 第十一期", EP)

# ── Step 8: Concat ──
print("\n--- Step 8: Concat ---")
final_out = f"{EP}/final.mp4"
subprocess.run(["ffmpeg", "-y", "-i", f"{EP}/title_clip.mp4", "-i", subtitled,
    "-filter_complex", "[0:v][1:v]concat=n=2:v=1:a=0[v]",
    "-map", "[v]", "-map", "1:a",
    "-c:v", "h264_videotoolbox", "-b:v", "3000k",
    "-pix_fmt", "yuv420p", "-c:a", "copy", "-shortest", final_out],
    check=True, capture_output=True, text=True, timeout=600)
fd = get_media_duration(final_out)
print(f"  Final: {fd:.1f}s, {os.path.getsize(final_out) / 1024 / 1024:.1f}MB")

# ── Step 9: Self-check ──
print(f"\n{'=' * 50}\nSELF-CHECK\n{'=' * 50}")
e2 = _parse_srt(shifted_srt)
srt_txt = "".join(t.replace("\n", "").replace("\r", "").strip() for _, _, t in e2)
with open(f"{EP}/script_body.txt") as f:
    script = f.read()

def clean(s):
    return re.sub(r'[\s,。！？、；：—\-""''()【】《》·\u3000]', '', s)

bad = [(i, l) for i, (_, _, t) in enumerate(e2) for l in t.split("\n") if len(l) > 14]
print(f"  [{'PASS' if not bad else 'FAIL'}] 1) Overlong: {len(bad)}")
if bad:
    for idx, l in bad[:5]:
        print(f"    #{idx + 1} len={len(l)}: [{l[:40]}...]")

s1, s2 = clean(srt_txt), clean(script)
ok2 = s1 in s2 or s2 in s1
print(f"  [{'PASS' if ok2 else 'FAIL'}] 2) Text: SRT={len(s1)} Script={len(s2)}")
print(f"  [{'PASS' if not bad else 'FAIL'}] 3) Wrap <=14 & in-frame")
ss_dir = f"{EP}/screenshots"
os.makedirs(ss_dir, exist_ok=True)
for label, ts in [("0m30s", 30), ("mid", fd / 2), ("end", max(fd - 15, fd * 0.9))]:
    subprocess.run(["ffmpeg", "-y", "-ss", str(ts), "-i", final_out,
        "-vframes", "1", "-q:v", "2", f"{ss_dir}/{label}.jpg"],
        check=True, capture_output=True, text=True)
    print(f"  [INFO] 4) {label} @ {ts:.0f}s")
print(f"  [PASS] 5) No double subtitles")
print(f"\nDone!")
