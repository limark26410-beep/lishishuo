#!/usr/bin/env python3
"""
十大女将 第3-10期 批量出片（GL-20260910）
每期：① DeepSeek 生成提示词（强制古代） → ② 流水线 run.py（生图+配音+字幕）
     → ③ 复用共享空镜补齐 → ④ 自动挑片头背景 → ⑤ 流水线重跑加片头 → ⑥ 交付+归档
用法: ./venv/bin/python tools/run_empress_series.py [起始期号]
"""
import json, os, re, shutil, subprocess, sys, time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))
from paths import shared_scenery_dir, title_bg_dir, series_dir  # noqa: E402

VENV = str(ROOT / "venv" / "bin" / "python")
SHARED = shared_scenery_dir()
BGTMP = title_bg_dir()
SERIES = series_dir("十大女将")

# 期号, 标题, 朝代（用于提示词古代约束）
EPISODES = [
    ("113", "冼夫人·岭南圣母", "南北朝至隋代（梁、陈、隋）"),
    ("114", "萧绰·铁腕太后", "辽代与北宋时期"),
    ("115", "平阳昭公主·娘子军统帅", "隋末唐初"),
    ("116", "梁红玉·击鼓战金山", "南宋"),
    ("117", "荀灌娘·十三岁突围", "东晋"),
    ("118", "花木兰·替父从军", "北魏"),
    ("119", "穆桂英·挂帅出征", "北宋"),
    ("120", "樊梨花·征西女帅", "唐代"),
]

ANCIENT = ("【重要】画面必须是中国古代场景，人物一律古代服饰：古代士兵、古代铠甲将军、"
           "古代兵器（刀枪剑戟弓箭）、古代战马、古代城池营帐。"
           "严禁任何现代元素：现代军装、枪械、汽车、现代建筑、现代人、现代器物。")


def dskey():
    for l in open(ROOT / ".env", encoding="utf-8"):
        if l.startswith("DEEPSEEK_API_KEY"):
            return l.split("=", 1)[-1].strip()
    return ""


def gen_prompts(ep: str, title: str, era: str, n: int = 24) -> list:
    """DeepSeek 生成 n 条古代场景提示词"""
    import requests
    script = (ROOT / "episodes" / ep / "script.txt").read_text(encoding="utf-8")
    prompt = f"""你是历史纪录片视觉导演。为「{title}」生成 {n} 条中文生图提示词，朝代背景：{era}。

{ANCIENT}

要求：
1. 风格前缀统一：中国古风，水墨质感，纪录片氛围，无文字，场景宏大，写意风格
2. 按稿子叙事推进：出身/习武/巾帼英姿/古代军营/古代战场厮杀/古代城池攻守/朝堂/结局与后世
3. 每条 30-55 字，突出 {era} 的服饰、建筑、兵器特征，突出场景/光影/构图/氛围
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
           "--title", title, "--series", "十大女将",
           "--name", f"{int(ep)-110:02d}-{title.split('·')[0]}",
           "--rate", "-4%", "--no-archive"] + (extra or [])
    log = f"/tmp/series_{ep}.log"
    with open(log, "w") as f:
        p = subprocess.run(cmd, cwd=str(ROOT), stdout=f, stderr=subprocess.STDOUT)
    return p.returncode == 0


def reuse_scenery(ep: str, target: int = 8):
    """从共享空镜库复制 target 张到该期 images（省预算）"""
    d = ROOT / "episodes" / ep / "images"
    d.mkdir(parents=True, exist_ok=True)
    files = sorted(SHARED.glob("*.jpg"))
    for i, f in enumerate(files[:target]):
        shutil.copy(f, d / f"shared_{i:02d}.jpg")
    return min(target, len(files))


def pick_title_bg(ep: str, title: str):
    """自动挑片头背景：偏暗 + 标题区有层次"""
    import numpy as np
    from PIL import Image
    files = sorted((ROOT / "episodes" / ep / "images").glob("*.jpg"))
    best, best_score = None, -1
    for f in files:
        if f.name.startswith("shared_"):
            continue
        a = np.array(Image.open(f).convert("RGB")).astype(int)
        h, w, _ = a.shape
        zone = a[int(h*0.22):int(h*0.48), :]
        m, s = zone.mean(), zone.std()
        if m > 130 or s < 20:
            continue
        score = (130 - m) * 0.5 + (60 - abs(s - 50)) * 0.5
        if score > best_score:
            best, best_score = f, score
    if not best:
        return None
    from PIL import Image
    a = Image.open(best); w, h = a.size
    r = max(1080/w, 1920/h)
    a2 = a.resize((int(w*r), int(h*r)), Image.LANCZOS)
    x = (a2.width-1080)//2; y = (a2.height-1920)//2
    out = BGTMP / f"{title.split('·')[0]}_自动.jpg"
    a2.crop((x, y, x+1080, y+1920)).save(out, quality=95)
    return str(out)


def deliver(ep: str, title: str):
    num = f"{int(ep)-110:02d}"
    base = f"{num}-{title.split('·')[0]}"
    src = ROOT / "episodes" / ep / "final.mp4"
    if not src.exists():
        return False
    # 只归档到目录，不再额外拷贝到桌面根目录
    for sub, ext in [("成片", ".mp4"), ("录音", ".mp3"), ("字幕", ".srt"), ("稿子", ".txt")]:
        (SERIES / sub).mkdir(parents=True, exist_ok=True)
    shutil.copy(src, SERIES / "成片" / f"{base}.mp4")
    for sub, name in [("录音", "audio.mp3"), ("字幕", "subs_processed.srt"), ("稿子", "script.txt")]:
        f = ROOT / "episodes" / ep / name
        if f.exists():
            shutil.copy(f, SERIES / sub / f"{base}{f.suffix}")
    return True


def main():
    start = sys.argv[1] if len(sys.argv) > 1 else "113"
    for ep, title, era in EPISODES:
        if ep < start:
            continue
        t0 = time.time()
        print(f"\n{'='*60}\n▶ 第{int(ep)-110}期 {title}（{era}）\n{'='*60}", flush=True)
        # ① 提示词
        try:
            lines = gen_prompts(ep, title, era)
            print(f"  ① 提示词 {len(lines)} 条 ✓", flush=True)
        except Exception as e:
            print(f"  ✗ 提示词失败: {e}", flush=True)
            continue
        # ② 流水线（生图+配音+字幕）
        print(f"  ② 流水线出片（生图 {len(lines)} 张）…", flush=True)
        if not run_pipeline(ep, title):
            print(f"  ✗ 流水线失败，见 /tmp/series_{ep}.log", flush=True)
            continue
        # ③ 复用空镜补齐
        n = reuse_scenery(ep, 8)
        print(f"  ③ 复用共享空镜 {n} 张 ✓", flush=True)
        # ④ 挑片头背景
        bg = pick_title_bg(ep, title)
        print(f"  ④ 片头背景: {os.path.basename(bg) if bg else '未找到(用默认)'}", flush=True)
        # ⑤ 重跑加片头
        extra = ["--skip-images", "--skip-tts"]
        if bg:
            extra += ["--title-bg", bg]
        if not run_pipeline(ep, title, extra):
            print(f"  ⚠ 片头重跑失败", flush=True)
        # ⑥ 交付
        ok = deliver(ep, title)
        print(f"  ⑥ 交付: {'✓' if ok else '✗'}  用时 {(time.time()-t0)/60:.1f} 分钟", flush=True)
    print("\n🎉 全部完成", flush=True)


if __name__ == "__main__":
    main()
