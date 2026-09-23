#!/usr/bin/env python3
"""
白酒短剧出片编排器（GL-20260923）
一个项目目录 = 一集「人物对话」短片，一条命令跑完三步：
  ① 智谱 CogVideoX 生成每场 AI 视频
  ② 豆包多角色配音（逐句合成 → 按镜头时长拼音轨 + 对话字幕）
  ③ assemble 拼装（片头 + 视频 + 配音 + 字幕 + 配乐）

用法：
  ./venv/bin/python run_baijiu.py <项目目录> [model]
  model 缺省 cogvideox-flash（免费 5s/段）；付费档传 cogvideox-3（10s/段）

项目目录需含 project.json：
{
  "title": "清引",
  "series": "白酒酿造短剧 · 清香地缸之源",
  "scenes": [
    {"role":"沈清","voice":"zh_female_vv_uranus_bigtts","rate":0,
     "text":"这酒，怎么浑成这样？",
     "prompt":"北宋古装……沈清尝酒皱眉……"}
  ]
}
"""
import sys, os, json, subprocess, tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "lib"))

from paths import load_env  # noqa: E402
load_env()

SLOT = {"cogvideox-flash": 5.0, "cogvideox-3": 10.0}  # 每段视频秒数


def _ffprobe_dur(path: str) -> float:
    from tts_utils import _audio_duration_sec
    return _audio_duration_sec(path)


def _ts(sec: float) -> str:
    h, m = int(sec // 3600), int(sec % 3600 // 60)
    s = sec % 60
    return f"{h:02d}:{m:02d}:{s:06.3f}".replace(".", ",")


def step_audio(proj: Path, scenes: list, slot: float):
    """② 多角色配音：豆包逐句合成 → 垫到 slot 秒 → 拼音轨 + 生成 subs.srt"""
    import requests
    from tts_utils import _doubao_synth_once, _concat_audios

    sess = requests.Session(); sess.trust_env = False  # 绕过残留代理
    tmp = Path(tempfile.mkdtemp(prefix="baijiu_audio_"))
    padded, sub_entries = [], []

    for i, s in enumerate(scenes, 1):
        audio, _ = _doubao_synth_once(s["text"], s["voice"], s.get("rate", 0), session=sess)
        raw = tmp / f"{i:02d}_raw.mp3"
        raw.write_bytes(audio)
        dur = _ffprobe_dur(str(raw))
        # 垫到 slot 秒（对齐视频镜头）；台词显示时长取实际发音时长
        pad = tmp / f"{i:02d}_pad.mp3"
        subprocess.run(["ffmpeg", "-y", "-i", str(raw), "-af", "apad",
                        "-t", f"{slot}", "-c:a", "libmp3lame", "-q:a", "6",
                        str(pad)], check=True, capture_output=True)
        padded.append(str(pad))
        start = (i - 1) * slot
        sub_entries.append((s["role"], s["text"], start, start + min(dur, slot)))
        print(f"  [{i}] {s['role']}: {s['text'][:20]}… ({dur:.1f}s)")

    _concat_audios(padded, str(proj / "audio.mp3"))
    # 生成对话字幕（带角色名）
    lines = []
    for n, (role, text, s, e) in enumerate(sub_entries, 1):
        lines += [str(n), f"{_ts(s)} --> {_ts(e)}", f"{role}：{text}", ""]
    (proj / "subs.srt").write_text("\n".join(lines), encoding="utf-8")
    print(f"  ✓ audio.mp3 + subs.srt（{len(sub_entries)} 条对话字幕）")


def main():
    proj = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path.cwd()
    model = sys.argv[2] if len(sys.argv) > 2 else "cogvideox-flash"
    slot = SLOT.get(model, 5.0)

    cfg = json.loads((proj / "project.json").read_text(encoding="utf-8"))
    title, series = cfg["title"], cfg.get("series", "")
    scenes = cfg["scenes"]
    print(f"▶ {title} · {len(scenes)} 场对话 · {model}（{slot}s/段）", flush=True)

    # 片头标题
    (proj / "title_text.txt").write_text(f"{title}\n{series}", encoding="utf-8")

    # ① 生视频
    prompts = [{"seg": i, "prompt": s["prompt"]} for i, s in enumerate(scenes, 1)]
    (proj / "ai_video_prompts.json").write_text(
        json.dumps(prompts, ensure_ascii=False, indent=2), encoding="utf-8")
    print("① 生成 AI 视频…", flush=True)
    from gen_ai_video_zhipu import gen_sequence
    gen_sequence(prompts, str(proj / "ai_video"), model)

    # ② 多角色配音 + 字幕
    print("② 豆包多角色配音…", flush=True)
    step_audio(proj, scenes, slot)

    # ③ 拼装（project.json 的 bgm 字段控制是否铺配乐，默认铺）
    print("③ 拼装…", flush=True)
    env = os.environ.copy()
    if cfg.get("bgm", True):
        env["WITH_BGM"] = "1"
    subprocess.run([sys.executable, str(ROOT / "lib" / "assemble_ai_video.py"), str(proj)],
                   check=True, env=env)
    print(f"\n🎬 成片: {proj / 'final.mp4'}", flush=True)


if __name__ == "__main__":
    main()
