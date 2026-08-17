# -*- coding: utf-8 -*-
"""段落情绪标注（GL-20260817-04 批次3 分段配乐）

- tag_emotions(): 稿子段落列表 → 每段情绪标签（悬念/平叙/高潮/收束）
  * 优先 DeepSeek 语义标注（一次调用，几分钱）
  * 失败/无 key → 关键词规则兜底；全失败默认平叙
  * 首段默认悬念、末段默认收束（规则层保证）
- 配乐映射：lib/ffmpeg_utils.mix_audio 按段时间用对应情绪乐段
"""

import json
import re

EMOTIONS = ["悬念", "平叙", "高潮", "收束"]

# 规则兜底关键词表（按优先级）
_HIGH_KEYS = ["震撼", "最", "转折", "衰落", "爆发", "顶峰", "逆转", "危机",
              "决战", "崛起", "传奇", "辉煌", "奇迹", "惊心动魄", "一夜之间",
              "巅峰", "低谷", "崩塌", "突围", "封神", "巅峰时刻"]
_SUS_KEYS = ["为什么", "秘密", "真相", "竟然", "没想到", "谜", "悬疑",
             "究竟", "背后", "藏着", "暗藏"]
_END_KEYS = ["最后", "终究", "如今", "留下", "回忆", "依然", "再也没有",
             "尾声", "落幕", "余", "回望", "至今"]


def _rule_tag(segments: list) -> list:
    """规则兜底：首段悬念、末段收束，中间按关键词识别高潮/悬念，默认平叙"""
    out = []
    n = len(segments)
    for i, s in enumerate(segments):
        text = s.get("text") or ""
        if n == 1:
            out.append({"index": s["index"], "emotion": "平叙"})
            continue
        if i == 0:
            out.append({"index": s["index"], "emotion": "悬念"})
        elif i == n - 1:
            out.append({"index": s["index"], "emotion": "收束"})
        else:
            if any(k in text for k in _HIGH_KEYS):
                out.append({"index": s["index"], "emotion": "高潮"})
            elif any(k in text for k in _SUS_KEYS):
                out.append({"index": s["index"], "emotion": "悬念"})
            else:
                out.append({"index": s["index"], "emotion": "平叙"})
    return out


def _ai_tag(segments: list, api_key: str) -> list:
    """DeepSeek 语义标注：一次调用给所有段落打情绪标签"""
    from ai_script_gen import _call_qwen, _extract_json, AIScriptError

    seg_lines = "\n".join(
        f"[{s['index']}] {s['text'][:120]}" for s in segments)
    system = (
        "你是视频配乐的情绪标注器。根据每段内容的叙事情绪，给每段标一个情绪标签，"
        f"只能从 {EMOTIONS} 里选。规则：开头钩子段→悬念；结尾段→收束；"
        "转折/震撼/高潮段→高潮；其余→平叙。"
        '只输出 JSON：{"segments": [{"index": 0, "emotion": "悬念"}, ...]}'
    )
    user = f"稿子段落：\n{seg_lines}"
    content = _call_qwen(system, user, api_key)
    data = _extract_json(content)
    items = data.get("segments") or []
    if not items:
        raise AIScriptError("情绪标注返回空")
    by_idx = {}
    for it in items:
        if it.get("emotion") in EMOTIONS:
            by_idx[int(it["index"])] = it["emotion"]
    out = []
    for s in segments:
        out.append({"index": s["index"],
                    "emotion": by_idx.get(s["index"], "平叙")})
    return out


def tag_emotions(segments: list, api_key: str = "") -> list:
    """段落 → 情绪标签列表（与 segments 等长）。AI 失败自动降级规则。"""
    if not segments:
        return []
    if api_key:
        try:
            out = _ai_tag(segments, api_key)
            print("  ✓ 情绪标注（AI）: " + _fmt(out))
            return out
        except Exception as e:  # noqa: BLE001 网络/解析失败降级规则
            print(f"  ⚠ AI 情绪标注失败（{type(e).__name__}），降级规则识别")
    out = _rule_tag(segments)
    print("  ✓ 情绪标注（规则）: " + _fmt(out))
    return out


def _fmt(out: list) -> str:
    """['悬念','平叙',...] → 逗号分隔字符串"""
    return ", ".join(o["emotion"] for o in out)
