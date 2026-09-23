"""
智谱 CogVideoX AI 视频生成（GL-20260903）
cogvideox-flash 免费 / cogvideox-3 1元/次(10s 最高4K)
用法: python lib/gen_ai_video_zhipu.py <ep> [all|N] [model]
默认 model=cogvideox-flash（免费档）；付费档传 cogvideox-3
输出到 <ep>/ai_video/seg_XX.mp4（与 wan 版同目录同命名，assemble 通用）
"""
import sys, os, json, time
import requests

KEY = os.environ.get("ZHIPU_API_KEY", "") or open(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".env"),
    encoding="utf-8").read().split("ZHIPU_API_KEY=")[1].splitlines()[0].strip()

SUBMIT_URL = "https://open.bigmodel.cn/api/paas/v4/videos/generations"
RESULT_URL = "https://open.bigmodel.cn/api/paas/v4/async-result/{}"
DEFAULT_MODEL = "cogvideox-flash"

# GL-20260923: 忽略系统代理直连（Shadowrocket 残留的 127.0.0.1:1082 对智谱返回503）
_sess = requests.Session()
_sess.trust_env = False


def submit(prompt: str, model: str = DEFAULT_MODEL,
           size: str = "1920x1080", duration: int = 10) -> str:
    body = {"model": model, "prompt": prompt,
            "size": size, "duration": duration, "fps": 30,
            "with_audio": False}
    last = None
    for i in range(6):
        r = _sess.post(SUBMIT_URL,
                       headers={"Authorization": f"Bearer {KEY}",
                                "Content-Type": "application/json"},
                       json=body, timeout=60)
        if r.status_code == 429:  # 免费档限速：退避重试
            last = r
            wait = 20 * (i + 1)
            print(f"  ⏸ 限速(429)，{wait}s 后重试…")
            time.sleep(wait)
            continue
        r.raise_for_status()
        return r.json()["id"]
    raise requests.exceptions.HTTPError(f"429 限速重试仍失败: {last.text[:200]}")


def _get_retry(url, headers=None, timeout=30, tries=4):
    """GET 带重试：网络不稳时（Connection reset）自动重试。"""
    last = None
    for i in range(tries):
        try:
            return _sess.get(url, headers=headers, timeout=timeout)
        except requests.exceptions.RequestException as e:
            last = e
            if i < tries - 1:
                time.sleep(5 * (i + 1))
    raise last


def poll(task_id: str, timeout=900) -> str:
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = _get_retry(RESULT_URL.format(task_id),
                       headers={"Authorization": f"Bearer {KEY}"}, timeout=30)
        d = r.json()
        st = d.get("task_status", "")
        if st == "SUCCESS":
            # 新格式：video_result[].url；旧格式：content[].video_url/url
            for key in ("video_result", "content"):
                for c in (d.get(key) or []):
                    u = c.get("url") or c.get("video_url") or ""
                    if u:
                        return u
            raise RuntimeError(f"成功但无视频 URL: {json.dumps(d, ensure_ascii=False)[:300]}")
        elif st in ("FAIL", "FAILED"):
            raise RuntimeError(f"生成失败: {json.dumps(d, ensure_ascii=False)[:300]}")
        time.sleep(15)
    raise RuntimeError(f"超时: {task_id}")


def download(url: str, out_path: str):
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    data = _get_retry(url, timeout=180, tries=5).content
    with open(out_path, "wb") as f:
        f.write(data)
    print(f"  ✓ {os.path.basename(out_path)} ({len(data)//1024//1024:.1f}MB)")


def gen_sequence(prompts: list, out_dir: str, model: str = DEFAULT_MODEL,
                 duration: int = 10):
    os.makedirs(out_dir, exist_ok=True)
    # 全部先提交再逐个轮询
    tasks = []
    for i, p in enumerate(prompts, 1):
        print(f"  提交 [{i}/{len(prompts)}] ({model}): {p['prompt'][:30]}...")
        tid = submit(p["prompt"], model, duration=duration)
        tasks.append((i, p, tid))
        time.sleep(10)  # 免费档并发低，拉长提交间隔避免 429
    print(f"\n共 {len(tasks)} 个任务，开始轮询...")
    for i, p, tid in tasks:
        print(f"  [{i}/{len(tasks)}] 等待生成...")
        url = poll(tid)
        download(url, os.path.join(out_dir, f"seg_{i:02d}.mp4"))
    return sorted(os.path.join(out_dir, f"seg_{i:02d}.mp4")
                  for i in range(1, len(prompts) + 1))


if __name__ == "__main__":
    ep = sys.argv[1] if len(sys.argv) > 1 else "episodes/135"
    prompts = json.load(open(os.path.join(ep, "ai_video_prompts.json"), encoding="utf-8"))
    out_dir = os.path.join(ep, "ai_video")
    which = sys.argv[2] if len(sys.argv) > 2 else "all"
    model = sys.argv[3] if len(sys.argv) > 3 else DEFAULT_MODEL
    if which == "all":
        gen_sequence(prompts, out_dir, model)
    else:
        idx = int(which) - 1
        p = prompts[idx]
        print(f"生成单条 seg_{which} ({model}): {p['prompt'][:50]}")
        tid = submit(p["prompt"], model)
        url = poll(tid)
        download(url, os.path.join(out_dir, f"seg_{int(which):02d}.mp4"))
