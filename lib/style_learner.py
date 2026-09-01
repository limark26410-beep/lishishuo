"""
个人风格学习模块（GL-20260901：AI 稿子越写越像使用者）

核心思路：AI 出稿是初稿，使用者在出片前会人工改稿。每一次「初稿 → 终稿」的
差异，就是使用者的一次风格表达。本模块：
1. 对比初稿/终稿，用 DeepSeek 提炼成 3~5 条可操作的风格规则
2. 累积到 persona.json（规则 + 历次真实改稿样例，做 few-shot 参考）
3. 供 ai_script_gen 写稿时注入 system prompt，让新稿子带使用者的味道

文件位置：
- persona.json 存 BASE_DIR（web_app 同目录），不提交 git
- 每期对比结果只追加不覆盖，历史可回溯
"""

import json
import os
import re
import time

import requests

_PERSONA_FILE = "persona.json"
_API_ENDPOINT = "https://api.deepseek.com/chat/completions"
_MODEL = "deepseek-v4-pro"
_TIMEOUT = 90
_MAX_RULES = 8          # 画像里保留的最多规则条数
_MAX_SAMPLES = 3        # 保留的最多「初稿→终稿」样例（few-shot 用）
_DIFF_SIM_THRESHOLD = 0.92   # 相似度高于此值视为"没怎么改"，不学习


def _sim_ratio(a: str, b: str) -> float:
    """字符级相似度（0~1）：基于公共子串长度 / 总长度 的粗糙估计。"""
    a, b = re.sub(r"\s+", "", a or ""), re.sub(r"\s+", "", b or "")
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    # LCS 字符数（简化：用集合交集加权 + 长度比）
    common = len(set(a) & set(b))
    return (common / max(len(set(a)), 1)) * 0.6 + (min(len(a), len(b)) / max(len(a), len(b))) * 0.4


def _load_persona(base_dir: str) -> dict:
    path = os.path.join(base_dir, _PERSONA_FILE)
    if os.path.exists(path):
        try:
            return json.load(open(path, encoding="utf-8"))
        except Exception:
            pass
    return {"rules": [], "samples": [], "learned_at": None, "episodes": 0}


def _save_persona(base_dir: str, persona: dict) -> str:
    path = os.path.join(base_dir, _PERSONA_FILE)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(persona, f, ensure_ascii=False, indent=2)
    return path


def get_persona(base_dir: str) -> dict:
    """读取画像（web 面板展示用）"""
    return _load_persona(base_dir)


def reset_persona(base_dir: str) -> dict:
    """清空画像重学"""
    persona = _load_persona(base_dir)
    persona["rules"] = []
    persona["samples"] = []
    persona["learned_at"] = None
    persona["episodes"] = 0
    _save_persona(base_dir, persona)
    return persona


def _analyze_diff(draft: str, final: str, api_key: str) -> list:
    """调 DeepSeek 分析初稿→终稿的差异，提炼风格规则（每条一句话，可直接复用）。"""
    system = (
        "你是一位文案风格分析师。给你同一篇稿子的「AI 初稿」和「创作者终稿」，"
        "请分析创作者改了什么，提炼出他个人的写作风格规则。\n"
        "只输出 JSON 数组，每项是一条规则（中文，一句话，能直接指导 AI 写稿），"
        "例如：\n"
        '["删掉\'大家好/欢迎收看\'类开场白，习惯用\'说实话/那一年\'直接入戏", '
        '"长句拆短句，单句不超过20字", '
        '"爱加生活化细节（几个人一合计、山还封着之类）"]\n'
        "要求：3~6 条，具体可操作，不要空泛的'口语化'这类描述，"
        "不要提'保持史实准确'这类通用要求，聚焦他个人的改法。"
    )
    user = f"【AI 初稿】\n{draft}\n\n【创作者终稿】\n{final}"
    try:
        resp = requests.post(
            _API_ENDPOINT,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "model": _MODEL,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "temperature": 0.4,
                "max_tokens": 500,
                "thinking": {"type": "disabled"},  # 与 ai_script_gen 一致：不关思维链 content 会被吃空
            },
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        text = resp.json()["choices"][0]["message"]["content"]
        if not text or not text.strip():
            return []
        m = re.search(r"\[.*\]", text, re.S)
        if not m:
            return []
        rules = json.loads(m.group(0))
        if not isinstance(rules, list):
            return []
        return [str(r).strip() for r in rules if str(r).strip()][:_MAX_RULES]
    except Exception as e:
        print(f"  ⚠ 风格分析失败: {e}")
        return []


def learn_from_edit(draft: str, final: str, api_key: str, base_dir: str) -> dict:
    """核心：学习一次改稿，更新 persona.json。

    返回 {"learned": bool, "reason": str, "persona": {...}, "path": str}
    - 没改/改太少 → learned=False（不消耗 LLM）
    - 成功 → 新规则与旧规则去重合并，追加样例（保留最近 _MAX_SAMPLES 个）
    """
    draft = (draft or "").strip()
    final = (final or "").strip()
    if not draft or not final:
        return {"learned": False, "reason": "初稿或终稿为空"}
    if _sim_ratio(draft, final) >= _DIFF_SIM_THRESHOLD:
        return {"learned": False, "reason": "这次没有改动（或改动极少），跳过学习"}

    persona = _load_persona(base_dir)
    rules = _analyze_diff(draft, final, api_key)
    if not rules:
        return {"learned": False, "reason": "风格分析未产出规则"}

    # 新规则去重合并（与已有规则关键词重叠的跳过）
    old_rules = list(persona.get("rules", []))
    merged = list(old_rules)
    for r in rules:
        key = r[:12]  # 前缀近似去重
        if not any(key in o or o[:12] in r for o in merged):
            merged.append(r)
    persona["rules"] = merged[:_MAX_RULES]

    # 追加样例（few-shot），保留最近 _MAX_SAMPLES 个
    samples = list(persona.get("samples", []))
    samples.append({"draft": draft[:1200], "final": final[:1200],
                    "at": time.strftime("%Y-%m-%d %H:%M")})
    persona["samples"] = samples[-_MAX_SAMPLES:]
    persona["learned_at"] = time.strftime("%Y-%m-%d %H:%M")
    persona["episodes"] = int(persona.get("episodes", 0)) + 1

    path = _save_persona(base_dir, persona)
    return {"learned": True, "reason": f"已学习 {len(rules)} 条新规则（累计 {len(merged)} 条）",
            "persona": persona, "path": path}


def persona_prompt(base_dir: str, max_samples: int = 1) -> str:
    """生成注入写稿 system prompt 的风格段落（无画像则空串）。"""
    persona = _load_persona(base_dir)
    rules = persona.get("rules") or []
    samples = persona.get("samples") or []
    if not rules:
        return ""
    lines = ["", "【模仿以下创作者的个人风格】你正在为这位创作者代笔，稿子要像他写的："]
    for i, r in enumerate(rules, 1):
        lines.append(f"{i}. {r}")
    if samples and max_samples > 0:
        lines.append("参考他最近一次的真实改稿样例（AI初稿→他的终稿），体会他的改法：")
        s = samples[-1]
        lines.append(f"—— AI 初稿 ——\n{s.get('draft','')[:600]}")
        lines.append(f"—— 他的终稿 ——\n{s.get('final','')[:600]}")
    return "\n".join(lines) + "\n"
