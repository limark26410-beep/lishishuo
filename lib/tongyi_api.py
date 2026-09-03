"""
通义万相 API 接入模块
异步提交 + 轮询 + 并发限速 + 失败重试 + 断点续跑
使用 subprocess + curl（兼容 Python SSL 证书缺失的环境）
"""

import os, json, time, hashlib, subprocess, tempfile
from pathlib import Path
from typing import Optional
import concurrent.futures


API_BASE = "https://dashscope.aliyuncs.com/api/v1/services/aigc/text2image/image-synthesis"
MODEL = "wanx2.1-t2i-turbo"
BATCH_SIZE = 3
MAX_RETRIES = 3
POLL_INTERVAL = 10
TIMEOUT = 600  # 免费额度降速严重，单张最长等 10 分钟
STYLE_ANCHOR = "中国古风，水墨质感，纪录片氛围，无文字，场景宏大，写意风格"


def _curl_json(args: list, timeout: int = 30) -> dict:
    """调用 curl 并解析 JSON 返回"""
    cmd = ["curl", "-s", "--connect-timeout", "10", "--max-time", str(timeout)] + args
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 10)
    if result.returncode != 0:
        raise RuntimeError(f"curl failed (rc={result.returncode}): {result.stderr[:200]}")
    if not result.stdout.strip():
        raise RuntimeError("curl returned empty response")
    return json.loads(result.stdout)


class TongyiImageGen:
    """通义万相/qwen-image 文生图客户端（GL-20260902：支持 qwen-image-3.0 同步接口）"""

    def __init__(self, api_key: str = "", model: str = "", size: str = ""):
        self.api_key = api_key or os.environ.get("DASHSCOPE_API_KEY", "")
        if not self.api_key:
            raise ValueError("DASHSCOPE_API_KEY 未设置！")
        # model 为空时用模块级 MODEL（wanx）；config 可传 qwen-image-3.0 等
        self.model = model or MODEL
        self.size = size or "1080*1440"
        self.total_images = 0
        self.total_cost = 0.0
        self.failures = []

    def _is_qwen_image(self) -> bool:
        return self.model.startswith("qwen-image")

    def _qwen_submit_sync(self, prompt: str, output_path: str) -> bool:
        """qwen-image 同步接口：POST multimodal-generation，直接拿 URL 下载。
        用 requests（curl 走系统 SSL 可能证书失败；requests 正常）。"""
        import requests
        full_prompt = f"{prompt}，{STYLE_ANCHOR}"
        body = {
            "model": self.model,
            "input": {"messages": [{"role": "user",
                                    "content": [{"text": full_prompt}]}]},
            "parameters": {"size": self.size, "n": 1,
                           "prompt_extend": True, "watermark": False},
        }
        for attempt in range(MAX_RETRIES):
            try:
                resp = requests.post(
                    "https://dashscope.aliyuncs.com/api/v1/services/"
                    "aigc/multimodal-generation/generation",
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    json=body, timeout=240)
                if resp.status_code != 200:
                    msg = resp.text[:150]
                    print(f"  ⚠ qwen-image 提交失败 (try {attempt+1}/"
                          f"{MAX_RETRIES}): {msg}")
                    if "rate" in msg.lower() or "quota" in msg.lower():
                        time.sleep(8)
                    elif attempt < MAX_RETRIES - 1:
                        time.sleep(3)
                    continue
                d = resp.json()
                choices = (d.get("output") or {}).get("choices") or []
                if not choices:
                    print(f"  ⚠ qwen-image 空返回: {str(d)[:120]}")
                    if attempt < MAX_RETRIES - 1:
                        time.sleep(3)
                    continue
                content = choices[0].get("message", {}).get("content") or []
                img_url = next((c.get("image") for c in content
                                if c.get("image")), None)
                if not img_url:
                    print(f"  ⚠ qwen-image 无图片 URL")
                    return False
                dl = requests.get(img_url, timeout=90)
                if dl.status_code == 200 and len(dl.content) > 1000:
                    with open(output_path, "wb") as f:
                        f.write(dl.content)
                    print(f"  ✓ {os.path.basename(output_path)} "
                          f"({len(dl.content)/1024:.0f}KB)")
                    return True
                return False
            except Exception as e:
                print(f"  ⚠ qwen-image 异常 (try {attempt+1}/{MAX_RETRIES}): {e}")
                if attempt < MAX_RETRIES - 1:
                    time.sleep(4)
        return False

    def _submit(self, prompt: str) -> Optional[str]:
        if self._is_qwen_image():
            # qwen-image 走同步（返回 None task 表示同步完成），
            # 由 batch_generate 的同步路径处理；这里抛给调用方分支
            raise RuntimeError("qwen-image 应走 _gen_sync_path")
        full_prompt = f"{prompt}，{STYLE_ANCHOR}"
        data = json.dumps({
            "model": MODEL,
            "input": {"prompt": full_prompt},
            "parameters": {"size": "1080*1440", "n": 1},
        })

        for attempt in range(MAX_RETRIES):
            try:
                result = _curl_json([
                    "-X", "POST",
                    "-H", f"Authorization: Bearer {self.api_key}",
                    "-H", "Content-Type: application/json",
                    "-H", "X-DashScope-Async: enable",
                    "-d", data,
                    API_BASE,
                ], timeout=30)
                task_id = result.get("output", {}).get("task_id")
                if task_id:
                    return task_id
                msg = result.get('message', '') or result.get('code', '')
                print(f"  ⚠ 提交失败 (try {attempt+1}/{MAX_RETRIES}): {msg[:60]}")
                if 'rate limit' in msg.lower():
                    time.sleep(5)
                elif attempt < MAX_RETRIES - 1:
                    time.sleep(2)
            except Exception as e:
                print(f"  ⚠ 提交异常 (try {attempt+1}/{MAX_RETRIES}): {e}")
                if attempt < MAX_RETRIES - 1:
                    time.sleep(3)
        return None

    def _poll_and_download(self, task_id: str, output_path: str) -> bool:
        deadline = time.time() + TIMEOUT
        while time.time() < deadline:
            try:
                result = _curl_json([
                    "-H", f"Authorization: Bearer {self.api_key}",
                    f"https://dashscope.aliyuncs.com/api/v1/tasks/{task_id}",
                ], timeout=15)
                status = result.get("output", {}).get("task_status", "")

                if status == "SUCCEEDED":
                    url = result.get("output", {}).get("results", [{}])[0].get("url", "")
                    if url:
                        dl = subprocess.run([
                            "curl", "-sL", "--connect-timeout", "30",
                            "--max-time", "60", url, "-o", output_path,
                        ], capture_output=True, text=True, timeout=90)
                        if os.path.exists(output_path) and os.path.getsize(output_path) > 1000:
                            sz = os.path.getsize(output_path)
                            print(f"  \u2713 {Path(output_path).name} ({sz/1024:.0f}KB)")
                            return True
                    return False
                elif status == "FAILED":
                    msg = result.get("output", {}).get("message", "")
                    print(f"  \u2717 任务失败: {msg[:60]}")
                    return False
                time.sleep(POLL_INTERVAL)
            except Exception as e:
                print(f"  \u26a0 轮询异常: {e}")
                time.sleep(POLL_INTERVAL)

        print(f"  \u2717 超时 ({TIMEOUT}s)")
        return False

    def batch_generate(self, prompts: list, output_dir: str,
                       batch_size: int = BATCH_SIZE) -> dict:
        os.makedirs(output_dir, exist_ok=True)
        self.failures = []
        image_map = {}
        results = []

        print(f"\n{'='*50}")
        print(f"生图：{len(prompts)} 条提示词，并发 {batch_size}"
              f"（模型 {self.model}）")
        print(f"{'='*50}")

        if self._is_qwen_image():
            # ── qwen-image 同步路径：逐个生成（无 task 轮询）──
            for i, prompt in enumerate(prompts):
                print(f"  [{i+1}/{len(prompts)}] 生成: {prompt[:40]}...")
                ph = hashlib.sha1(prompt.encode()).hexdigest()[:8]
                out = os.path.join(output_dir, f"img_{i:03d}_{ph}.jpg")
                if os.path.exists(out) and os.path.getsize(out) > 1000:
                    print(f"  ✓ 已存在，跳过: {Path(out).name}")
                    image_map[i] = out
                    results.append({"index": i, "prompt": prompt,
                                    "path": out, "task_id": ""})
                    continue
                ok = self._qwen_submit_sync(prompt, out)
                if ok:
                    image_map[i] = out
                    results.append({"index": i, "prompt": prompt,
                                    "path": out, "task_id": ""})
                else:
                    self.failures.append(i)
                    image_map[i] = None
                if (i + 1) % batch_size == 0 and i < len(prompts) - 1:
                    print(f"  ⏸ 暂停 3s 限速...")
                    time.sleep(3)
            success = sum(1 for v in image_map.values() if v is not None)
            self.total_images += success
            print(f"\n生图完成：成功 {success}/{len(prompts)}")
            if self.failures:
                print(f"  失败: {self.failures}")
            return {"total": len(prompts), "success": success,
                    "failures": self.failures, "image_map": image_map,
                    "results": results}

        # 提交所有任务（逐个，控制并发）
        tasks = []
        for i, prompt in enumerate(prompts):
            print(f"  [{i+1}/{len(prompts)}] \u63d0\u4ea4: {prompt[:40]}...")
            task_id = self._submit(prompt)
            if task_id:
                tasks.append((i, prompt, task_id))
            else:
                self.failures.append(i)
                image_map[i] = None
            if (i + 1) % batch_size == 0 and i < len(prompts) - 1:
                print(f"  \u23f8 \u6682\u505c 5s \u9650\u901f...")
                time.sleep(5)

        # 轮询 + 下载
        print(f"\n\u8f6e\u8be2 {len(tasks)} \u4e2a\u4efb\u52a1...")
        for idx, prompt, task_id in tasks:
            ph = hashlib.sha1(prompt.encode()).hexdigest()[:8]
            out = os.path.join(output_dir, f"img_{idx:03d}_{ph}.jpg")

            if os.path.exists(out) and os.path.getsize(out) > 1000:
                print(f"  [{idx+1}] \u5df2\u5b58\u5728\uff0c\u8df3\u8fc7: {Path(out).name}")
                image_map[idx] = out
                results.append({"index": idx, "prompt": prompt, "path": out, "task_id": task_id})
                continue

            print(f"  [{idx+1}] \u7b49\u5f85...", end="", flush=True)
            ok = self._poll_and_download(task_id, out)
            if ok:
                image_map[idx] = out
                results.append({"index": idx, "prompt": prompt, "path": out, "task_id": task_id})
            else:
                # 超时后重新提交
                print(f"  \u2192 \u91cd\u65b0\u63d0\u4ea4\u4efb\u52a1 [{idx+1}]...", end="", flush=True)
                new_task_id = self._submit(prompt)
                if new_task_id:
                    print(f"  [{idx+1}] \u7b49\u5f85(\u91cd\u8bd5)...", end="", flush=True)
                    ok = self._poll_and_download(new_task_id, out)
                    if ok:
                        image_map[idx] = out
                        results.append({"index": idx, "prompt": prompt, "path": out, "task_id": new_task_id})
                        continue
                self.failures.append(idx)
                image_map[idx] = None
                print()

        success = sum(1 for v in image_map.values() if v is not None)
        self.total_images += success
        print(f"\n\u751f\u56fe\u5b8c\u6210\uff1a\u6210\u529f {success}/{len(prompts)}")
        if self.failures:
            print(f"  \u5931\u8d25: {self.failures}")

        return {
            "total": len(prompts),
            "success": success,
            "failures": self.failures,
            "image_map": image_map,
            "results": results,
        }
