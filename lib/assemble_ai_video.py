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
         "-an", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "20", "-preset", "medium", n])
print(f"✓ {len(segs)} 段已统一规格 (25fps 1920x1080)")

concat_list = os.path.join(EP, "_concat.txt")

# ── 2. 标题叠加：直接叠在第 1 段开头(0-3.6s 淡入淡出)，无独立片头 ──
font = "/System/Library/Fonts/PingFang.ttc"
def _esc(t: str) -> str:
    return t.replace("\\", "\\\\").replace("'", "\\'").replace(":", "\\:")

first = os.path.join(EP, "_first_titled.mp4")
main_fs = 120 if len(main_text) <= 4 else (84 if len(main_text) <= 8 else 64)
sub_fs = 38 if len(series_text) >= 12 else 46
main_y = int(H * 0.30)          # 主标在中上部（字幕在底部，不冲突）
sub_y = main_y + main_fs + 30
# 轻压暗条仅 0-3.6s 出现（不遮整画面，解决片头太暗）；标题 alpha 淡入淡出
run(["ffmpeg", "-y", "-i", norm[0],
     "-vf",
     (f"drawbox=x=0:y={int(H*0.24)}:w={W}:h={int(H*0.32)}:color=black@0.28:t=fill:enable='lt(t,3.6)',"
      f"drawtext=fontfile={font}:text='{_esc(main_text)}':fontsize={main_fs}:fontcolor=0xD4AF37:"
      f"borderw=6:bordercolor=black@0.5:"
      f"x=(w-text_w)/2:y={main_y}:"
      f"alpha='if(lt(t,0.6),t/0.6,if(lt(t,3.0),1,(3.6-t)/0.6))',"
      f"drawtext=fontfile={font}:text='{_esc(series_text)}':fontsize={sub_fs}:fontcolor=white:"
      f"borderw=4:bordercolor=black@0.5:"
      f"x=(w-text_w)/2:y={sub_y}:"
      f"alpha='if(lt(t,0.6),t/0.6,if(lt(t,3.0),1,(3.6-t)/0.6))'"),
     "-an", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "20", "-preset", "medium", first])
print("✓ 标题已叠入第1段开头(0-3.6s 淡入淡出)")

# ── 3. 拼接（第1段带标题版 + 其余段）──
video_noaudio = os.path.join(EP, "_video_noaudio.mp4")
with open(concat_list, "w") as f:
    f.write(f"file '{os.path.abspath(first)}'\n")
    for n in norm[1:]:
        f.write(f"file '{os.path.abspath(n)}'\n")
run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", concat_list,
     "-c", "copy", video_noaudio])
print("✓ 正片拼接完成（无独立片头）")

# ── 4. 混音：配音(从3s开始) ──
audio = os.path.join(EP, "audio.mp3")
# 配乐优先用优质古筝曲；旧 bgm.mp3 质量差(32kbps像噪音)已弃用
bgm = os.path.join(EP, "..", "..", "assets", "bgm_guzheng.mp3")
if not os.path.exists(bgm):
    bgm = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "assets", "bgm_guzheng.mp3")
mixed = os.path.join(EP, "_mixed.mp4")

# 探测视频总时长（配音轨补静音到该长度）
def _probe_duration(path: str) -> float:
    r = subprocess.run(["ffmpeg", "-i", path], capture_output=True, text=True)
    import re
    m = re.search(r"Duration: (\d+):(\d+):([\d.]+)", r.stderr)
    if not m:
        raise RuntimeError(f"探测时长失败: {path}")
    h, mm, s = (float(m.group(1)), float(m.group(2)), float(m.group(3)))
    return h * 3600 + mm * 60 + s

video_sec = _probe_duration(video_noaudio)
print(f"  视频总长: {video_sec:.2f}s")

# 配音从 0s 开始（标题就在第1段开头，语音配乐同步起），apad 补静音，atrim 到视频全长
run(["ffmpeg", "-y", "-i", video_noaudio, "-i", audio,
     "-filter_complex",
     f"[1:a]aresample=48000,apad,atrim=0:{video_sec}[vc]",
     "-map", "0:v", "-map", "[vc]",
     "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", mixed])
print("✓ 配音已混入 (从 0s 开始，静音补齐至片尾)")

# ── 5. 配乐（可选：WITH_BGM=1 才铺，默认不用）──
if os.environ.get("WITH_BGM") == "1" and os.path.exists(bgm):
    with_music = os.path.join(EP, "_with_music.mp4")
    # bgm 输入无限循环，古筝曲音质好只需小幅衰减铺底
    run(["ffmpeg", "-y", "-i", mixed,
         "-stream_loop", "-1", "-i", bgm,
         "-filter_complex",
         "[1:a]volume=0.18,atrim=0:{0}[bg];[0:a][bg]amix=inputs=2:duration=first:dropout_transition=3,atrim=0:{0}[a]".format(video_sec),
         "-map", "0:v", "-map", "[a]",
         "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", with_music])
    mixed = with_music
    print("✓ 配乐已铺底（古筝曲，循环至片尾）")
else:
    print("○ 无配乐（纯配音）")

# ── 6. 字幕（SRT 原时间轴 → ASS 显式1080分辨率 → 烧录；配音 0s 起无需平移）──
import shutil
subs = os.path.join(EP, "subs.srt")
if os.path.exists(subs):
    shifted = os.path.join(EP, "_subs_shifted.srt")
    from pipeline_steps import shift_srt_file
    shift_srt_file(subs, 0.0, shifted)

    # SRT → ASS：显式 PlayResY=1080，避免 libass 按 384x288 默认放大 3.75 倍导致字幕溢出
    ass_path = os.path.join(EP, "_subs.ass")
    def _srt_to_ass(srt_path: str, ass_path: str, fontsize: int = 44,
                    margin_v: int = 70) -> str:
        def _ts(s: str) -> str:
            h, m, rest = s.split(":")
            sec, ms = rest.split(",")
            return f"{int(h)}:{int(m)}:{int(sec)}.{int(ms)//10:02d}"
        blocks = open(srt_path, encoding="utf-8").read().strip().split("\n\n")
        header = (
            "[Script Info]\nScriptType: v4.00+\nPlayResX: 1920\nPlayResY: 1080\n"
            "WrapStyle: 0\nScaledBorderAndShadow: yes\n\n"
            "[V4+ Styles]\n"
            "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
            "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
            "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
            "Alignment, MarginL, MarginR, MarginV, Encoding\n"
            f"Style: Default,PingFang SC,{fontsize},&H00FFFFFF,&H000000FF,"
            "&H64000000,&H00000000,0,0,0,0,100,100,0,0,1,2.5,0,2,80,80,"
            f"{margin_v},1\n\n[Events]\n"
            "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n")
        events = []
        for b in blocks:
            lines = b.strip().split("\n")
            if len(lines) < 3 or "-->" not in lines[1]:
                continue
            t0, t1 = (x.strip() for x in lines[1].split("-->"))
            text = "\\N".join(lines[2:]).replace("{", "｛").replace("}", "｝")
            events.append(f"Dialogue: 0,{_ts(t0)},{_ts(t1)},Default,,0,0,0,,{text}")
        with open(ass_path, "w", encoding="utf-8") as f:
            f.write(header + "\n".join(events) + "\n")
        return ass_path

    _srt_to_ass(shifted, ass_path)
    burned = os.path.join(EP, "_burned_final.mp4")
    run(["ffmpeg", "-y", "-i", mixed,
         "-vf", f"ass='{ass_path}'",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "20", "-preset", "medium",
         "-c:a", "copy", burned])
    shutil.move(burned, OUT)
    print("✓ 字幕烧录完成（ASS 1080p，自动换行）")
else:
    shutil.move(mixed, OUT)

# 清理中间
for p in norm + [concat_list, first, video_noaudio, mixed]:
    try: os.remove(p)
    except OSError: pass

print(f"\n🎬 成片: {OUT}")
r = subprocess.run(["ffmpeg", "-i", OUT], capture_output=True, text=True)
for l in r.stderr.split("\n"):
    if "Duration" in l: print("  时长:", l.strip())
