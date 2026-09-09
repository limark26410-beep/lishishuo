# 批量抠图工具（batch_cutout）

AI 商品图抠图：把带背景的产品照片抠成**透明底 PNG**。
基于 rembg（u2net），本地运行免费，无需联网（模型首用下载一次 ~176MB）。

## 环境（重要）
rembg 依赖需要 **Python 3.11**（项目主环境是 3.14，onnxruntime 不支持）。
专用虚拟环境已建在：`/Users/local/lishishuo/venv311`

```bash
# 用 venv311 跑（不要用主 venv）
/Users/local/lishishuo/venv311/bin/python tools/batch_cutout.py
```

## CLI 用法
```bash
# 默认：抠 ~/Desktop/钟茂丰/茂丰原浆2006年冬酿礼盒 → 同目录 cut/
venv311/bin/python tools/batch_cutout.py

# 指定目录/输出/模型
venv311/bin/python tools/batch_cutout.py <源目录> [输出目录] [模型]

# 模型可选：u2net(默认均衡) / isnet / isnet-general-use（边缘更细）
```

## 作为模块复用（融合进其他工具）
```python
import sys
sys.path.insert(0, '/Users/local/lishishuo/tools')
from batch_cutout import cutout_image, batch_cutout

# 单张
cutout_image('产品图.jpg', '输出.png', max_side=1600)

# 批量（返回 [(源名, 输出路径 or None)]）
results = batch_cutout('图片目录', '输出目录', model='u2net')
```

## 注意事项
- 输出统一 `xxx_cut.png`（透明底 RGBA）
- 灰瓶+灰背景（如 DSC_2327）u2net 可能抠不全 → 换 `isnet-general-use` 或提高输入分辨率
- 同进程多次调用会自动缓存模型会话（不重复加载）
