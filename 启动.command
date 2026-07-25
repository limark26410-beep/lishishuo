#!/bin/bash
# 双击启动"历史说出片工具"
# 自动定位到脚本所在目录，启动网页服务

cd "$(dirname "$0")"

echo ""
echo "  ┌─────────────────────────────────┐"
echo "  │     历史说 · 出片工具            │"
echo "  └─────────────────────────────────┘"
echo ""

# 检查 python3
if ! command -v python3 &> /dev/null; then
    echo "  ✗ 没找到 python3，请先安装 Python 3"
    echo "  按回车键关闭…"
    read
    exit 1
fi

# 检查依赖（缺了就自动装）
python3 -c "import yaml, PIL, edge_tts, requests, docx" 2>/dev/null
if [ $? -ne 0 ]; then
    echo "  首次运行，正在安装依赖（只需这一次）…"
    pip3 install --quiet pyyaml pillow edge-tts requests python-docx 2>&1 | tail -1
    echo "  ✓ 依赖就绪"
    echo ""
fi

echo "  正在启动… 浏览器会自动打开"
echo "  用完直接关掉这个窗口即可"
echo ""

python3 web_app.py
