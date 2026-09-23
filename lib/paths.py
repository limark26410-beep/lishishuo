"""
路径统一管理（GL-20260923）：让流水线可在任意机器/任意目录运行。

仓库根：自动定位（本文件在 <repo>/lib/ 下，上溯一级即仓库根），
       clone 到任何路径都通用，不再写死 /Users/local/...。
素材库根：可配置。优先级：
      1. 环境变量 LISHISHUO_MATERIAL_ROOT
      2. config.yaml 的 paths.material_root
      3. 默认 ~/Desktop/历史说素材

用法：
    from paths import REPO_ROOT, material_root
"""
import os
from pathlib import Path

# 仓库根 = 本文件所在目录的上一级（lib/ 的上一级）
REPO_ROOT = Path(__file__).resolve().parent.parent

_ENV_KEY = "LISHISHUO_MATERIAL_ROOT"
_DEFAULT = Path("~/Desktop/历史说素材")


def material_root() -> Path:
    """素材库根目录（含共享空镜/片头背景/bgm/系列归档等）。"""
    # 1) 环境变量优先
    env = os.environ.get(_ENV_KEY, "").strip()
    if env:
        return Path(env).expanduser()
    # 2) config.yaml
    try:
        import yaml
        cfg_path = REPO_ROOT / "config.yaml"
        if cfg_path.exists():
            cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
            v = (cfg.get("paths") or {}).get("material_root", "")
            if v:
                return Path(v).expanduser()
    except Exception:
        pass
    # 3) 默认
    return _DEFAULT.expanduser()


def load_env() -> None:
    """把 .env 里的 KEY=VALUE 加载进 os.environ（已存在的环境变量不覆盖）。

    所有脚本在用到 API Key 前调用一次，即可统一从 .env 读密钥——
    同事只需复制 .env.example 为 .env 填自己的 key，无需 export。
    """
    env_path = REPO_ROOT / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip()
        if k and k not in os.environ:
            os.environ[k] = v


def shared_scenery_dir() -> Path:
    return material_root() / "共享空镜"


def title_bg_dir() -> Path:
    return material_root() / "片头背景"


def series_dir(name: str) -> Path:
    """系列归档目录，如 series_dir('十大谋士') → <素材库>/十大谋士"""
    return material_root() / name
