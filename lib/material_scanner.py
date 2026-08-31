# -*- coding: utf-8 -*-
"""素材库扫描模块（视频混剪第一步）

目录规范（定稿 v2.3）：
    {素材库根}/{题材}/{主题}_{编号}.mp4
    例：~/历史说素材/视频/历史/隋唐/唐-长安城_01.mp4
  - 题材 = 根目录下第一级目录（历史/商品/美食…）
  - 主题 = 第二级目录（隋唐…），无第二级则空
  - README.txt：每个主题目录一份，每行 `文件名：画面内容描述`，
    供第二步 AI 选材语义匹配使用（本次先扫进清单）

缓存：清单写 {素材库根}/_scan_cache.json，按目录树最新 mtime 失效重扫。
"""

import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

VIDEO_EXTS = (".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v")
CACHE_NAME = "_scan_cache.json"
_CACHE_LOCK = threading.Lock()  # 写缓存加锁（watcher 与预览可能并发）


def _ffprobe_info(path: str) -> dict:
    """读时长/分辨率。优先真 ffprobe；没有/坏 ffprobe 时回退 ffmpeg -i 解析 stderr。
    失败返回空 dict（坏文件由调用方跳过计数）。
    """
    probe = shutil.which("ffprobe")
    if probe:
        try:
            r = subprocess.run(
                [probe, "-v", "error",
                 "-show_entries", "format=duration",
                 "-show_entries", "stream=width,height",
                 "-of", "json", str(path)],
                capture_output=True, text=True, timeout=30,
            )
            if r.returncode == 0 and r.stdout.strip().startswith("{"):
                data = json.loads(r.stdout or "{}")
                dur = float(data.get("format", {}).get("duration") or 0)
                width = height = 0
                for s in data.get("streams", []):
                    if s.get("width") and s.get("height"):
                        width = int(s["width"])
                        height = int(s["height"])
                        break
                if dur:
                    return {"duration_sec": round(dur, 2),
                            "width": width, "height": height}
        except Exception:
            pass
    # 回退：ffmpeg -i 输出 stderr 解析（无 ffprobe 或 ffprobe 异常时）
    try:
        r = subprocess.run(["ffmpeg", "-i", str(path)],
                           capture_output=True, text=True, timeout=30)
        err = r.stderr or ""
        m = re.search(r"Duration: (\d+):(\d+):(\d+\.?\d*)", err)
        # 分辨率：ffmpeg -i 输出形如 "yuv420p, 720x1280 [SAR 1:1]"，用逗号锚定避免匹配 fourcc(0x31637661)
        wm = re.search(r",\s*(\d{3,5})x(\d{3,5})", err)
        if m:
            h, mi, s = float(m.group(1)), float(m.group(2)), float(m.group(3))
            dur = h * 3600 + mi * 60 + s
            w = int(wm.group(1)) if wm else 0
            hh = int(wm.group(2)) if wm else 0
            return {"duration_sec": round(dur, 2), "width": w, "height": hh}
    except Exception:
        pass
    return {}


def _read_readme(theme_dir: Path) -> dict:
    """读 README.txt：每行 `文件名：画面内容描述`（支持全角/半角冒号），
    返回 {文件名: 描述}"""
    desc = {}
    p = theme_dir / "README.txt"
    if not p.exists():
        return desc
    try:
        for ln in p.read_text(encoding="utf-8", errors="ignore").splitlines():
            ln = ln.strip()
            if not ln:
                continue
            m = re.match(r"^(.+?)[：:](.*)$", ln)
            if not m:
                continue
            name = m.group(1).strip()
            if name:
                desc[name] = m.group(2).strip()
    except Exception:
        pass
    return desc


def _scan_dir(theme_dir: Path, topic: str, theme: str,
              desc_map: dict, materials: list):
    """扫描单层目录下的视频文件，收集素材条目（不含媒体信息，稍后并行 ffprobe）"""
    for f in sorted(theme_dir.iterdir()):
        if not f.is_file():
            continue
        if f.name.lower().endswith(VIDEO_EXTS) and not f.name.startswith("_"):
            materials.append({
                "path": str(f),
                "name": f.stem,                      # 文件名（去扩展名）
                "topic": topic,                      # 题材
                "theme": theme,                      # 主题（无则空）
                "desc": desc_map.get(f.name, ""),    # README 描述
                "mtime": f.stat().st_mtime,          # 修改时间（新素材优先用）
                "duration_sec": 0.0,
                "width": 0,
                "height": 0,
            })


def _dir_mtime_key(library_root: str) -> float:
    """目录树最新文件 mtime（缓存失效依据；_scan_cache.json 自身不计）"""
    root = Path(library_root)
    latest = 0.0
    for p in root.rglob("*"):
        try:
            if p.is_file() and p.name != CACHE_NAME:
                latest = max(latest, p.stat().st_mtime)
        except OSError:
            continue
    return round(latest, 1)


def scan_material(library_root: str, max_workers: int = 4) -> list:
    """扫描素材库根目录，返回素材清单（按题材分组排序）。

    返回每条：{path, name, topic, theme, duration_sec, width, height, desc}
    坏文件（读不到时长/分辨率）跳过并计数打印。
    """
    root = Path(os.path.expanduser(library_root))
    if not root.is_dir():
        raise FileNotFoundError(f"素材库目录不存在: {root}")

    cache_path = root / CACHE_NAME
    mtime_key = _dir_mtime_key(str(root))

    # 命中缓存（目录没变化）直接返回
    if cache_path.exists():
        try:
            cache = json.loads(cache_path.read_text(encoding="utf-8"))
            if cache.get("mtime_key") == mtime_key and isinstance(
                    cache.get("materials"), list):
                return cache["materials"]
        except Exception:
            pass

    materials = []
    sub_dirs = [p for p in sorted(root.iterdir())
                if p.is_dir() and not p.name.startswith("_")]
    if sub_dirs:
        # 两级目录：题材/主题/（或 题材/ 下直接放素材）
        for topic_dir in sub_dirs:
            topic = topic_dir.name
            theme_dirs = [p for p in sorted(topic_dir.iterdir())
                          if p.is_dir() and not p.name.startswith("_")]
            if theme_dirs:
                for theme_dir in theme_dirs:
                    desc_map = _read_readme(theme_dir)
                    _scan_dir(theme_dir, topic, theme_dir.name, desc_map, materials)
            else:
                # 题材目录下直接放素材（无主题层）
                desc_map = _read_readme(topic_dir)
                _scan_dir(topic_dir, topic, "", desc_map, materials)
    else:
        # 素材直接放库根：单题材库（根目录名即题材）
        desc_map = _read_readme(root)
        _scan_dir(root, root.name, "", desc_map, materials)

    # 并行 ffprobe 读时长/分辨率
    skipped = 0
    if materials:
        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            futures = {ex.submit(_ffprobe_info, m["path"]): m for m in materials}
            for fut, m in futures.items():
                info = fut.result()
                if not info or not info.get("duration_sec"):
                    skipped += 1
                    continue
                m.update(info)
        materials = [m for m in materials if m.get("duration_sec")]

    # 写缓存（加锁 + 原子写，避免并发半写文件）
    try:
        with _CACHE_LOCK:
            _tmp = cache_path.with_suffix(".json.tmp")
            _tmp.write_text(
                json.dumps({"mtime_key": mtime_key, "materials": materials},
                           ensure_ascii=False, indent=1),
                encoding="utf-8")
            _tmp.replace(cache_path)
    except Exception:
        pass

    if skipped:
        print(f"  素材扫描: {len(materials)} 条可用, {skipped} 条跳过(无法读取时长/分辨率)")
    return materials


if __name__ == "__main__":
    import sys as _sys
    root = _sys.argv[1] if len(_sys.argv) > 1 else "~/历史说素材/视频/"
    mats = scan_material(root)
    print(f"\n素材清单（{len(mats)} 条）:")
    for m in mats:
        print(f"  [{m['topic']}/{m['theme']}] {m['name']} "
              f"{m['duration_sec']}s {m['width']}x{m['height']} "
              f"{('| ' + m['desc'][:30]) if m['desc'] else ''}")
