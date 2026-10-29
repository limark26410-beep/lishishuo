"""
happyhorse i2v 图生视频（GL-20260903）
用首帧图片 + prompt 生成动态视频。免费额度（req_limit 5/并发）。
用法: python lib/gen_i2v_happyhorse.py <ep> <seg_no> <image_path> [output_name]

GL-20261013：统一走 Session(trust_env=False) 直连 —— 本机残留代理
（127.0.0.1:1082）会把请求转到不可达链路，与 gen_ai_video_zhipu.py 同一处理。
"""
import sys, os, json, time, base64
import requests

KEY = os.environ.get("DASHSCOPE_API_KEY", "") or open(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".env"),
    encoding="utf-8").read().split("DASHSCOPE_API_KEY=")[1].splitlines()[0].strip()

MODEL = "happyhorse-1.1-i2v"
SUBMIT_URL = "https://dashscope.aliyuncs.com/api/v1/services/aigc/video-generation/video-synthesis"
TASK_URL = "https://dashscope.aliyuncs.com/api/v1/tasks/{}"

# 忽略系统代理直连（与 gen_ai_video_zhipu.py 一致）
_sess = requests.Session()
_sess.trust_env = False


def _post(url, **kw):
    """POST 带重试：网络抖动时自动重连。"""
    last = None
    for i in range(4):
        try:
            return _sess.post(url, **kw)
        except requests.exceptions.RequestException as e:
            last = e
            if i < 3:
                time.sleep(5 * (i + 1))
    raise last


def _get(url, **kw):
    last = None
    for i in range(4):
        try:
            return _sess.get(url, **kw)
        except requests.exceptions.RequestException as e:
            last = e
            if i < 3:
                time.sleep(5 * (i + 1))
    raise last


def submit(prompt: str, image_path: str, duration: int = 15,
           size: str = "1280*720") -> str:
    ext = os.path.splitext(image_path)[1].lower().lstrip(".")
    if ext not in ("jpg", "jpeg", "png", "webp"):
        ext = "jpeg"
    b64 = base64.b64encode(open(image_path, "rb").read()).decode()
    body = {
        "model": MODEL,
        "input": {
            "prompt": prompt,
            "media": [{"type": "first_frame",
                       "url": f"data:image/{ext};base64,{b64}"}],
        },
        "parameters": {"size": size, "duration": duration,
                       "watermark": False},
    }
    r = _post(SUBMIT_URL,
              headers={"Authorization": f"Bearer {KEY}",
                       "Content-Type": "application/json",
                       "X-DashScope-Async": "enable"},
              json=body, timeout=60)
    if r.status_code != 200:
        raise RuntimeError(f"提交失败 HTTP {r.status_code}: {r.text[:300]}")
    return r.json()["output"]["task_id"]


def poll(task_id: str, timeout=900) -> str:
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = _get(TASK_URL.format(task_id),
                 headers={"Authorization": f"Bearer {KEY}"}, timeout=30)
        d = r.json()
        out = d.get("output", {})
        st = out.get("task_status", "")
        if st == "SUCCEEDED":
            url = out.get("video_url") or (out.get("results") or [{}])[0].get("url", "")
            return url
        elif st == "FAILED":
            raise RuntimeError(f"生成失败: {json.dumps(out, ensure_ascii=False)[:250]}")
        time.sleep(20)
    raise RuntimeError(f"超时: {task_id}")


def download(url: str, out_path: str):
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    data = _get(url, timeout=240).content
    if len(data) < 10000:
        raise RuntimeError(f"下载内容过小（{len(data)}B），可能不是视频: {url[:120]}")
    with open(out_path, "wb") as f:
        f.write(data)
    print(f"  ✓ {os.path.basename(out_path)} ({len(data)//1024//1024:.1f}MB)")


if __name__ == "__main__":
    ep = sys.argv[1]
    seg_no = int(sys.argv[2])
    image = sys.argv[3]
    name = sys.argv[4] if len(sys.argv) > 4 else f"seg_{seg_no:02d}"
    prompt = sys.argv[5] if len(sys.argv) > 5 else (
        "画面缓缓动起来：老者在酒坊中缓步走动查看酒坛，蒸汽袅袅，烛光摇曳，"
        "镜头缓慢推近，电影质感，中国古代酒坊")
    out = os.path.join(ep, "ai_video", f"{name}.mp4")
    print(f"提交 i2v: {image} -> {out}")
    tid = submit(prompt, image)
    print(f"task_id={tid}，轮询中...")
    url = poll(tid)
    download(url, out)
