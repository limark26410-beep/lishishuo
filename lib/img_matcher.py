# -*- coding: utf-8 -*-
"""图片智能匹配：稿子段落关键词 ↔ 图片文件名标签，重排图片顺序。

升级自 video-talkcraft 的"背景图匹配"思路：
- 图库文件名自带中文描述（如"秦国大军列阵出征_旌旗上书_秦_字_...jpg"）
- 每段稿子提取关键词（人名/战役/数字/朝代），与图名标签算重合度
- 高分图优先给该段落；无匹配的段落回退原顺序

回滚：删除 run.py 中对该模块的调用行即可恢复平均轮播。
"""
from __future__ import annotations

import re
from pathlib import Path


# 关键词表：朝代 → 关联词（图名/稿子里都会出现）
ERA_KEYWORDS = {
    "战国": ["战国", "秦军", "秦国", "赵国", "韩魏", "诸侯", "列阵", "旌旗", "战车", "攻城"],
    "秦汉": ["秦", "汉", "咸阳", "长城", "匈奴", "漠北", "龙城", "骑兵", "大漠"],
    "三国": ["三国", "魏", "蜀", "吴", "曹操", "赤壁", "官渡", "军阵"],
    "唐": ["唐", "长安", "虎牢关", "突厥", "李世民", "大唐"],
    "宋": ["宋", "岳家军", "金", "郾城", "铁浮屠", "守城"],
    "明": ["明", "洪武", "鄱阳", "倭寇", "戚家军", "鸳鸯阵", "北伐"],
    "清": ["清", "康熙", "雅克萨", "台湾", "郑成功", "黑龙江", "尼布楚"],
}

# 通用战争词（任何战争段落都可能命中）
WAR_WORDS = ["大军", "出征", "战场", "列阵", "旌旗", "战车", "骑兵", "攻城", "守城", "激战", "冲锋", "铁骑", "军队", "战鼓", "刀剑", "长矛"]


def _strip_ext(name: str) -> str:
    return Path(name).stem


def _extract_keywords(para: str) -> list[str]:
    """从段落文本提取匹配关键词：优先朝代词 + 人名/数字，其次通用战争词。"""
    kws = []
    # 朝代关联词
    for era, words in ERA_KEYWORDS.items():
        for w in words:
            if w in para:
                kws.append(w)
    # 数字（四十万、七十余战等）
    for m in re.findall(r"[0-9一二三四五六七八九十百千万]+万?|[0-9一二三四五六七八九十]+战", para):
        kws.append(m)
    # 通用战争词
    for w in WAR_WORDS:
        if w in para:
            kws.append(w)
    return list(dict.fromkeys(kws))  # 去重保序


def _score_image(img_name: str, kws: list[str]) -> int:
    """图名与关键词重合度：命中朝代词权重高，通用词次之。"""
    name = _strip_ext(img_name)
    score = 0
    for kw in kws:
        if kw in name:
            # 长关键词（人名/战役名）权重更高
            score += 3 if len(kw) >= 3 else 1
    return score


def match_images_to_segments(
    segments: list[dict], image_paths: list[str], max_per_seg: int = 4
) -> tuple[list[str], dict]:
    """把图片按段落匹配重排。

    Args:
        segments: split_segments 输出 [{"index", "text"}, ...]
        image_paths: 可用图片路径列表
        max_per_seg: 每段最多分配几张图

    Returns:
        (重排后的图片路径列表, 诊断信息 dict)
    """
    if not segments or not image_paths:
        return image_paths, {"matched": False, "reason": "no segments or images"}

    # 每段提取关键词
    seg_kws = [_extract_keywords(seg["text"]) for seg in segments]

    # 每张图对每段打分
    scored: list[tuple[int, int, int, str]] = []  # (score, seg_idx, orig_idx, name)
    for seg_idx, kws in enumerate(seg_kws):
        for orig_idx, img in enumerate(image_paths):
            s = _score_image(img, kws)
            if s > 0:
                scored.append((s, seg_idx, orig_idx, img))

    if not scored:
        return image_paths, {"matched": False, "reason": "no keyword hits"}

    # 贪心分配：按分数降序，每段不超过 max_per_seg
    scored.sort(key=lambda x: -x[0])
    seg_count: dict[int, int] = {}
    assigned: list[str] = []
    assigned_orig: set[int] = set()
    for s, seg_idx, orig_idx, img in scored:
        if seg_count.get(seg_idx, 0) >= max_per_seg:
            continue
        if orig_idx in assigned_orig:
            continue
        assigned.append(img)
        assigned_orig.add(orig_idx)
        seg_count[seg_idx] = seg_count.get(seg_idx, 0) + 1

    # 未命中的图按原顺序补在后面（保持总张数不变）
    remaining = [img for i, img in enumerate(image_paths) if i not in assigned_orig]
    final = assigned + remaining

    diag = {
        "matched": True,
        "segments_with_hits": sum(1 for k in seg_kws if k),
        "assigned_by_seg": dict(seg_count),
        "matched_images": len(assigned),
        "total_images": len(image_paths),
    }
    return final, diag
