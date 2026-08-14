#!/bin/bash
# 启动"历史说出片工具"（Linux 服务器）
cd "$(dirname "$0")"
VENV_PYTHON="./venv/bin/python3"

echo ""
echo "  ┌─────────────────────────────────┐"
echo "  │     历史说 · 出片工具            │"
echo "  └─────────────────────────────────┘"
echo ""

# 检查 ffmpeg（视频处理必需，同事自装）
if ! command -v ffmpeg >/dev/null 2>&1; then
    echo "  ⚠ 未检测到 ffmpeg，请先安装："
    echo "     sudo apt install ffmpeg        # Debian/Ubuntu"
    echo "     sudo yum install ffmpeg        # CentOS/RHEL"
    echo "  （详见 使用说明.md）"
    exit 1
fi
echo "  ✓ ffmpeg 已就绪 ($(command -v ffmpeg))"

# 检查虚拟环境
if [ ! -f "$VENV_PYTHON" ]; then
    echo "  ⚠ 未找到虚拟环境，正在创建…"
    python3 -m venv venv
    ./venv/bin/pip install -i https://pypi.tuna.tsinghua.edu.cn/simple pyyaml pillow edge-tts requests python-docx
    echo "  ✓ 环境就绪"
fi

echo "  正在启动…"
echo ""

exec "$VENV_PYTHON" web_app.py
