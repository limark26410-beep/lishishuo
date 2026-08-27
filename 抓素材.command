#!/bin/bash
# 双击运行：自动抓取视频素材工具（交互式，不需要记命令）
cd "$(dirname "$0")"

clear
echo "  ┌──────────────────────────────────┐"
echo "  │    历史说 · 自动抓素材工具        │"
echo "  └──────────────────────────────────┘"
echo ""
echo "  从 YouTube 搜索/下载视频片段，自动入库到素材库（AI 选材直接可用）"
echo ""

while true; do
  echo ""
  echo "  ────────────────────────────────"
  echo "  1) 搜索素材（输入关键词，列出候选）"
  echo "  2) 下载素材（粘贴视频链接/ID，可选截取时间段）"
  echo "  0) 退出"
  read -p "  请选择: " opt
  case "$opt" in
    1)
      read -p "  搜索关键词（如: ancient chinese war / siege / 长平之战）: " kw
      ./tools/fetch_video_materials.py --search "$kw" --count 5
      ;;
    2)
      read -p "  视频链接或ID: " url
      read -p "  题材目录（如 战争/历史，默认 素材）: " tp
      read -p "  截取起点（如 00:01:00，留空=整段）: " st
      read -p "  截取终点（如 00:03:00，留空=不截）: " en
      read -p "  画面描述（中文，供 AI 选材识别，如: 古代士兵攻城场面）: " ds
      args=""
      [ -n "$st" ] && args="$args --start $st"
      [ -n "$en" ] && args="$args --end $en"
      ./tools/fetch_video_materials.py --download "$url" --topic "${tp:-素材}" $args --desc "$ds"
      ;;
    0)
      echo "  再见！"
      exit 0
      ;;
    *)
      echo "  无效选择"
      ;;
  esac
done
