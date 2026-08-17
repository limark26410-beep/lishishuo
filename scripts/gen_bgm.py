#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""程序合成氛围垫乐 v2（增强版）——4 情绪 × 2 首 → ~/Desktop/历史说素材/bgm/{情绪}/

v2 增强（GL-20260817-04 A+B）：
- 悬念：低频 drone + 心跳脉冲 + 稀疏音符
- 平叙：分解和弦 + 五声旋律线（有辨识度）
- 高潮：和弦渐强 + kick/snare 鼓点 + 上行琶音
- 收束：缓慢和弦 + 慢旋律长音 + 长尾音
纯算法原创（公共领域性质，无版权）。输出 mp3（自动转码），覆盖同名旧文件。
"""
import math, os, subprocess, sys, wave
import numpy as np

SR = 44100

def note(freq, dur, amp=0.25, harmonics=(1.0, 0.4, 0.18, 0.08, 0.04)):
    """音符：基频 + 柔和泛音 + 指数衰减包络"""
    n = int(SR * dur)
    t = np.arange(n) / SR
    wave = np.zeros(n)
    for i, h in enumerate(harmonics):
        if h <= 0:
            continue
        wave += h * np.sin(2 * np.pi * freq * (i + 1) * t)
    env = np.ones(n)
    atk = int(0.05 * SR)
    env[:atk] = np.linspace(0, 1, atk)
    env *= np.exp(-2.0 * t / max(dur, 0.001))
    return amp * wave * env

def chord(freqs, dur, amp=0.22):
    out = np.zeros(int(SR * dur))
    for f in freqs:
        seg = note(f, dur, amp=amp / len(freqs))
        out[:len(seg)] += seg
    return out

def kick(amp=0.5):
    """底鼓：低频快速衰减"""
    dur = 0.18
    n = int(SR * dur)
    t = np.arange(n) / SR
    f = 95 * np.exp(-16 * t) + 48
    phase = 2 * np.pi * np.cumsum(f) / SR
    return amp * np.sin(phase) * np.exp(-13 * t)

def snare(amp=0.22):
    """军鼓：高频噪声 + 快速衰减"""
    dur = 0.12
    n = int(SR * dur)
    t = np.arange(n) / SR
    noise = np.random.default_rng(42).normal(0, 1, n)
    # 高通近似：差分
    noise = np.diff(noise, prepend=0)
    return amp * noise * np.exp(-22 * t)

def heartbeat(amp=0.28):
    """心跳：双脉冲（lub-dub）"""
    def thump(a):
        dur = 0.16
        n = int(SR * dur)
        t = np.arange(n) / SR
        f = 70 * np.exp(-14 * t) + 40
        phase = 2 * np.pi * np.cumsum(f) / SR
        return a * np.sin(phase) * np.exp(-16 * t)
    a = thump(amp)
    b = thump(amp * 0.7)
    out = np.zeros(len(a) + len(b) + int(0.12 * SR))
    out[:len(a)] = a
    out[len(a) + int(0.12 * SR):len(a) + int(0.12 * SR) + len(b)] = b
    return out

def mix(tracks, total):
    out = np.zeros(int(SR * total))
    for start, seg in tracks:
        i0 = int(start * SR)
        i1 = min(i0 + len(seg), len(out))
        out[i0:i1] += seg[:i1 - i0]
    return out

def render(tracks, total, mp3_path, fade_in=1.0, fade_out=3.0):
    out = mix(tracks, total)
    n = len(out)
    fi, fo = int(fade_in * SR), int(fade_out * SR)
    if fi: out[:fi] *= np.linspace(0, 1, fi)
    if fo: out[-fo:] *= np.linspace(1, 0, fo)
    peak = np.max(np.abs(out)) or 1.0
    out = out / peak * 0.35  # 峰值 0.35（配乐 seg_volume 0.18 下可闻）
    wav = mp3_path.replace(".mp3", "_tmp.wav")
    with open(wav, "wb") as fh:
        w = wave.open(fh, "wb")
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(SR)
        w.writeframes((out * 32767).astype(np.int16).tobytes())
        w.close()
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", wav,
                    "-codec:a", "libmp3lame", "-b:a", "192k", mp3_path],
                   check=True)
    os.remove(wav)

def f(name):
    names = {"C":0,"C#":1,"D":2,"D#":3,"E":4,"F":5,"F#":6,"G":7,"G#":8,"A":9,"A#":10,"B":11}
    note_n, octv = name[:-1], int(name[-1])
    midi = (octv + 1) * 12 + names[note_n]
    return 440.0 * 2 ** ((midi - 69) / 12)

# ── 悬念：drone + 心跳 + 稀疏音符 ──
def sus_01():
    total = 24
    tr = [(0, note(f("A2"), total, amp=0.30, harmonics=(1.0, 0.2, 0.08)))]
    for t in range(0, total, 2):
        tr.append((t, heartbeat(0.30)))
    for i, nf in enumerate([f("B3"), f("C4"), f("D4"), f("B3"), f("C4")]):
        tr.append((2 + i * 4.5, note(nf, 2.0, amp=0.13)))
    render(tr, total, sus_01_path)

def sus_02():
    total = 28
    tr = [(0, note(f("D3"), total, amp=0.28, harmonics=(1.0, 0.2)))]
    for t in range(0, total, 2):
        tr.append((t, heartbeat(0.26)))
    for i, nf in enumerate([f("A3"), f("F4"), f("E4"), f("D4"), f("E4"), f("F4")]):
        tr.append((2 + i * 4.2, note(nf, 2.2, amp=0.12)))
    render(tr, total, sus_02_path)

# ── 平叙：分解和弦 + 五声旋律 ──
_PENTA = {"C": [f("C5"), f("D5"), f("E5"), f("G5"), f("A5")],
          "G": [f("G4"), f("A4"), f("B4"), f("D5"), f("E5")],
          "A": [f("A4"), f("B4"), f("C5"), f("E5"), f("F5")],
          "F": [f("F4"), f("G4"), f("A4"), f("C5"), f("D5")]}

def ping_01():
    total = 48
    prog = [["C3","E4","G4"], ["G3","D4","B4"], ["A3","E4","C5"], ["F3","C4","A4"]]
    tr = []
    for i, ch in enumerate(prog):
        base = i * 4
        for j, nf in enumerate(ch):
            tr.append((base + j * 1.0, note(f(nf), 1.8, amp=0.13)))
        tr.append((base, note(f(ch[0].replace("4","2").replace("3","2")), 3.8, amp=0.10)))
        # 五声旋律线：每个和弦 2 个音（0.5s 间隔）
        scale = _PENTA[ch[0][0]]
        tr.append((base + 0.5, note(scale[0], 1.6, amp=0.11)))
        tr.append((base + 2.0, note(scale[2], 1.8, amp=0.11)))
    render(tr, total, ping_01_path, fade_in=1.2, fade_out=3.0)

def ping_02():
    total = 52
    prog = [["A3","E4","C5"], ["F3","C4","A4"], ["C3","E4","G4"], ["G3","D4","B4"]]
    tr = []
    for i, ch in enumerate(prog):
        base = i * 4
        order = ch + [ch[0]]
        for j, nf in enumerate(order):
            tr.append((base + j * 0.8, note(f(nf), 1.4, amp=0.12)))
        scale = _PENTA[ch[0][0]]
        tr.append((base + 0.4, note(scale[1], 1.4, amp=0.10)))
        tr.append((base + 2.2, note(scale[3], 1.6, amp=0.10)))
    render(tr, total, ping_02_path, fade_in=1.2, fade_out=3.0)

# ── 高潮：和弦渐强 + 鼓点 + 上行琶音 ──
def gao_01():
    total = 30
    tr = []
    tr.append((0, chord([f("F3"), f("C4"), f("A4")], 10, amp=0.16)))
    tr.append((10, chord([f("G3"), f("D4"), f("B4")], 10, amp=0.20)))
    tr.append((20, chord([f("A3"), f("E4"), f("C5")], 10, amp=0.24)))
    for t in range(0, total, 1):
        tr.append((t, kick(amp=0.42 if t % 2 == 0 else 0.26)))
        if t % 2 == 1:
            tr.append((t, snare(0.16)))
    up = [f("A3"), f("C4"), f("E4"), f("A4"), f("C5"), f("E5")]
    for i in range(16):
        tr.append((10 + i * 0.5, note(up[i % len(up)], 0.9, amp=0.10)))
    render(tr, total, gao_01_path, fade_in=0.5, fade_out=2.0)

def gao_02():
    total = 34
    tr = []
    tr.append((0, chord([f("A3"), f("C4"), f("E4")], 8, amp=0.15)))
    tr.append((8, chord([f("A3"), f("D4"), f("F4")], 8, amp=0.18)))
    tr.append((16, chord([f("A3"), f("E4"), f("G4")], 8, amp=0.21)))
    tr.append((24, chord([f("A3"), f("C4"), f("E4")], 10, amp=0.25)))
    for t in range(0, total, 1):
        tr.append((t, kick(amp=0.45 if t % 2 == 0 else 0.28)))
        if t % 2 == 1:
            tr.append((t, snare(0.18)))
    up = [f("A3"), f("C4"), f("E4"), f("A4"), f("C5"), f("E5"), f("A5")]
    for i in range(20):
        tr.append((12 + i * 0.5, note(up[i % len(up)], 0.9, amp=0.11)))
    render(tr, total, gao_02_path, fade_in=0.5, fade_out=2.0)

# ── 收束：缓慢和弦 + 慢旋律 + 长尾音 ──
def shou_01():
    total = 24
    tr = []
    tr.append((0, chord([f("C3"), f("E4"), f("G4")], 8, amp=0.18)))
    tr.append((8, chord([f("F3"), f("A4"), f("C5")], 8, amp=0.15)))
    tr.append((16, chord([f("C3"), f("E4"), f("G4")], 8, amp=0.13)))
    for i, nf in enumerate([f("C5"), f("B4"), f("A4"), f("G4")]):
        tr.append((2 + i * 5, note(nf, 3.2, amp=0.10)))
    render(tr, total, shou_01_path, fade_in=1.0, fade_out=4.5)

def shou_02():
    total = 26
    tr = []
    tr.append((0, chord([f("A3"), f("C4"), f("E4")], 12, amp=0.16)))
    tr.append((12, chord([f("F3"), f("A4"), f("C5")], 14, amp=0.13)))
    for i, nf in enumerate([f("C5"), f("B4"), f("A4"), f("G4"), f("E4"), f("C5")]):
        tr.append((2 + i * 4, note(nf, 3.4, amp=0.09)))
    render(tr, total, shou_02_path, fade_in=1.0, fade_out=5.0)

if __name__ == "__main__":
    root = sys.argv[1] if len(sys.argv) > 1 else "/Users/local/Desktop/历史说素材/bgm"
    g = {}
    for emo in ["悬念", "平叙", "高潮", "收束"]:
        g[emo] = os.path.join(root, emo)
        os.makedirs(g[emo], exist_ok=True)
    globals().update({
        "sus_01_path": os.path.join(g["悬念"], "悬念垫乐_01.mp3"),
        "sus_02_path": os.path.join(g["悬念"], "悬念垫乐_02.mp3"),
        "ping_01_path": os.path.join(g["平叙"], "平叙垫乐_01.mp3"),
        "ping_02_path": os.path.join(g["平叙"], "平叙垫乐_02.mp3"),
        "gao_01_path": os.path.join(g["高潮"], "高潮垫乐_01.mp3"),
        "gao_02_path": os.path.join(g["高潮"], "高潮垫乐_02.mp3"),
        "shou_01_path": os.path.join(g["收束"], "收束垫乐_01.mp3"),
        "shou_02_path": os.path.join(g["收束"], "收束垫乐_02.mp3"),
    })
    for fn in [sus_01, sus_02, ping_01, ping_02, gao_01, gao_02, shou_01, shou_02]:
        fn()
        print("  ✓", fn.__name__)
    print("增强版垫乐生成完成:", root)
