#!/usr/bin/env python3
"""Narrator voice processing: EQ, compression, convolution reverb — over the whole track.

Reverb is afir with a synthetic impulse (pink noise with exponential decay), deterministic by
seed, so renders are reproducible. Presets were tuned on the video Jev vs LLM (gpt-audio ash
voice); "trailer" is what made the final cut.

Прод собирает дорожку шиной (voice_bus.py) и применяет обработку один раз на весь ролик.
Обработка каждого клипа отдельно обрезала хвост реверберации на каждой реплике и
выравнивала громкость каждой отдельно: комната и уровень «прыгали» на стыке, и это слышалось
как щелчок при нулевом уровне сэмпловых разрывов. Здесь остаётся только инструмент для
сравнения пресетов на слух.

  python3 scripts/voice_fx.py projects/x/audio/fit projects/x/audio/fx --preset trailer
  python3 scripts/voice_fx.py projects/x/audio/fit /tmp/ab --compare 0       # A/B of all presets
  python3 scripts/voice_fx.py ... --preset trailer --bass 7 --wet 0.25     # manual tweaking
"""

import argparse
import glob
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from ndv_common import edge_fades, ffmpeg  # noqa: E402

PRESETS = {
    # bass: dB shelf at 110 Hz; presence: dB at 3 kHz; comp: ratio (0 = no compressor);
    # room: reverb tail seconds; wet: reverb share; pitch: semitones (negative = lower);
    # band: "lo-hi" Hz bandpass (radio). loudnorm at the end evens out loudness,
    # so small EQ differences are barely audible: presets are made clearly different.
    "natural":   dict(bass=0, presence=0,   comp=0, room=0.0, wet=0.0,  pitch=0,  band=""),
    "trailer":   dict(bass=5, presence=2.5, comp=2, room=1.4, wet=0.18, pitch=0,  band=""),
    "deep":      dict(bass=6, presence=2,   comp=2, room=1.4, wet=0.15, pitch=-2, band=""),
    "titan":     dict(bass=7, presence=3,   comp=3, room=1.8, wet=0.20, pitch=-4, band=""),
    "cathedral": dict(bass=3, presence=1.5, comp=2, room=3.2, wet=0.45, pitch=0,  band=""),
    "radio":     dict(bass=0, presence=4,   comp=8, room=0.0, wet=0.0,  pitch=0,  band="350-3400"),
}

# Динамический диапазон сжатия задаёт LRA: 7 превращало живую подачу в ровную,
# 11–15 оставляет интонацию, а loudnorm всё равно держит общую громкость.
LOUD_I, LOUD_TP, LOUD_LRA = -16, -1.5, 13


def make_ir(path, room, seed=7):
    ffmpeg("-f", "lavfi", "-i", f"anoisesrc=d={room}:c=pink:r=48000:a=0.5:seed={seed}",
           "-af", f"afade=t=out:st=0:d={room}:curve=exp,lowpass=f=5500,highpass=f=200", "-ac", "2", path)


def chain(p, ir=None):
    """Обработка голоса одним куском: EQ, компрессор, реверберация, громкость.

    Реверберация и loudnorm идут по всей дорожке, а не по клипу: комната и уровень —
    свойства ролика. Обрабатывая каждый фрагмент отдельно, мы обрезали хвост реверберации
    на каждой реплике и выравнивали громкость каждой отдельно, и на стыках это слышалось
    как щелчок, хотя на уровне сэмплов там ничего не было.
    """
    f = ["aresample=48000", "highpass=f=60"]
    if p["pitch"]:
        r = 2 ** (p["pitch"] / 12)            # lower pitch, same duration
        f += [f"asetrate={48000 * r:.0f}", "aresample=48000", f"atempo={1 / r:.5f}"]
    if p["band"]:
        lo, hi = p["band"].split("-")
        f += [f"highpass=f={lo}", f"lowpass=f={hi}"]
    if p["bass"]:
        f.append(f"bass=g={p['bass']}:f=110:w=0.7")
    if p["presence"]:
        f.append(f"equalizer=f=3000:t=q:w=1.2:g={p['presence']}")
    if p["comp"]:
        f.append(f"acompressor=threshold=0.1:ratio={p['comp']}:attack=5:release=90:makeup=3")
    f.append("aformat=channel_layouts=stereo")
    head = ",".join(f)
    # loudnorm ресемплит в 192 кГц и съедает хвост, поэтому после него — aresample
    tail = f"loudnorm=I={LOUD_I}:TP={LOUD_TP}:LRA={LOUD_LRA},aresample=48000"
    if p["wet"] > 0 and ir:
        return (f"[0:a]{head},asplit=2[dry][w];[w][1:a]afir=dry=0:wet=10:length=1[wet];"
                f"[dry][wet]amix=inputs=2:weights='1 {p['wet']}':normalize=0,{tail}[o]", 1)
    return (f"[0:a]{head},{tail}[o]", 0)


def apply(src, dst, p, ir=None):
    """Обработать файл целиком, без рамп по краям."""
    fc, inputs = chain(p, ir)
    args = ["-i", src] + (["-i", ir] if inputs else [])
    ffmpeg(*args, "-filter_complex", fc, "-map", "[o]", dst)


def process(src, dst, p, ir, fin, fout):
    """Обработка одного клипа с рампами по краям — только для A/B пресетов.

    Прод выигрывает шина (voice_bus.py): там рампы ставятся на стыке реплик, а не
    на краях кусков.
    """
    fc, inputs = chain(p, ir)
    if fin or fout:
        fc = fc[:-3] + "," + edge_fades(fin=fin, fout=fout) + "[o]"
    args = ["-i", src] + (["-i", ir] if inputs else [])
    ffmpeg(*args, "-filter_complex", fc, "-map", "[o]", dst)


def edge_seconds(p, fout_arg):
    """Tail fade long enough to also let the reverb die; 50 ms unless the room is loud.

    wet² because the tail is heard in proportion to the wet share, not to the room length:
    trailer (1.4 s / 18%) gets 75 ms, cathedral (3.2 s / 45%) the 200 ms cap, natural 50 ms.
    """
    return fout_arg if fout_arg is not None else min(0.20, max(0.05, 0.03 + p["room"] * p["wet"] ** 2))


def params(a, name):
    p = dict(PRESETS[name])
    for k in ("bass", "presence", "comp", "room", "wet", "pitch"):
        if getattr(a, k) is not None:
            p[k] = getattr(a, k)
    return p


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("in_dir", help="файл wav или каталог клипов (только для --compare)")
    ap.add_argument("out_dir")
    ap.add_argument("--preset", default="trailer", choices=PRESETS)
    ap.add_argument("--compare", type=int, nargs="*", help="line numbers for an A/B file of all presets")
    ap.add_argument("--fade-in", type=float, default=0.005, help="A/B: рампа входа, секунды")
    ap.add_argument("--fade-out", type=float, help="A/B: рампа выхода, по умолчанию под хвост реверберации")
    for k in ("bass", "presence", "comp", "room", "wet", "pitch"):
        ap.add_argument(f"--{k}", type=float)
    a = ap.parse_args()
    os.makedirs(a.out_dir, exist_ok=True)

    if a.compare is not None:
        if not os.path.isdir(a.in_dir):
            raise SystemExit("--compare ждёт каталог с line_NN.wav")
        parts = []
        for name in PRESETS:
            p = params(a, name)
            ir = os.path.join(a.out_dir, f"ir_{name}.wav") if p["wet"] else None
            if ir:
                make_ir(ir, p["room"])
            fout = edge_seconds(p, a.fade_out)
            for i in a.compare:
                out = os.path.join(a.out_dir, f"ab_{name}_{i:02d}.wav")
                process(os.path.join(a.in_dir, f"line_{i:02d}.wav"), out, p, ir, a.fade_in, fout)
                parts.append(out)
        # concatenate with pauses in preset order
        inputs = sum((["-i", x] for x in parts), [])
        fc = "".join(f"[{k}:a]apad=pad_dur=1.0[a{k}];" for k in range(len(parts)))
        fc += "".join(f"[a{k}]" for k in range(len(parts))) + f"concat=n={len(parts)}:v=0:a=1[o]"
        ab = os.path.join(a.out_dir, "AB-" + "-".join(PRESETS) + ".wav")
        ffmpeg(*inputs, "-filter_complex", fc, "-map", "[o]", ab)
        print("A/B:", ab, "order:", ", ".join(PRESETS))
        return

    if os.path.isdir(a.in_dir):
        raise SystemExit("прод собирает голос шиной: scripts/voice_bus.py narration.json audio/fit audio/fx. "
                         "Здесь нужен один файл (--compare — каталог клипов).")
    p = params(a, a.preset)
    ir = os.path.join(a.out_dir, "ir.wav") if p["wet"] else None
    if ir:
        make_ir(ir, p["room"])
    out = a.out_dir if a.out_dir.endswith(".wav") else os.path.join(a.out_dir, os.path.basename(a.in_dir))
    apply(a.in_dir, out, p, ir)
    print(f"{a.preset} {p} → {out}")


if __name__ == "__main__":
    main()
