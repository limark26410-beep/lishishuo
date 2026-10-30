#!/usr/bin/env python3
"""
中发漫剧短剧出片编排器（GL-20261013）
与 run_baijiu.py 的区别：**多一步「首帧合成」，用参考图锁住人物一致性**。

    run_baijiu.py : 提示词 ──> 文生视频（智谱）──> 拼装
    run_drama.py  : 角色立绘+场景图 ──> 首帧合成（qwen-image-3.0-pro）
                                     ──> 图生视频（happyhorse i2v）
                                     ──> 配音 + 拼装

project.json 里，**某一场只要有 first_frame 字段就走 i2v 链路**；
没有的场次跳过（本脚本只处理配了 first_frame 的场次）。

    {
      "title": "柜台没人接",
      "series": "中发市场短剧 · 第1集",
      "scenes": [
        {"role": "客商", "voice": "zh_male_ruyayichen_saturn_bigtts", "rate": 0,
         "text": "货款收齐了，回去发货。",
         "first_frame": {
            "chars": ["首帧素材/客商_全身正面.png", "首帧素材/二哥_全身正面.png"],
            "scene": "../../中发短剧素材/.../老院子四视图.png",
            "prompt": "客商背着军绿色旧帆布书包跨过小院木门槛，旁边二哥侧身让路"
         }}
      ]
    }

用法：
  python run_drama.py <项目目录> [--slot 5] [--dry-run]
  python run_drama.py <项目目录> --only-s07     # 只跑某几镜（试片用）
  python run_drama.py <项目目录> --skip-video   # 复用已有 seg_*.mp4

分步跑（推荐先试一镜）：
  python lib/gen_first_frame.py <项目目录> 7   # 只合成 S07 首帧
  python run_drama.py <项目目录> --skip-first-frame --only 7
"""
import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "lib"))

from paths import load_env  # noqa: E402

load_env()


def parse_only(spec: str) -> set:
    """'7' / '5,7,11' → {7} / {5,7,11}"""
    if not spec:
        return set()
    out = set()
    for part in spec.replace("，", ",").split(","):
        part = part.strip()
        if part:
            out.add(int(part))
    return out


def step_first_frames(proj: Path, scenes: list, todo: list, model: str, size: str):
    """① 首帧合成：角色立绘 + 场景图 → 角色在场景中的一帧"""
    from gen_first_frame import compose_first_frame

    ff_dir = proj / "首帧"
    ff_dir.mkdir(exist_ok=True)
    print(f"① 首帧合成（{len(todo)} 镜，模型 {model}）", flush=True)
    for i in todo:
        s = scenes[i - 1]
        ff = s["first_frame"]
        out = ff_dir / f"S{i:02d}.png"
        chars = [(proj / p).resolve() for p in ff.get("chars", [])]
        scene = (proj / ff["scene"]).resolve() if ff.get("scene") else ""
        print(f"  [{i}] {s['role']} ← {len(chars)} 张角色图"
              + (f" + 场景 {Path(scene).name}" if scene else ""))
        compose_first_frame(
            prompt=(ff.get("prompt") or s.get("prompt", "")),
            char_refs=[str(c) for c in chars],
            scene_ref=str(scene),
            out_path=str(out), model=model, size=size)


def step_local_first_frames(proj: Path, scenes: list, todo: list, size: str):
    """①-b 本地拼合首帧（零成本，不调任何图像 API）。

    质量定位：构图对、人物对，但光影融合粗糙 —— 靠下一步 i2v 重新渲染去"洗"。
    实测（20261013）：粗糙拼合交给 cogvideox-flash 后，成片从"明显贴纸"
    变成"cinematic frame，仅光线略有出入"。够用于试片。
    """
    from compose_first_frame_local import compose, pick_panel
    from PIL import Image

    W, H = [int(x) for x in size.replace("x", "*").split("*")]
    # 横屏：本地合成走 16:9（scale 到 1660x934 与 i2v 的 1920x1080 同比例）
    lw = 1660
    lh = int(lw * 9 / 16)
    ff_dir = proj / "首帧"
    ff_dir.mkdir(exist_ok=True)
    print(f"① 本地拼合首帧（{len(todo)} 镜，{lw}x{lh}，零成本）", flush=True)

    for i in todo:
        s = scenes[i - 1]
        ff = s["first_frame"]
        chars = [(proj / p).resolve() for p in ff.get("chars", [])]
        scene = (proj / ff["scene"]).resolve() if ff.get("scene") else ""
        # 布局：从 first_frame.layout 读，缺省按人数自动均分
        layout = ff.get("layout") or []
        if not layout:
            n = max(1, len(chars))
            layout = [{"height": 0.78, "x": (k + 1) / (n + 1), "bottom": 0.96}
                      for k in range(n)]
        people = [dict(layout[k] if k < len(layout) else {}, img=str(c))
                  for k, c in enumerate(chars)]
        for k, p in enumerate(people):
            p.setdefault("height", 0.78)
            p.setdefault("x", (k + 1) / (len(people) + 1))
            p.setdefault("bottom", 0.96)
        print(f"  [{i}] {s['role']} ← {len(chars)} 角色"
              + (f" + 场景 {Path(scene).name}" if scene else ""))
        compose(scene=str(scene), out=str(ff_dir / f"S{i:02d}.png"),
                people=people, size=(lw, lh),
                scene_panel=ff.get("scene_panel", 0),
                scene_cols=ff.get("scene_cols", 2),
                scene_rows=ff.get("scene_rows", 2))


def _prepare_upload(img: Path, max_w: int = 1280, quality: int = 88) -> str:
    """把首帧压成 JPEG 再 base64：原 PNG 约 2.6MB → 约 200KB。

    智谱免费档对请求体敏感，压小能显著降低被限流的概率。
    """
    import base64
    import io
    from PIL import Image

    im = Image.open(img).convert("RGB")
    if im.width > max_w:
        im = im.resize((max_w, int(im.height * max_w / im.width)), Image.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=quality, optimize=True)
    return base64.b64encode(buf.getvalue()).decode()


def step_videos(proj: Path, scenes: list, todo: list, slot: int, size: str,
                backend: str = "zhipu", model: str = "cogvideox-flash"):
    """② 图生视频：首帧 → 一段视频。

    backend:
      zhipu —— 智谱 CogVideoX（cogvideox-flash 免费档；实测支持 image_url 传首帧）
      ali   —— 阿里 happyhorse-1.1-i2v（走 DASHSCOPE_API_KEY，额度另算）
    """
    import base64
    import time as _t

    ff_dir = proj / "首帧"
    out_dir = proj / "ai_video"
    out_dir.mkdir(exist_ok=True)
    print(f"② 图生视频（{len(todo)} 镜，{slot}s/段，后端={backend}/{model}）", flush=True)

    # ── 轮流排队：免费档 1305 极频繁，单镜头死磕不划算 ──
    # 每轮对每个「还没提交成功」的镜头试一次，谁排上算谁的。
    import base64
    import time as _t
    from gen_ai_video_zhipu import submit as zsubmit, BusyError

    payloads = {}
    skipped = []
    for i in todo:
        img = ff_dir / f"S{i:02d}.png"
        if not img.exists():
            print(f"  ✗ S{i:02d} 缺首帧 {img.name}，跳过")
            continue
        # 断点续跑：已有成片就跳过（重跑只补没生成的）
        done = out_dir / f"seg_{i:02d}.mp4"
        if done.exists() and done.stat().st_size > 100000:
            skipped.append(i)
            continue
        s = scenes[i - 1]
        motion = s["first_frame"].get("motion") or s.get("motion") or (
            "画面自然动起来：人物有细微的动作，镜头缓慢推近，"
            "光线与氛围连贯，电影质感，动作幅度小、不变形")
        # 每镜用自己的时长（旁白时长决定），缺省回退全局 slot
        dur = int(s["first_frame"].get("duration") or s.get("duration") or slot)
        if backend == "zhipu":
            payloads[i] = (motion, _prepare_upload(img), dur)
        else:
            payloads[i] = (motion, str(img), dur)

    if skipped:
        print(f"  ↩ 跳过已生成的 {len(skipped)} 镜: {skipped}")
    pending = sorted(payloads)
    submitted = []
    rnd = 0
    ROUNDS = int(os.environ.get("I2V_ROUNDS", "20"))
    while pending and rnd < ROUNDS:
        rnd += 1
        still = []
        print(f"  ── 第 {rnd}/{ROUNDS} 轮：{len(pending)} 镜排队 ──", flush=True)
        for i in pending:
            motion, payload, dur = payloads[i]
            try:
                if backend == "zhipu":
                    tid = zsubmit(motion, model, size=size, duration=dur,
                                  image_url=payload, max_retries=1, quiet=True)
                    submitted.append(("zhipu", i, tid))
                else:
                    import gen_i2v_happyhorse as i2v
                    # 阿里用 * 分隔尺寸（如 1920*1080），智谱用 x
                    ali_size = size.replace("x", "*")
                    tid = i2v.submit(motion, payload, duration=dur, size=ali_size)
                    submitted.append(("ali", i, tid))
                print(f"    ✓ S{i:02d} 已提交")
            except BusyError:
                still.append(i)
            except Exception as e:  # noqa: BLE001
                print(f"    ✗ S{i:02d} 提交异常: {str(e)[:120]}")
                still.append(i)
            _t.sleep(4)
        pending = still
        if pending:
            print(f"    …本轮排上 {len(submitted)} 镜，剩 {len(pending)} 镜，等 45s 再来")
            _t.sleep(45)

    if pending:
        print(f"  ⚠ 跑满 {ROUNDS} 轮仍有 {len(pending)} 镜没排上: {pending}")
    if not submitted:
        print("  ✗ 一镜都没排上，稍后再跑一次即可（免费档波动）")
        return

    print(f"  共 {len(submitted)} 个任务，开始轮询…")
    for be, i, tid in submitted:
        try:
            if be == "zhipu":
                from gen_ai_video_zhipu import poll as zpoll, download as zdl
                zdl(zpoll(tid), str(out_dir / f"seg_{i:02d}.mp4"))
            else:
                import gen_i2v_happyhorse as i2v
                i2v.download(i2v.poll(tid), str(out_dir / f"seg_{i:02d}.mp4"))
        except Exception as e:  # noqa: BLE001
            print(f"  ✗ S{i:02d} 生成失败: {e}")


def main():
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("proj")
    ap.add_argument("--slot", type=int, default=5, help="每段视频秒数（默认 5）")
    ap.add_argument("--size", default="1920x1080", help="i2v 尺寸（默认 1920x1080 横屏）")
    ap.add_argument("--backend", default="zhipu", choices=["zhipu", "ali"],
                    help="图生视频后端：zhipu=智谱免费档（默认）/ ali=happyhorse")
    ap.add_argument("--video-model", default="cogvideox-flash",
                    help="智谱视频模型（cogvideox-flash 免费 / cogvideox-3 付费1元）")
    ap.add_argument("--local-first-frame", action="store_true",
                    help="用本地 PIL 拼合首帧（不调图像 API，零成本，质量粗）")
    ap.add_argument("--ff-model", default="", help="首帧合成模型（默认 qwen-image-3.0-pro）")
    ap.add_argument("--ff-size", default="", help="首帧尺寸（默认 1664*928 横屏）")
    ap.add_argument("--only", default="", help="只跑指定镜号，如 7 或 5,7,11")
    ap.add_argument("--skip-first-frame", action="store_true")
    ap.add_argument("--skip-video", action="store_true")
    ap.add_argument("--skip-audio", action="store_true")
    ap.add_argument("--skip-assemble", action="store_true")
    ap.add_argument("--only-first-frame", action="store_true",
                    help="只合成首帧就停（不生成视频）")
    ap.add_argument("--dry-run", action="store_true", help="只打印计划，不调任何 API")
    args = ap.parse_args()

    proj = Path(args.proj).expanduser().resolve()
    cfg = json.loads((proj / "project.json").read_text(encoding="utf-8"))
    scenes = cfg["scenes"]
    only = parse_only(args.only)

    # 只处理配了 first_frame 的场次（i2v 链路）
    todo = [i for i, s in enumerate(scenes, 1) if s.get("first_frame")]
    if only:
        todo = [i for i in todo if i in only]

    print(f"▶ {cfg['title']} · i2v 链路 · {len(todo)} 镜"
          f"（project.json 共 {len(scenes)} 场）", flush=True)
    print(f"  项目目录: {proj}")

    # 前置检查
    missing = []
    for i in todo:
        ff = scenes[i - 1]["first_frame"]
        for p in ff.get("chars", []):
            if not (proj / p).exists():
                missing.append(f"S{i:02d} 角色图 {p}")
        if ff.get("scene") and not (proj / ff["scene"]).exists():
            missing.append(f"S{i:02d} 场景图 {ff['scene']}")
    if missing:
        print("\n⚠ 缺少参考图，先修好再跑：")
        for m in missing:
            print("   -", m)
        sys.exit(1)
    if not todo:
        print("⚠ 没有任何场次配置 first_frame；本脚本只跑 i2v 链路")
        sys.exit(1)

    if args.dry_run:
        print("\n[dry-run] 计划：")
        for i in todo:
            s = scenes[i - 1]
            ff = s["first_frame"]
            print(f"  S{i:02d} {s['role']:<5} 首帧← {len(ff.get('chars', []))}角色"
                  f"+{'场景' if ff.get('scene') else '无场景'}"
                  f" → seg_{i:02d}.mp4 ({args.slot}s)")
            print(f"       台词: {s['text']}")
        print(f"\n  首帧输出: {proj/'首帧'}/S01.png … S{len(scenes):02d}.png")
        print(f"  视频输出: {proj/'ai_video'}/seg_XX.mp4")
        return

    # ① 首帧
    if not args.skip_first_frame:
        if args.local_first_frame:
            step_local_first_frames(proj, scenes, todo, args.size)
        else:
            step_first_frames(proj, scenes, todo, args.ff_model, args.ff_size)
    else:
        print("① 跳过首帧合成（--skip-first-frame）")

    if args.only_first_frame:
        print(f"\n已停在第①步。首帧在 {proj/'首帧'}/")
        return

    # ② 视频
    if not args.skip_video:
        step_videos(proj, scenes, todo, args.slot, args.size,
                    backend=args.backend, model=args.video_model)
    else:
        print("② 跳过视频生成（--skip-video）")

    # ③ 配音 + 字幕
    if not args.skip_audio:
        print("③ 多角色配音…", flush=True)
        from run_baijiu import step_audio
        step_audio(proj, scenes, float(args.slot))
    else:
        print("③ 跳过配音（--skip-audio）")

    # ④ 拼装（复用现成模块：片头 + 视频 + 配音 + 字幕 + 配乐）
    if not args.skip_assemble:
        print("④ 拼装…", flush=True)
        (proj / "title_text.txt").write_text(
            f"{cfg['title']}\n{cfg.get('series', '')}", encoding="utf-8")
        env = os.environ.copy()
        if cfg.get("bgm", True):
            env["WITH_BGM"] = "1"
        subprocess.run([sys.executable, str(ROOT / "lib" / "assemble_ai_video.py"),
                        str(proj)], check=True, env=env)
        print(f"\n🎬 成片: {proj / 'final.mp4'}")
    else:
        print("④ 跳过拼装（--skip-assemble）")


if __name__ == "__main__":
    main()
