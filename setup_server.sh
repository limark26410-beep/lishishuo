# 文史长音频自动化流水线 · 部署依赖
# 在 Linux 生产服务器上首次运行时执行

set -e

echo "=== 安装系统依赖 ==="

# ffmpeg（需 --enable-libass 支持烧字幕）
if ! command -v ffmpeg &> /dev/null; then
  echo "安装 ffmpeg..."
  apt-get update -qq
  apt-get install -y -qq ffmpeg libavcodec-extra 2>/dev/null || {
    echo "从源码编译 ffmpeg + libass..."
    apt-get install -y -qq build-essential pkg-config \
      libx264-dev libmp3lame-dev libass-dev \
      libavformat-dev libavcodec-dev libavfilter-dev \
      libavdevice-dev libavutil-dev libswscale-dev
    # 或者直接用静态构建
    echo "建议: 下载静态 ffmpeg 带 libass:"
    echo "  wget https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz"
  }
fi

# 中文字体（烧字幕用）
echo "安装中文字体..."
apt-get install -y -qq fonts-noto-cjk fonts-noto-cjk-extra 2>/dev/null || {
  echo "提示: 需手动安装 Noto Sans CJK SC 字体"
  echo "  apt-get install fonts-noto-cjk"
}

# Python 依赖
pip3 install pyyaml requests --break-system-packages -q

# edge-tts（配音引擎）
pip3 install edge-tts --break-system-packages -q

echo ""
echo "=== 验证 ==="
ffmpeg -filters 2>/dev/null | grep -q subtitles && echo "✅ subtitles 滤镜可用" || echo "❌ subtitles 不可用（缺 --enable-libass）"
edge-tts --list-voices >/dev/null 2>&1 && echo "✅ edge-tts 可用" || echo "❌ edge-tts 不可用"
fc-list :lang=zh 2>/dev/null | head -1 || echo "ℹ️ 未检测到中文字体"

echo ""
echo "=== 就绪 ==="
echo "配置 DASHSCOPE_API_KEY 后即可运行:"
echo "  export DASHSCOPE_API_KEY=sk-xxx"
echo "  python3 run.py --episode 001"
