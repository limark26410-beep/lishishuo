#!/usr/bin/env python3
"""
茂丰原浆2006年冬酿 · 电商详情长图 v3（GL-20260910）
风格参考：622x12952 电商长图（大图铺满 + 文字叠加 + 清冷雅致）
原则：真图优先（老大提供的实拍+抠图），仅 3 张 AI 补图（鉴湖/老酒坊/品饮）

9 屏：主视觉 / 产品展示 / 产品参数 / 大坛窖藏 / 品鉴指南 / 鉴湖本源 /
      四季工艺 / 品饮场景 / 品牌故事
用法: python tools/make_detail_v3.py
"""
import os
from PIL import Image, ImageDraw, ImageFont, ImageFilter

W = 750
F_SONG = "C:/Windows/Fonts/simsun.ttc"
F_PF = "C:/Windows/Fonts/msyh.ttc"

BG = (247, 243, 236)
DARK = (34, 26, 20)
GOLD = (188, 152, 88)
GOLD_L = (216, 190, 140)
INK = (54, 44, 34)
INK_L = (126, 112, 96)
RED = (158, 58, 46)

SRC = os.path.expanduser("~/Desktop/钟茂丰")
BOX = os.path.expanduser("~/Desktop/钟茂丰抠图结果/茂丰原浆2006年冬酿礼盒_副本")
CUT = os.path.join(BOX, "cut")
AI = os.path.join(SRC, "AI补图")

# ── 素材映射（真图优先）──
P_MAIN = os.path.join(BOX, "DSC_2334.JPG")      # 白瓶+红礼盒（原图，白瓶完整）
P_BOTTLE = os.path.join(BOX, "DSC_2327.JPG")    # 白瓶（原图）
P_BOX = os.path.join(CUT, "DSC_2336_cut.png")   # 红礼盒（抠图，红盒抠得好）
P_GIFT_RAW = os.path.join(BOX, "DSC_2336.JPG")  # 红礼盒原图
R_CUP = os.path.join(SRC, "微信图片_20260909154309_5_9.jpg")    # 瓷杯酒液
R_TAN = os.path.join(SRC, "微信图片_20260909154311_7_9.png")    # 酒坛
R_SHOP = os.path.join(SRC, "微信图片_20260909154310_6_9.png")   # 车间盖缸
R_QU = os.path.join(SRC, "微信图片_20260909154315_11_9.png")    # 麦曲
R_YAO = os.path.join(SRC, "微信图片_20260909154314_10_9.png")   # 夏做酒曲
R_LAKE = os.path.join(SRC, "微信图片_20260909154313_9_9.png")   # 水乡湖景
R_TAN2 = os.path.join(SRC, "微信图片_20260909154316_12_9.png")  # 酒坛2
A_LAKE = os.path.join(AI, "06_鉴湖风光.png")
A_DRINK = os.path.join(AI, "08_品饮小聚.png")
A_OLD = os.path.join(AI, "09_老酒坊.png")


def font(size, bold=False, song=True):
    path = F_SONG if song else F_PF
    try:
        return ImageFont.truetype(path, size, index=1 if bold and song else 0)
    except Exception:
        return ImageFont.truetype(path, size)


def canvas(h):
    img = Image.new("RGBA", (W, h), BG + (255,))
    return img, ImageDraw.Draw(img)


def ctext(d, y, text, f, fill=INK, tracking=0):
    if tracking and len(text) > 1:
        ws = [d.textlength(c, font=f) for c in text]
        total = sum(ws) + tracking * (len(text) - 1)
        x = (W - total) / 2
        for c, cw in zip(text, ws):
            d.text((x, y), c, font=f, fill=fill)
            x += cw + tracking
        return
    tw = d.textlength(text, font=f)
    d.text(((W - tw) / 2, y), text, font=f, fill=fill)


def wrap(d, x, y, text, f, fill, max_w, lh=None):
    lh = lh or int(f.size * 1.55)
    line = ""
    for ch in text:
        if d.textlength(line + ch, font=f) <= max_w:
            line += ch
        else:
            d.text((x, y), line, font=f, fill=fill)
            y += lh
            line = ch
    if line:
        d.text((x, y), line, font=f, fill=fill)
    return y + lh


def load(p, max_w, max_h, rgba=False):
    if not os.path.exists(p):
        return None
    im = Image.open(p).convert("RGBA" if rgba else "RGB")
    r = min(max_w / im.width, max_h / im.height, 1.0)
    if r < 1.0:
        im = im.resize((int(im.width * r), int(im.height * r)), Image.LANCZOS)
    return im


def cover(p, w, h):
    """图片铺满裁剪（cover 模式，用于满宽大图）"""
    if not os.path.exists(p):
        return None
    im = Image.open(p).convert("RGB")
    r = max(w / im.width, h / im.height)
    im = im.resize((int(im.width * r), int(im.height * r)), Image.LANCZOS)
    x = (im.width - w) // 2
    y = (im.height - h) // 2
    return im.crop((x, y, x + w, y + h))


def shadow_paste(base, cut, cx, cy, blur=14, alpha=90):
    sh = Image.new("RGBA", base.size, (0, 0, 0, 0))
    sd = ImageDraw.Draw(sh)
    x = int(cx - cut.width / 2); y = int(cy - cut.height / 2)
    sd.ellipse([x + 20, y + cut.height - 10, x + cut.width - 20, y + cut.height + 26],
               fill=(28, 18, 10, alpha))
    sh = sh.filter(ImageFilter.GaussianBlur(blur))
    base.alpha_composite(sh)
    base.alpha_composite(cut, (x, y))


def sec_head(d, y, en, cn):
    ctext(d, y, en, font(14, song=False), fill=GOLD)
    ctext(d, y + 24, cn, font(38, bold=True), fill=INK)
    d.line([(W / 2 - 40, y + 78), (W / 2 + 40, y + 78)], fill=GOLD, width=1)
    return y + 100


# ═══ 1. 主视觉 ═══
def s1_main():
    img, d = canvas(1180)
    # 上半屏：真图铺满（白瓶+红礼盒完整）
    ph = cover(P_MAIN, W, 640)
    if ph:
        img.paste(ph, (0, 0))
    # 下半屏：深棕文字区
    d.rectangle([0, 640, W, 1180], fill=DARK)
    ctext(d, 672, "钟 茂 丰", font(26, bold=True), fill=GOLD_L, tracking=8)
    ctext(d, 714, "ZHONG MAO FENG · SINCE 1738", font(12, song=False), fill=(150, 138, 120))
    d.line([(W/2 - 110, 748), (W/2 + 110, 748)], fill=GOLD, width=1)
    ctext(d, 790, "茂丰原浆 · 2006年冬酿", font(44, bold=True), fill=GOLD_L)
    ctext(d, 858, "手工绍兴黄酒 · 封藏二十年", font(21), fill=(198, 184, 164))
    tags = ["20年陈酿原浆", "非遗古法手工", "IWSC国际银奖"]
    x = (W - len(tags) * 172) / 2
    for t in tags:
        f = font(19, bold=True)
        tw = d.textlength(t, font=f)
        d.rounded_rectangle([x, 928, x + 164, 972], radius=8, outline=GOLD, width=1,
                            fill=(52, 42, 32))
        d.text((x + 82 - tw / 2, 936), t, font=f, fill=GOLD_L)
        x += 172
    ctext(d, 1050, "— 绍兴鉴湖 · 原坛封藏 —", font(17, song=False), fill=(158, 146, 128))
    return img


# ═══ 2. 产品展示（真图 4 格）══
def s2_product():
    img, d = canvas(1180)
    y = 46
    y = sec_head(d, y, "PRODUCT GALLERY", "产品实拍")
    # 2x2 真图网格
    cells = [(P_MAIN, "整瓶礼盒全景"), (P_BOTTLE, "瓶身细节"),
             (R_CUP, "酒体倒杯"), (P_GIFT_RAW, "礼盒外观")]
    cw, ch = 330, 400
    for i, (p, label) in enumerate(cells):
        cx = 30 + (i % 2) * 360
        cy = y + (i // 2) * 440
        d.rounded_rectangle([cx, cy, cx + cw, cy + ch], radius=10,
                            fill=(255, 255, 255), outline=(226, 214, 196))
        ph = cover(p, cw - 20, ch - 58)
        if ph:
            img.paste(ph, (cx + 10, cy + 14))
        f = font(17, song=False)
        tw = d.textlength(label, font=f)
        d.text((cx + cw / 2 - tw / 2, cy + ch - 34), label, font=f, fill=INK_L)
    return img


# ═══ 3. 产品参数（真图 + 表格）══
def s3_specs():
    img, d = canvas(1000)
    y = 46
    y = sec_head(d, y, "SPECIFICATIONS", "产品参数")
    # 左侧酒瓶
    cut = load(P_BOTTLE, 240, 480, rgba=True)
    if cut:
        shadow_paste(img, cut, 160, y + 260, alpha=70)
    specs = [("品牌", "钟茂丰"), ("品名", "茂丰原浆2006年冬酿"),
             ("系列", "原浆系列"), ("净含量", "500ml"),
             ("酒精度", "≥14.0%vol"), ("含糖量", "15.1-40.0g/L"),
             ("酒龄", "20年陈酿原浆"), ("质量等级", "优等品"),
             ("配料", "鉴湖水、糯米、小麦"), ("产地", "浙江绍兴"),
             ("保质期", "10年"), ("标准号", "GB/T 17946")]
    sx = 320
    for i, (k, v) in enumerate(specs):
        yy = y + i * 42
        d.text((sx, yy + 8), k, font=font(16, bold=True), fill=GOLD)
        d.text((sx + 92, yy + 8), v, font=font(16, song=False), fill=INK)
        d.line([(sx, yy + 36), (W - 30, yy + 36)], fill=(232, 222, 206), width=1)
    return img


# ═══ 4. 大坛窖藏（真图酒坛满宽）══
def s4_cellar():
    H = 900
    img, d = canvas(H)
    ph = cover(R_TAN, W, H)
    if ph:
        img.paste(ph, (0, 0))
    # 底部渐变压暗 + 文字
    ov = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    od = ImageDraw.Draw(ov)
    for i in range(260):
        a = int(210 * (i / 260) ** 1.4)
        od.line([(0, H - 260 + i), (W, H - 260 + i)], fill=(24, 16, 10, a))
    img.alpha_composite(ov)
    d = ImageDraw.Draw(img)
    ctext(d, H - 220, "封 藏 二 十 年", font(40, bold=True), fill=GOLD_L, tracking=6)
    ctext(d, H - 160, "7300 多个日夜 · 陶坛陈化 · 越陈越香", font(19), fill=(226, 214, 196))
    return img


# ═══ 5. 品鉴指南（真图酒杯 + 四格）══
def s5_tasting():
    H = 1250
    img, d = canvas(H)
    ph = cover(R_CUP, W, 620)
    if ph:
        img.paste(ph, (0, 0))
    d = ImageDraw.Draw(img)
    y = 660
    y = sec_head(d, y, "TASTING NOTES", "品鉴指南")
    items = [("观 色", "色如琥珀，温润有光"),
             ("闻 香", "谷物发酵香，果干蜂蜜交织"),
             ("尝 味", "甘酸苦辛鲜，五味平和"),
             ("品 格", "奔放醇厚，回味悠长")]
    for i, (t1, t2) in enumerate(items):
        yy = y + i * 108
        d.ellipse([60, yy + 10, 76, yy + 26], fill=GOLD)
        d.text((94, yy), t1, font=font(22, bold=True), fill=RED)
        d.text((190, yy + 4), t2, font=font(18, song=False), fill=INK_L)
        if i < 3:
            d.line([(60, yy + 82), (W - 60, yy + 82)], fill=(234, 224, 208), width=1)
    return img


# ═══ 6. 鉴湖本源（真图水乡 + AI鉴湖）══
def s6_lake():
    H = 1100
    img, d = canvas(H)
    # 上：真图水乡（裁掉右下角水印区域）
    ph = cover(R_LAKE, W, 560)
    if ph:
        img.paste(ph, (0, 0))
    y = 610
    d = ImageDraw.Draw(img)
    y = sec_head(d, y, "THE WATER OF JIAN LAKE", "原料本源 · 鉴湖")
    wrap(d, 60, y, "“盖山阴、会稽之间，水最宜酒，易地则不能为良”",
         font(20), INK, W - 120)
    d.text((60, y + 42), "——清 · 梁章钜《浪迹续谈》", font=font(15, song=False), fill=GOLD)
    wrap(d, 60, y + 86, "鉴湖位于绍兴之南，水质清冽，造就绍兴黄酒的独特风味。"
                        "好水才能出好酒——茂丰原浆，只取鉴湖核心产区之水。",
         font(18, song=False), INK_L, W - 120)
    # 右下 AI 鉴湖小图
    ph2 = load(A_LAKE, 200, 180)
    if ph2:
        img.paste(ph2, (W - 220, H - 200))
    return img


# ═══ 7. 四季工艺（真图 3 + 文字）══
def s7_craft():
    H = 1180
    img, d = canvas(H)
    y = 46
    y = sec_head(d, y, "CRAFT ALL YEAR ROUND", "古法工艺 · 四季酿造")
    steps = [("春 耕", "春分封坛，泥封静候"),
             ("夏 生", "夏做酒药，辣蓼草与米粉"),
             ("秋 收", "秋制麦曲，金黄小麦"),
             ("冬 酿", "立冬开酿，落缸发酵"),
             ("封 藏", "陶坛陈化，二十年为期")]
    cw = (W - 60 - 4 * 8) // 5
    for i, (t1, t2) in enumerate(steps):
        cx = 30 + i * (cw + 8)
        d.rounded_rectangle([cx, y, cx + cw, y + 96], radius=8,
                            fill=DARK, outline=GOLD)
        f = font(21, bold=True)
        tw = d.textlength(t1, font=f)
        d.text((cx + cw / 2 - tw / 2, y + 32), t1, font=f, fill=GOLD_L)
    y += 130
    # 真图三格
    shots = [(R_YAO, "夏做酒药"), (R_QU, "秋制麦曲"), (R_SHOP, "冬酿发酵")]
    cw2 = (W - 60 - 20) // 3
    for i, (p, label) in enumerate(shots):
        cx = 30 + i * (cw2 + 10)
        ph = cover(p, cw2, 330)
        if ph:
            img.paste(ph, (cx, y))
            d.rectangle([cx, y, cx + cw2, y + 330], outline=(226, 214, 196), width=1)
        f = font(16, song=False)
        tw = d.textlength(label, font=f)
        d.text((cx + cw2 / 2 - tw / 2, y + 342), label, font=f, fill=INK_L)
    y += 400
    wrap(d, 60, y, "前后 110 余道工序，工匠以「听、闻、摸、尝」四法把控发酵，"
                   "酿出的酒带着时间与手心的温度，是机械标准化生产无法取代的风味。",
         font(18, song=False), INK_L, W - 120)
    return img


# ═══ 8. 品饮场景（AI 补图）══
def s8_drink():
    H = 900
    img, d = canvas(H)
    ph = cover(A_DRINK, W, H)
    if ph:
        img.paste(ph, (0, 0))
    ov = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    od = ImageDraw.Draw(ov)
    for i in range(240):
        a = int(200 * (i / 240) ** 1.3)
        od.line([(0, H - 240 + i), (W, H - 240 + i)], fill=(24, 16, 10, a))
    img.alpha_composite(ov)
    d = ImageDraw.Draw(img)
    ctext(d, H - 190, "亲友小聚 · 温一壶老酒", font(32, bold=True), fill=GOLD_L)
    ctext(d, H - 136, "好入口 · 微醺不上头 · 暖身暖心", font(18), fill=(226, 214, 196))
    return img


# ═══ 9. 品牌故事（AI 老酒坊 + 时间线）══
def s9_story():
    H = 1350
    img, d = canvas(H)
    ph = cover(A_OLD, W, 620)
    if ph:
        img.paste(ph, (0, 0))
    d = ImageDraw.Draw(img)
    y = 660
    y = sec_head(d, y, "280 YEARS OF HERITAGE", "品牌故事")
    tl = [("1738", "钟氏先祖肇业，乾隆年间立坊，七代传承"),
          ("1941", "烽火毁坊，一坛老酒藏于故土"),
          ("2019", "钟大年、钟亦鸣两代传人重振祖业"),
          ("今日", "茂丰原浆2006，原坛开藏")]
    ty = y + 6
    for year, txt in tl:
        d.ellipse([60, ty + 8, 74, ty + 22], fill=GOLD)
        d.text((92, ty), year, font=font(22, bold=True), fill=RED)
        wrap(d, 178, ty + 4, txt, font(17, song=False), INK_L, W - 240)
        ty += 76
    # 结尾
    yy = ty + 20
    d.line([(60, yy), (W - 60, yy)], fill=GOLD, width=1)
    ctext(d, yy + 22, "茂丰原浆 · 2006年冬酿", font(30, bold=True), fill=INK)
    ctext(d, yy + 72, "送礼有面子 · 自饮有味道 · 收藏有价值", font(18), fill=INK_L)
    ctext(d, yy + 112, "— 钟茂丰 始于乾隆 · 280 年老字号 —",
          font(15, song=False), fill=GOLD)
    ctext(d, yy + 158, "过度饮酒有害健康 · 孕妇及儿童不宜饮酒 · 请勿酒后驾车",
          font(12, song=False), fill=(158, 148, 136))
    return img


def main():
    print("渲染详情长图 v3（真图优先 + 参考图风格）…")
    segs = [s1_main(), s2_product(), s3_specs(), s4_cellar(), s5_tasting(),
            s6_lake(), s7_craft(), s8_drink(), s9_story()]
    total = sum(s.height for s in segs)
    out = os.path.join(SRC, "茂丰原浆2006年冬酿_详情长图_v3.png")
    cv = Image.new("RGB", (W, total), BG)
    y = 0
    for s in segs:
        cv.paste(s.convert("RGB"), (0, y))
        y += s.height
    cv.save(out)
    print(f"✓ {out}")
    print(f"  尺寸 {W}x{total}  ({total/W:.0f} 屏@宽750)")
    return out


if __name__ == "__main__":
    main()
