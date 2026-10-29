# -*- coding: utf-8 -*-
"""首帧合成（GL-20261013）：让「角色四视图」真正进入画面，解决人物一致性。

背景
----
智谱 CogVideoX 是**文生视频**，喂不进参考图，角色长相全靠提示词描述，
每段生成的同一个人长得都不一样。要保一致性，必须拆成两步：

    四视图 ──[本模块]──> 角色在场景中的首帧图 ──[i2v]──> 视频段

本模块负责第一步：把「角色全身立绘 + 场景图」一起喂给 qwen-image-3.0-pro，
由模型把角色合成进场景，输出一张构图完整的首帧。

用法（Python）
--------------
    from gen_first_frame import compose_first_frame
    compose_first_frame(
        prompt="一位四十多岁的南方客商背着军绿色旧帆布书包，正跨过北京胡同小院的木门槛；"
               "旁边一位中年男性侧身让路。傍晚暖光，纪实电影质感。",
        char_refs=["首帧素材/客商_全身正面.png", "首帧素材/二哥_全身正面.png"],
        scene_ref="场景/老院子四视图.png",
        out_path="首帧/S07.png",
    )

用法（CLI）
----------
    ./venv/bin/python lib/gen_first_frame.py <项目目录> [镜号|all]

依赖
----
DASHSCOPE_API_KEY（.env）。走 curl 而不是 requests —— 本机 Python 的 TLS
握手对阿里云域名会超时，curl 正常（与 tongyi_api.py 同一处理）。
"""
from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from paths import load_env  # noqa: E402

load_env()

API_URL = ("https://dashscope.aliyuncs.com/api/v1/services/"
           "aigc/multimodal-generation/generation")

# 可用 qwen-image 家族（GL-20261013 实测自 /compatible-mode/v1/models）：
#   qwen-image-3.0-pro ← 默认（最强）
#   qwen-image-3.0 / qwen-image-2.1-pro / qwen-image-2.0-pro / wan2.7-image-pro
# 注：清单里没有 qwen-image-edit，编辑能力由 qwen-image-3.0 系列承担。
DEFAULT_MODEL = os.environ.get("FIRST_FRAME_MODEL", "qwen-image-3.0-pro")
MODEL_FALLBACKS = ["qwen-image-3.0", "qwen-image-2.1-pro", "wan2.7-image-pro"]
MAX_RETRIES = 5          # 本机出口到阿里云是间歇性可用的，多试几次
TIMEOUT = 300

QUOTA_HINT = (
    "\n  ⛔ 免费额度耗尽（AllocationQuota.FreeTierOnly）。\n"
    "     这不是代码问题，是账户问题，二选一：\n"
    "       A. 去阿里云百炼控制台充值：https://bailian.console.aliyun.com\n"
    "       B. 或关闭「仅使用免费额度」模式（改用付费额度）\n"
    "     修复后重跑本条命令即可。\n")

# 竖屏/横屏：本项目走横屏 16:9（与 assemble_ai_video.py 的 1920x1080 一致）
DEFAULT_SIZE = os.environ.get("FIRST_FRAME_SIZE", "1664*928")

NEGATIVE = ("模糊，低清，变形，多手多脚，五官崩坏，文字，水印，logo，现代品牌标识，"
            "画面分割，拼图，多格，四宫格，分镜图，设定图，角色多视图，白底证件照，抠图感")


def _key() -> str:
    k = os.environ.get("DASHSCOPE_API_KEY", "").strip()
    if not k:
        raise RuntimeError("DASHSCOPE_API_KEY 未设置！请在 .env 配置")
    return k


def _data_url(path: str) -> str:
    p = Path(path).expanduser()
    if not p.exists():
        raise FileNotFoundError(f"参考图不存在: {p}")
    ext = p.suffix.lower().lstrip(".") or "png"
    if ext == "jpg":
        ext = "jpeg"
    b = base64.b64encode(p.read_bytes()).decode()
    return f"data:image/{ext};base64,{b}"


def _curl_post(url: str, body: dict, timeout: int = TIMEOUT) -> dict:
    """curl POST（绕开本机 Python SSL 握手超时）。body 走临时文件，避免 argv 过长。"""
    tmp = Path(os.environ.get("TMPDIR", "/tmp")) / f"_ff_body_{os.getpid()}.json"
    tmp.write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")
    try:
        r = subprocess.run(
            ["curl", "-s", "--connect-timeout", "15", "--max-time", str(timeout),
             url,
             "-H", f"Authorization: Bearer {_key()}",
             "-H", "Content-Type: application/json",
             "--data-binary", f"@{tmp}"],
            capture_output=True, text=True, timeout=timeout + 30)
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass
    if r.returncode != 0:
        raise RuntimeError(f"curl 失败 (rc={r.returncode}): {r.stderr[:200]}")
    if not r.stdout.strip():
        raise RuntimeError("curl 返回空响应")
    return json.loads(r.stdout)


def _download(url: str, out_path: str) -> bool:
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(
        ["curl", "-sL", "--connect-timeout", "20", "--max-time", "180",
         url, "-o", out_path],
        capture_output=True, text=True, timeout=240)
    if r.returncode != 0:
        print(f"  ⚠ 下载失败: {r.stderr[:150]}")
        return False
    return os.path.exists(out_path) and os.path.getsize(out_path) > 1000


def _crop_scene_panel(path: str, panel: int, cols: int, rows: int) -> str:
    """把拼版场景图切成单格后再当参考图。

    GL-20261013 实测踩坑：直接传 2x2 四视图当参考，qwen-image 会"照着参考图的
    结构"也输出四格拼图（S06/S07/S09 中招）。切单格后模型只看到一张完整画面。
    """
    if panel < 0:
        return path
    from PIL import Image
    im = Image.open(path)
    W, H = im.size
    cw, ch = W // cols, H // rows
    c, r = panel % cols, panel // cols
    out = Path(os.environ.get("TMPDIR", "/tmp")) / f"_scene_p{panel}.jpg"
    im.crop((c * cw, r * ch, (c + 1) * cw, (r + 1) * ch)).convert("RGB").save(
        out, quality=92)
    return str(out)


def compose_first_frame(prompt: str, char_refs: list, scene_ref: str = "",
                        out_path: str = "", model: str = "",
                        size: str = "", negative: str = "",
                        scene_panel: int = -1, scene_cols: int = 2,
                        scene_rows: int = 2) -> str:
    """合成首帧。成功返回输出路径，失败抛异常。

    Args:
        prompt:     合成指令（描述谁在什么场景做什么，含构图/光线/质感）
        char_refs:  角色全身立绘路径列表（每张只含一个人）
        scene_ref:  场景参考图路径（可空）
        out_path:   输出图路径
        model:      覆盖默认模型
        size:       覆盖默认尺寸（如 1664*928 / 928*1664）
    """
    model = model or DEFAULT_MODEL
    size = size or DEFAULT_SIZE
    neg = negative or NEGATIVE

    if scene_ref and scene_panel >= 0:
        scene_ref = _crop_scene_panel(scene_ref, scene_panel, scene_cols, scene_rows)

    content = []
    for i, c in enumerate(char_refs):
        content.append({"image": _data_url(c)})
    if scene_ref:
        content.append({"image": _data_url(scene_ref)})

    # 把「每张参考图是什么」说清楚，模型才知道该用哪张
    legend = []
    for i, c in enumerate(char_refs):
        legend.append(f"参考图{i+1}（{Path(c).stem}）是需要保留长相与服装的人物")
    if scene_ref:
        legend.append(f"参考图{len(char_refs)+1}（{Path(scene_ref).stem}）是场景环境")
    full_prompt = ("\n".join(legend) + "\n\n" + prompt +
                   "\n\n要求：人物的面部特征、发型、服装必须与参考图一致；"
                   "场景的空间结构、陈设、色调与场景参考图一致；"
                   "输出的是一张完整的电影感画面，不是拼图或设定图。")
    content.append({"text": full_prompt})

    body = {
        "model": model,
        "input": {"messages": [{"role": "user", "content": content}]},
        "parameters": {"negative_prompt": neg, "watermark": False,
                       "prompt_extend": True, "size": size},
    }

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            d = _curl_post(API_URL, body)
        except Exception as e:  # noqa: BLE001
            print(f"  ⚠ 请求异常 ({attempt}/{MAX_RETRIES}): {e}")
            time.sleep(4 * attempt)
            continue

        if "code" in d and d.get("code"):
            code = str(d.get("code", ""))
            msg = str(d.get("message", d))[:200]
            # 额度问题：立刻停，别重试（重试也不会好）
            if "Quota" in code or "quota" in msg.lower() or "FreeTier" in code:
                raise RuntimeError(f"{code}: {msg}{QUOTA_HINT}")
            if "Arrearage" in code or "overdue" in msg.lower():
                raise RuntimeError(f"{code}: {msg}\n  ⛔ 账户欠费，请先结清。")
            print(f"  ⚠ 接口报错 ({attempt}/{MAX_RETRIES}): {code} {msg}")
            if "model" in msg.lower() and ("not" in msg.lower() or "exist" in msg.lower()):
                raise RuntimeError(
                    f"模型 {model} 不可用。候选："
                    + " / ".join([model] + MODEL_FALLBACKS) +
                    "\n  用 FIRST_FRAME_MODEL=xxx 指定，或看 --list-models")
            time.sleep(4 * attempt)
            continue

        choices = (d.get("output") or {}).get("choices") or []
        if not choices:
            print(f"  ⚠ 空返回 ({attempt}/{MAX_RETRIES}): {str(d)[:150]}")
            time.sleep(4 * attempt)
            continue

        url = next((c.get("image") for c in
                    (choices[0].get("message", {}).get("content") or [])
                    if c.get("image")), None)
        if not url:
            print(f"  ⚠ 无图片 URL ({attempt}/{MAX_RETRIES})")
            time.sleep(4 * attempt)
            continue

        if _download(url, out_path):
            print(f"  ✓ 首帧 {Path(out_path).name} "
                  f"({os.path.getsize(out_path)/1024:.0f}KB)")
            return out_path
        time.sleep(3)

    raise RuntimeError(f"首帧合成失败（重试 {MAX_RETRIES} 次）: {out_path}")


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    proj = Path(sys.argv[1]).expanduser().resolve()
    which = sys.argv[2] if len(sys.argv) > 2 else "all"

    cfg = json.loads((proj / "project.json").read_text(encoding="utf-8"))
    scenes = cfg["scenes"]
    ff_dir = proj / "首帧"
    ff_dir.mkdir(exist_ok=True)

    todo = [(i, s) for i, s in enumerate(scenes, 1) if s.get("first_frame")]
    if which != "all":
        n = int(which)
        todo = [(n, scenes[n - 1])]
    if not todo:
        print("⚠ project.json 里没有任何场次配置 first_frame，无需合成")
        return

    print(f"▶ 首帧合成：{len(todo)} 镜（模型 {DEFAULT_MODEL}）")
    ok = 0
    for i, s in todo:
        ff = s["first_frame"]
        out = ff_dir / f"S{i:02d}.png"
        txt = (ff.get("prompt") or s.get("prompt") or "").strip()
        try:
            compose_first_frame(
                prompt=txt,
                char_refs=[str((proj / p).resolve()) for p in ff.get("chars", [])],
                scene_ref=str((proj / ff["scene"]).resolve()) if ff.get("scene") else "",
                out_path=str(out),
                scene_panel=ff.get("scene_panel", -1),
                scene_cols=ff.get("scene_cols", 2),
                scene_rows=ff.get("scene_rows", 2))
            ok += 1
        except Exception as e:  # noqa: BLE001
            print(f"  ✗ S{i:02d} 失败: {e}")
    print(f"\n完成：{ok}/{len(todo)}")


if __name__ == "__main__":
    main()
