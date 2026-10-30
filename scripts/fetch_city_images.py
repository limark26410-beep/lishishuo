# -*- coding: utf-8 -*-
"""古都系列素材抓取：Pexels 实拍 + Wikimedia Commons 补充 → 裁 9:16 → 统一调色。

用法:
    python scripts/fetch_city_images.py <期号> [--dry-run]

双源策略：
- Pexels：热门地标（西安城墙、兵马俑、故宫、龙门石窟…），免费商用无需署名
- Commons：冷门地标（殷墟、甲骨文、丛台…），CC 授权，简介需署名（记录在 sources.md）
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
for _d in (r"C:\ffmpeg\bin", r"C:\MV_WT\tools\ffmpeg"):
    if not shutil.which("ffmpeg") and os.path.exists(os.path.join(_d, "ffmpeg.exe")):
        os.environ["PATH"] = _d + os.pathsep + os.environ.get("PATH", "")
FFMPEG = shutil.which("ffmpeg") or "ffmpeg"

# Commons / Pexels 必须走代理（国内直连 Commons 不通）。
# 默认本机代理 127.0.0.1:7897；可用环境变量 LISHISHUO_PROXY 覆盖，
# 设成 direct/none/off 可强制直连（不建议，Commons 会失败）。
_PROXY = os.environ.get("LISHISHUO_PROXY", "http://127.0.0.1:7897").strip()
if _PROXY and _PROXY.lower() not in ("direct", "none", "off"):
    os.environ.setdefault("HTTP_PROXY", _PROXY)
    os.environ.setdefault("HTTPS_PROXY", _PROXY)
    os.environ.setdefault("ALL_PROXY", _PROXY)

# 每期素材源配置
# 经验（实测）：Pexels 只适合"明确地标英文名"（Terracotta Army / Forbidden City），
# 抽象概念（Warring States bronze）会搜出二战纪念碑等无关图；
# Commons 用【中文精准词】命中率最高（"丛台""龙门石窟""开封铁塔"）。
SOURCES = {
    # 161 安阳
    "161": {
        "pexels": [("Chinese bronze museum", 3)],
        "commons": [("殷墟", 6), ("Oracle bone inscription", 5), ("Anyang Henan", 3),
                    ("Yinxu museum", 3), ("Shang dynasty bronze", 3)],
    },
    # 162 西安（Pexels 明确地标，已验证准确）
    "162": {
        "pexels": [("Xian city wall", 4), ("Terracotta Army", 4), ("Xian Bell Tower", 3),
                   ("Big Wild Goose Pagoda", 3), ("Xian night city wall", 2)],
        "commons": [],
    },
    # 163 洛阳
    "163": {
        "pexels": [("Luoyang China", 3)],
        "commons": [("龙门石窟", 5), ("白马寺", 4), ("Luoyang ancient city", 3),
                    ("Longmen Grottoes Buddha", 3), ("洛阳博物馆", 3)],
    },
    # 164 邯郸（Pexels 完全无覆盖，全用 Commons 中文词）
    "164": {
        "pexels": [],
        "commons": [("丛台", 6), ("赵王城遗址", 5), ("娲皇宫", 4),
                    ("广府古城", 3), ("响堂山", 3)],
    },
    # 165 开封
    "165": {
        "pexels": [("Kaifeng China", 3)],
        "commons": [("开封铁塔", 5), ("清明上河园", 4), ("Kaifeng ancient city", 3),
                    ("开封龙亭", 3), ("Song dynasty painting", 3)],
    },
    # 166 南京
    "166": {
        "pexels": [("Nanjing China city", 3)],
        "commons": [("南京城墙", 5), ("明孝陵", 4), ("Nanjing ancient architecture", 3),
                    ("中山陵", 3), ("Nanjing Confucius Temple", 3)],
    },
    # 167 北京（Pexels 明确地标，已验证准确）
    "167": {
        "pexels": [("Forbidden City Beijing", 4), ("Temple of Heaven Beijing", 3),
                   ("Beijing old street hutong", 3), ("Great Wall China", 3),
                   ("Beijing cityscape traditional", 3)],
        "commons": [],
    },
    # ===== 十大文豪系列 =====
    # 168 屈原（端午/龙舟/楚文化）
    # 179 班昭（史书/竹简/东观/女师）
    "179": {
        "pexels": [("ancient chinese books manuscript", 4), ("bamboo slips writing", 4),
                   ("chinese ink calligraphy scroll", 3), ("ancient chinese library", 3)],
        "commons": [("班昭", 4), ("汉书", 4), ("班固", 3), ("曹大家", 2),
                    ("Han dynasty bamboo slips", 4)],
    },
    # 180 蔡文姬（草原/胡笳/归汉）
    "180": {
        "pexels": [("mongolian steppe grassland horse", 4), ("grassland sunset nomad", 3),
                   ("chinese guqin zither", 3), ("chinese ink landscape mist", 4)],
        "commons": [("蔡文姬", 5), ("胡笳十八拍", 4), ("文姬归汉", 3),
                    ("蔡邕", 2), ("Mongolian steppe", 2)],
    },
    # 181 苏蕙（织锦/回文/璇玑图）
    "181": {
        "pexels": [("chinese silk weaving loom", 5), ("chinese silk embroidery", 4),
                   ("colorful silk fabric texture", 3), ("chinese ancient textile", 3)],
        "commons": [("苏蕙", 3), ("璇玑图", 4), ("回文锦", 2), ("织锦", 4), ("窦滔", 2)],
    },
    # 182 谢道韫（东晋/咏絮/雪/清谈）
    "182": {
        "pexels": [("snow falling willow tree", 5), ("snowy winter landscape china", 4),
                   ("chinese ink painting snow", 3), ("chinese bamboo snow", 3)],
        "commons": [("谢道韫", 4), ("咏絮", 3), ("谢安", 3), ("王羲之 兰亭", 3),
                    ("东晋 画像砖", 3)],
    },
    # 183 上官婉儿（唐宫/诏书/诗文）
    "183": {
        "pexels": [("chinese ancient palace architecture", 4), ("chinese calligraphy brush ink", 4),
                   ("chinese tang dynasty style", 3), ("chinese lantern palace night", 3)],
        "commons": [("上官婉儿", 5), ("武则天", 3), ("唐代 宫廷", 3), ("唐 壁画 仕女", 4)],
    },
    # 184 薛涛（成都/浣花溪/纸笺/诗）
    "184": {
        "pexels": [("chinese rice paper texture", 4), ("chinese calligraphy brush", 4),
                   ("chengdu china river park", 3), ("chinese ink painting bamboo", 3)],
        "commons": [("薛涛", 4), ("薛涛笺", 3), ("浣花溪", 3), ("望江楼", 3), ("唐代 仕女图", 3)],
    },
    # 185 管道昇（元/墨竹/书画/夫妻）
    "185": {
        "pexels": [("chinese ink bamboo painting", 5), ("chinese calligraphy brush ink", 4),
                   ("chinese ink landscape mist", 3), ("chinese ancient painting scroll", 3)],
        "commons": [("管道昇", 4), ("赵孟頫", 4), ("墨竹", 5), ("元代 绘画", 3)],
    },
    # 186 柳如是（明末/秦淮/绛云楼/书画）
    "186": {
        "pexels": [("chinese ancient garden pavilion", 4), ("chinese ink landscape mist", 4),
                   ("chinese calligraphy scroll", 3), ("chinese ancient architecture", 3)],
        "commons": [("柳如是", 5), ("钱谦益", 3), ("秦淮河", 3), ("明末 仕女图", 3),
                    ("绛云楼", 2)],
    },
    # 187 秋瑾（清末/绍兴/女报/就义）
    "187": {
        "pexels": [("shaoxing china ancient town", 4), ("chinese old newspaper", 3),
                   ("chinese ancient archway street", 3), ("chinese lake sunset hangzhou", 3)],
        "commons": [("秋瑾", 5), ("绍兴 轩亭口", 3), ("中国女报", 3), ("西湖 秋瑾墓", 3),
                    ("清末 女子", 3)],
    },
    # 178 卓文君（汉代/古琴/酒肆/临邛）
    "178": {
        "pexels": [("chinese guqin zither", 4), ("ancient chinese wine jar", 4),
                   ("bamboo slips ancient china", 3), ("chinese ink landscape mist", 3)],
        "commons": [("卓文君", 4), ("司马相如", 3), ("文君井", 3), ("古琴", 4),
                    ("汉代画像砖", 4), ("Han dynasty wine vessel", 3)],
    },
    "168": {
        "pexels": [("dragon boat race", 4), ("ancient chinese bronze", 3),
                   ("chinese calligraphy brush", 3), ("chinese river mountain landscape", 3)],
        "commons": [("屈原", 5), ("Chu state bronze", 4), ("九歌", 3), ("离骚", 3),
                    ("曾侯乙编钟", 3), ("汨罗江", 2), ("Duanwu Festival", 2), ("楚国地图", 2)],
    },
    # 169 司马迁（史官/竹简/祠庙）
    "169": {
        "pexels": [("chinese ancient temple hall", 4), ("bamboo slips writing", 3),
                   ("chinese ancient architecture", 4), ("chinese history museum", 3)],
        "commons": [("司马迁祠", 4), ("韩城", 3), ("史记", 3)],
    },
    # 170 陶渊明（田园/菊花/乡村）
    "170": {
        "pexels": [("chinese rice terrace countryside", 4), ("chrysanthemum flower", 3),
                   ("chinese ancient village", 4), ("chinese mountain field", 3)],
        "commons": [("陶渊明", 4), ("庐山", 3), ("桃花源", 3)],
    },
    # 171 李白（山水/月/酒）
    "171": {
        "pexels": [("chinese mountain mist landscape", 4), ("full moon night china", 3),
                   ("chinese rice wine cup", 3), ("chinese waterfall mountain", 4)],
        "commons": [("李白", 4), ("太白楼", 3), ("采石矶", 3)],
    },
    # 172 杜甫（草堂/园林/江）
    "172": {
        "pexels": [("chinese thatched cottage", 3), ("chinese classical garden", 4),
                   ("chinese river landscape", 3), ("chinese ancient city wall", 4)],
        "commons": [("杜甫草堂", 5), ("杜甫", 3)],
    },
    # 173 白居易（西湖/桥/塔）
    "173": {
        "pexels": [("west lake hangzhou", 4), ("chinese ancient bridge", 3),
                   ("chinese pagoda temple", 4), ("chinese garden pond", 3)],
        "commons": [("白居易", 3), ("白堤", 3), ("香山寺", 3)],
    },
    # 174 苏轼（祠庙/湖桥/竹）
    "174": {
        "pexels": [("chinese temple architecture", 4), ("chinese lake bridge", 3),
                   ("chinese bamboo forest", 4), ("chinese ancient town", 3)],
        "commons": [("三苏祠", 4), ("苏堤", 3), ("东坡书院", 3)],
    },
    # 175 李清照（荷/园/春景）
    "175": {
        "pexels": [("chinese lotus pond", 4), ("chinese classical garden", 3),
                   ("chinese spring blossom", 3), ("chinese ink wash style", 4)],
        "commons": [("李清照", 4), ("趵突泉", 3), ("大明湖", 3)],
    },
    # 176 辛弃疾（战场/关塞/剑）
    "176": {
        "pexels": [("chinese ancient battlefield", 3), ("chinese mountain fortress", 3),
                   ("chinese ancient sword", 3), ("great wall china", 4)],
        "commons": [("辛弃疾", 4), ("稼轩", 3), ("上饶", 3)],
    },
    # 177 曹雪芹（园林/宅院/梅花）
    "177": {
        "pexels": [("chinese classical garden", 4), ("chinese ancient mansion", 3),
                   ("chinese plum blossom", 3), ("chinese traditional courtyard", 4)],
        "commons": [("曹雪芹", 3), ("红楼梦", 4), ("大观园", 3)],
    },
}


def _grade_filter() -> str:
    """统一调色：压暗 + 降饱和 + 略暖（向历史纪录片质感靠）"""
    return (
        "scale=1080:1920:force_original_aspect_ratio=increase,"
        "crop=1080:1920,"
        "eq=brightness=-0.06:saturation=0.82:contrast=1.06,"
        "colorbalance=rs=0.04:gs=0.02:bs=-0.04"
    )


def load_pexels_key() -> str:
    key = os.environ.get("PEXELS_API_KEY", "").strip()
    if key:
        return key
    env = REPO_ROOT / ".env"
    for ln in env.read_text(encoding="utf-8").splitlines():
        if ln.strip().startswith("PEXELS_API_KEY"):
            return ln.split("=", 1)[1].strip()
    raise SystemExit("PEXELS_API_KEY 未配置")


def search_pexels(key: str, query: str, want: int) -> list[dict]:
    """搜索 Pexels 竖版高分辨率图片"""
    import urllib.parse
    q = urllib.parse.quote(query)
    url = (f"https://api.pexels.com/v1/search?query={q}"
           f"&per_page={max(want * 3, 9)}&orientation=portrait&size=large")
    r = subprocess.run(["curl", "-s", "--connect-timeout", "15", "--max-time", "30",
                        url, "-H", f"Authorization: {key}"],
                       capture_output=True, text=True)
    try:
        d = json.loads(r.stdout)
    except Exception:
        return []
    out = []
    for p in d.get("photos", []):
        if p["height"] < p["width"]:
            continue
        out.append({
            "url": p["src"].get("large2x") or p["src"].get("original"),
            "width": p["width"], "height": p["height"],
            "alt": (p.get("alt") or "")[:60],
            "photographer": p.get("photographer", ""),
            "page": p.get("url", ""),
            "license": "Pexels License",
        })
    return out[:want]


# 文件名/标题黑名单：命中即跳过（现代设施、无关主体、同名误匹配）
BLACKLIST = [
    "oracle campus", "austin", "metro", "station", "地铁", "tv tower", "电视塔",
    "bus ", "airport", "hotel", "restaurant", "shopping", "mall", "stadium",
    "star", "汽车", "车展", "公路", "highway", "office", "company",
    "world war", "monument of soldiers", "memorial",
]


def search_commons(query: str, want: int) -> list[dict]:
    """搜索 Wikimedia Commons（补冷门地标）。只取竖版/近方形图，记录许可与作者。"""
    import urllib.parse
    q = urllib.parse.quote(query)
    url = ("https://commons.wikimedia.org/w/api.php?action=query"
           f"&generator=search&gsrsearch={q}&gsrnamespace=6&gsrlimit={want * 4}"
           "&prop=imageinfo&iiprop=url|size|extmetadata&iiurlwidth=1600&format=json")
    r = subprocess.run(["curl", "-s", "--connect-timeout", "15", "--max-time", "40",
                        url, "-H", "User-Agent: lishishuo-image-fetch/1.0"],
                       capture_output=True, text=True)
    try:
        d = json.loads(r.stdout)
    except Exception:
        return []
    pages = (d.get("query") or {}).get("pages", {}) or {}
    out = []
    for p in pages.values():
        ii = (p.get("imageinfo") or [{}])[0]
        w, h = ii.get("width", 0), ii.get("height", 0)
        src = ii.get("thumburl") or ii.get("url")
        if not src or not w or not h:
            continue
        # 只接受真图片（Commons 会混进 PDF/SVG/DJVU）
        base = src.lower().split("?")[0]
        if not base.endswith((".jpg", ".jpeg", ".png")):
            continue
        if not re.search(r"\.(jpg|jpeg|png)$", p.get("title", "").lower()):
            continue
        if w < 800:
            continue
        # 黑名单过滤（现代设施/同名误匹配）
        title_l = p.get("title", "").lower()
        if any(b in title_l for b in BLACKLIST):
            continue
        meta = ii.get("extmetadata", {}) or {}
        lic = (meta.get("LicenseShortName", {}) or {}).get("value", "unknown")
        artist = re.sub(r"<[^>]+>", "", (meta.get("Artist", {}) or {}).get("value", ""))[:40]
        out.append({
            "url": src, "width": w, "height": h,
            "alt": re.sub(r"<[^>]+>", "", p.get("title", "")).replace("File:", "")[:60],
            "photographer": artist or "Wikimedia contributor",
            "page": ii.get("descriptionurl", ""),
            "license": lic,
        })
        if len(out) >= want:
            break
    return out


def download_and_grade(url: str, out_path: str) -> bool:
    """下载 + 竖屏化（竖版直裁；横版模糊填充，保住完整画面）+ 调色"""
    tmp = os.path.join(tempfile.gettempdir(), "_city_fetch_raw.jpg")
    subprocess.run(["curl", "-sL", "--connect-timeout", "20", "--max-time", "90",
                    "-H", "User-Agent: lishishuo-image-fetch/1.0",
                    url, "-o", tmp], capture_output=True, text=True)
    if not os.path.exists(tmp) or os.path.getsize(tmp) < 5000:
        return False

    # 探测尺寸与格式
    probe = subprocess.run([FFMPEG, "-i", tmp],
                           capture_output=True, text=True)
    m = re.search(r"(\d{3,5})x(\d{3,5})", probe.stderr)
    if not m:
        return False
    w, h = int(m.group(1)), int(m.group(2))
    is_portrait = h / max(w, 1) >= 0.9

    grade = ("eq=brightness=-0.06:saturation=0.82:contrast=1.06,"
             "colorbalance=rs=0.04:gs=0.02:bs=-0.04")

    if is_portrait:
        vf = ("scale=1080:1920:force_original_aspect_ratio=increase,"
              f"crop=1080:1920,{grade}")
    else:
        # 横版：主体居中，上下用同图放大模糊填充（不裁掉内容）
        vf = (f"[0:v]scale=1080:-2,split[bg][fg];"
              f"[bg]scale=1080:1920:force_original_aspect_ratio=increase,"
              f"crop=1080:1920,gblur=sigma=28,{grade}[bgb];"
              f"[fg]{grade}[fgb];"
              f"[bgb][fgb]overlay=(W-w)/2:(H-h)/2")

    cmd = [FFMPEG, "-y", "-loglevel", "error", "-i", tmp]
    if is_portrait:
        cmd += ["-vf", vf]
    else:
        cmd += ["-filter_complex", vf]
    cmd += ["-q:v", "3", out_path]
    subprocess.run(cmd, capture_output=True, text=True)

    ok = os.path.exists(out_path) and os.path.getsize(out_path) > 10000
    if os.path.exists(tmp):
        os.remove(tmp)
    return ok


def main():
    episode = sys.argv[1] if len(sys.argv) > 1 else "162"
    dry = "--dry-run" in sys.argv
    cfg = SOURCES.get(episode)
    if not cfg:
        raise SystemExit(f"期号 {episode} 未配置关键词")

    key = load_pexels_key()
    out_dir = str(REPO_ROOT / "episodes" / episode / "images")
    os.makedirs(out_dir, exist_ok=True)

    sources = []
    idx = 0
    plan = [("pexels", cfg.get("pexels", [])), ("commons", cfg.get("commons", []))]
    for src_name, queries in plan:
        for query, want in queries:
            print(f"\n=== [{src_name}] {query} (要 {want} 张) ===")
            results = (search_pexels(key, query, want) if src_name == "pexels"
                       else search_commons(query, want))
            if not results:
                print("  ⚠ 无结果")
                continue
            for item in results:
                idx += 1
                slug = re.sub(r"[^a-z0-9]+", "_", query.lower()).strip("_")[:26]
                if not slug:      # 中文关键词 → 用稳定 hash 编号
                    import hashlib
                    slug = "cn" + hashlib.md5(query.encode()).hexdigest()[:6]
                fname = f"img_{idx:03d}_{src_name}_{slug}.jpg"
                out_path = os.path.join(out_dir, fname)
                if dry:
                    print(f"  [dry] {fname} ← {item['width']}x{item['height']} | {item['alt'][:50]}")
                    continue
                if download_and_grade(item["url"], out_path):
                    sz = os.path.getsize(out_path) // 1024
                    print(f"  ✅ {fname} ({sz}KB) | {item['alt'][:38]}")
                    sources.append({"file": fname, "source": src_name,
                                    "query": query, "alt": item["alt"],
                                    "photographer": item["photographer"],
                                    "page": item["page"], "license": item.get("license", "")})
                else:
                    print(f"  ❌ {fname} 失败")
                    idx -= 1
                time.sleep(0.4)

    if not dry:
        src_path = str(REPO_ROOT / "episodes" / episode / "sources.md")
        lines = [f"# 第{episode}期 图片来源", "",
                 "> Pexels：免费商用、无需署名。Wikimedia Commons：CC 授权，发布时请在简介注明来源。", ""]
        for s in sources:
            tag = "Pexels" if s["source"] == "pexels" else "Commons"
            lines.append(f"- `{s['file']}` ← [{tag}] [{s['query']}]({s['page']}) "
                         f"by {s['photographer']} ｜ {s.get('license','')} ｜ {s['alt']}")
        Path(src_path).write_text("\n".join(lines) + "\n", encoding="utf-8")
        n_pex = sum(1 for s in sources if s["source"] == "pexels")
        n_com = len(sources) - n_pex
        print(f"\n✅ 完成: {len(sources)} 张（Pexels {n_pex} + Commons {n_com}）→ {out_dir}")
        print(f"   来源记录: {src_path}")


if __name__ == "__main__":
    main()
