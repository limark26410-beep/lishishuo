"""
武则天 AI 视频生成（GL-20260902）
用 wan2.7-t2v 免费额度生成 4 条横版视频 → 拼接成 1 分钟素材
"""
import sys, os, json, time
import requests

KEY = os.environ.get("DASHSCOPE_API_KEY", "") or open(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".env"),
    encoding="utf-8").read().split("DASHSCOPE_API_KEY=")[1].splitlines()[0].strip()

MODEL = "wan2.7-t2v-2026-06-12"
SUBMIT_URL = "https://dashscope.aliyuncs.com/api/v1/services/aigc/video-generation/video-synthesis"
TASK_URL = "https://dashscope.aliyuncs.com/api/v1/tasks/{}"


def submit(prompt: str, duration: int = 15) -> str:
    body = {
        "model": MODEL,
        "input": {"prompt": prompt},
        "parameters": {"size": "1280*720", "duration": duration,
                       "watermark": False},
    }
    r = requests.post(SUBMIT_URL,
                      headers={"Authorization": f"Bearer {KEY}",
                               "Content-Type": "application/json",
                               "X-DashScope-Async": "enable"},
                      json=body, timeout=60)
    r.raise_for_status()
    return r.json()["output"]["task_id"]


def poll(task_id: str, timeout=900) -> str:
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = requests.get(TASK_URL.format(task_id),
                         headers={"Authorization": f"Bearer {KEY}"}, timeout=30)
        d = r.json()
        status = d.get("output", {}).get("task_status", "")
        if status == "SUCCEEDED":
            out = d.get("output", {})
            url = out.get("video_url") or ""
            if not url:
                res = out.get("results") or []
                url = res[0].get("url", "") if res else ""
            return url
        elif status == "FAILED":
            raise RuntimeError(f"生成失败: {json.dumps(d.get('output'), ensure_ascii=False)[:200]}")
        time.sleep(20)
    raise RuntimeError(f"超时: {task_id}")


def download(url: str, out_path: str):
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    data = requests.get(url, timeout=120).content
    with open(out_path, "wb") as f:
        f.write(data)
    print(f"  ✓ {os.path.basename(out_path)} ({len(data)//1024//1024:.1f}MB)")


def gen_sequence(prompts: list, out_dir: str, duration: int = 15,
                 parallel: bool = True):
    """生成一组视频。parallel=True 时全部先提交再逐个轮询（快）"""
    os.makedirs(out_dir, exist_ok=True)
    if parallel:
        tasks = []
        for i, p in enumerate(prompts, 1):
            print(f"  提交 [{i}/{len(prompts)}]: {p['prompt'][:40]}...")
            tid = submit(p["prompt"], duration)
            tasks.append((i, p, tid))
            time.sleep(3)  # 限速
        print(f"\n共 {len(tasks)} 个任务，开始轮询...")
        for i, p, tid in tasks:
            print(f"  [{i}/{len(tasks)}] 等待生成...")
            url = poll(tid)
            download(url, os.path.join(out_dir, f"seg_{i:02d}.mp4"))
    else:
        for i, p in enumerate(prompts, 1):
            print(f"  [{i}/{len(prompts)}] 提交+等待: {p['prompt'][:40]}...")
            tid = submit(p["prompt"], duration)
            url = poll(tid)
            download(url, os.path.join(out_dir, f"seg_{i:02d}.mp4"))
    return sorted(os.path.join(out_dir, f"seg_{i:02d}.mp4")
                  for i in range(1, len(prompts) + 1))


if __name__ == "__main__":
    ep = sys.argv[1] if len(sys.argv) > 1 else "episodes/134"
    prompts_path = os.path.join(ep, "ai_video_prompts.json")
    prompts = json.load(open(prompts_path, encoding="utf-8"))
    out_dir = os.path.join(ep, "ai_video")
    which = sys.argv[2] if len(sys.argv) > 2 else "all"
    if which == "all":
        gen_sequence(prompts, out_dir)
    else:
        idx = int(which) - 1
        p = prompts[idx]
        print(f"生成单条 seg_{which}: {p['prompt'][:50]}")
        tid = submit(p["prompt"], 15)
        url = poll(tid)
        download(url, os.path.join(out_dir, f"seg_{int(which):02d}.mp4"))
