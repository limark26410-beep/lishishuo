@echo off
chcp 65001 >nul
rem 双击启动"历史说出片工具"（Windows）
cd /d "%~dp0"

echo.
echo   ┌─────────────────────────────────┐
echo   │     历史说 · 出片工具            │
echo   └─────────────────────────────────┘
echo.

rem 检查 ffmpeg（视频处理必需，同事自装）
where ffmpeg >nul 2>nul
if errorlevel 1 (
    echo   ⚠ 未检测到 ffmpeg，请先安装（装一次永久有效）：
    echo.
    echo      winget install ffmpeg
    echo.
    echo   或从 https://www.gyan.dev/ffmpeg/builds/ 下载 release 版，
    echo   解压后把 bin 目录加入系统 PATH（详见 使用说明.md）
    echo.
    pause
    exit /b 1
)
echo   ✓ ffmpeg 已就绪

rem 检查虚拟环境
if not exist venv\Scripts\python.exe (
    echo   ⚠ 未找到虚拟环境，正在创建（首次运行需联网，约1分钟）…
    python -m venv venv
    venv\Scripts\pip install -i https://pypi.tuna.tsinghua.edu.cn/simple pyyaml pillow edge-tts requests python-docx
    echo   ✓ 环境就绪
    echo.
)

echo   正在启动… 浏览器会自动打开
echo   用完直接关掉这个窗口即可
echo.

venv\Scripts\python web_app.py
pause
