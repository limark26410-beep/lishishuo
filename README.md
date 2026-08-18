# 文史长音频自动化流水线

输入 script.txt + prompts.json → 输出 final.mp4（配音 + 生图 + 字幕 + 背景乐）

## 快速开始

```bash
# 1. 安装依赖
pip3 install pyyaml requests edge-tts
# 服务器: bash setup_server.sh

# 2. 配 API Key
export DASHSCOPE_API_KEY=sk-xxx

# 3. 跑一期
python3 run.py --episode 001
```

## 目录结构

```
/pipeline
  config.yaml              # 全配置（编码器/转场/字幕字体等）
  run.py                   # 主编排脚本
  setup_server.sh          # 生产服务器一键初始化
  assets/bgm.mp3           # 背景音乐
  episodes/
    {期号}/
      script.txt           # 终审定稿（输入）
      prompts.json          # 分板块绘画提示词（输入）
      audio.mp3            # TTS 音频（自动生成）
      subs.srt             # 字幕（自动生成）
      images/              # 生图（用完可删）
      clips/               # Ken Burns 片段（用完可删）
      final.mp4            # 成品（输出）
  lib/
    tts_utils.py           # edge-tts 封装
    tongyi_api.py          # 通义万相 API 客户端
    ffmpeg_utils.py        # FFmpeg 封装（Ken Burns / 拼接 / 混音 / 烧字幕）
```

## 配置要点

| 配置项 | 生产（Linux） | 开发（macOS） |
|--------|-------------|-------------|
| encoder | `libx264`（默认） | `h264_videotoolbox`（auto 自动切） |
| preset | `medium`（默认） | 同上 |
| transition | `concat`（硬切，默认） | 同上 |
| subtitle font | `Noto Sans CJK SC` | `AdobeHeitiStd-Regular` |

## 状态

- ✅ 配音（edge-tts，YunjianNeural，-4%）
- ✅ 字幕（VTT→SRT 自动转换）
- ✅ Ken Burns 动效（zoompan 慢推近）
- ✅ 视频拼接（concat 硬切秒出 / xfade 交叉溶解）
- ✅ 混音（配音 + 背景乐）
- ✅ 通义万相 API（异步提交+轮询+重试+断点续跑）
- ✅ 并行 TTS+生图
- 🟡 烧字幕（需 ffmpeg --enable-libass）
- 🟡 真实生图（需 DASHSCOPE_API_KEY）

## CLI

```bash
python3 run.py --episode 001              # 正常运行
python3 run.py --episode 001 --dry-run    # 检查素材
python3 run.py --episode 001 --skip-tts   # 跳过配音（重跑混剪）
python3 run.py --episode 001 --skip-images --no-cleanup  # 跳过生图（重跑混剪）
```
