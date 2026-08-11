"""
AI 智能出稿模块
封装通义千问（qwen-max）API 调用：输入自然语言指令 → 输出结构化稿件。

兼容 lishishuo 1.0 流水线：
- script.txt: hook + 正文（不含标题行；标题走 run.py --title 参数，
  避免 edge-tts 把「【标题】xxx」整行朗读出来）
- prompts.json: [{"id": "auto_01", "title": "AI 生成", "prompts": [...]}]
  与 run.py step_image_gen() 解析逻辑兼容
"""

import json
import re
import time

import requests

API_ENDPOINT = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
MODEL = "qwen-max"
TIMEOUT = 60          # 秒
MAX_RETRIES = 2       # 最多重试 2 次（首次 + 2 次重试）


class AIScriptError(Exception):
    """AI 出稿异常。raw 保存 LLM 原文便于排查。"""

    def __init__(self, msg, raw=""):
        super().__init__(msg)
        self.raw = raw


def _build_messages(instruction, duration_min, image_count, series_name, style_anchor):
    """构造 system + user 消息。"""
    anchor_line = ""
    if style_anchor:
        anchor_line = f'8. 生图提示词需额外融入风格描述：「{style_anchor}」。\n'
    system = f"""你是一位专业的文史类短视频文案创作者，专为抖音/视频号平台撰写口播稿。
你的文案风格：口语化、有故事感、有感染力、三秒钩子抓住观众。

输出规则：
1. 稿子结构：钩子（1-2句，必须放在最开头，制造悬念或情感冲击）→ 正文（分段叙述）→ 收尾金句（1句）
2. 语速基准：中文约250字/分钟。本次目标时长 {duration_min} 分钟，正文输出约 {duration_min * 250} 字。
3. 史实准确：涉及人名、地名、年代、事件必须真实可查，不可虚构。
4. 正文中可用"一、""二、"分段，但钩子那段不能带序号。
5. 不要用"大家好""欢迎收看"之类的开场白，直接入戏。
6. 同时生成 {image_count} 个生图提示词，每个是一句完整的中文描述，风格统一为"中国古风，水墨质感，纪录片氛围，无文字"。
7. 系列名默认「{series_name}」，除非用户指令明确指定其他系列。
{anchor_line}
你必须严格按以下 JSON 格式输出（不要输出任何其他文字，只输出 JSON）：

{{
 "episode_title": "用作片头主标题的短句，用·分隔主副标题，如'李白·诗仙传奇'",
 "hook": "开头钩子，1-2句",
 "script_body": "正文全部内容，分段分行，含收尾金句",
 "image_prompts": ["提示词1", "提示词2", ...],
 "series_name": "系列名",
 "episode_name": "归档名建议，如'17-李白'"
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
            return data["choices"][0]["message"]["content"]
        except Exception as e:  # noqa: BLE001 网络/超时/限流统一重试
            last_err = e
            if attempt <= MAX_RETRIES:
                time.sleep(2 * attempt)
    raise AIScriptError("通义千问调用失败：" + str(last_err))


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
                    series_name="上下五千年", style_anchor=""):
    """
    调用 qwen-max 生成稿件。

    参数:
        instruction: 用户输入，如"出一个关于李白的故事"
        duration_min: 目标时长（分钟），默认 3
        api_key: DashScope API Key
        series_name: 系列名，默认"上下五千年"
        style_anchor: 生图风格锚定词（可空）

    返回:
        {
          "title": 片头标题（用·分隔）,
          "hook": 开头钩子文案,
          "script_body": 正文（不含标题行）,
          "full_script": 完整朗读稿（hook + 正文，供写 script.txt）,
          "image_prompts": ["生图提示词1", ...],
          "char_count": 字数,
          "series_name": 系列名,
          "episode_name": 归档名建议
        }

    异常:
        AIScriptError
    """
    if not api_key:
        raise AIScriptError("未配置 DASHSCOPE_API_KEY，请到高级设置里填写")

    # 每 18 秒一张图（含 Ken Burns 动效），最少 6 张
    image_count = max(6, (duration_min * 60) // 18)

    system, user = _build_messages(
        instruction, duration_min, image_count, series_name, style_anchor)
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
    }
