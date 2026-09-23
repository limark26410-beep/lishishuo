#!/usr/bin/env python3
"""
十大谋士 第3-10期 批量出片（GL-20260918）
每期：① DeepSeek 生成提示词（强制古代·谋士场景） → ② 流水线 run.py（生图+配音+字幕）
     → ③ 复用共享空镜/前几期图片补齐 → ④ 自动挑片头背景 → ⑤ 流水线重跑加片头 → ⑥ 交付+归档
用法: ./venv/bin/python tools/run_strategist_series.py [起始期号]
配额用尽：生图不足时自动用共享空镜 + 前几期图片补满 24 张。
"""
import json, os, re, shutil, subprocess, sys, time
from pathlib import Path

# 仓库根自动定位（本脚本在 tools/ 下，上溯一级）+ 素材库路径可配置
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))
from paths import shared_scenery_dir, title_bg_dir, series_dir  # noqa: E402

VENV = str(ROOT / "venv" / "bin" / "python")
SHARED = shared_scenery_dir()
BGTMP = title_bg_dir()
SERIES = series_dir("十大谋士")
TOTAL_IMAGES = 24          # 每期总图数（与第1/2期一致）
FRESH_N = 16               # 每期新鲜生图数（省预算，其余用共享空镜补齐）

# 期号, 标题, 朝代（用于提示词古代约束）
EPISODES = [
    ("153", "张良·运筹帷幄", "秦末汉初（楚汉之争）"),
    ("154", "诸葛亮·出师未捷", "三国蜀汉"),
    ("155", "范蠡·功成身退", "春秋越国"),
    ("156", "郭嘉·鬼才早逝", "东汉末年（曹魏）"),
    ("157", "荀彧·王佐之才", "东汉末年（曹魏）"),
    ("158", "王猛·功盖诸葛", "东晋十六国·前秦"),
    ("159", "刘伯温·一统江山", "元末明初"),
    ("160", "姚广孝·黑衣宰相", "明初（靖难之役）"),
]

ANCIENT = ("【重要】画面必须是中国古代场景，人物一律古代服饰：谋士/文臣穿布衣长衫、峨冠博带，"
           "场景为古代朝堂、竹简案牍、古代营帐议事、古代车马、古代城池街巷、古代战争。"
           "严禁任何现代元素：现代西装、眼镜、汽车、现代建筑、现代人、现代器物、手机、电子屏幕。")


def dskey():
    for l in open(ROOT / ".env", encoding="utf-8"):
        if l.startswith("DEEPSEEK_API_KEY"):
            return l.split("=", 1)[-1].strip()
    return ""


def gen_prompts(ep: str, title: str, era: str, n: int = FRESH_N) -> list:
    """DeepSeek 生成 n 条古代谋士场景提示词"""
    import requests
    script = (ROOT / "episodes" / ep / "script.txt").read_text(encoding="utf-8")
    prompt = f"""你是历史纪录片视觉导演。为「{title}」生成 {n} 条中文生图提示词，朝代背景：{era}。
{ANCIENT}
要求：
1. 风格前缀统一：中国古风，水墨质感，纪录片氛围，无文字，场景宏大，写意风格
2. 按稿子叙事推进：出身/落魄/遇明主/出谋划策/朝堂/军帐/战场/结局与后世
3. 每条 30-55 字，突出 {era} 的服饰、建筑、器物特征，突出场景/光影/构图/氛围
4. 不含具体人名文字
5. 只输出 {n} 行提示词，每行一条，无序号无解释
【稿子】
{script[:1500]}"""
    r = requests.post("https://api.deepseek.com/chat/completions",
                      headers={"Authorization": f"Bearer {dskey()}",
                               "Content-Type": "application/json"},
                      json={"model": "deepseek-chat",
                            "messages": [{"role": "user", "content": prompt}],
                            "temperature": 0.85, "max_tokens": 2600}, timeout=200)
    c = r.json()["choices"][0]["message"]["content"]
    lines = [l.strip().lstrip("0123456789.、-* ").strip() for l in c.split("\n") if l.strip()]
    lines = [l for l in lines if len(l) > 12][:n]
    data = [{"id": "auto_01", "title": f"{title}生图", "prompts": lines}]
    (ROOT / "episodes" / ep / "prompts.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return lines


def run_pipeline(ep: str, title: str, extra=None) -> bool:
    cmd = [VENV, "run.py", "--episode", ep,
           "--engine", "edge", "--canvas", "portrait",
           "--title", title, "--series", "十大谋士",
           "--name", f"{int(ep)-150:02d}-{title.split('·')[0]}",
           "--rate", "-4%", "--no-archive"] + (extra or [])
    log = f"/tmp/strategist_{ep}.log"
    with open(log, "w") as f:
        p = subprocess.run(cmd, cwd=str(ROOT), stdout=f, stderr=subprocess.STDOUT)
    return p.returncode == 0


def fresh_images(ep: str) -> list:
    d = ROOT / "episodes" / ep / "images"
    if not d.is_dir():
        return []
    return sorted(p.name for p in d.glob("*.jpg")
                  if not p.name.startswith(("shared_", "reuse_")))


def fill_images(ep: str, target: int = TOTAL_IMAGES):
    """补齐到 target 张：先共享空镜，再复用前几期新鲜图。返回补充张数。"""
    d = ROOT / "episodes" / ep / "images"
    d.mkdir(parents=True, exist_ok=True)
    have = len(fresh_images(ep)) + len(list(d.glob("shared_*.jpg")))
    need = target - have
    if need <= 0:
        return 0
    added = 0
    # 1) 共享空镜
    shared_files = sorted(SHARED.glob("*.jpg")) if SHARED.is_dir() else []
    for f in shared_files:
        if added >= need:
            break
        shutil.copy(f, d / f"shared_{added:02d}.jpg")
        added += 1
    # 2) 复用前几期新鲜图（作为 filler）
    if added < need:
        prev = []
        for e in sorted(os.listdir(ROOT / "episodes")):
            if e.isdigit() and int(e) < int(ep) and (ROOT / "episodes" / e / "images").is_dir():
                prev += [p for p in (ROOT / "episodes" / e / "images").glob("*.jpg")
                         if not p.name.startswith("shared_")]
        for f in prev:
            if added >= need:
                break
            shutil.copy(f, d / f"reuse_{added:02d}.jpg")
            added += 1
    return added


def pick_title_bg(ep: str, title: str):
    """自动挑片头背景：偏暗 + 标题区有层次。优先新鲜图，无则回退复用图。"""
    import numpy as np
    from PIL import Image
    files = sorted((ROOT / "episodes" / ep / "images").glob("*.jpg"))
    for allow_reuse in (False, True):
        best, best_score = None, -1
        for f in files:
            if not allow_reuse and f.name.startswith(("shared_", "reuse_")):
                continue
            try:
                a = np.array(Image.open(f).convert("RGB")).astype(int)
            except Exception:
                continue
            h, w, _ = a.shape
            zone = a[int(h*0.22):int(h*0.48), :]
            m, s = zone.mean(), zone.std()
            if m > 130 or s < 20:
                continue
            score = (130 - m) * 0.5 + (60 - abs(s - 50)) * 0.5
            if score > best_score:
                best, best_score = f, score
        if best:
            break
    if not best:
        return None
    a = Image.open(best); w, h = a.size
    r = max(1080/w, 1920/h)
    a2 = a.resize((int(w*r), int(h*r)), Image.LANCZOS)
    x = (a2.width-1080)//2; y = (a2.height-1920)//2
    out = BGTMP / f"{title.split('·')[0]}_自动.jpg"
    out.parent.mkdir(parents=True, exist_ok=True)
    a2.crop((x, y, x+1080, y+1920)).save(out, quality=95)
    return str(out)


def deliver(ep: str, title: str):
    num = f"{int(ep)-150:02d}"
    base = f"{num}-{title.split('·')[0]}"
    src = ROOT / "episodes" / ep / "final.mp4"
    if not src.exists():
        return False
    # 只归档到目录，不再额外拷贝到桌面根目录
    for sub in ["成片", "录音", "字幕", "稿子"]:
        (SERIES / sub).mkdir(parents=True, exist_ok=True)
    shutil.copy(src, SERIES / "成片" / f"{base}.mp4")
    for sub, name in [("录音", "audio.mp3"), ("字幕", "subs_processed.srt"), ("稿子", "script.txt")]:
        f = ROOT / "episodes" / ep / name
        if f.exists():
            shutil.copy(f, SERIES / sub / f"{base}{f.suffix}")
    return True


def main():
    start = sys.argv[1] if len(sys.argv) > 1 else "153"
    quota_hit = False
    for ep, title, era in EPISODES:
        if ep < start:
            continue
        t0 = time.time()
        num = f"{int(ep)-150:02d}"
        print(f"\n{'='*60}\n▶ 第{int(num)}期 {title}（{era}）\n{'='*60}", flush=True)

        # ① 提示词（已存在则跳过，避免重复调用 DeepSeek）
        if (ROOT / "episodes" / ep / "prompts.json").exists():
            print("  ① 提示词已存在，跳过", flush=True)
        else:
            try:
                lines = gen_prompts(ep, title, era, n=FRESH_N)
                print(f"  ① 提示词 {len(lines)} 条 ✓", flush=True)
            except Exception as e:
                print(f"  ✗ 提示词失败: {e}", flush=True)
                continue

        # ② 出片：额度耗尽走复用（仍生成配音），否则先生图
        if quota_hit:
            print("  ② 生图额度已尽，复用旧图 + 配音", flush=True)
            fill_images(ep, TOTAL_IMAGES)
            ok = run_pipeline(ep, title, ["--skip-images"])
        else:
            print(f"  ② 流水线出片（生图 {FRESH_N} 张）…", flush=True)
            ok = run_pipeline(ep, title)
            if not ok:
                print("  ✗ 生图失败，清空后复用旧图重跑", flush=True)
                d = ROOT / "episodes" / ep / "images"
                if d.is_dir():
                    shutil.rmtree(d)
                fill_images(ep, TOTAL_IMAGES)
                ok = run_pipeline(ep, title, ["--skip-images"])
        if not ok:
            print(f"  ✗ 出片失败，见 /tmp/strategist_{ep}.log", flush=True)
            continue

        # ③ 挑片头背景（无新图时也从复用图里挑）
        bg = pick_title_bg(ep, title)
        print(f"  ③ 片头背景: {os.path.basename(bg) if bg else '未找到(用默认)'}", flush=True)

        # ④ 重跑加片头（跳过生图+配音）
        extra = ["--skip-images", "--skip-tts"]
        if bg:
            extra += ["--title-bg", bg]
        if not run_pipeline(ep, title, extra):
            print("  ⚠ 片头重跑失败", flush=True)

        # ⑤ 交付（只归档到目录）
        ok = deliver(ep, title)
        print(f"  ⑤ 交付: {'✓' if ok else '✗'}  用时 {(time.time()-t0)/60:.1f} 分钟", flush=True)

        # 配额检测：生图张数明显不足 → 标记，后续走复用
        if not quota_hit:
            n_fresh = len(fresh_images(ep))
            if n_fresh < FRESH_N * 0.6:
                quota_hit = True
                print(f"  ⚠ 生图仅 {n_fresh}/{FRESH_N}，判定额度将尽，后续期走复用", flush=True)

    print("\n🎉 全部完成", flush=True)


if __name__ == "__main__":
    main()
