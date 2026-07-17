# lishishuo — 文史长音频自动化流水线 · 约束文件

## 画幅
- 9:16 竖屏 1080×1920，锁死

## 流程闸门
- 终审是必经闸门，不用未终审初稿出片
- 工具不自行更换，要换先报 Mark

## 生图
- 提供商：通义万相（历史说专用 key，存 ~/.zshrc）
- 每期约 26-28 张，不是每段字幕一张
- 风格锚定词：中国古风、水墨质感、纪录片氛围、无文字

## 动效
- Ken Burns：先放大 2 倍再 zoompan，防糊边

## 配音
- edge-tts / zh-CN-YunjianNeural / rate=-4%

## 编码
- 生产用 libx264（videotoolbox 是 macOS 独有，不上生产）
- subtitle: libass（subtitles 滤镜），不用 PIL ProRes 绕法

## 字号
- 字幕：60（可 54-72 微调），BorderStyle=3 半透明底衬
- 封面主标题：130（可 125-135）

## 发布
- 手动上传，不做自动

## API Key
- 只进 ~/.zshrc 环境变量，不进仓库

## 成片归档
- ~/Desktop/历史说/成片/{期号}-{标题}/
