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
import subprocess
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

import yaml

BASE_DIR = Path(__file__).parent.resolve()
CONFIG_PATH = BASE_DIR / "config.yaml"
PORT = 8765

# 任务状态（单任务，够用）
TASK = {
    "running": False,
    "logs": [],
    "step": "",
    "progress": 0,
    "done": False,
    "error": None,
    "result_dir": "",
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
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


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
    out = {"guess_episode": "", "guess_title": "", "guess_name": ""}
    if not title_line:
        return out

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


def run_pipeline(params):
    """后台线程跑流水线"""
    TASK.update({"running": True, "logs": [], "step": "准备中",
                 "progress": 1, "done": False, "error": None})
    try:
        episode = params["episode"]
        ep_dir = BASE_DIR / "episodes" / episode
        ep_dir.mkdir(parents=True, exist_ok=True)

        # 1. 准备稿子：去标题，写入 script.txt
        title_line, body = read_script(params["script_path"])
        if body is None:
            raise RuntimeError("稿子读取失败")
        (ep_dir / "script.txt").write_text(body, encoding="utf-8")
        log(f"✓ 稿子就绪：{count_han(body)} 字（已去标题）")

        # 2. 本地图片
        cmd = [sys.executable, str(BASE_DIR / "run.py"), "-e", episode]
        if params.get("images_dir"):
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

        if params.get("title"):
            cmd += ["--title", params["title"]]
        if params.get("name"):
            cmd += ["--name", params["name"]]

        log(f"▶ 开始制作：{' '.join(cmd[2:])}")

        proc = subprocess.Popen(
            cmd, cwd=str(BASE_DIR), stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, bufsize=1,
        )
        for line in proc.stdout:
            line = line.rstrip()
            if line:
                log(line)
        proc.wait()

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


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        b = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

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
                r = subprocess.run(["edge-tts", "--list-voices"],
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
            if "img_model" in adv and adv["img_model"]:
                cfg.setdefault("image", {}).setdefault("tongyi", {})["model"] = adv["img_model"]
            if "img_size" in adv and adv["img_size"]:
                cfg.setdefault("image", {}).setdefault("tongyi", {})["size"] = adv["img_size"]
            save_cfg(cfg)
            return self._json({"ok": True})

        if u.path == "/api/save-key":
            """把 API Key 写入 .env（不进 config.yaml，避免随代码泄露）"""
            key = (data.get("key") or "").strip()
            if not key:
                return self._json({"ok": False, "msg": "密钥为空"})
            if len(key) < 10:
                return self._json({"ok": False, "msg": "密钥格式不对"})
            env_path = BASE_DIR / ".env"
            lines = []
            if env_path.exists():
                lines = [ln for ln in env_path.read_text(encoding="utf-8").splitlines()
                         if not ln.strip().startswith("DASHSCOPE_API_KEY")]
            lines.append(f"DASHSCOPE_API_KEY={key}")
            env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            try:
                os.chmod(env_path, 0o600)
            except Exception:
                pass
            return self._json({"ok": True, "masked": key[:6] + "…" + key[-4:]})

        if u.path == "/api/pick":
            """调用系统原生选择框，拿到真实路径"""
            kind = data.get("kind", "file")   # file | dir
            try:
                if sys.platform == "darwin":
                    if kind == "dir":
                        script = 'POSIX path of (choose folder with prompt "选择图片目录")'
                    else:
                        script = ('POSIX path of (choose file with prompt "选择稿子" '
                                  'of type {"docx","txt","md"})')
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
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    url = f"http://127.0.0.1:{PORT}"
    print(f"\n{'='*44}")
    print(f"   历史说 · 出片工具  已启动")
    print(f"{'='*44}")
    print(f"\n   请在浏览器打开这个地址：\n")
    print(f"       {url}\n")
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
