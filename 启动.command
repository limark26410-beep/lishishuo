#!/bin/bash
# 双击启动"历史说出片工具"（macOS）
# 自动定位到脚本所在目录，检查 ffmpeg，启动网页服务

cd "$(dirname "$0")"
VENV_PYTHON="./venv/bin/python3"

echo ""
echo "  ┌─────────────────────────────────┐"
echo "  │     历史说 · 出片工具            │"
echo "  └─────────────────────────────────┘"
echo ""

# 检查 ffmpeg（视频处理必需，同事自装）
if ! command -v ffmpeg >/dev/null 2>&1; then
    echo "  ⚠ 未检测到 ffmpeg，请先安装（装一次永久有效）："
    echo ""
    echo "      brew install ffmpeg"
    echo ""
    echo "  （没有 brew 先跑：/bin/bash -c \"\$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)\"）"
    echo "  详见 使用说明.md"
    echo ""
    read -n 1 -s -r -p "  按任意键退出…"
    echo ""
    exit 1
fi
echo "  ✓ ffmpeg 已就绪 ($(command -v ffmpeg))"

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
