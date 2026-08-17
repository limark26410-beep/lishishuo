#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""程序合成氛围垫乐（4 情绪 × 2 首）→ ~/Desktop/历史说素材/bgm/{情绪}/
纯算法原创（公共领域性质，无版权问题）。风格朴素但干净、不抢人声。
"""
import math, os, subprocess, sys
import numpy as np

SR = 44100

def note(freq, dur, amp=0.25, harmonics=(1.0, 0.35, 0.12, 0.05)):
    """一个音符：基频 + 柔和泛音 + 指数衰减包络"""
    n = int(SR * dur)
    t = np.arange(n) / SR
    wave = np.zeros(n)
    for i, h in enumerate(harmonics):
        if h <= 0:
            continue
        wave += h * np.sin(2 * np.pi * freq * (i + 1) * t)
    # 包络：attack 0.06s + 指数衰减
    env = np.ones(n)
    atk = int(0.06 * SR)
    env[:atk] = np.linspace(0, 1, atk)
    env *= np.exp(-2.2 * t / max(dur, 0.001))
    return amp * wave * env

def chord(freqs, dur, amp=0.2):
    """和弦：多个音符同时响"""
    out = np.zeros(int(SR * dur))
    for f in freqs:
        seg = note(f, dur, amp=amp / len(freqs))
        out[:len(seg)] += seg
    return out

def kick(amp=0.5):
    """底鼓：低频快速衰减（高潮节奏用）"""
    dur = 0.18
    n = int(SR * dur)
    t = np.arange(n) / SR
    f = 90 * np.exp(-18 * t) + 45
    phase = 2 * np.pi * np.cumsum(f) / SR
    env = np.exp(-14 * t)
    return amp * np.sin(phase) * env

def mix(tracks, total):
    """叠加音轨（tracks: [(start_sec, array)]）"""
    out = np.zeros(int(SR * total))
    for start, seg in tracks:
        i0 = int(start * SR)
        i1 = min(i0 + len(seg), len(out))
        out[i0:i1] += seg[:i1 - i0]
    return out

def render(tracks, total, path, fade_in=1.0, fade_out=2.5):
    """合成 + 淡入淡出 + 归一化 → wav"""
    out = mix(tracks, total)
    n = len(out)
    fi = int(fade_in * SR)
    fo = int(fade_out * SR)
    if fi > 0:
        out[:fi] *= np.linspace(0, 1, fi)
    if fo > 0:
        out[-fo:] *= np.linspace(1, 0, fo)
    peak = np.max(np.abs(out)) or 1.0
    out = out / peak * 0.30  # 留 headroom，流水线还会压
    data = (out * 32767).astype(np.int16)
    with open(path, "wb") as f:
        import wave
        w = wave.open(f, "wb")
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(SR)
        w.writeframes(data.tobytes())
        w.close()

# 音名 → 频率（A4=440）
def f(name):
    names = {"C":0,"C#":1,"D":2,"D#":3,"E":4,"F":5,"F#":6,"G":7,"G#":8,"A":9,"A#":10,"B":11}
    note_n, octv = name[:-1], int(name[-1])
    midi = (octv + 1) * 12 + names[note_n]
    return 440.0 * 2 ** ((midi - 69) / 12)

# ── 悬念（低频 drone + 稀疏不协和音，留白多）──
def sus_01():
    total = 22
    tr = []
    tr.append((0, note(f("A2"), total, amp=0.30, harmonics=(1.0, 0.2, 0.08))))
    tr.append((0, note(f("E3"), total, amp=0.15, harmonics=(1.0, 0.3))))
    for i, nf in enumerate([f("B3"), f("C4"), f("D4"), f("B3")]):
        tr.append((2 + i * 5, note(nf, 2.2, amp=0.12)))
    render(tr, total, sus_01_path)

def sus_02():
    total = 26
    tr = []
    tr.append((0, note(f("D3"), total, amp=0.28, harmonics=(1.0, 0.2))))
    for i, nf in enumerate([f("A3"), f("F4"), f("E4"), f("D4")]):
        tr.append((2 + i * 6, note(nf, 2.8, amp=0.11)))
    render(tr, total, sus_02_path)

# ── 平叙（温暖分解和弦 I-V-vi-IV）──
def ping_01():
    total = 44
    prog = [["C3","E4","G4"], ["G3","D4","B4"], ["A3","E4","C5"], ["F3","C4","A4"]]
    tr = []
    for i, ch in enumerate(prog):
        base = i * 4
        # 分解琶音（每个音符 1s）
        for j, nf in enumerate(ch):
            tr.append((base + j * 1.0, note(f(nf), 1.8, amp=0.14)))
        tr.append((base, note(f(ch[0].replace("4","2").replace("3","2")), 3.8, amp=0.10)))
    render(tr, total, ping_01_path, fade_in=1.2, fade_out=3.0)

def ping_02():
    total = 48
    prog = [["A3","E4","C5"], ["F3","C4","A4"], ["C3","E4","G4"], ["G3","D4","B4"]]
    tr = []
    for i, ch in enumerate(prog):
        base = i * 4
        order = ch + [ch[0]]
        for j, nf in enumerate(order):
            tr.append((base + j * 0.8, note(f(nf), 1.4, amp=0.12)))
    render(tr, total, ping_02_path, fade_in=1.2, fade_out=3.0)

# ── 高潮（渐强 + 鼓点推进）──
def gao_01():
    total = 26
    tr = []
    # 和弦长音 F-G-Am 渐强
    tr.append((0, chord([f("F3"), f("C4"), f("A4")], 8, amp=0.16)))
    tr.append((8, chord([f("G3"), f("D4"), f("B4")], 8, amp=0.19)))
    tr.append((16, chord([f("A3"), f("E4"), f("C5")], 10, amp=0.23)))
    # 鼓点节奏 0.5s 一拍
    for t in range(0, int(total / 0.5)):
        tr.append((t * 0.5, kick(amp=0.35 if t % 2 else 0.22)))
    render(tr, total, gao_01_path, fade_in=0.5, fade_out=2.0)

def gao_02():
    total = 30
    tr = []
    tr.append((0, chord([f("A3"), f("C4"), f("E4")], 7, amp=0.15)))
    tr.append((7, chord([f("A3"), f("D4"), f("F4")], 7, amp=0.18)))
    tr.append((14, chord([f("A3"), f("E4"), f("G4")], 7, amp=0.21)))
    tr.append((21, chord([f("A3"), f("C4"), f("E4")], 9, amp=0.25)))
    for t in range(0, int(total / 0.5)):
        tr.append((t * 0.5, kick(amp=0.4 if t % 2 else 0.25)))
    # 上行琶音加速
    up = [f("A3"), f("C4"), f("E4"), f("A4"), f("C5"), f("E5")]
    for i in range(12):
        tr.append((12 + i * 0.5, note(up[i % len(up)], 0.9, amp=0.10)))
    render(tr, total, gao_02_path, fade_in=0.5, fade_out=2.0)

# ── 收束（舒缓收尾，渐弱）──
def shou_01():
    total = 22
    tr = []
    tr.append((0, chord([f("C3"), f("E4"), f("G4")], 8, amp=0.18)))
    tr.append((8, chord([f("F3"), f("A4"), f("C5")], 8, amp=0.15)))
    tr.append((16, chord([f("C3"), f("E4"), f("G4")], 6, amp=0.13)))
    tr.append((16, note(f("C5"), 6, amp=0.08)))
    render(tr, total, shou_01_path, fade_in=1.0, fade_out=4.0)

def shou_02():
    total = 24
    tr = []
    tr.append((0, chord([f("A3"), f("C4"), f("E4")], 12, amp=0.16)))
    tr.append((12, chord([f("F3"), f("A4"), f("C5")], 12, amp=0.13)))
    # 慢旋律 C5-B4-A4-G4-E4
    for i, nf in enumerate([f("C5"), f("B4"), f("A4"), f("G4"), f("E4")]):
        tr.append((2 + i * 4, note(nf, 3.0, amp=0.09)))
    render(tr, total, shou_02_path, fade_in=1.0, fade_out=4.0)

if __name__ == "__main__":
    root = sys.argv[1] if len(sys.argv) > 1 else "/Users/local/Desktop/历史说素材/bgm"
    g = {}
    for emo in ["悬念", "平叙", "高潮", "收束"]:
        g[emo] = os.path.join(root, emo)
        os.makedirs(g[emo], exist_ok=True)
    # 全局路径变量（渲染函数用）
    globals().update({
        "sus_01_path": os.path.join(g["悬念"], "悬念垫乐_01.wav"),
        "sus_02_path": os.path.join(g["悬念"], "悬念垫乐_02.wav"),
        "ping_01_path": os.path.join(g["平叙"], "平叙垫乐_01.wav"),
        "ping_02_path": os.path.join(g["平叙"], "平叙垫乐_02.wav"),
        "gao_01_path": os.path.join(g["高潮"], "高潮垫乐_01.wav"),
        "gao_02_path": os.path.join(g["高潮"], "高潮垫乐_02.wav"),
        "shou_01_path": os.path.join(g["收束"], "收束垫乐_01.wav"),
        "shou_02_path": os.path.join(g["收束"], "收束垫乐_02.wav"),
    })
    for fn in [sus_01, sus_02, ping_01, ping_02, gao_01, gao_02, shou_01, shou_02]:
        fn()
        print("  ✓", fn.__name__)
    print("生成完成:", root)
