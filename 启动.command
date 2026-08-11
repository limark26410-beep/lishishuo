#!/bin/bash
# 双击启动"历史说出片工具"
# 自动定位到脚本所在目录，启动网页服务

cd "$(dirname "$0")"
VENV_PYTHON="./venv/bin/python3"

echo ""
echo "  ┌─────────────────────────────────┐"
echo "  │     历史说 · 出片工具            │"
echo "  └─────────────────────────────────┘"
echo ""

# 检查虚拟环境
if [ ! -f "$VENV_PYTHON" ]; then
    echo "  ⚠ 未找到虚拟环境，正在创建（首次运行需联网，约1分钟）…"
    /usr/local/bin/python3 -m venv venv
    ./venv/bin/pip install -i https://pypi.tuna.tsinghua.edu.cn/simple --trusted-host pypi.tuna.tsinghua.edu.cn pyyaml pillow edge-tts requests python-docx
    echo "  ✓ 环境就绪"
    echo ""
fi

echo "  正在启动… 浏览器会自动打开"
echo "  用完直接关掉这个窗口即可"
echo ""

"$VENV_PYTHON" web_app.py
