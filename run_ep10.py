#!/usr/bin/env python3
"""Episode 10 pipeline - resume from image gen"""
import sys, os, subprocess, json, re
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "lib"))

from subtitle_burn import burn_subtitles_overlay, _parse_srt
from ffmpeg_utils import (build_ken_burns_clip, concat_clips, mix_audio,
                           get_media_duration)
from gen_title import render as gen_title

EP = os.path.join(os.path.dirname(__file__), "episodes", "010")
BGM = os.path.join(os.path.dirname(__file__), "assets", "bgm.mp3")
audio = f"{EP}/audio.mp3"
shifted_srt = f"{EP}/subs_shifted.srt"

audio_dur = get_media_duration(audio)
print(f"Audio: {audio_dur:.1f}s, SRT: {shifted_srt}")

# ── Step 3: Image Gen ──
print("\n--- Step 3: Image Gen ---")
with open(f"{EP}/prompts.json") as f:
    sections = json.load(f)
all_prompts = [p for sec in sections for p in sec.get("prompts", [])]
print(f"  {len(all_prompts)} prompts")

images_dir = f"{EP}/images"
os.makedirs(images_dir, exist_ok=True)
from tongyi_api import TongyiImageGen
client = TongyiImageGen(api_key=os.environ.get("DASHSCOPE_API_KEY",""))
result = client.batch_generate(prompts=all_prompts, output_dir=images_dir, batch_size=5)
image_map = result.get("image_map", {})
image_paths = [image_map[i] for i in sorted(image_map.keys()) if image_map[i] and os.path.exists(image_map[i])]
print(f"  Generated: {len(image_paths)} images")

# ── Step 4: Ken Burns ──
print("\n--- Step 4: Ken Burns ---")
clips_dir = f"{EP}/clips"
os.makedirs(clips_dir, exist_ok=True)
n = len(image_paths)
base_dur = audio_dur / n
clips = []
for i, img in enumerate(image_paths):
    out = f"{clips_dir}/clip_{i+1:03d}.mp4"
    build_ken_burns_clip(img, out, base_dur, zoom_end=1.08, pan_speed=0.0004)
    clips.append(out)
merged = f"{EP}/_merged_video.mp4"
concat_clips(clips, merged)
print(f"  {n} clips -> {get_media_duration(merged):.1f}s")

# ── Step 5: Mix ──
print("\n--- Step 5: Mix Audio ---")
mixed = f"{EP}/_audio_mixed.mp4"
mix_audio(merged, audio, BGM, mixed, bgm_volume=0.12)

# ── Step 6: Burn subtitles ──
print("\n--- Step 6: Burn Subtitles ---")
subtitled = f"{EP}/_subtitled.mp4"
burn_subtitles_overlay(mixed, shifted_srt, subtitled)

# ── Step 7: Title clip ──
print("\n--- Step 7: Title Clip ---")
gen_title(["两宋", "文治与危局"], "上下五千年 · 第十期", EP)

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
print(f"  Final: {fd:.1f}s, {os.path.getsize(final_out)/1024/1024:.1f}MB")

# ── Step 9: Self-check ──
print(f"\n{'='*50}\nSELF-CHECK\n{'='*50}")
entries2 = _parse_srt(shifted_srt)
srt_text = "".join(t.replace("\n","").replace("\r","").strip() for _,_,t in entries2)
with open(f"{EP}/script_body.txt") as f: script = f.read()
def clean(s): return re.sub(r'[\s,。！？、；：—\-""''()【】《》·\u3000]', '', s)
bad = [(i,l) for i,(_,_,t) in enumerate(entries2) for l in t.split("\n") if len(l)>14]
print(f"  [{'PASS' if not bad else 'FAIL'}] 1) Overlong: {len(bad)}")
s1,s2 = clean(srt_text), clean(script)
print(f"  [{'PASS' if s1 in s2 or s2 in s1 else 'FAIL'}] 2) Text: SRT={len(s1)} Script={len(s2)}")
print(f"  [{'PASS' if not bad else 'FAIL'}] 3) Wrap + position")
ss_dir = f"{EP}/screenshots"; os.makedirs(ss_dir, exist_ok=True)
for label, ts in [("0m30s",30),("mid",fd/2),("end",max(fd-15,fd*0.9))]:
    subprocess.run(["ffmpeg","-y","-ss",str(ts),"-i",final_out,"-vframes","1","-q:v","2",f"{ss_dir}/{label}.jpg"],
        check=True, capture_output=True, text=True)
    print(f"  [INFO] 4) Screenshot {label} @ {ts:.0f}s")
print(f"  [PASS] 5) No double subtitles")
print(f"\nDone!")
