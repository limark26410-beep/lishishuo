#!/usr/bin/env python3
"""
历史说 · 出片工具（网页界面）

启动：python3 web_app.py
然后浏览器打开 http://127.0.0.1:8765
"""

import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

import yaml

from lib.ai_script_gen import AIScriptError, generate_script

BASE_DIR = Path(__file__).parent.resolve()
CONFIG_PATH = BASE_DIR / "config.yaml"
EXAMPLE_CONFIG_PATH = BASE_DIR / "config.example.yaml"
PORT = 8765

# ── PATH 检测（GL-20260814-02 可移植化）：venv/bin 优先，ffmpeg 自动补齐 ──
# launchd/服务器等干净环境 PATH 可能只有 /usr/bin:/bin，找不到 venv/bin 下的
# edge-tts 和系统 ffmpeg；这里把 venv/bin + 常见 ffmpeg 安装目录 + which 探测结果
# 全部补进 PATH，让 run.py 等子进程都能继承到。ffmpeg 由使用者自装，
# 工具只负责找到它；实在找不到则按平台提示安装命令。
_path_dirs = [str(BASE_DIR / "venv/bin")]
for _d in ("/usr/local/bin", "/opt/homebrew/bin", "/opt/ffmpeg/bin"):
    if os.path.isdir(_d) and _d not in _path_dirs:
        _path_dirs.append(_d)
_ffmpeg = shutil.which("ffmpeg")
if _ffmpeg:
    _d = os.path.dirname(_ffmpeg)
    if _d not in _path_dirs:
        _path_dirs.append(_d)
os.environ["PATH"] = ":".join(_path_dirs + [os.environ.get("PATH", "")])
if not shutil.which("ffmpeg"):
    _hint = {
        "darwin": "brew install ffmpeg",
        "win32": "winget install ffmpeg（或从 https://www.gyan.dev/ffmpeg/builds/ 下载）",
    }.get(sys.platform, "sudo apt install ffmpeg 或 sudo yum install ffmpeg")
    print(f"⚠ 未检测到 ffmpeg，请先安装：{_hint}（详见 使用说明.md）")

# 加载 .env 到环境变量（不覆盖已存在的），供 run.py 子进程继承生图 key
# 这样无论从终端还是双击启动，子进程都能拿到 DASHSCOPE_API_KEY
try:
    _env_path = BASE_DIR / ".env"
    if _env_path.exists():
        for _ln in _env_path.read_text(encoding="utf-8").splitlines():
            _ln = _ln.strip()
            if _ln and not _ln.startswith("#") and "=" in _ln:
                _k, _v = _ln.split("=", 1)
                _k = _k.strip()
                if _k and _k not in os.environ:
                    os.environ[_k] = _v.strip()
except Exception:
    pass

# 任务状态（单任务，够用）
TASK = {
    "running": False,
    "logs": [],
    "step": "",
    "progress": 0,
    "done": False,
    "error": None,
    "result_dir": "",
    "task_id": "",   # GL-20260814-02：SDK 任务 id（= 期号）
    "proc": None,
    "stopped": False,
}
LOG_LOCK = threading.Lock()

# 进度关键词 → 百分比
STEP_MARKS = [
    ("STEP 1", "配音生成", 5),
    ("STEP 2:", "生成图片", 20),
    ("STEP 2.5", "生成片头", 35),
    ("Ken Burns", "画面动效", 45),
    ("拼接 clips", "拼接画面", 60),
    ("混音", "混音", 68),
    ("字幕折行", "字幕处理", 72),
    ("自动检查", "自动检查", 76),
    ("烧录字幕", "烧录字幕", 80),
    ("片头叠加", "叠加片头", 90),
    ("STEP 4", "归档", 95),
    ("STEP 5", "生成验收包", 98),
]


def load_cfg():
    """读 config.yaml；不存在时从 config.example.yaml 复制生成；
    存在但缺字段时用模板默认值补齐（用户已有值优先）。"""
    if not CONFIG_PATH.exists():
        if EXAMPLE_CONFIG_PATH.exists():
            shutil.copy2(EXAMPLE_CONFIG_PATH, CONFIG_PATH)
            print(f"✓ 首次启动：已从模板生成 {CONFIG_PATH.name}")
        else:
            raise FileNotFoundError(f"缺少配置文件：{CONFIG_PATH}")
    with open(CONFIG_PATH, encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    # 缺字段补默认（模板优先，不覆盖用户已有值）
    if EXAMPLE_CONFIG_PATH.exists():
        try:
            defaults = yaml.safe_load(
                EXAMPLE_CONFIG_PATH.read_text(encoding="utf-8")) or {}
            _deep_merge(defaults, cfg)
        except Exception:
            pass
    return cfg


def _deep_merge(defaults: dict, cfg: dict):
    """把 defaults 里 cfg 缺失的键补进去（递归）；cfg 已有值不覆盖"""
    for k, v in defaults.items():
        if k not in cfg:
            cfg[k] = v
        elif isinstance(v, dict) and isinstance(cfg[k], dict):
            _deep_merge(v, cfg[k])


def save_cfg(cfg):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        yaml.safe_dump(cfg, f, allow_unicode=True, sort_keys=False)


def log(line):
    with LOG_LOCK:
        TASK["logs"].append(line)
        if len(TASK["logs"]) > 500:
            TASK["logs"] = TASK["logs"][-500:]
        for kw, label, pct in STEP_MARKS:
            if kw in line:
                TASK["step"] = label
                TASK["progress"] = pct
                break


def count_han(text):
    return len(re.findall(r"[\u4e00-\u9fff]", text))


def _docx_to_text(path):
    """读 docx：优先 python-docx，其次 pandoc，最后直接解 XML（都不装也能用）"""
    # 1) python-docx
    try:
        import docx  # pip3 install python-docx
        d = docx.Document(str(path))
        return "\n".join(p.text for p in d.paragraphs)
    except ImportError:
        pass
    except Exception:
        pass

    # 2) pandoc（装了就用）
    try:
        r = subprocess.run(["pandoc", "-t", "plain", str(path)],
                           capture_output=True, text=True, check=True, timeout=60)
        return r.stdout
    except Exception:
        pass

    # 3) 兜底：docx 本质是 zip，直接抽 XML 里的文字
    try:
        import zipfile
        from xml.etree import ElementTree as ET
        NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
        with zipfile.ZipFile(str(path)) as z:
            xml = z.read("word/document.xml")
        root = ET.fromstring(xml)
        paras = []
        for p in root.iter(f"{NS}p"):
            texts = [t.text or "" for t in p.iter(f"{NS}t")]
            paras.append("".join(texts))
        return "\n".join(paras)
    except Exception:
        return None


def guess_fields(title_line: str) -> dict:
    """
    从稿子标题行推导：期号 / 片头标题 / 归档名
    例：'上下五千年 第14期 清朝·康乾盛世'
        → episode=014, title='清朝·康乾盛世', name='14-清朝'
    """
    out = {"guess_episode": "", "guess_title": "", "guess_name": "", "guess_series": ""}
    if not title_line:
        return out
    ms = re.match(r"^([\u4e00-\u9fff]{2,8})\s*第", title_line)
    if ms:
        out["guess_series"] = ms.group(1)

    # 期号：第14期 / 第 14 期 / 第十四期
    num = None
    m = re.search(r"第\s*(\d+)\s*期", title_line)
    if m:
        num = int(m.group(1))
    else:
        cn = "零一二三四五六七八九十"
        m2 = re.search(r"第\s*([一二三四五六七八九十]+)\s*期", title_line)
        if m2:
            t = m2.group(1)
            if t == "十":
                num = 10
            elif t.startswith("十"):
                num = 10 + cn.index(t[1])
            elif len(t) == 3 and t[1] == "十":
                num = cn.index(t[0]) * 10 + cn.index(t[2])
            elif len(t) == 2 and t[1] == "十":
                num = cn.index(t[0]) * 10
            else:
                num = cn.index(t)
    if num:
        out["guess_episode"] = f"{num:03d}"

    # 片头标题：取"第X期"之后的部分
    rest = title_line
    m3 = re.search(r"第\s*[\d一二三四五六七八九十]+\s*期\s*(.+)$", title_line)
    if m3:
        rest = m3.group(1).strip()
    else:
        rest = re.sub(r"^上下五千年\s*", "", title_line).strip()
    rest = rest.replace("_", "·").strip(" ·")
    out["guess_title"] = rest

    # 归档名：期号 + 朝代（取·前面那段）
    if num and rest:
        dynasty = rest.split("·")[0].strip()
        out["guess_name"] = f"{num:02d}-{dynasty}"
    return out


def next_episode_id():
    """读 episodes/ 下最大数字目录 +1，返回 3 位期号（如 028）"""
    eps_dir = BASE_DIR / "episodes"
    mx = 0
    if eps_dir.exists():
        for d in eps_dir.iterdir():
            if d.is_dir() and d.name.isdigit():
                mx = max(mx, int(d.name))
    return f"{mx + 1:03d}"


def read_script(path):
    """读取稿子（支持 txt / docx），返回 (标题行, 正文)"""
    p = Path(path)
    if p.suffix.lower() == ".docx":
        text = _docx_to_text(p)
        if text is None:
            return None, None
    else:
        text = p.read_text(encoding="utf-8", errors="ignore")
    lines = [ln for ln in text.split("\n")]
    # 跳过开头空行找标题
    idx = 0
    while idx < len(lines) and not lines[idx].strip():
        idx += 1
    title = lines[idx].strip() if idx < len(lines) else ""
    body = "\n".join(lines[idx + 1:]).strip()
    return title, body


def resolve_script_text(script):
    """script 三选一：稿子文本 / 文件路径 / 投递目录路径（目录内找 script.txt/稿子.txt/第一个 txt）"""
    s = os.path.expanduser((script or "").strip())
    if os.path.isdir(s):
        d = Path(s)
        cand = []
        for p in sorted(d.iterdir()):
            if p.name.lower() in ("script.txt", "稿子.txt", "script.md"):
                cand.insert(0, p)
            elif p.suffix.lower() in (".txt", ".md"):
                cand.append(p)
        if cand:
            _t, body = read_script(cand[0])
            return body
        return None
    if os.path.isfile(s):
        _t, body = read_script(s)
        return body
    return script  # 视为稿子文本


def _get_env_key(prefix: str) -> str:
    """读 .env 里的 key（os.environ 已加载则直接用）"""
    if os.environ.get(prefix):
        return os.environ[prefix]
    env_path = BASE_DIR / ".env"
    if env_path.exists():
        for ln in env_path.read_text(encoding="utf-8").splitlines():
            if ln.strip().startswith(prefix):
                return ln.split("=", 1)[-1].strip()
    return ""


def run_pipeline(params):
    """后台线程跑流水线"""
    TASK.update({"running": True, "logs": [], "step": "准备中",
                 "progress": 1, "done": False, "error": None,
                 "stopped": False, "task_id": str(params.get("episode", ""))})
    try:
        episode = params["episode"]
        ep_dir = BASE_DIR / "episodes" / episode
        ep_dir.mkdir(parents=True, exist_ok=True)

        # 1. 准备稿子：AI 模式 script.txt 已由 ai-generate 写入；手动模式去标题写入
        ai_mode = params.get("ai_mode", False)
        if ai_mode:
            body = (ep_dir / "script.txt").read_text(encoding="utf-8")
            log(f"✓ AI 稿子就绪：{count_han(body)} 字")
        else:
            title_line, body = read_script(params["script_path"])
            if body is None:
                raise RuntimeError("稿子读取失败")
            (ep_dir / "script.txt").write_text(body, encoding="utf-8")
            log(f"✓ 稿子就绪：{count_han(body)} 字（已去标题）")

        # 2. 本地图片 / 视频素材
        cmd = [sys.executable, str(BASE_DIR / "run.py"), "-e", episode]
        if params.get("video_dir"):
            # 视频模式：素材目录，run.py 自动切 clip 模式并跳过生图
            cmd += ["--video-dir", os.path.expanduser(params["video_dir"])]
            log(f"✓ 视频素材库：{params['video_dir']}")
        elif params.get("images_dir"):
            src = Path(params["images_dir"])
            dst = ep_dir / "images"
            dst.mkdir(exist_ok=True)
            n = 0
            for f in sorted(src.iterdir()):
                if f.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp"):
                    if not (dst / f.name).exists():
                        (dst / f.name).write_bytes(f.read_bytes())
                    n += 1
            log(f"✓ 本地图片：{n} 张")
            cmd.append("--skip-images")
            if params.get("shuffle_images"):
                cmd.append("--shuffle-images")

        if params.get("title"):
            cmd += ["--title", params["title"]]
        if params.get("series"):
            cmd += ["--series", params["series"]]
        if params.get("title_bg"):
            cmd += ["--title-bg", params["title_bg"]]
        if params.get("name"):
            cmd += ["--name", params["name"]]

        log(f"▶ 开始制作：{' '.join(cmd[2:])}")

        proc = subprocess.Popen(
            cmd, cwd=str(BASE_DIR), stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, bufsize=1,
            start_new_session=True,   # 独立进程组，便于整组终止
        )
        TASK["proc"] = proc
        for line in proc.stdout:
            line = line.rstrip()
            if line:
                log(line)
        proc.wait()

        if TASK.get("stopped"):
            log("⏹ 已手动停止")
            TASK["step"] = "已停止"
            return
        if proc.returncode != 0:
            raise RuntimeError(f"流水线退出码 {proc.returncode}")

        TASK["result_dir"] = str(ep_dir)
        TASK["progress"] = 100
        TASK["step"] = "完成"
        log("✅ 制作完成")
    except Exception as e:
        TASK["error"] = str(e)
        log(f"❌ 出错：{e}")
    finally:
        TASK["running"] = False
        TASK["done"] = True
        TASK["proc"] = None


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        b = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Access-Control-Allow-Origin", "*")  # SDK：同事浏览器/服务端集成
        self.end_headers()
        self.wfile.write(b)

    def do_OPTIONS(self):
        """CORS 预检（SDK 集成用）"""
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/":
            html = (BASE_DIR / "web_ui.html").read_text(encoding="utf-8")
            b = html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)
        elif u.path == "/api/status":
            with LOG_LOCK:
                self._json({
                    "running": TASK["running"], "step": TASK["step"],
                    "progress": TASK["progress"], "done": TASK["done"],
                    "error": TASK["error"], "logs": TASK["logs"][-60:],
                    "result_dir": TASK["result_dir"],
                })
        elif u.path == "/api/voices":
            """列出可用中文声线（edge-tts --list-voices），失败则用内置清单"""
            fallback = [
                {"name": "zh-CN-YunjianNeural",   "label": "云健 · 男声 · 沉稳解说（当前用）"},
                {"name": "zh-CN-YunxiNeural",     "label": "云希 · 男声 · 年轻清朗"},
                {"name": "zh-CN-YunxiaNeural",    "label": "云夏 · 男声 · 少年感"},
                {"name": "zh-CN-YunyangNeural",   "label": "云扬 · 男声 · 新闻播报"},
                {"name": "zh-CN-XiaoxiaoNeural",  "label": "晓晓 · 女声 · 温柔自然"},
                {"name": "zh-CN-XiaoyiNeural",    "label": "晓伊 · 女声 · 活泼"},
                {"name": "zh-CN-liaoning-XiaobeiNeural", "label": "晓北 · 女声 · 东北口音"},
                {"name": "zh-CN-shaanxi-XiaoniNeural",   "label": "晓妮 · 女声 · 陕西口音"},
                {"name": "zh-HK-WanLungNeural",   "label": "云龙 · 男声 · 粤语"},
                {"name": "zh-TW-YunJheNeural",    "label": "云哲 · 男声 · 台湾腔"},
            ]
            try:
                r = subprocess.run([str(BASE_DIR / "venv/bin/edge-tts"), "--list-voices"],
                                   capture_output=True, text=True, timeout=25)
                names = sorted(set(re.findall(r"(zh-[A-Za-z\-]+Neural)", r.stdout)))
                if names:
                    known = {v["name"]: v["label"] for v in fallback}
                    out = [{"name": n, "label": known.get(n, n)} for n in names]
                    # 已知的排前面
                    out.sort(key=lambda x: (x["name"] not in known, x["name"]))
                    return self._json({"voices": out})
            except Exception:
                pass
            self._json({"voices": fallback})

        elif u.path == "/api/key-status":
            env_path = BASE_DIR / ".env"
            key = ""
            if env_path.exists():
                for ln in env_path.read_text(encoding="utf-8").splitlines():
                    if ln.strip().startswith("DASHSCOPE_API_KEY"):
                        key = ln.split("=", 1)[-1].strip()
                        break
            if key:
                self._json({"configured": True,
                            "masked": key[:6] + "…" + key[-4:] if len(key) > 12 else "已配置"})
            else:
                self._json({"configured": False, "masked": ""})

        elif u.path == "/api/config":
            self._json(load_cfg())
        elif u.path == "/api/video-topics":
            """视频素材库题材列表（video_source.root 下第一级目录名）"""
            try:
                cfg = load_cfg()
                root = os.path.expanduser(
                    cfg.get("video_source", {}).get("root", "~/历史说素材/视频/"))
                topics = []
                if os.path.isdir(root):
                    topics = sorted(
                        d.name for d in Path(root).iterdir()
                        if d.is_dir() and not d.name.startswith("_"))
                self._json({"root": root, "topics": topics})
            except Exception as e:
                self._json({"root": "", "topics": [], "error": str(e)})
        elif u.path.startswith("/api/task/"):
            """SDK：查任务进度（兼容任意 id，单任务模型下返回当前/最近任务状态）"""
            tid = u.path.rsplit("/", 1)[-1]
            with LOG_LOCK:
                status = ("done" if TASK["done"]
                          else "running" if TASK["running"] else "idle")
                self._json({
                    "task_id": TASK.get("task_id") or tid,
                    "status": status,
                    "step": TASK["step"],
                    "progress": TASK["progress"],
                    "logs": TASK["logs"][-80:],
                    "error": TASK["error"],
                })
        elif u.path.startswith("/api/output/"):
            """SDK：下载成片 mp4（task_id = 期号）"""
            tid = u.path.rsplit("/", 1)[-1]
            fp = (BASE_DIR / "episodes" / tid / "final.mp4")
            if not fp.exists():
                return self._json({"ok": False,
                                   "msg": "成片不存在（任务未完成或期号错误）"}, 404)
            data = fp.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Content-Disposition",
                             f'attachment; filename="{tid}.mp4"')
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(data)
        else:
            self.send_error(404)

    def do_POST(self):
        u = urlparse(self.path)
        n = int(self.headers.get("Content-Length", 0))
        data = json.loads(self.rfile.read(n) or "{}")

        if u.path == "/api/check-script":
            path = data.get("path", "")
            if not os.path.exists(path):
                return self._json({"ok": False, "msg": "文件不存在"})
            title, body = read_script(path)
            if body is None:
                return self._json({"ok": False, "msg": "稿子读取失败"})
            has_sub = bool(re.search(r"^[一二三四五六七八九十]、", body, re.M))
            guess = guess_fields(title)
            return self._json({
                "ok": True, "chars": count_han(body), "title_line": title,
                "has_subheading": has_sub,
                "preview": body[:60],
                **guess,
            })

        if u.path == "/api/check-images":
            d = data.get("dir", "")
            if not os.path.isdir(d):
                return self._json({"ok": False, "msg": "目录不存在"})
            n = len([f for f in os.listdir(d)
                     if f.lower().endswith((".jpg", ".jpeg", ".png", ".webp"))])
            return self._json({"ok": True, "count": n})

        if u.path == "/api/stop":
            proc = TASK.get("proc")
            if not TASK["running"] or proc is None:
                return self._json({"ok": False, "msg": "当前没有正在运行的任务"})
            TASK["stopped"] = True
            try:
                import os as _os, signal as _sig
                # 终止整个进程组(含 ffmpeg 子进程)
                try:
                    _os.killpg(_os.getpgid(proc.pid), _sig.SIGTERM)
                except Exception:
                    proc.terminate()
                # 宽限后强杀
                import time as _t
                for _ in range(20):
                    if proc.poll() is not None:
                        break
                    _t.sleep(0.1)
                if proc.poll() is None:
                    try:
                        _os.killpg(_os.getpgid(proc.pid), _sig.SIGKILL)
                    except Exception:
                        proc.kill()
                # 兜底：清理可能残留的 ffmpeg
                subprocess.run(["pkill", "-9", "-f", "ffmpeg"], capture_output=True)
            except Exception as e:
                return self._json({"ok": False, "msg": f"停止失败：{e}"})
            return self._json({"ok": True})

        if u.path == "/api/ai-generate":
            """AI 智能出稿：调 qwen-max 生成稿子 → 建期号目录 → 写 script.txt + prompts.json"""
            if TASK["running"]:
                return self._json({"ok": False, "msg": "已有任务在跑，请先等它完成"})
            instruction = (data.get("instruction") or "").strip()
            if not instruction:
                return self._json({"ok": False, "msg": "请先输入你想做的内容"})
            try:
                duration_min = max(1, min(30, int(data.get("duration_min") or 3)))
            except (TypeError, ValueError):
                duration_min = 3
            # 读 .env 拿 DEEPSEEK_API_KEY
            env_path = BASE_DIR / ".env"
            api_key = ""
            if env_path.exists():
                for ln in env_path.read_text(encoding="utf-8").splitlines():
                    if ln.strip().startswith("DEEPSEEK_API_KEY"):
                        api_key = ln.split("=", 1)[-1].strip()
                        break
            if not api_key:
                return self._json({"ok": False,
                                   "msg": "未配置 DEEPSEEK_API_KEY，请到高级设置里填写"})
            cfg = load_cfg()
            try:
                result = generate_script(
                    instruction, duration_min, api_key,
                    series_name=cfg.get("title_card", {}).get("series_name", "上下五千年"),
                    style_anchor=cfg.get("image", {}).get("style_anchor", ""),
                )
            except AIScriptError as e:
                return self._json({"ok": False, "msg": str(e)})
            # 期号自动递增 + 写文件
            episode_id = next_episode_id()
            ep_dir = BASE_DIR / "episodes" / episode_id
            ep_dir.mkdir(parents=True, exist_ok=True)
            (ep_dir / "script.txt").write_text(result["full_script"], encoding="utf-8")
            prompts = [{"id": "auto_01", "title": "AI 生成",
                        "prompts": result["image_prompts"]}]
            (ep_dir / "prompts.json").write_text(
                json.dumps(prompts, ensure_ascii=False, indent=2), encoding="utf-8")
            log(f"✓ AI 出稿：期号 {episode_id}，{result['char_count']} 字，"
                f"{len(result['image_prompts'])} 张图")
            return self._json({
                "ok": True,
                "episode_id": episode_id,
                "episode_dir": str(ep_dir),
                "script_path": str(ep_dir / "script.txt"),
                "title": result["title"],
                "hook": result["hook"],
                "script_preview": result["full_script"],
                "char_count": result["char_count"],
                "image_count": len(result["image_prompts"]),
                "series_name": result["series_name"],
                "episode_name": result["episode_name"],
            })

        if u.path == "/api/ai-start":
            """AI 模式开始制作：script.txt/prompts.json 已就位，直接跑 run_pipeline"""
            if TASK["running"]:
                return self._json({"ok": False, "msg": "已有任务在跑"})
            episode = (data.get("episode") or "").strip()
            ep_dir = BASE_DIR / "episodes" / episode
            if not episode or not (ep_dir / "script.txt").exists():
                return self._json({"ok": False, "msg": "期号不存在或缺少 script.txt，请先 AI 生成"})
            data["ai_mode"] = True
            threading.Thread(target=run_pipeline, args=(data,), daemon=True).start()
            return self._json({"ok": True})

        if u.path == "/api/start":
            if TASK["running"]:
                return self._json({"ok": False, "msg": "已有任务在跑"})
            threading.Thread(target=run_pipeline, args=(data,), daemon=True).start()
            return self._json({"ok": True})

        if u.path == "/api/save-config":
            cfg = load_cfg()
            adv = data.get("advanced", {})
            if "title_duration" in adv:
                cfg.setdefault("title_card", {})["duration"] = int(adv["title_duration"])
            if "margin_bottom" in adv:
                cfg.setdefault("subtitle", {})["margin_bottom"] = int(adv["margin_bottom"])
            if "font_size" in adv:
                cfg.setdefault("subtitle", {})["font_size"] = int(adv["font_size"])
            if "crf" in adv:
                cfg["video"]["encoder_options"]["libx264"]["crf"] = int(adv["crf"])
            if "voice" in adv:
                cfg.setdefault("tts", {})["voice"] = adv["voice"]
            if "rate" in adv:
                cfg.setdefault("tts", {})["rate"] = adv["rate"]
            if "library_root" in adv:
                cfg.setdefault("output", {})["library_root"] = adv["library_root"]
            if "video_source_root" in adv and adv["video_source_root"]:
                cfg.setdefault("video_source", {})["root"] = adv["video_source_root"]
            if "series_name" in adv:
                cfg.setdefault("title_card", {})["series_name"] = adv["series_name"]
            if "img_model" in adv and adv["img_model"]:
                cfg.setdefault("image", {}).setdefault("tongyi", {})["model"] = adv["img_model"]
            if "img_size" in adv and adv["img_size"]:
                cfg.setdefault("image", {}).setdefault("tongyi", {})["size"] = adv["img_size"]
            save_cfg(cfg)
            return self._json({"ok": True})

        if u.path == "/api/material-plan":
            """GL-20260814-03：AI 选材预览（不写 episode、不触发流水线）
            body: {script, 素材库?}  script 支持文本/文件路径/投递目录（同 /api/render）
            返回: {ok, plan: [{index,text,material,clip_start,reason,duration_sec,desc,theme}],
                   素材库, degraded, pool_sec, estimate_sec}
            """
            script = data.get("script") or ""
            if not isinstance(script, str) or not script.strip():
                return self._json({"ok": False, "error": "script 不能为空（文本/文件路径/目录路径）"})
            body = resolve_script_text(script)
            if not body or not body.strip():
                return self._json({"ok": False, "error": "无法解析稿子（目录里没找到 script.txt/稿子.txt）"})
            video_lib = (data.get("素材库") or data.get("video_dir")
                         or data.get("library") or "")
            if not video_lib:
                video_lib = load_cfg().get("video_source", {}).get(
                    "root", "~/历史说素材/视频/")
            video_lib = os.path.expanduser(video_lib)

            try:
                from lib.material_scanner import scan_material
                from lib.script_seg import split_segments
                from lib.ai_script_gen import recommend_material, AIScriptError

                materials = scan_material(video_lib)
                if not materials:
                    return self._json({"ok": False,
                                       "error": f"素材库为空或素材不可用：{video_lib}"})
                segments = split_segments(body)
                if not segments:
                    return self._json({"ok": False, "error": "稿子为空，无法分段"})

                api_key = _get_env_key("DEEPSEEK_API_KEY")
                if not api_key:
                    return self._json({"ok": False,
                                       "error": "未配置 DEEPSEEK_API_KEY，请到高级设置里填写"})

                degraded = False
                try:
                    plan = recommend_material(segments, materials, api_key)
                except AIScriptError as e:
                    degraded = True
                    # 降级：顺序轮播（AI 失败不阻塞预览）
                    plan = []
                    for i, s in enumerate(segments):
                        m = materials[i % len(materials)]
                        plan.append({
                            "index": s["index"], "text": s["text"],
                            "material": os.path.basename(m["path"]),
                            "material_path": m["path"],
                            "clip_start": 0.0,
                            "reason": f"顺序轮播（AI 选材失败：{e}）",
                        })

                # 附素材信息供界面显示
                by_name = {}
                for m in materials:
                    by_name[m["name"]] = m
                    by_name[os.path.basename(m["path"])] = m
                for p in plan:
                    mat = by_name.get(os.path.basename(str(p.get("material") or "")))
                    if mat:
                        p["duration_sec"] = mat.get("duration_sec") or 0
                        p["desc"] = mat.get("desc") or ""
                        p["theme"] = mat.get("theme") or mat.get("topic") or ""
                    else:
                        p["duration_sec"] = 0
                        p["desc"] = ""
                        p["theme"] = ""

                pool_sec = sum(m.get("duration_sec") or 0 for m in materials)
                estimate_sec = round(count_han(body) / 250.0 * 60, 1)
                # 素材池简要列表（界面「换素材」下拉用）
                pool = [{
                    "name": os.path.basename(m["path"]),
                    "duration_sec": m.get("duration_sec") or 0,
                    "desc": (m.get("desc") or "")[:80],
                    "theme": m.get("theme") or m.get("topic") or "",
                } for m in materials]
                return self._json({
                    "ok": True,
                    "plan": plan,
                    "素材库": video_lib,
                    "degraded": degraded,
                    "pool_sec": pool_sec,
                    "estimate_sec": estimate_sec,
                    "pool": pool,
                })
            except Exception as e:
                return self._json({"ok": False, "error": str(e)})

        if u.path == "/api/render":
            """SDK：投稿子出片（GL-20260814-02/03）
            body: {script, 素材库?, title?, duration?, name?, material_plan?}
            script 三选一：稿子文本 / 文件路径 / 投递目录路径（目录内找 script.txt 或第一个 .txt）
            material_plan（可选）：预览确认后的选材计划，传入则写入 episode 目录直接消费
            """
            if TASK["running"]:
                return self._json({"ok": False,
                                   "msg": "已有任务在跑，请先等它完成"}, 409)
            script = data.get("script") or ""
            if not isinstance(script, str) or not script.strip():
                return self._json({"ok": False, "msg": "script 不能为空（文本/文件路径/目录路径）"})

            body = resolve_script_text(script)
            if not body or not body.strip():
                return self._json({"ok": False, "msg": "无法解析稿子（目录里没找到 script.txt/稿子.txt）"})

            # 期号自动递增 + 写稿
            episode = next_episode_id()
            ep_dir = BASE_DIR / "episodes" / episode
            ep_dir.mkdir(parents=True, exist_ok=True)
            (ep_dir / "script.txt").write_text(body, encoding="utf-8")

            # GL-20260814-03：预览确认后的选材计划 → 写入 material_plan.json（跳过内部 AI 选材）
            mp = data.get("material_plan")
            if mp and isinstance(mp, list) and mp:
                try:
                    (ep_dir / "material_plan.json").write_text(
                        json.dumps(mp, ensure_ascii=False, indent=1),
                        encoding="utf-8")
                    log(f"✓ 使用预览确认的选材计划：{len(mp)} 段")
                except Exception as e:
                    return self._json({"ok": False, "msg": f"选材计划写入失败：{e}"})


            # 素材库：素材库字段（中文）或 video_dir / library 别名
            video_lib = (data.get("素材库") or data.get("video_dir")
                         or data.get("library") or "")
            params = {
                "episode": episode,
                "ai_mode": True,
                "video_dir": video_lib,
                "title": data.get("title") or "",
                "name": data.get("name") or "",
                "series": data.get("series") or "",
            }
            threading.Thread(target=run_pipeline, args=(params,), daemon=True).start()
            log(f"✓ SDK render 任务启动：期号 {episode}，{count_han(body)} 字")
            return self._json({"ok": True, "task_id": episode,
                               "episode": episode})


        if u.path == "/api/save-key":
            """把 API Key 写入 .env（不进 config.yaml，避免随代码泄露）
            key_type: dashscope（生图）| deepseek（写稿），默认 dashscope。
            两个 key 并存，互不影响。"""
            key = (data.get("key") or "").strip()
            key_type = (data.get("key_type") or "dashscope").strip()
            if not key:
                return self._json({"ok": False, "msg": "密钥为空"})
            if len(key) < 10:
                return self._json({"ok": False, "msg": "密钥格式不对"})
            prefix_map = {
                "dashscope": "DASHSCOPE_API_KEY",
                "deepseek": "DEEPSEEK_API_KEY",
            }
            target_prefix = prefix_map.get(key_type, "DASHSCOPE_API_KEY")
            env_path = BASE_DIR / ".env"
            lines = []
            if env_path.exists():
                for ln in env_path.read_text(encoding="utf-8").splitlines():
                    stripped = ln.strip()
                    # 跳过当前 key_type 的旧行（下面重新追加），保留另一种 key
                    if stripped.startswith(target_prefix):
                        continue
                    lines.append(ln)
            lines.append(f"{target_prefix}={key}")
            env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            try:
                os.chmod(env_path, 0o600)
            except Exception:
                pass
            return self._json({"ok": True, "masked": key[:6] + "…" + key[-4:]})

        if u.path == "/api/pick":
            """调用系统原生选择框，拿到真实路径"""
            kind = data.get("kind", "file")   # file | dir
            # 文件类型：稿子选 docx/txt/md，背景图选图片格式
            ftypes = data.get("types", None)
            try:
                if sys.platform == "darwin":
                    if kind == "dir":
                        script = 'POSIX path of (choose folder with prompt "选择图片目录")'
                    else:
                        if ftypes:
                            type_str = ",".join(f'"{t}"' for t in ftypes)
                        else:
                            type_str = '"docx","txt","md"'
                        script = (f'POSIX path of (choose file with prompt "选择文件" '
                                  f'of type {{{type_str}}})')
                    r = subprocess.run(["osascript", "-e", script],
                                       capture_output=True, text=True, timeout=180)
                    path = r.stdout.strip()
                    if not path:
                        return self._json({"ok": False, "msg": "已取消"})
                    return self._json({"ok": True, "path": path})
                else:
                    # Linux/其他：尝试 zenity
                    args = ["zenity", "--file-selection"]
                    if kind == "dir":
                        args.append("--directory")
                    r = subprocess.run(args, capture_output=True, text=True, timeout=180)
                    path = r.stdout.strip()
                    if not path:
                        return self._json({"ok": False, "msg": "已取消"})
                    return self._json({"ok": True, "path": path})
            except Exception as e:
                return self._json({"ok": False, "msg": f"选择失败：{e}"})

        if u.path == "/api/open-folder":
            d = data.get("dir", "")
            if os.path.isdir(d):
                opener = "open" if sys.platform == "darwin" else "xdg-open"
                subprocess.Popen([opener, d])
                return self._json({"ok": True})
            return self._json({"ok": False, "msg": "目录不存在"})

        self.send_error(404)


def main():
    # 端口被占用自动 +1（GL-20260814-02 可移植化）
    port = PORT
    while True:
        try:
            srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
            break
        except OSError:
            port += 1
    url = f"http://127.0.0.1:{port}"
    print(f"\n{'='*44}")
    print(f"   历史说 · 出片工具  已启动")
    print(f"{'='*44}")
    print(f"\n   请在浏览器打开这个地址：\n")
    print(f"       {url}\n")
    if port != PORT:
        print(f"   （默认端口 {PORT} 被占用，已自动切换到 {port}）\n")
    print(f"   （下面会尝试自动打开；没弹出就手动复制上面地址）")
    print(f"   用完关掉此窗口即可退出\n")
    print(f"{'='*44}\n")

    # macOS 用系统 open 命令最可靠；其他平台退回 webbrowser
    opened = False
    try:
        if sys.platform == "darwin":
            subprocess.Popen(["open", url])
            opened = True
        elif sys.platform.startswith("linux"):
            subprocess.Popen(["xdg-open", url])
            opened = True
        else:
            opened = webbrowser.open(url)
    except Exception as e:
        print(f"   （自动打开失败：{e}，请手动复制上面地址）\n")
    if not opened:
        try:
            opened = webbrowser.open(url)
        except Exception:
            pass

    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n  已退出")


if __name__ == "__main__":
    main()
