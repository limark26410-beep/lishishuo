# 历史说 · 出片工具

> 📖 **同事请看完整上手文档：[docs/同事上手.md](docs/同事上手.md)**

两条出片流程，一个仓库搞定：

| 流程 | 入口命令 | 用途 | 说明 |
|------|---------|------|------|
| **历史说**（纪录片式） | `run.py` | 文史口播/旁白 + 生图 + 字幕 | 输入 script.txt + prompts.json |
| **白酒短剧**（人物对话式） | `run_baijiu.py` | AI 视频 + 多角色配音 + 对话字幕 | 输入 project.json |

### 需要的 API Key（谁用谁注册，各填各的）

| Key | 干什么 | 去哪注册 | 用到 |
|-----|--------|---------|------|
| `DASHSCOPE_API_KEY` | AI 生图 / 图生视频 | 阿里云百炼 bailian.console.aliyun.com | 历史说 |
| `DEEPSEEK_API_KEY` | 写稿 / 提示词 | platform.deepseek.com | 两者 |
| `ZHIPU_API_KEY` | AI 视频（文生视频） | open.bigmodel.cn | 白酒 |
| `DOUBAO_TTS_KEY` | 配音（可多角色） | console.volcengine.com（语音合成） | 两者 |
| `ARK_API_KEY` | 豆包大模型（备用） | console.volcengine.com/ark | 可选 |
| `PEXELS_API_KEY` | 抓视频/图片素材 | pexels.com/api | 可选 |

### 免费 vs 付费

| 服务 | 免费档 | 付费档 |
|------|--------|--------|
| 配音 edge-tts（微软，无需 key） | ✅ 完全免费 | — |
| AI 视频 · 智谱 | ✅ `cogvideox-flash` | `cogvideox-3` 1元/段 |
| 素材 Pexels | ✅ 完全免费 | — |
| 配音 · 豆包 | 有免费额度 | 超额付费 |
| 大模型 · 豆包（ARK） | 有免费额度 | 超额付费 |
| 写稿 · DeepSeek | 少量 | 按 token，极便宜（一篇稿几分钱） |
| 生图 · 阿里云（wanx） | 少量免费额度 | **0.14元/张** |
| 对口型 · 可灵/即梦（另注册） | 每日免费积分 | 会员 |

> **零成本跑通**：智谱 `flash` + edge-tts + Pexels，一分钱不花。
> **要花钱的三处**：阿里云生图（历史说批量生图大头）、智谱 `cogvideox-3`（要画质）、可灵/即梦对口型（要嘴型）。

### 本地素材（省掉线上生图）

历史说流程**有本地素材就直接用本地，不花生图钱**：

```bash
# 本地图片（跳过线上生图）
./venv/bin/python run.py --episode 001 --images-dir /你的图片目录

# 本地视频素材（走视频混剪，不生成图片）
./venv/bin/python run.py --episode 001 --video-dir /你的视频素材库

# 或者手动把图片放进 episodes/001/images/，然后 --skip-images
./venv/bin/python run.py --episode 001 --skip-images
```

---

## 同事上手（5 分钟）

```bash
# 1. 克隆
git clone https://github.com/limark26410-beep/lishishuo.git
cd lishishuo

# 2. 装依赖
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
# 另需安装真实版 ffmpeg（brew install ffmpeg）

# 3. 填你自己的 API Key（谁用谁注册，各填各的，不共耗账号）
cp .env.example .env
# 编辑 .env，填 6 个 key（文件里有每个 key 的注册地址）

# 4. 改素材库路径（可选，默认 ~/Desktop/历史说素材）
# 方法A：改 config.yaml 的 paths.material_root
# 方法B：export LISHISHUO_MATERIAL_ROOT=/你的素材库路径

# 5. 跑
./venv/bin/python run.py --episode 001                       # 历史说
./venv/bin/python run_baijiu.py ~/Desktop/白酒短剧/清引_第1集   # 白酒短剧
```

---

## 一、历史说流程（纪录片式）

输入 `episodes/{期号}/script.txt` + `prompts.json` → 输出 `final.mp4`（配音 + 生图 + Ken Burns + 字幕 + 配乐 + 片头）。

```bash
./venv/bin/python run.py --episode 001
./venv/bin/python run.py --episode 001 --dry-run          # 检查素材
./venv/bin/python run.py --episode 001 --skip-tts         # 跳过配音（重跑混剪）
./venv/bin/python run.py --episode 001 --skip-images      # 跳过生图（重跑混剪）
```

常用参数：`--title 主·副 --series 系列名 --name 归档名 --canvas portrait --rate -4%`。

## 二、白酒短剧流程（人物对话式）

一个项目目录 = 一集对话短片。目录里放一个 `project.json`：

```json
{
  "title": "清引",
  "series": "白酒酿造短剧 · 清香地缸之源",
  "bgm": true,
  "scenes": [
    {"role": "沈清", "voice": "zh_female_vv_uranus_bigtts", "rate": 0,
     "text": "这酒，怎么浑成这样？",
     "prompt": "北宋古装……沈清尝酒皱眉……"}
  ]
}
```

一条命令跑完三步（智谱生视频 → 豆包多角色配音 → 拼装）：

```bash
./venv/bin/python run_baijiu.py ~/Desktop/白酒短剧/清引_第1集
# 免费档 cogvideox-flash（5s/段）；付费档：加参数 cogvideox-3（10s/段）
```

**豆包音色**（可换 `voice`）：`zh_female_vv_uranus_bigtts`（vivi 女）、`zh_male_dayi_saturn_bigtts`（大壹 男）、`zh_male_ruyayichen_saturn_bigtts`（儒雅逸辰 男）、`zh_female_gaolengyujie_uranus_bigtts`（高冷御姐 女）等 8 个。

---

## 目录结构

```
lishishuo/
  config.yaml              # 全配置（含 paths.material_root、编码器、字幕字体）
  run.py                   # 历史说主流程
  run_baijiu.py            # 白酒短剧流程
  .env.example             # 密钥模板（复制为 .env 填自己的 key）
  assets/bgm*.mp3          # 背景音乐
  episodes/{期号}/         # 历史说每期（script.txt + prompts.json → final.mp4）
  lib/                     # 工具库（tts/生图/视频/字幕/路径）
  tools/                   # 批量出片脚本（十大谋士/女将等）
```

## 配置要点

| 配置项 | 说明 |
|--------|------|
| `paths.material_root` | 素材库根目录（共享空镜/片头背景/bgm/归档），可改 |
| `image.provider` | `tongyi`（通义万相 wanx） / `zhipu`（智谱） |
| `tts.engine` | `edge`（微软） / `doubao`（豆包，更自然） |
| `video.encoder` | `libx264` / `h264_videotoolbox` / `auto` |

## 状态

- ✅ 配音（edge-tts / 豆包多角色）
- ✅ 字幕（自动折行、断词保护）
- ✅ 生图（通义万相 / 智谱）+ Ken Burns
- ✅ AI 视频（智谱 CogVideoX，免费/付费档）
- ✅ 片头 + 配乐 + 归档
- 🟡 对口型（需可灵/即梦，另配）
