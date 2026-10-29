#!/usr/bin/env python3
"""
茂丰原浆2006年冬酿 · 电商详情长图生成器（GL-20260909）
750px 宽 × 多屏，高端国风：深棕/鎏金/米白 + 宋体/苹方

板块：1主视觉 2核心卖点 3品鉴指南 4原料与工艺 5品牌故事 6产品参数 7结尾
用法: python make_detail_longimg.py [输出png]
"""
import os
from PIL import Image, ImageDraw, ImageFont, ImageFilter, ImageEnhance

# ── 常量 ──
W = 750                      # 电商详情页标准宽
FONT_DIR = "/System/Library/Fonts"
F_SONG = os.path.join(FONT_DIR, "Songti.ttc")      # 宋体（标题）
F_PF   = os.path.join(FONT_DIR, "PingFang.ttc")    # 苹方（正文）

# 国风调色
BG      = (248, 244, 236)    # 米白
BG_DARK = (38, 28, 20)       # 深棕
GOLD    = (196, 158, 90)     # 鎏金
GOLD_LT = (222, 194, 140)    # 浅金
INK     = (60, 48, 36)       # 墨褐
INK_LT  = (120, 105, 88)     # 浅墨
RED     = (166, 62, 48)      # 印章红

SRC = "/Users/local/Desktop/钟茂丰"   # 素材目录


def font(size, bold=False, song=True):
    """取字体；Songti 粗体用 bold index 试取"""
    path = F_SONG if song else F_PF
    try:
        return ImageFont.truetype(path, size, index=1 if bold and song else 0)
    except Exception:
        return ImageFont.truetype(path, size)


def new_canvas(h):
    img = Image.new("RGBA", (W, h), BG + (255,))
    return img, ImageDraw.Draw(img)


def center_text(d, y, text, f, fill=INK, tracking=0):
    """居中绘制（tracking: 字间距 px）"""
    if tracking and len(text) > 1:
        widths = [d.textlength(ch, font=f) for ch in text]
        total = sum(widths) + tracking * (len(text) - 1)
        x = (W - total) / 2
        for ch, cw in zip(text, widths):
            d.text((x, y), ch, font=f, fill=fill)
            x += cw + tracking
        return total
    tw = d.textlength(text, font=f)
    d.text(((W - tw) / 2, y), text, font=f, fill=fill)
    return tw


def load_photo(name, max_w=W - 120, max_h=900):
    """加载产品图并等比缩放"""
    p = os.path.join(SRC, name)
    if not os.path.exists(p):
        return None
    im = Image.open(p).convert("RGB")
    r = min(max_w / im.width, max_h / im.height, 1.0)
    return im.resize((int(im.width * r), int(im.height * r)), Image.LANCZOS)


def load_cut(name, max_w=W - 120, max_h=900):
    """加载透明底抠图 PNG（RGBA）并等比缩放，宽或高超出则缩放"""
    p = os.path.join(SRC, name)
    if not os.path.exists(p):
        return None
    im = Image.open(p).convert("RGBA")
    r = min(max_w / im.width, max_h / im.height, 1.0)
    if r < 1.0:
        im = im.resize((int(im.width * r), int(im.height * r)), Image.LANCZOS)
    return im


def paste_cut_center(base, cut, cx=None, cy=None, shadow=True):
    """透明产品图贴到背景中央（可选柔投影）。返回贴图顶 y"""
    cx = cx if cx is not None else W // 2
    cy = cy if cy is not None else base.height // 2
    x = int(cx - cut.width / 2)
    y = int(cy - cut.height / 2)
    if shadow:
        # 简单椭圆投影
        sh = Image.new("RGBA", base.size, (0, 0, 0, 0))
        sd = ImageDraw.Draw(sh)
        sx0, sy0 = x + 24, y + cut.height - 8
        sx1, sy1 = x + cut.width - 24, y + cut.height + 30
        sd.ellipse([sx0, sy0, sx1, sy1], fill=(30, 20, 10, 90))
        sh = sh.filter(ImageFilter.GaussianBlur(12))
        base.alpha_composite(sh)
    base.alpha_composite(cut, (x, y))
    return y


def paste_centered(base, photo, cy):
    """把图水平居中放到底图 cy 中心"""
    x = (W - photo.width) // 2
    y = cy - photo.height // 2
    base.paste(photo, (x, y))
    return y


def divider(d, y, label=None):
    """鎏金细线 + 可选小标签"""
    d.line([(60, y), (W - 60, y)], fill=GOLD, width=1)
    if label:
        f = font(16)
        tw = d.textlength(label, font=f)
        d.text(((W - tw) / 2, y + 6), label, font=f, fill=GOLD_LT)


def sec_title(d, y, en, cn):
    """板块标题：小英文 + 大中文（居中）"""
    center_text(d, y, en, font(15, song=False), fill=GOLD_LT)
    y2 = y + 26
    center_text(d, y2, cn, font(42, bold=True), fill=INK)
    return y2 + 70


def place_photo_bg(img, d, y, h, name, mode="light"):
    """放置带衬底/边框的产品图块，返回块底 y"""
    ph = load_photo(name, max_h=h - 20)
    if ph is None:
        return y + h
    # 衬底卡片
    pad = 24
    bx, by = pad, y + 10
    bw, bh = W - pad * 2, ph.height + 40
    d.rounded_rectangle([bx, by, bx + bw, by + bh], radius=12,
                        fill=(255, 255, 255) if mode == "light" else (52, 40, 30),
                        outline=(224, 210, 186), width=1)
    img.paste(ph, ((W - ph.width) // 2, by + 20))
    return by + bh + 16



# ─────────────────────────────────────────────
# 各板块图片映射（已确认）
# 主视觉: 礼盒DSC_2334(白瓶红礼盒) | 卖点: 2327酒瓶 | 品鉴: 微信图9(瓷杯酒液)
# 工艺: 微信图10(车间) | 原料: 微信图15(麦曲) 图14(夏做酒曲) | 酒坛: 11/16
# 品牌故事: 酒坛 或 老酒坊 | 结尾: DSC_2336(红礼盒)
# ─────────────────────────────────────────────
CUT_DIR  = "茂丰原浆2006年冬酿礼盒/cut"              # 透明底抠图目录
IMG_MAIN   = CUT_DIR + "/DSC_2334_cut.png"   # 白瓶+红礼盒(透明)
IMG_BOTTLE = CUT_DIR + "/DSC_2333_cut.png"   # 白色酒瓶(透明,边缘干净)
IMG_TASTE  = "微信图片_20260909154309_5_9.jpg"        # 瓷杯酒液品鉴(场景保留)
IMG_SHOP   = "微信图片_20260909154310_6_9.png"        # 酿造车间(场景保留)
IMG_QU     = "微信图片_20260909154315_11_9.png"       # 麦曲(场景保留)
IMG_TAN    = "微信图片_20260909154311_7_9.png"        # 酒坛(场景保留)
IMG_GIFT   = CUT_DIR + "/DSC_2336_cut.png"   # 红色礼盒(透明)


def seg_main_visual():
    img, d = new_canvas(1500)
    d.rectangle([0, 0, W, 1500], fill=BG_DARK)
    # 顶部品牌
    center_text(d, 60, "钟 茂 丰", font(30, bold=True), fill=GOLD_LT, tracking=6)
    d.line([(W/2 - 90, 118), (W/2 + 90, 118)], fill=GOLD, width=1)
    # 产品大图（透明底，深棕背景上直接贴 + 投影）
    cut = load_cut(IMG_MAIN, max_w=W - 80, max_h=700)
    if cut:
        paste_cut_center(img, cut, cy=520, shadow=True)
    # 标题
    y = 1000
    center_text(d, y, "茂丰原浆 · 2006年冬酿", font(52, bold=True), fill=GOLD_LT)
    center_text(d, y + 80, "手工绍兴黄酒 · 封藏二十年", font(26), fill=(200, 188, 170))
    # 三条金句
    tags = ["20年陈酿原浆", "非遗古法手工", "IWSC国际银奖"]
    tw_all = 0
    for i, t in enumerate(tags):
        f = font(24, bold=True)
        tw = d.textlength(t, font=f)
        # 金框标签
        x0 = (W - 480) / 2 + i * 160 + (160 - tw) / 2
        d.rounded_rectangle([x0 - 10, 1170, x0 + tw + 10, 1224], radius=6,
                            outline=GOLD, width=1)
    # 重新排版三个金句均匀分布
    n = len(tags)
    gap = 130
    total = n * 150
    x = (W - total) / 2
    for t in tags:
        f = font(24, bold=True)
        tw = d.textlength(t, font=f)
        d.rounded_rectangle([x - 12, 1170, x + 138, 1224], radius=8,
                            outline=GOLD, width=1, fill=(52, 40, 30))
        d.text((x + 69 - tw/2, 1179), t, font=f, fill=GOLD_LT)
        x += 150
    center_text(d, 1280, "— 绍兴鉴湖 · 原坛封藏 —", font(20, song=False), fill=(160, 148, 132))
    center_text(d, 1330, "绍兴市 · 钟茂丰酒坊", font(18, song=False), fill=(140, 128, 112))
    return img


def seg_selling_points():
    img, d = new_canvas(1050)
    y = 60
    y = sec_title(d, y, "CORE ADVANTAGES", "核心卖点") + 10
    # 白瓶图块（透明抠图放浅金渐变卡片上，右侧文字）
    cut = load_cut(IMG_BOTTLE, max_w=250, max_h=400)
    bx = 40; by = y + 30
    d.rounded_rectangle([bx, by, bx + 300, by + 430], radius=12,
                        fill=(246, 242, 233), outline=(224, 210, 186))
    if cut:
        paste_cut_center(img, cut, cx=bx + 150, cy=by + 215, shadow=True)
    # 卖点列表（左侧）
    pts = [
        ("二十年陈酿", "一坛封藏7300余日，琥珀流光，醇香甜美，好入口不上头"),
        ("非遗手工冬酿", "110余道工序，听闻摸尝，匠人手温成就的活酒"),
        ("IWSC 国际银奖", "世界级烈酒大赛背书，中国黄酒实力名片"),
    ]
    ly = y + 20
    for title, desc in pts:
        # 序号点
        d.ellipse([460, ly + 8, 482, ly + 30], fill=GOLD)
        d.text((469, ly + 8), str(pts.index((title, desc)) + 1),
               font=font(16, bold=True), fill=(40, 30, 22))
        d.text((500, ly + 2), title, font=font(28, bold=True), fill=INK)
        # 描述折行
        _wrap(d, 500, ly + 46, desc, font(20), INK_LT, max_w=230)
        ly += 130
    divider(d, 1000)
    return img


def seg_tasting():
    img, d = new_canvas(1150)
    y = 40
    y = sec_title(d, y, "TASTING NOTES", "品鉴指南") + 6
    # 品鉴场景图
    ph = load_photo(IMG_TASTE, max_w=400, max_h=420)
    d.rounded_rectangle([(W-440)//2 - 14, y, (W-440)//2 + 414, y + 440], radius=14,
                        fill=(255,255,255), outline=(224,210,186))
    img.paste(ph, ((W - ph.width)//2, y + 18))
    y += 470
    # 四格
    items = [
        ("观色", "色如琥珀，温润有光"),
        ("闻香", "谷物发酵香，果干蜂蜜交织"),
        ("尝味", "甘酸苦辛鲜，五味平和"),
        ("品格", "奔放醇厚，回味悠长"),
    ]
    cw = (W - 60 - 30) // 4
    for i, (t1, t2) in enumerate(items):
        x0 = 30 + i * (cw + 10)
        d.rounded_rectangle([x0, y, x0 + cw, y + 150], radius=10,
                            fill=(255,255,255), outline=(224,210,186))
        d.text((x0 + cw/2 - d.textlength(t1, font=font(26, bold=True))/2, y + 14),
               t1, font=font(26, bold=True), fill=RED)
        _wrap(d, x0 + 12, y + 62, t2, font(16), INK_LT, max_w=cw - 24)
    y += 180
    return img


def seg_craft():
    img, d = new_canvas(1200)
    y = 40
    y = sec_title(d, y, "ORIGIN & CRAFT", "原料与工艺") + 6
    # 上：鉴湖水引文
    quote = "“盖山阴、会稽之间，水最宜酒，易地则不能为良”"
    fq = font(22)
    tw = d.textlength(quote, font=fq)
    d.text(((W - tw)/2, y), quote, font=fq, fill=INK_LT)
    center_text(d, y + 40, "——清·梁章钜《浪迹续谈》论鉴湖水", font(16, song=False), fill=GOLD)
    y += 90
    # 左车间图 右酒曲
    ph = load_photo(IMG_SHOP, max_w=340, max_h=400)
    d.rounded_rectangle([30, y, 380, y + 420], radius=12, fill=(255,255,255),
                        outline=(224,210,186))
    img.paste(ph, (30 + (350-ph.width)//2, y + 10 + (410-ph.height)//2))
    ph2 = load_photo(IMG_QU, max_w=300, max_h=400)
    d.rounded_rectangle([400, y, W-30, y + 420], radius=12, fill=(255,255,255),
                        outline=(224,210,186))
    img.paste(ph2, (400 + (320-ph2.width)//2, y + 10 + (410-ph2.height)//2))
    y += 450
    # 工艺步骤
    steps = ["春耕", "夏生", "秋收", "冬酿", "封藏"]
    cw = (W - 60 - 40) // 5
    for i, s in enumerate(steps):
        x0 = 30 + i * (cw + 10)
        d.rounded_rectangle([x0, y, x0 + cw, y + 96], radius=10,
                            fill=BG_DARK, outline=GOLD)
        f = font(26, bold=True)
        d.text((x0 + cw/2 - d.textlength(s, font=f)/2, y + 30),
               s, font=f, fill=GOLD_LT)
    y += 130
    center_text(d, y, "110 余道工序 · 手工古法 · 一冬一酿", font(22), fill=INK)
    return img


def seg_story():
    img, d = new_canvas(1150)
    y = 40
    y = sec_title(d, y, "280 YEARS HERITAGE", "品牌故事") + 10
    # 左：酒坛图（放大铺满） 右：时间线文字
    ph = load_photo(IMG_TAN, max_w=310, max_h=700)
    d.rounded_rectangle([36, y, 360, y + 720], radius=12, fill=(255,255,255),
                        outline=(224,210,186))
    img.paste(ph, (36 + (324-ph.width)//2, y + 10 + (710-ph.height)//2))
    # 时间线（4条拉宽行距铺满右列）
    tl = [
        ("1738", "钟氏先祖肇始，乾隆年间立坊"),
        ("1941", "烽火毁坊，一坛老酒藏于故土"),
        ("2019", "钟大年、钟亦鸣两代传人重振"),
        ("今 日", "茂丰原浆2006，原坛开藏"),
    ]
    ty = y + 30
    for year, txt in tl:
        d.ellipse([430, ty + 6, 442, ty + 18], fill=GOLD)
        d.text((460, ty), year, font=font(26, bold=True), fill=RED)
        _wrap(d, 460, ty + 38, txt, font(19), INK_LT, max_w=250)
        ty += 160
    # 底部品牌语补足左列
    _wrap(d, 52, y + 750, "280 年，七代人的一坛酒。", font(20), INK, max_w=290)
    return img


def seg_specs():
    img, d = new_canvas(1050)
    y = 40
    y = sec_title(d, y, "PRODUCT INFO", "产品参数") + 16
    specs = [
        ("品牌", "钟茂丰"), ("品名", "手工绍兴黄酒 茂丰原浆2006年冬酿"),
        ("系列", "原浆系列"), ("净含量", "500ml"),
        ("酒精度", "≥14.0%vol"), ("含糖量", "15.1-40.0g/L"),
        ("配料", "鉴湖水、糯米、小麦、焦糖色"),
        ("酒龄", "20年陈酿原浆"), ("质量等级", "优等品"),
        ("产地", "浙江绍兴"), ("保质期", "10年"),
        ("产品标准号", "GB/T 17946"),
    ]
    y0 = y
    for i, (k, v) in enumerate(specs):
        yy = y + (i // 2) * 62
        col = i % 2
        x0 = 40 + col * 350
        d.rounded_rectangle([x0, yy, x0 + 330, yy + 52], radius=8,
                            fill=(245, 241, 232), outline=(232, 222, 204))
        d.text((x0 + 16, yy + 11), k, font=font(18, bold=True), fill=GOLD)
        fv = font(17)
        tv = d.textlength(v, font=fv)
        if tv > 190:
            v = v[:11] + "…"
        d.text((x0 + 100, yy + 13), v, font=fv, fill=INK)
    y += (len(specs) // 2 + 1) * 62 + 20
    divider(d, y)
    return img


def seg_footer():
    img, d = new_canvas(900)
    # 红礼盒透明抠图，贴暖金卡片
    d.rounded_rectangle([(W - 520)//2, 34, (W - 520)//2 + 520, 520], radius=16,
                        fill=(240, 234, 222), outline=(224, 210, 186))
    cut = load_cut(IMG_GIFT, max_w=430, max_h=430)
    if cut:
        paste_cut_center(img, cut, cx=W // 2, cy=277, shadow=True)
    center_text(d, 560, "茂丰原浆 · 2006年冬酿", font(38, bold=True), fill=INK)
    center_text(d, 630, "送礼有面子 · 自饮有味道 · 收藏有价值", font(20), fill=INK_LT)
    center_text(d, 700, "— 钟茂丰 始于乾隆 · 280年老字号 —", font(18, song=False), fill=GOLD)
    center_text(d, 780, "过度饮酒有害健康 · 孕妇及儿童不宜饮酒 · 请勿酒后驾车",
                font(13, song=False), fill=(150, 140, 128))
    return img


def _wrap(d, x, y, text, f, fill, max_w, lh=None):
    """按 max_w 折行绘制（PIL 无自动换行）"""
    lh = lh or int(f.size * 1.4)
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


def main():
    print("渲染长图 7 屏…")
    segs = [seg_main_visual(), seg_selling_points(), seg_tasting(),
            seg_craft(), seg_story(), seg_specs(), seg_footer()]
    total_h = sum(s.height for s in segs)
    print(f"总高 {total_h}px（{total_h/1500:.1f} 屏@750宽）")
    canvas = Image.new("RGB", (W, total_h), BG)
    y = 0
    for s in segs:
        canvas.paste(s.convert("RGB"), (0, y))
        y += s.height
    out = "/Users/local/Desktop/钟茂丰/茂丰原浆2006年冬酿_详情长图.png"
    canvas.save(out)
    print(f"✓ 已保存: {out} ({total_h}px 高)")
    return out


if __name__ == "__main__":
    main()
