"""
通义万相 API 接入模块
异步提交 + 轮询 + 并发限速 + 失败重试 + 断点续跑
"""

import os
import json
import time
import hashlib
import requests
from pathlib import Path
from typing import Optional


# === 配置 ===
API_BASE = os.environ.get(
    "TONGYI_API_BASE",
    "https://dashscope.aliyuncs.com/api/v1/services/aigc/text2image/image-synthesis"
)
DASHSCOPE_API_KEY = os.environ.get("DASHSCOPE_API_KEY", "")

MODEL = "wanx2.1-t2i-turbo"      # 通义万相快速版
SIZE = "1080x1920"                # 9:16 竖屏
BATCH_SIZE = 5                    # 并发提交数
MAX_RETRIES = 3
POLL_INTERVAL = 5                 # 轮询间隔秒
TIMEOUT = 120                     # 单张超时秒
STYLE_ANCHOR = "中国古风，水墨质感，纪录片氛围，无文字，场景宏大，写意风格"


def _sha1(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


class TongyiImageGen:
    """
    通义万相文生图客户端
    用法：
        client = TongyiImageGen(api_key="xxx")
        results = client.batch_generate(prompts, output_dir="images/")
    """

    def __init__(self, api_key: str = ""):
        self.api_key = api_key or DASHSCOPE_API_KEY
        if not self.api_key:
            raise ValueError(
                "DASHSCOPE_API_KEY 未设置！"
                "请通过环境变量或构造参数传入"
            )

        self.headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "X-DashScope-Async": "enable",
        }

        # 成本统计
        self.total_images = 0
        self.total_cost = 0.0   # 预估，通义万相免费期内为 0
        self.failures = []

    def _build_prompt(self, prompt: str) -> str:
        """追加风格锚定词"""
        return f"{prompt}，{STYLE_ANCHOR}"

    def _submit(self, prompt: str) -> Optional[str]:
        """提交单张生图任务，返回 task_id"""
        data = {
            "model": MODEL,
            "input": {
                "prompt": self._build_prompt(prompt),
            },
            "parameters": {
                "size": SIZE,
                "n": 1,
            }
        }

        for attempt in range(MAX_RETRIES):
            try:
                resp = requests.post(
                    API_BASE,
                    headers=self.headers,
                    json=data,
                    timeout=30,
                )
                if resp.status_code == 200:
                    result = resp.json()
                    task_id = result.get("output", {}).get("task_id")
                    if task_id:
                        return task_id

                # 限频 429 时退避
                if resp.status_code == 429:
                    wait = 2 ** (attempt + 1)
                    print(f"  ⚠ Rate limited, retrying in {wait}s...")
                    time.sleep(wait)
                    continue

                print(f"  ⚠ Submit failed (HTTP {resp.status_code}): "
                      f"{resp.text[:200]}")
                if attempt < MAX_RETRIES - 1:
                    time.sleep(2 ** attempt)

            except requests.RequestException as e:
                print(f"  ⚠ Submit error: {e}")
                if attempt < MAX_RETRIES - 1:
                    time.sleep(2 ** attempt)

        return None

    def _poll(self, task_id: str) -> Optional[str]:
        """轮询任务直到完成，返回图片 URL"""
        url = f"{API_BASE}/task/{task_id}"
        deadline = time.time() + TIMEOUT

        while time.time() < deadline:
            try:
                resp = requests.get(url, headers=self.headers, timeout=15)
                if resp.status_code == 200:
                    data = resp.json()
                    status = data.get("output", {}).get("task_status", "")

                    if status == "SUCCEEDED":
                        results = data.get("output", {}).get("results", [])
                        if results:
                            return results[0].get("url")

                    elif status in ("FAILED", "CANCELED"):
                        msg = data.get("output", {}).get("message", "")
                        print(f"  ⚠ Task failed/canceled: {msg}")
                        return None

                    # RUNNING / PENDING - 继续轮询
                    time.sleep(POLL_INTERVAL)
                else:
                    print(f"  ⚠ Poll HTTP {resp.status_code}, retrying...")
                    time.sleep(2)

            except requests.RequestException as e:
                print(f"  ⚠ Poll error: {e}")
                time.sleep(2)

        print(f"  ⚠ Poll timeout ({TIMEOUT}s)")
        return None

    def _download(self, url: str, output_path: str) -> bool:
        """下载图片到本地"""
        for attempt in range(MAX_RETRIES):
            try:
                resp = requests.get(url, timeout=60)
                if resp.status_code == 200:
                    with open(output_path, "wb") as f:
                        f.write(resp.content)
                    size_kb = len(resp.content) / 1024
                    print(f"  ✓ Downloaded: {Path(output_path).name} ({size_kb:.0f}KB)")
                    return True
            except requests.RequestException as e:
                print(f"  ⚠ Download error: {e}")
                if attempt < MAX_RETRIES - 1:
                    time.sleep(2)
        return False

    def batch_generate(
        self,
        prompts: list,
        output_dir: str,
        batch_size: int = BATCH_SIZE,
        style_anchor: bool = True,
    ) -> dict:
        """
        批量生图
        - 一次性提交全部 task_id（并发控制 batch_size）
        - 再统一轮询
        - 失败重试 + 单张失败不影响其他
        返回 { results: [url, ...], failures: [idx, ...], image_map: {0: path, ...} }
        """
        if style_anchor:
            # 切换风格锚定
            global STYLE_ANCHOR
            STYLE_ANCHOR = os.environ.get(
                "STYLE_ANCHOR",
                "中国古风，水墨质感，纪录片氛围，无文字，场景宏大，写意风格"
            )

        os.makedirs(output_dir, exist_ok=True)

        self.failures = []
        image_map = {}
        submitted = []  # [(index, task_id, prompt_hash), ...]

        print(f"\n{'='*50}")
        print(f"生图开始：共 {len(prompts)} 条提示词")
        print(f"并发: {batch_size}, 模型: {MODEL}")
        print(f"{'='*50}")

        # Phase 1: 批量提交
        for i, prompt in enumerate(prompts):
            ph = _sha1(prompt)[:8]
            print(f"  [{i+1}/{len(prompts)}] 提交: {prompt[:50]}...")

            task_id = self._submit(prompt)
            if task_id:
                submitted.append((i, task_id, ph))
            else:
                self.failures.append(i)
                image_map[i] = None

            # 限速：每 BATCH_SIZE 个等一下
            if (i + 1) % batch_size == 0 and i < len(prompts) - 1:
                print(f"  ⏸ 已提交 {i+1}，暂停 3s 限速...")
                time.sleep(3)

        # Phase 2: 统一轮询
        print(f"\n轮询中：{len(submitted)} 个任务...")
        for idx, task_id, ph in submitted:
            output_path = os.path.join(output_dir, f"img_{idx:03d}_{ph}.jpg")

            # 断点续跑：如果文件已存在则跳过
            if os.path.exists(output_path) and os.path.getsize(output_path) > 1024:
                print(f"  [{idx+1}] 已存在，跳过: img_{idx:03d}_{ph}.jpg")
                image_map[idx] = output_path
                continue

            print(f"  [{idx+1}] 轮询 task_{task_id[:8]}...", end="", flush=True)
            url = self._poll(task_id)

            if url:
                ok = self._download(url, output_path)
                if ok:
                    image_map[idx] = output_path
                else:
                    self.failures.append(idx)
                    image_map[idx] = None
            else:
                self.failures.append(idx)
                image_map[idx] = None
                print("  ✗ failed")

        # 统计
        success = sum(1 for v in image_map.values() if v is not None)
        self.total_images += success
        print(f"\n{'='*50}")
        print(f"生图完成：成功 {success}/{len(prompts)}")
        if self.failures:
            print(f"失败索引: {self.failures}")
        print(f"{'='*50}")

        return {
            "total": len(prompts),
            "success": success,
            "failures": self.failures,
            "image_map": image_map,
        }
