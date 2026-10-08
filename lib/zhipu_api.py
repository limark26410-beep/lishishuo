# -*- coding: utf-8 -*-
"""智谱 CogView-4 生图适配：接口对齐 TongyiImageGen.batch_generate。

run.py 无需大改——step_image_gen 里把 TongyiImageGen 换成 ZhipuImageGen
即可（同一 batch_generate 契约：返回 {total, success, failures, image_map, results}）。

智谱 CogView-4 同步返回图片 URL，逐个生成 + 下载，失败重试。
费用：走智谱账户额度（新用户有免费资源包）。
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

API_BASE = "https://open.bigmodel.cn/api/paas/v4/images/generations"
MODEL = "cogview-4-250304"
BATCH_SIZE = 1   # 智谱生图较慢，串行更稳
MAX_RETRIES = 3
TIMEOUT = 120    # 单张最长等 2 分钟（同步返回通常几秒）

# 历史国风风格锚（与通义风格接近，保证系列一致）
STYLE_ANCHOR = "中国古风，水墨质感，纪录片氛围，无文字，场景宏大，写意风格，9:16竖版构图"


def _curl_json(args: list, timeout: int = 30) -> dict:
    cmd = ["curl", "-s", "--connect-timeout", "10", "--max-time", str(timeout)] + args
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 10)
    if result.returncode != 0:
        raise RuntimeError(f"curl failed (rc={result.returncode}): {result.stderr[:200]}")
    if not result.stdout.strip():
        raise RuntimeError("curl returned empty response")
    return json.loads(result.stdout)


class ZhipuImageGen:
    """智谱 CogView-4 文生图客户端（兼容 TongyiImageGen.batch_generate 契约）"""

    def __init__(self, api_key: str = "", model: str = "", size: str = ""):
        self.api_key = api_key or os.environ.get("ZHIPU_API_KEY", "")
        if not self.api_key:
            raise ValueError("ZHIPU_API_KEY 未设置！请在 .env 配置智谱 API key")
        self.model = model or MODEL
        self.size = size or "1024x1024"   # CogView-4 支持 1024x1024；竖版可传 768x1344
        self.total_images = 0
        self.total_cost = 0.0
        self.failures = []

    def _generate_one(self, prompt: str, output_path: str) -> bool:
        """生成一张并下载到 output_path，成功返回 True。"""
        full_prompt = f"{prompt}，{STYLE_ANCHOR}"
        data = json.dumps({
            "model": self.model,
            "prompt": full_prompt,
            "size": self.size,
        }, ensure_ascii=False)

        for attempt in range(MAX_RETRIES):
            try:
                payload_file = "/tmp/_zhipu_payload.json"
                Path(payload_file).write_text(data, encoding="utf-8")
                result = _curl_json([
                    "-X", "POST",
                    "-H", f"Authorization: Bearer {self.api_key}",
                    "-H", "Content-Type: application/json",
                    "-d", "@" + payload_file,
                    API_BASE,
                ], timeout=60)
                urls = result.get("data", [])
                if not urls or "url" not in urls[0]:
                    msg = str(result)[:200]
                    print(f"  ⚠ 无图片URL (try {attempt+1}/{MAX_RETRIES}): {msg[:120]}")
                    time.sleep(2)
                    continue
                url = urls[0]["url"]
                # 下载
                dl = subprocess.run([
                    "curl", "-sL", "--connect-timeout", "20",
                    "--max-time", "90", url, "-o", output_path,
                ], capture_output=True, text=True, timeout=120)
                if os.path.exists(output_path) and os.path.getsize(output_path) > 1000:
                    sz = os.path.getsize(output_path)
                    print(f"  ✓ {Path(output_path).name} ({sz/1024:.0f}KB)")
                    return True
                print(f"  ⚠ 下载失败 (try {attempt+1}/{MAX_RETRIES})")
                time.sleep(2)
            except Exception as e:
                msg = str(e)
                # 额度不足识别
                if "quota" in msg.lower() or "resource" in msg.lower() or "额度" in msg:
                    print(f"  ⚠ 智谱额度/资源问题: {msg[:150]}")
                    return False
                print(f"  ⚠ 提交异常 (try {attempt+1}/{MAX_RETRIES}): {str(e)[:120]}")
                if attempt < MAX_RETRIES - 1:
                    time.sleep(3)
        return False

    def batch_generate(self, prompts: list, output_dir: str,
                       batch_size: int = BATCH_SIZE) -> dict:
        """与 TongyiImageGen.batch_generate 同契约。"""
        os.makedirs(output_dir, exist_ok=True)
        self.failures = []
        image_map = {}
        results = []

        print(f"\n{'='*50}")
        print(f"生图：{len(prompts)} 条提示词（智谱 {self.model}，串行）")
        print(f"{'='*50}")

        for i, prompt in enumerate(prompts):
            print(f"  [{i+1}/{len(prompts)}] 生成: {prompt[:40]}...", end="", flush=True)
            ph = hashlib.sha1(prompt.encode()).hexdigest()[:8]
            out = os.path.join(output_dir, f"img_{i:03d}_{ph}.jpg")
            if os.path.exists(out) and os.path.getsize(out) > 1000:
                print(f"  ✓ 已存在，跳过: {Path(out).name}")
                image_map[i] = out
                results.append({"index": i, "prompt": prompt, "path": out})
                continue
            ok = self._generate_one(prompt, out)
            if ok:
                image_map[i] = out
                results.append({"index": i, "prompt": prompt, "path": out})
            else:
                self.failures.append(i)
                image_map[i] = None
            if (i + 1) % 3 == 0 and i < len(prompts) - 1:
                print("  ⏸ 暂停 2s 限速...")
                time.sleep(2)

        success = sum(1 for v in image_map.values() if v is not None)
        self.total_images += success
        print(f"\n生图完成：成功 {success}/{len(prompts)}")
        if self.failures:
            print(f"  失败: {self.failures}")
        return {"total": len(prompts), "success": success,
                "failures": self.failures, "image_map": image_map,
                "results": results}
