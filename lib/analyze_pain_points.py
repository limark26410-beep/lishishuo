"""
AI 痛点×卖点分析工具（GL-20260904）
输入商品资料文本 → DeepSeek 推理用户痛点 → 卖点映射 → 话术钩子
输出: 屏幕报告 + <out_dir>/pain_points.json（结构化，供工作台/脚本生成复用）

用法: python lib/analyze_pain_points.py "<商品资料文本或文件路径>" [输出目录]
"""
import os, sys, json, re
import requests

KEY = os.environ.get("DEEPSEEK_API_KEY", "") or open(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".env"),
    encoding="utf-8").read().split("DEEPSEEK_API_KEY=")[1].splitlines()[0].strip()

PROMPT_TPL = """你是资深电商选品/短视频营销策划。请分析下面这款商品的【用户痛点】，并映射到商品卖点。

【商品资料】
{product_info}

请输出（严格 JSON，不要多余文字）：
{{
  "product": "商品名",
  "audiences": [
    {{"name": "人群名(如:35-55岁商务男性)", "desc": "一句话画像",
     "pain_points": [{{"pain": "具体场景化痛点(要扎心真实,参考该品类真实用户吐槽)", "intensity": "高/中/低"}}]}}
  ],
  "mappings": [
    {{"audience": "人群名", "pain": "痛点原文", "selling_point": "对应卖点(必须来自商品资料,不许编造)"}}
  ],
  "hooks": ["3个短视频开头钩子(可直接口播)", "..."],
  "compliance_notes": ["广告法风险提示(若有夸大/极限词则指出)", "..."]
}}

要求：
1. 痛点必须真实反映该品类用户的典型抱怨（可参考：口感/价格/送礼/健康顾虑/使用场景/品牌信任/颜值包装/科技狠活等角度）
2. 卖点映射只能使用商品资料里真实存在的信息，不得虚构
3. 3个钩子要能直接当短视频开头，锋利、有画面感
"""


def analyze(product_text: str) -> dict:
    r = requests.post(
        "https://api.deepseek.com/chat/completions",
        headers={"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"},
        json={"model": "deepseek-chat",
              "messages": [{"role": "user",
                            "content": PROMPT_TPL.format(product_info=product_text)}],
              "temperature": 0.7, "max_tokens": 3000},
        timeout=180)
    if r.status_code != 200:
        raise RuntimeError(f"DeepSeek 调用失败: {r.status_code} {r.text[:200]}")
    content = r.json()["choices"][0]["message"]["content"]
    # 提取 JSON（模型可能包在 ```json 里）
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", content, re.S)
    raw = m.group(1) if m else content
    m2 = re.search(r"\{.*\}", raw, re.S)
    if m2:
        raw = m2.group(0)
    return json.loads(raw)


def _fmt_report(d: dict) -> str:
    lines = [f"\n🎯 商品: {d.get('product', '')}", "=" * 46]
    for a in d.get("audiences", []):
        lines.append(f"\n👥 {a.get('name', '')} — {a.get('desc', '')}")
        for p in a.get("pain_points", []):
            lines.append(f"   · [{p.get('intensity', '')}] {p.get('pain', '')}")
    lines.append(f"\n🔗 痛点→卖点映射 ({len(d.get('mappings', []))}条)")
    for m in d.get("mappings", []):
        lines.append(f"   · {m.get('pain', '')} → {m.get('selling_point', '')}")
    lines.append("\n🎬 话术钩子")
    for i, h in enumerate(d.get("hooks", []), 1):
        lines.append(f"   {i}. {h}")
    if d.get("compliance_notes"):
        lines.append("\n⚠️ 合规提示")
        for c in d["compliance_notes"]:
            lines.append(f"   · {c}")
    return "\n".join(lines)


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) > 1 else ""
    out_dir = sys.argv[2] if len(sys.argv) > 2 else "."
    if not arg:
        print("用法: python lib/analyze_pain_points.py \"商品资料文本\" 或 文件路径")
        sys.exit(1)
    if os.path.exists(arg):
        product_text = open(arg, encoding="utf-8").read().strip()
    else:
        product_text = arg
    print("⏳ AI 推理痛点分析中（约 30-60s）...")
    result = analyze(product_text)
    print(_fmt_report(result))
    os.makedirs(out_dir, exist_ok=True)
    out = os.path.join(out_dir, "pain_points.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f"\n📄 结构化结果已存: {out}")
