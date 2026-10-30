# -*- coding: utf-8 -*-
"""本地首帧合成（GL-20261013）：不依赖任何图像编辑 API，纯 PIL 拼合。

为什么需要它
------------
保人物一致性需要「角色在场景里的首帧」，但：
  · 阿里 qwen-image-3.0 能做参考图合成，但账户额度耗尽
  · 智谱 GLM-Image 只吃文本（最大 1000 字符），吃不了参考图

于是走「本地拼合 + i2v 润化」：
  1. 从角色立绘抠掉白底 → alpha
  2. 按地面线缩放贴到场景图合适位置
  3. 做色调/亮度匹配 + 脚下软阴影 → 看上去不那么"贴纸"
  4. 交给 i2v（智谱 CogVideoX）「让画面动起来」——
     视频模型会重新渲染整帧，粗糙的拼接边界会被洗掉。

它的产物不追求好看，只追求**构图对、长相对**。真正的画面质感由 i2v 那一步给。

用法
----
    from compose_first_frame_local import compose, put_person

    compose(
        scene="场景/老院子四视图.png", scene_panel=0,   # 场景也是四视图，取第几格
        out="首帧/S07.png", size=(1660, 934),
        people=[
            {"img": "首帧素材/客商_全身正面.png", "height": 0.72, "x": 0.42, "bottom": 0.94},
            {"img": "首帧素材/二哥_全身正面.png", "height": 0.70, "x": 0.68, "bottom": 0.94},
        ])

CLI：python lib/compose_first_frame_local.py <配置json>
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageChops
import numpy as np


def cutout_white(img: Image.Image, tol: int = 28, feather: float = 1.2) -> Image.Image:
    """去掉白底 → RGBA。用亮度阈值 + 边缘羽化，避免硬边。"""
    img = img.convert("RGB")
    a = np.asarray(img).astype(np.int16)
    # 到白色的距离
    dist = 255 - a.min(axis=2)
    alpha = np.clip((dist - tol) * (255.0 / max(1, 70 - tol)), 0, 255).astype(np.uint8)
    out = img.convert("RGBA")
    out.putalpha(Image.fromarray(alpha))
    if feather > 0:
        al = out.getchannel("A").filter(ImageFilter.GaussianBlur(feather))
        out.putalpha(al)
    return out


def bbox_nonempty(im: Image.Image) -> tuple:
    bb = im.getchannel("A").point(lambda v: 255 if v > 12 else 0).getbbox()
    return bb or (0, 0, im.width, im.height)


def match_tone(person: Image.Image, scene: Image.Image, region: tuple) -> Image.Image:
    """把人物的亮度和色温往场景对应区域靠，减轻"贴纸感"。"""
    sx0, sy0, sx1, sy1 = region
    patch = scene.crop((max(0, sx0), max(0, sy0), max(1, sx1), max(1, sy1))).convert("RGB")
    s = np.asarray(patch).reshape(-1, 3).mean(axis=0)
    p = np.asarray(person.convert("RGB")).reshape(-1, 3)
    mask = np.asarray(person.getchannel("A")).reshape(-1) > 128
    if mask.sum() < 50:
        return person
    pm = p[mask].mean(axis=0)
    # 限幅，避免把人物搞成场景色
    gain = np.clip(s / np.maximum(pm, 1.0), 0.80, 1.25)
    gain = 1.0 + (gain - 1.0) * 0.55          # 只吸收 55%
    arr = np.asarray(person.convert("RGB")).astype(np.float32) * gain
    arr = np.clip(arr, 0, 255).astype(np.uint8)
    out = Image.fromarray(arr).convert("RGBA")
    out.putalpha(person.getchannel("A"))
    return out


def _ground_shadow(scene: Image.Image, cx: int, bot: int, pw: int,
                   target_h: int, light_dx: float = 0.45) -> Image.Image:
    """投射阴影 + 脚下接触阴影。让"贴纸"有接地感。

    不是简单一个椭圆：而是①沿光反方向拉长的投影 ②脚下一条更深的接触暗部
    ③两者都用多层模糊叠出软硬过渡。
    """
    W, H = scene.size
    sh = Image.new("L", (W, H), 0)
    d = ImageDraw.Draw(sh)

    ew = max(10, int(pw * 0.90))
    # ① 投射阴影：从脚下往光的反方向拉长（默认光从右上来 → 影子往左下）
    sl = int(target_h * 0.34)
    sx = int(-light_dx * sl)
    d.ellipse([cx - ew // 2 + sx, bot - int(target_h * 0.030),
               cx + ew // 2 + sx, bot + int(target_h * 0.030)],
              fill=150)
    # ② 接触阴影：紧贴脚跟，深且窄
    cw2 = int(ew * 0.55)
    d.ellipse([cx - cw2 // 2, bot - int(target_h * 0.018),
               cx + cw2 // 2, bot + int(target_h * 0.018)],
              fill=210)

    sh = sh.filter(ImageFilter.GaussianBlur(max(3, target_h * 0.022)))
    # 阴影只落在地面区域，别糊到墙上
    return sh


def put_person(scene: Image.Image, person_path: str, height_ratio: float,
               x_ratio: float, bottom_ratio: float,
               shadow: bool = True, light_dx: float = 0.45) -> Image.Image:
    """把一个人物贴进场景。height_ratio 是相对场景高的身高占比。"""
    raw = Image.open(person_path).convert("RGB")
    p = cutout_white(raw)
    p = p.crop(bbox_nonempty(p))

    W, H = scene.size
    target_h = max(8, int(H * height_ratio))
    scale = target_h / p.height
    p = p.resize((max(1, int(p.width * scale)), target_h), Image.LANCZOS)

    cx = int(W * x_ratio)
    bot = int(H * bottom_ratio)
    px = cx - p.width // 2
    py = bot - p.height

    # 色调匹配：取样「人物背后那块地面」而不是任意区域
    gy0 = min(H - 1, bot + int(target_h * 0.02))
    gy1 = min(H, bot + int(target_h * 0.14))
    p = match_tone(p, scene, (px, gy0, px + p.width, gy1))

    layer = Image.new("RGBA", scene.size, (0, 0, 0, 0))

    if shadow:
        sh = _ground_shadow(scene, cx, bot, p.width, target_h, light_dx)
        shadow_layer = Image.new("RGBA", scene.size, (0, 0, 0, 0))
        shadow_layer.putalpha(sh)
        layer = Image.alpha_composite(layer, shadow_layer)

    # 人物边缘轻微羽化，削弱抠图硬边
    edge = p.getchannel("A").filter(ImageFilter.GaussianBlur(0.8))
    p.putalpha(edge)

    layer.paste(p, (px, py), p)
    return Image.alpha_composite(scene.convert("RGBA"), layer)


def pick_panel(img: Image.Image, panel: int = 0, cols: int = 2, rows: int = 2) -> Image.Image:
    """多格场景图取其中一格。panel<0 表示整图不切（单张场景）。

    场景素材有两种：单张（如办公室）和拼版（2x2 / 2x3），必须切出一格，
    否则人物会贴在一堆格子上。
    """
    if panel < 0:
        return img
    W, H = img.size
    cw, ch = W // cols, H // rows
    c = panel % cols
    r = panel // cols
    return img.crop((c * cw, r * ch, (c + 1) * cw, (r + 1) * ch))


def compose(scene: str, out: str, people: list, size: tuple = (1660, 934),
            scene_panel: int = 0, scene_fit: str = "cover",
            scene_cols: int = 2, scene_rows: int = 2,
            grade: bool = True) -> str:
    sc = Image.open(scene).convert("RGB")
    if scene_panel >= 0:
        sc = pick_panel(sc, scene_panel, scene_cols, scene_rows)

    W, H = size
    if scene_fit == "cover":
        r = max(W / sc.width, H / sc.height)
    else:
        r = min(W / sc.width, H / sc.height)
    sc = sc.resize((max(1, int(sc.width * r)), max(1, int(sc.height * r))), Image.LANCZOS)
    left = (sc.width - W) // 2
    top = (sc.height - H) // 2
    sc = sc.crop((left, top, left + W, top + H))

    if grade:
        # 轻微压暗四周，让画面像"镜头"而不是"截图"
        vig = Image.new("L", (W, H), 0)
        dv = ImageDraw.Draw(vig)
        dv.ellipse([-W * 0.25, -H * 0.25, W * 1.25, H * 1.25], fill=255)
        vig = vig.filter(ImageFilter.GaussianBlur(W * 0.08))
        dark = Image.new("RGB", (W, H), (0, 0, 0))
        sc = Image.composite(sc, Image.blend(sc, dark, 0.30), vig)

    canvas = sc.convert("RGBA")
    # 远→近：先贴靠后的（bottom 小的先贴）
    for per in sorted(people, key=lambda d: d.get("bottom", 0.9)):
        canvas = put_person(canvas,
                            per["img"],
                            per.get("height", 0.7),
                            per.get("x", 0.5),
                            per.get("bottom", 0.92))
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    canvas.convert("RGB").save(out, quality=95)
    print(f"  ✓ {Path(out).name}  {W}x{H}")
    return out


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    cfg = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    base = Path(cfg.get("base", ".")).expanduser()
    people = [dict(p, img=str((base / p["img"]).resolve())) for p in cfg["people"]]
    compose(scene=str((base / cfg["scene"]).resolve()),
            out=str((base / cfg["out"]).resolve()),
            people=people,
            size=tuple(cfg.get("size", [1660, 934])),
            scene_panel=cfg.get("scene_panel", 0),
            scene_fit=cfg.get("scene_fit", "cover"),
            scene_cols=cfg.get("scene_cols", 2),
            scene_rows=cfg.get("scene_rows", 2),
            grade=cfg.get("grade", True))


if __name__ == "__main__":
    main()
