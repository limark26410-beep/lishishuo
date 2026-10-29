#!/usr/bin/env python3
"""
茂丰原浆2006冬酿 · AI 补缺场景图（qwen-image-3.0-pro 免费额度）
只生成真图没有的氛围场景：鉴湖风光 / 老酒坊（品牌故事缺图）
9:16 竖版 1080x1920，国风写实无文字
用法: ./venv/bin/python tools/gen_scene_imgs.py
"""
import os, time
import requests

KEY = os.environ.get("DASHSCOPE_API_KEY", "") or open(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".env"),
    encoding="utf-8").read().split("DASHSCOPE_API_KEY=")[1].splitlines()[0].strip()

URL = "https://dashscope.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation"
OUT = os.path.expanduser("~/Desktop/钟茂丰/AI补图")
os.makedirs(OUT, exist_ok=True)

PREFIX = ("国风高级绍兴黄酒电商详情页，写实商业产品摄影，柔和漫射自然光，电影氛围感，"
          "低饱和配色，主色调琥珀棕、米宣纸白、炭墨黑，画面大量留白，构图大气，"
          "无任何文字，无水印，无logo，高清8K，9:16竖版")

SCENES = {
    "06_鉴湖风光": "浙江绍兴鉴湖风光，江南水乡湖面，平静清澈湖水，远处绿树远山，清晨柔和薄雾，秋日氛围，自然写实摄影，画面大量留白",
    "09_老酒坊": "复古复原清代绍兴老酒坊，木结构老房子，院内一排排老陶酒坛，淡淡薄雾，黄昏柔和光影，古朴沧桑历史氛围，不要现代元素，写实复原老作坊场景",
}


def gen_one(name: str, desc: str, retries=4):
    prompt = PREFIX + "，" + desc
    body = {"model": "qwen-image-3.0-pro",
            "input": {"messages": [{"role": "user",
                                    "content": [{"text": prompt}]}]},
            "parameters": {"size": "1080*1920", "n": 1,
                           "prompt_extend": True, "watermark": False}}
    for a in range(retries):
        try:
            r = requests.post(URL,
                              headers={"Authorization": f"Bearer {KEY}"},
                              json=body, timeout=240)
            if r.status_code == 200:
                d = r.json()
                ch = (d.get("output") or {}).get("choices") or []
                content = ch[0].get("message", {}).get("content") or [] if ch else []
                url = next((c.get("image") for c in content if c.get("image")), None)
                if url:
                    dl = requests.get(url, timeout=120)
                    p = os.path.join(OUT, f"{name}.png")
                    open(p, "wb").write(dl.content)
                    print(f"  ✓ {name}.png ({len(dl.content)//1024}KB)")
                    return True
            msg = r.text[:120] if r.status_code != 200 else "无图URL"
            print(f"  ⚠ {name} 第{a+1}次: {msg}")
            time.sleep(20 if "quota" in msg.lower() else 8)
        except Exception as e:
            print(f"  ⚠ {name} 异常: {str(e)[:100]}")
            time.sleep(8)
    print(f"  ✗ {name} 失败")
    return False


if __name__ == "__main__":
    print(f"AI 补 {len(SCENES)} 张缺图 → {OUT}\n")
    for i, (name, desc) in enumerate(SCENES.items(), 1):
        print(f"[{i}/{len(SCENES)}] {name}")
        gen_one(name, desc)
        time.sleep(3)
    print("\n完成")
