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
BATCH_SIZE = 3              # 低并发，避免超时
MAX_RETRIES = 2
POLL_INTERVAL = 5
TIMEOUT = 120
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
    """通义万相文生图客户端"""

    def __init__(self, api_key: str = ""):
        self.api_key = api_key or os.environ.get("DASHSCOPE_API_KEY", "")
        if not self.api_key:
            raise ValueError("DASHSCOPE_API_KEY 未设置！")
        self.total_images = 0
        self.total_cost = 0.0
        self.failures = []

    def _submit(self, prompt: str) -> Optional[str]:
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
                print(f"  ⚠ No task_id: {result.get('message', '')}")
            except Exception as e:
                print(f"  ⚠ Submit error (attempt {attempt+1}): {e}")
                if attempt < MAX_RETRIES - 1:
                    time.sleep(2)
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
                            print(f"  ✓ {Path(output_path).name} ({sz/1024:.0f}KB)")
                            return True
                    return False
                elif status == "FAILED":
                    msg = result.get("output", {}).get("message", "")
                    print(f"  ✗ Task failed: {msg}")
                    return False
                time.sleep(POLL_INTERVAL)
            except Exception as e:
                print(f"  ⚠ Poll error: {e}")
                time.sleep(POLL_INTERVAL)
        print(f"  ✗ Poll timeout")
        return False

    def batch_generate(self, prompts: list, output_dir: str,
                       batch_size: int = BATCH_SIZE) -> dict:
        os.makedirs(output_dir, exist_ok=True)
        self.failures = []
        image_map = {}
        results = []

        print(f"\n{'='*50}")
        print(f"生图：{len(prompts)} 条提示词，并发 {batch_size}")
        print(f"{'='*50}")

        # 提交所有任务（逐个，控制并发）
        tasks = []  # [(idx, prompt, task_id)]
        for i, prompt in enumerate(prompts):
            print(f"  [{i+1}/{len(prompts)}] 提交: {prompt[:40]}...")
            task_id = self._submit(prompt)
            if task_id:
                tasks.append((i, prompt, task_id))
            else:
                self.failures.append(i)
                image_map[i] = None
            # 限速
            if (i + 1) % batch_size == 0 and i < len(prompts) - 1:
                print(f"  ⏸ 暂停 3s 限速...")
                time.sleep(3)

        # 轮询 + 下载
        print(f"\n轮询 {len(tasks)} 个任务...")
        for idx, prompt, task_id in tasks:
            ph = hashlib.sha1(prompt.encode()).hexdigest()[:8]
            out = os.path.join(output_dir, f"img_{idx:03d}_{ph}.jpg")

            # 断点续跑
            if os.path.exists(out) and os.path.getsize(out) > 1000:
                print(f"  [{idx+1}] 已存在，跳过: {Path(out).name}")
                image_map[idx] = out
                continue

            print(f"  [{idx+1}] 等待...", end="", flush=True)
            ok = self._poll_and_download(task_id, out)
            if ok:
                image_map[idx] = out
                results.append({
                    "index": idx,
                    "prompt": prompt,
                    "path": out,
                    "task_id": task_id,
                })
            else:
                self.failures.append(idx)
                image_map[idx] = None
                print()

        success = sum(1 for v in image_map.values() if v is not None)
        self.total_images += success
        print(f"\n生图完成：成功 {success}/{len(prompts)}")
        if self.failures:
            print(f"  失败: {self.failures}")

        return {
            "total": len(prompts),
            "success": success,
            "failures": self.failures,
            "image_map": image_map,
            "results": results,
        }
