#!/usr/bin/env python3
"""
批量抠图工具（rembg 封装）—— 可复用模块，供其他工具 import
GL-20260909：茂丰原浆礼盒图抠图，融合进营销工作台/长图工具等

环境: 必须用 venv311（rembg 依赖装在那里）
  /Users/local/lishishuo/venv311/bin/python

用法:
  1) CLI 批量: venv311/bin/python tools/batch_cutout.py [源目录] [输出目录]
  2) import 复用: from batch_cutout import cutout_image, batch_cutout
       cutout_image(src, dst, model='u2net', max_side=1600)
       batch_cutout(src_dir, out_dir=None, model='u2net', exts=('.jpg','.jpeg','.png'))
"""
import os
import sys

# rembg 模型可选：u2net(默认均衡) / isnet(边缘细) / isnet-general-use(人像产品通用)
# 不同模型首用需下载对应 onnx（几十~几百MB）
import importlib.util
_REMBG_OK = importlib.util.find_spec("rembg") is not None

if _REMBG_OK:
    from rembg import remove, new_session
else:
    remove = None
    new_session = None


def _session_cache():
    """会话缓存（同进程多次调用不重复加载模型）"""
    if not hasattr(_session_cache, "cache"):
        _session_cache.cache = {}
    return _session_cache.cache


def cutout_image(src, dst, model="u2net", max_side=1600):
    """单张抠图：src 图片 → dst 透明底 PNG"""
    from PIL import Image
    if not _REMBG_OK:
        raise RuntimeError("rembg 未安装，请用 venv311 环境运行")
    cache = _session_cache()
    if model not in cache:
        cache[model] = new_session(model)
    session = cache[model]

    img = Image.open(src)
    if img.mode != "RGB":
        img = img.convert("RGB")
    # 长边限幅防内存爆
    if max_side and max(img.size) > max_side:
        img.thumbnail((max_side, max_side), Image.LANCZOS)
    out = remove(img, session=session)
    os.makedirs(os.path.dirname(os.path.abspath(dst)), exist_ok=True)
    out.save(dst)
    return dst


def batch_cutout(src_dir, out_dir=None, model="u2net", max_side=1600,
                 exts=(".jpg", ".jpeg", ".png")):
    """目录批量抠图：输出 out_dir（缺省 = src_dir/cut）
    返回 [(源名, 输出路径 or None), ...]"""
    if out_dir is None:
        out_dir = os.path.join(src_dir, "cut")
    os.makedirs(out_dir, exist_ok=True)
    results = []
    for f in sorted(os.listdir(src_dir)):
        src = os.path.join(src_dir, f)
        if not os.path.isfile(src):
            continue
        if not f.lower().endswith(exts):
            continue
        base = os.path.splitext(f)[0]
        dst = os.path.join(out_dir, f"{base}_cut.png")
        print(f"  抠 {f} …", flush=True)
        try:
            cutout_image(src, dst, model=model, max_side=max_side)
            print(f"  ✓ → {dst}", flush=True)
            results.append((f, dst))
        except Exception as e:
            print(f"  ✗ {f}: {str(e)[:120]}", flush=True)
            results.append((f, None))
    return results


if __name__ == "__main__":
    src = sys.argv[1] if len(sys.argv) > 1 else os.path.expanduser(
        "~/Desktop/钟茂丰/茂丰原浆2006年冬酿礼盒")
    out = sys.argv[2] if len(sys.argv) > 2 else None
    model = sys.argv[3] if len(sys.argv) > 3 else "u2net"
    if not _REMBG_OK:
        print("需要 venv311 环境：/Users/local/lishishuo/venv311/bin/python 运行本脚本")
        sys.exit(1)
    print(f"批量抠图: {src} (model={model})")
    batch_cutout(src, out, model=model)
    print("完成")
