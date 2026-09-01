"""
AI 智能出稿模块
封装 DeepSeek（deepseek-v4-pro）API 调用：输入自然语言指令 → 输出结构化稿件。

兼容 lishishuo 1.0 流水线：
- script.txt: hook + 正文（不含标题行；标题走 run.py --title 参数，
  避免 edge-tts 把「【标题】xxx」整行朗读出来）
- prompts.json: [{"id": "auto_01", "title": "AI 生成", "prompts": [...]}]
  与 run.py step_image_gen() 解析逻辑兼容
"""

import json
import os
import re
import time

import requests

API_ENDPOINT = "https://api.deepseek.com/chat/completions"
MODEL = "deepseek-v4-pro"
TIMEOUT = 60          # 秒
MAX_RETRIES = 2       # 最多重试 2 次（首次 + 2 次重试）


class AIScriptError(Exception):
    """AI 出稿异常。raw 保存 LLM 原文便于排查。"""

    def __init__(self, msg, raw=""):
        super().__init__(msg)
        self.raw = raw


# GL-20260828：创作类型 → 写稿指令（追加进 system prompt）+ fetch_keywords 关键词风格
_TYPE_INSTRUCTIONS = {
    "商品": (
        "【本次类型：商品带货/测评】\n"
        "- 结构：卖点钩子（3秒）→ 场景痛点 → 产品亮点拆解（2-3个，多数据/对比）→ "
        "使用体验 → 转化收尾（自然带出，不喊口号）\n"
        "- 语气：口语化、有说服力、具体（尺寸/材质/效果数据），别空泛\n"
        "- fetch_keywords 偏向产品实拍、使用场景、细节特写类画面词"),
    "故事": (
        "【本次类型：人物/历史故事】\n"
        "- 结构：悬念钩子 → 时间线叙事（每段一个情节）→ 转折 → 情感共鸣金句收尾\n"
        "- 语气：纪录片旁白、娓娓道来、有画面感，像导演在镜头前讲故事\n"
        "- fetch_keywords 偏向纪录片、历史、人物场景类画面词"),
    "营销": (
        "【本次类型：品牌/引流营销】\n"
        "- 结构：强钩子（3秒抓住）→ 痛点共鸣 → 价值主张 → 行动号召（自然收尾）\n"
        "- 语气：短句、有力、节奏感强，像广告旁白\n"
        "- fetch_keywords 偏向宣传片、品牌、高冲击画面类词"),
}


# GL-20260901：文风预设（AI 出稿可选，注入 system prompt 追加段落）
_WSTYLE_INSTRUCTIONS = {
    "yuqiuyu": (
        "【本次文风：余秋雨式文化散文】\n"
        "- 从一件具体的器物/地点/画面起笔（一尊石像、一座塔、一页残卷、一条古道），"
        "先写看得见的东西，再引向看不见的历史与人心\n"
        "- 有历史纵深：把眼前之物放到千年尺度里写，用年代、王朝、地理作背景\n"
        "- 融入哲思与悲悯：文明、废墟、宿命、时间，让人在结尾感到一种苍凉的美\n"
        "- 句式讲究韵律：长短句交错，偶用对仗与排比，但不过度堆砌\n"
        "- 结尾一句格言式升华收束（如'于是，历史在这里沉默了'这类），不喊口号\n"
        "- 语气沉稳、克制、有分量，像一位学者在废墟前独自沉吟，而非激情演说"),
    "storyteller": (
        "【本次文风：讲书人式娓娓道来（《明朝那些事儿》感）】\n"
        "- 用大白话讲正史，像老朋友聊天，轻松但不轻浮\n"
        "- 常插入一句俏皮点评或现代类比（如'相当于今天的……'），拉近距离\n"
        "- 人物当活人写：有性格、有小动作、有心理活动\n"
        "- 节奏明快，多用短句，段落短小\n"
        "- 收尾干脆，常留一句余味或反转让读者回味"),
    "suspense": (
        "【本次文风：说书人式悬念迭起】\n"
        "- 开篇即悬念/谜面（一个反常细节、一个未解之谜、一个惊人数字）\n"
        "- 层层剥茧：每段揭一层，段尾留钩子（'可谁也没想到……''偏偏这时候……'）\n"
        "- 多用设问句推进，节奏紧张，像评书扣子\n"
        "- 转折要陡，最后揭底，让观众'哦——原来如此'\n"
        "- 语气带劲、有现场感，像说书先生醒木一拍"),
}

_STYLE_HINTS = {
    "yuqiuyu": "余秋雨式 · 文化散文",
    "storyteller": "讲书人式 · 娓娓道来",
    "suspense": "说书人式 · 悬念迭起",
}


def _build_messages(instruction, duration_min, image_count, series_name,
                    style_anchor, ctype="故事", persona_extra="", wstyle=""):
    """构造 system + user 消息。ctype: 商品/故事/营销（创作类型）"""
    anchor_line = ""
    if style_anchor:
        anchor_line = f'8. 生图提示词需额外融入风格描述：「{style_anchor}」。\n'
    type_inst = _TYPE_INSTRUCTIONS.get(ctype) or _TYPE_INSTRUCTIONS["故事"]
    wstyle_inst = _WSTYLE_INSTRUCTIONS.get(wstyle or "")
    wstyle_line = (wstyle_inst + "\n") if wstyle_inst else ""
    system = f"""你是一位专业的文史类短视频文案创作者，专为抖音/视频号平台撰写口播稿。

你的文案风格：口语化、有故事感、有感染力、三秒钩子抓住观众。像一位纪录片导演在镜头前娓娓道来，而不是在念百科条目。

硬性规则：
1. 稿子结构：钩子（1-2句，制造悬念或情感冲击）→ 正文（自然段落，每段讲一个情节，段与段之间用过渡句衔接）→ 收尾（1句金句或下期预告）
2. 语速基准：中文约250字/分钟。本次目标时长 {duration_min} 分钟，正文字数控制在 {duration_min * 250} 到 {duration_min * 300} 字之间（含标点）。宁多勿少，但超出上限会剪不完，必须控制。
3. 史实准确：涉及人名、地名、年代、事件必须真实可查，不可虚构。
4. 严格禁止在正文中使用"一、""二、"等序号分段。用自然段落和过渡句替代，比如"故事，要从……说起""那么，为什么……""更令人震撼的是……"。
5. 不要用"大家好""欢迎收看"之类的开场白，直接入戏。不要用"总结一下""综上所述"收尾。
6. 同时生成 {image_count} 个生图提示词，每个是一句完整的中文描述，风格统一为"中国古风，水墨质感，纪录片氛围，无文字"。
7. 系列名默认「{series_name}」，除非用户指令明确指定其他系列。
{anchor_line}
{type_inst}
{wstyle_line}
{persona_extra}
8. fetch_keywords：给出 2-3 个用于搜索视频素材画面的关键词（YouTube/B站搜索用），
   中英结合（英文命中率高），要能反映稿子的核心画面主题（人物/战争/城市/器物等），
   如"信陵君 战国 合纵 ancient china war"。不含年份、不含广告词。
   强调：要的是纪录片/实拍画面类素材，不要歌曲MV/歌词视频/翻唱。
你必须严格按以下 JSON 格式输出（不要输出任何其他文字，只输出 JSON）：

{{
 "episode_title": "用作片头主标题的短句，用·分隔主副标题，如'李白·诗仙传奇'",
 "hook": "开头钩子，1-2句",
 "script_body": "正文全部内容，自然段落分行，含收尾",
 "image_prompts": ["提示词1", "提示词2", ...],
 "series_name": "系列名",
 "episode_name": "归档名建议，如'17-李白'",
 "fetch_keywords": "视频素材搜索关键词，如'ancient china war 战国战争'"
}}"""
    return system, instruction


def _call_qwen(system, user, api_key):
    """调用通义千问 chat completions，返回 assistant 文本。"""
    payload = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0.7,
        "max_tokens": 4096,
        "thinking": {"type": "disabled"},  # 关闭思维链：写稿是结构化文本生成，不需要深度推理；
        # 不关的话 reasoning_content 会吃光 max_tokens，content 为空 → JSON 解析失败
    }
    last_err = None
    for attempt in range(1, MAX_RETRIES + 2):
        try:
            resp = requests.post(
                API_ENDPOINT,
                json=payload,
                headers={"Authorization": "Bearer " + api_key},
                timeout=TIMEOUT,
            )
            resp.raise_for_status()
            data = resp.json()
            content = data["choices"][0]["message"]["content"]
            if not content or not content.strip():
                # 防御：空 content 走重试而非伪报错「不是合法 JSON」
                raise ValueError("模型返回空内容")
            return content
        except Exception as e:  # noqa: BLE001 网络/超时/限流统一重试
            last_err = e
            if attempt <= MAX_RETRIES:
                time.sleep(2 * attempt)
    raise AIScriptError("AI 调用失败：" + str(last_err))


def _extract_json(text):
    """容错解析 LLM 输出：兼容 ```json 代码块包裹，否则直接 json.loads。"""
    t = (text or "").strip()
    m = re.search(r"```(?:json)?\s*(.*?)\s*```", t, re.S)
    if m:
        t = m.group(1).strip()
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        raise AIScriptError("AI 返回内容不是合法 JSON", raw=text)


def generate_script(instruction, duration_min=3, api_key="",
                    series_name="上下五千年", style_anchor="", ctype="故事",
                    base_dir="", style_enabled=True, wstyle=""):
    """
    调用 qwen-max 生成稿件。

    参数:
        instruction: 用户输入，如"出一个关于李白的故事"
        duration_min: 目标时长（分钟），默认 3
        api_key: DeepSeek API Key
        series_name: 系列名，默认"上下五千年"
        style_anchor: 生图风格锚定词（可空）
        ctype: 创作类型（商品/故事/营销）
        base_dir: 项目根目录（找 persona.json；空则不注入风格）
        style_enabled: 是否启用个人风格模仿（GL-20260901）
        wstyle: 文风预设（yuqiuyu/storyteller/suspense；空=默认，GL-20260901）

    返回:
        {
          "title": 片头标题（用·分隔）,
          "hook": 开头钩子文案,
          "script_body": 正文（不含标题行）,
          "full_script": 完整朗读稿（hook + 正文，供写 script.txt）,
          "image_prompts": ["生图提示词1", ...],
          "char_count": 字数,
          "series_name": 系列名,
          "episode_name": 归档名建议,
          "style_used": 本次是否注入了个人风格
        }

    异常:
        AIScriptError
    """
    if not api_key:
        raise AIScriptError("未配置 DEEPSEEK_API_KEY，请到高级设置里填写")

    # 每 18 秒一张图（含 Ken Burns 动效），最少 6 张
    image_count = max(6, (duration_min * 60) // 18)

    # GL-20260901：个人风格注入（persona.json 存在且有规则时才生效）
    persona_extra = ""
    style_used = False
    if style_enabled and base_dir:
        try:
            from lib.style_learner import persona_prompt
            persona_extra = persona_prompt(base_dir)
            style_used = bool(persona_extra)
        except Exception as e:
            print(f"  ⚠ 风格注入失败（忽略，照常出稿）: {e}")
            persona_extra = ""

    system, user = _build_messages(
        instruction, duration_min, image_count, series_name, style_anchor, ctype,
        persona_extra=persona_extra, wstyle=wstyle)
    content = _call_qwen(system, user, api_key)
    data = _extract_json(content)

    title = (data.get("episode_title") or "").strip()
    hook = (data.get("hook") or "").strip()
    body = (data.get("script_body") or "").strip()
    if not body:
        raise AIScriptError("AI 返回内容为空", raw=content)

    full_script = body if not hook else hook + "\n\n" + body
    char_count = len(re.sub(r"\s", "", full_script))

    image_prompts = data.get("image_prompts") or []
    if not isinstance(image_prompts, list) or not image_prompts:
        raise AIScriptError("AI 未返回生图提示词", raw=content)

    series = (data.get("series_name") or series_name or "上下五千年").strip()
    ep_name = (data.get("episode_name") or "").strip()

    return {
        "title": title,
        "hook": hook,
        "script_body": body,
        "full_script": full_script,
        "image_prompts": image_prompts,
        "char_count": char_count,
        "series_name": series,
        "episode_name": ep_name,
        "fetch_keywords": (data.get("fetch_keywords") or "").strip(),
        "style_used": style_used,
    }


def recommend_material(segments: list, materials: list, api_key: str = "") -> list:
    """AI 语义选材（题材无关，GL-20260814-03）。

    输入:
        segments: 稿子段落 [{"index", "text"}, ...]（lib/script_seg.split_segments 产出）
        materials: 素材清单（lib/material_scanner.scan_material 产出，
                   含 path/name/topic/theme/duration_sec/desc）
    返回: 与 segments 等长的选材计划
        [{"index", "text", "material": "文件名.mp4", "clip_start": 秒, "reason"}, ...]
    逐段兜底：某段缺 material / 素材名找不到 / 时长不足 → 从未分配素材按顺序补，
    全用完则从头循环（保证返回结构永远完整可消费）。
    异常: AIScriptError（网络/JSON 解析失败等，由调用方决定整体降级策略）
    """
    if not api_key:
        raise AIScriptError("未配置 DEEPSEEK_API_KEY，请到高级设置里填写")
    if not segments:
        return []
    if not materials:
        raise AIScriptError("素材清单为空，无法选材")

    # ── 素材清单：控制 token（desc 截断 80 字；超 40 条警告但仍全量传入）──
    if len(materials) > 40:
        print(f"  ⚠ 素材 {len(materials)} 条超过 40 条，全量传入（token 较大）")
    mat_lines = []
    for m in materials:
        desc = (m.get("desc") or "").strip()[:80]
        dur = m.get("duration_sec") or 0
        theme = m.get("theme") or m.get("topic") or ""
        fname = m["name"] + os.path.splitext(m["path"])[1]
        mat_lines.append(
            f"- {fname} | 目录: {theme} | 时长: {dur:.0f}s | 画面: {desc or '无描述'}")

    seg_lines = "\n".join(f"[{s['index']}] {s['text']}" for s in segments)
    mat_info = "\n".join(mat_lines)

    system = (
        "你是一位短视频画面选材编辑，负责为口播稿挑选最贴合的素材视频片段。\n"
        "匹配依据是【语义】——画面内容与稿子内容对得上即可，不靠任何固定词表\n"
        "（历史、商品、美食等任何题材都是同一套逻辑）。\n"
        "素材信息 = 文件名 + 所在目录 + 时长 + 画面描述；重点参考画面描述和目录名。\n"
        "规则：\n"
        "1. 每条素材最多用 1 次（除非素材条数少于段落数，允许循环复用）；\n"
        "2. 不要推荐时长明显短于段落音频时长的素材（会截不够）；\n"
        "3. clip_start 建议截取起点（秒）：避开素材开头突兀处，\n"
        "   可在 0 到 max(0, 素材时长-预估段落时长) 之间取合理值；\n"
        "4. 每段必须推荐，且推荐理由一句话说清画面与内容的对应关系。\n"
        "严格只输出 JSON 数组，不要输出任何其他文字。"
    )
    user = (
        "稿子段落（段落即一个画面单位，每段推荐一条素材）：\n"
        + seg_lines
        + "\n\n可选素材清单：\n"
        + mat_info
        + "\n\n输出 JSON 数组："
        '[{"index": 0, "material": "文件名.mp4", "clip_start": 0, "reason": "一句话理由"}, ...]'
    )

    content = _call_qwen(system, user, api_key)
    data = _extract_json(content)
    if isinstance(data, dict):
        for _k in ("plan", "selections", "items", "results"):
            if isinstance(data.get(_k), list):
                data = data[_k]
                break
    if not isinstance(data, list):
        raise AIScriptError("AI 选材返回格式不对（应为 JSON 数组）", raw=content)

    # ── 素材名索引（name / name+扩展名 / 完整文件名 都能匹配）──
    by_name = {}
    for m in materials:
        fname = m["name"] + os.path.splitext(m["path"])[1]
        by_name[m["name"]] = m
        by_name[fname] = m
        by_name[os.path.basename(m["path"])] = m

    # 逐段兜底：缺 material / 找不到 / 时长不足 → 从未分配素材按顺序补，用完循环
    assigned = set()
    fallback_idx = 0
    plan = []
    for seg in segments:
        idx = seg["index"]
        item = None
        for cand in data:
            if isinstance(cand, dict) and cand.get("index") == idx:
                item = cand
                break
        material_name = (item or {}).get("material") or ""
        mat = by_name.get(str(material_name).strip())

        if mat is None:
            # 单段兜底：顺序取未分配素材
            mat, fallback_idx = _fallback_pick(materials, assigned, fallback_idx)
            if material_name:
                print(f"  ⚠ 段 {idx} 推荐素材「{material_name}」不在清单，改用 {mat['name']}")
            else:
                print(f"  ⚠ 段 {idx} 未推荐素材，兜底用 {mat['name']}")
            reason = (item or {}).get("reason") or "兜底分配"
        else:
            assigned.add(mat["name"])
            reason = (item or {}).get("reason") or ""

        try:
            clip_start = float((item or {}).get("clip_start") or 0)
        except (TypeError, ValueError):
            clip_start = 0.0
        clip_start = max(0.0, clip_start)
        # 起点不超出素材可截范围（至少留 1s）
        avail = (mat.get("duration_sec") or 0) - 1.0
        if clip_start > max(0.0, avail):
            clip_start = 0.0

        plan.append({
            "index": idx,
            "text": seg["text"],
            "material": os.path.basename(mat["path"]),
            "material_path": mat["path"],
            "clip_start": round(clip_start, 2),
            "reason": reason,
        })
    return plan


def _fallback_pick(materials: list, assigned: set, fallback_idx: int):
    """从未分配素材按顺序取，全用完则从头循环"""
    n = len(materials)
    for _ in range(n):
        m = materials[fallback_idx % n]
        fallback_idx += 1
        if m["name"] not in assigned:
            assigned.add(m["name"])
            return m, fallback_idx
    # 全用完：从头循环（不强制唯一）
    m = materials[fallback_idx % n]
    fallback_idx += 1
    return m, fallback_idx
