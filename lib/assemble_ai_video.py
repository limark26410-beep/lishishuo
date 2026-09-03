"""
AI 视频成片拼接（GL-20260902 武则天 / 20260903 钟茂丰）
N 条 AI 视频 + 片头(标题读 title_text.txt，缺省武则天) + 配音 + 字幕 → 横版 mp4
用法: python lib/assemble_ai_video.py <ep_dir> [输出路径]
"""
import os, sys, json, subprocess
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

EP = sys.argv[1] if len(sys.argv) > 1 else "episodes/134"
OUT = sys.argv[2] if len(sys.argv) > 2 else os.path.join(EP, "final.mp4")
W, H = 1920, 1080

def run(cmd):
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg 失败: {r.stderr[-600:]}")
    return r

# ── 标题（目录里 title_text.txt 两行：主标/副标；缺省武则天）──
_tp = os.path.join(EP, "title_text.txt")
if os.path.exists(_tp):
    lines = [l.strip() for l in open(_tp, encoding="utf-8") if l.strip()]
    main_text = lines[0] if lines else "AI 成片"
    series_text = lines[1] if len(lines) > 1 else ""
else:
    main_text, series_text = "武则天·一代女皇", "历史说 · 女帝风云"

# ── 1. N 条视频统一转码 + concat ──
segs = sorted(os.path.join(EP, "ai_video", f) for f in os.listdir(os.path.join(EP, "ai_video"))
              if f.startswith("seg_") and f.endswith(".mp4"))
assert segs, f"{EP}/ai_video 下没有 seg_*.mp4"
norm = [os.path.join(EP, f"_norm_{i}.mp4") for i in range(1, len(segs) + 1)]
for i, (s, n) in enumerate(zip(segs, norm)):
    run(["ffmpeg", "-y", "-i", s,
         "-vf", f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},setsar=1,fps=25",
         "-an", "-c:v", "libx264", "-crf", "20", "-preset", "medium", n])
print(f"✓ {len(segs)} 段已统一规格 (25fps 1920x1080)")

concat_list = os.path.join(EP, "_concat.txt")
with open(concat_list, "w") as f:
    for n in norm:
        f.write(f"file '{os.path.abspath(n)}'\n")
merged = os.path.join(EP, "_merged.mp4")
run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", concat_list,
     "-c", "copy", merged])
print("✓ 4 段拼接完成 (60s)")

# ── 2. 片头：标题压暗条 3s（从黑场淡入）──
title = os.path.join(EP, "_title.mp4")
font = "/System/Library/Fonts/PingFang.ttc"
# 转义 drawtext 特殊字符，避免标题内容破坏滤镜解析
def _esc(t: str) -> str:
    return t.replace("\\", "\\\\").replace("'", "\\'").replace(":", "\\:")
main_fs = 96 if len(main_text) <= 4 else (72 if len(main_text) <= 8 else 56)
sub_fs = 28 if len(series_text) >= 12 else 36
run(["ffmpeg", "-y", "-f", "lavfi", "-i", f"color=c=black:s={W}x{H}:d=3:r=25",
     "-vf",
     (f"drawbox=x=0:y={int(H*0.60)}:w={W}:h={int(H*0.16)}:color=black@0.0:t=fill,"
      f"drawtext=fontfile={font}:text='{_esc(main_text)}':fontsize={main_fs}:fontcolor=0xD4AF37:"
      f"x=(w-text_w)/2:y={int(H*0.60)}," 
      f"drawtext=fontfile={font}:text='{_esc(series_text)}':fontsize={sub_fs}:fontcolor=white:"
      f"x=(w-text_w)/2:y={int(H*0.60)+int(main_fs*0.55)+16}," 
      f"fade=t=in:st=0:d=1"),
     "-c:v", "libx264", "-crf", "20", "-preset", "medium", "-an", title])
print("✓ 片头 3s 已生成")

# ── 3. 拼接片头 + 正片 ──
with open(concat_list, "w") as f:
    f.write(f"file '{os.path.abspath(title)}'\n")
    for n in norm:
        f.write(f"file '{os.path.abspath(n)}'\n")
video_noaudio = os.path.join(EP, "_video_noaudio.mp4")
run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", concat_list,
     "-c", "copy", video_noaudio])
print("✓ 片头+正片拼接 (63s)")

# ── 4. 混音：配音(从3s开始) + 配乐 ──
audio = os.path.join(EP, "audio.mp3")
bgm = os.path.join(EP, "..", "..", "assets", "bgm.mp3")
if not os.path.exists(bgm):
    bgm = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "assets", "bgm.mp3")
mixed = os.path.join(EP, "_mixed.mp4")
# 找配乐（用现成 assets/bgm.mp3）
filt = (
    f"[1:a]adelay=3000|3000,volume=1.0[vc];"
)
# 简单方案：配音直接 mux 到视频（从片头后开始用 adelay）
run(["ffmpeg", "-y", "-i", video_noaudio, "-i", audio,
     "-filter_complex",
     f"[1:a]adelay=3000|3000,aresample=48000[vc]",
     "-map", "0:v", "-map", "[vc]",
     "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", mixed])
print("✓ 配音已混入 (从 3s 片头后开始)")

# ── 5. 配乐（轻音量铺底）──
if os.path.exists(bgm):
    with_music = os.path.join(EP, "_with_music.mp4")
    run(["ffmpeg", "-y", "-i", mixed, "-i", bgm,
         "-filter_complex",
         "[1:a]volume=0.10[bg];[0:a][bg]amix=inputs=2:duration=first:dropout_transition=3[a]",
         "-map", "0:v", "-map", "[a]",
         "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", with_music])
    mixed = with_music
    print("✓ 配乐已铺底")

# ── 6. 字幕（从 3s 平移后烧录）──
import shutil
subs = os.path.join(EP, "subs.srt")
if os.path.exists(subs):
    # SRT 平移 +3s
    shifted = os.path.join(EP, "_subs_shifted.srt")
    from pipeline_steps import shift_srt_file
    shift_srt_file(subs, 3.0, shifted)
    # 简单烧录（用 ffmpeg subtitles filter）
    burned = os.path.join(EP, "_burned_final.mp4")
    run(["ffmpeg", "-y", "-i", mixed,
         "-vf", f"subtitles='{shifted}':force_style='FontName=PingFang SC,FontSize=22,PrimaryColour=&H00FFFFFF,OutlineColour=&H80000000,BorderStyle=1,Outline=1,MarginV=60'",
         "-c:v", "libx264", "-crf", "20", "-preset", "medium",
         "-c:a", "copy", burned])
    shutil.move(burned, OUT)
else:
    shutil.move(mixed, OUT)

# 清理中间
for p in norm + [concat_list, merged, title, video_noaudio, mixed]:
    try: os.remove(p)
    except OSError: pass

print(f"\n🎬 成片: {OUT}")
r = subprocess.run(["ffmpeg", "-i", OUT], capture_output=True, text=True)
for l in r.stderr.split("\n"):
    if "Duration" in l: print("  时长:", l.strip())
