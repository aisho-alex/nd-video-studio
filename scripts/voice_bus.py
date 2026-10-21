#!/usr/bin/env python3
"""Голос ролика одной дорожкой: раскладка реплик по таймлайну и одна обработка на весь ролик.

Почему шина, а не обработка каждого клипа. Реплики вырезаны из одной записи сцены, но
обрабатывались по отдельности — и на стыке это слышалось как щелчок:

- реверберация применялась к каждому куску, поэтому её хвост обрывался на каждой реплике,
  и комната «перезапускалась» тридцать шесть раз;
- loudnorm выравнивал каждую реплику к −16 LUFS отдельно, и громкость прыгала;
- компрессор сжимал каждую реплику отдельно, и подача «дышала» на стыках.

Всё это — разные куски одной дорожки, поэтому и комната, и уровень, и компрессор должны
быть общими. Рампы на стыках остаются: движок обрезает фразу на полуслове, и без них
начало реплики щёлкает.

Тишина между репликами — не ноль, а едва слышный розовый шум: цифровой ноль на паузе
делает любой стык заметным, а шумовая подложка маскирует его и звучит как комната.

  python3 scripts/voice_bus.py projects/x/narration.json projects/x/audio/fit projects/x/audio/fx
  python3 scripts/voice_bus.py narration.json audio/fit audio/fx --preset natural --room-tone-db -60
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from ndv_common import BUS_FADE_IN as FADE_IN, BUS_FADE_OUT as FADE_OUT  # noqa: E402
from ndv_common import clip_name, duration, edge_fades, ffmpeg  # noqa: E402
from voice_fx import PRESETS, apply as fx_apply, chain, make_ir, params  # noqa: E402

ROOM_TONE_DB = -50   # подложка под паузы; ≤ -120 = выключить


def layout(narration, in_dir, out, fade_in, fade_out):
    """Реплики на таймлайн: задержка по старту, рампы на стыках, сумма в одну дорожку."""
    lines = narration["lines"]
    total = narration["duration"]
    args = []
    for i in range(len(lines)):
        args += ["-i", os.path.join(in_dir, clip_name(i))]
    graph = []
    for i, ln in enumerate(lines):
        ms = int(ln["start"] * 1000)
        end = ln["start"] + duration(os.path.join(in_dir, clip_name(i)))
        ramps = edge_fades(dur=end, fin=fade_in, fout=fade_out, start=ln["start"])
        graph.append(f"[{i}:a]aresample=48000,aformat=channel_layouts=stereo,adelay={ms}|{ms},"
                     f"apad=whole_dur={total}{',' + ramps if ramps else ''}[v{i}]")
    graph.append("".join(f"[v{i}]" for i in range(len(lines))) +
                 f"amix=inputs={len(lines)}:normalize=0:duration=longest,atrim=0:{total}[o]")
    ffmpeg(*args, "-filter_complex", ";".join(graph), "-map", "[o]",
           "-ac", "2", "-ar", "48000", "-c:a", "pcm_s16le", out)


def finish(raw, out, p, ir, tone_db, total):
    """Обработка всей дорожки + шумовая подложка + dither на 16-бит."""
    fc, used = chain(p, ir)
    args = ["-i", raw]
    if used:
        args += ["-i", ir]
    parts, label = [fc], "[o]"
    if tone_db > -120:
        amp = 10 ** (tone_db / 20)
        args += ["-f", "lavfi", "-i", f"anoisesrc=r=48000:c=pink:a={amp:.6f}:seed=7"]
        bed = 1 + used                       # индексы входов: raw=0, ir=1 если есть, дальше подложка
        parts.append(f"[{bed}:a]atrim=0:{total},apad=whole_dur={total}[bed]")
        parts.append("[o][bed]amix=inputs=2:normalize=0:duration=longest:dropout_transition=0[m]")
        label = "[m]"
    # dither перед 16-битным выходом: без него тихие паузы квантуются ступеньками.
# Отдельного фильтра dither в ffmpeg до 7 нет, это опция aresample.
    parts.append(f"{label}apad=whole_dur={total},atrim=0:{total},"
                 f"aresample=48000:dither_method=triangular[fin]")
    ffmpeg(*args, "-filter_complex", ";".join(parts), "-map", "[fin]",
           "-ac", "2", "-ar", "48000", "-c:a", "pcm_s16le", out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("narration")
    ap.add_argument("in_dir", help="audio/fit")
    ap.add_argument("out_dir", help="audio/fx")
    ap.add_argument("--preset", default="trailer", choices=PRESETS)
    ap.add_argument("--fade-in", type=float, default=FADE_IN)
    ap.add_argument("--fade-out", type=float, default=FADE_OUT)
    ap.add_argument("--room-tone-db", type=float, default=ROOM_TONE_DB,
                    help="уровень шумовой подложки под паузами, 0 = без неё")
    for k in ("bass", "presence", "comp", "room", "wet", "pitch"):
        ap.add_argument(f"--{k}", type=float)
    a = ap.parse_args()

    import json
    with open(a.narration) as f:
        n = json.load(f)
    total = n["duration"]
    os.makedirs(a.out_dir, exist_ok=True)
    raw = os.path.join(a.out_dir, "bus_raw.wav")
    out = os.path.join(a.out_dir, "voice.wav")

    p = params(a, a.preset)
    ir = os.path.join(a.out_dir, "ir.wav") if p["wet"] else None
    if ir:
        make_ir(ir, p["room"])

    layout(n, a.in_dir, raw, a.fade_in, a.fade_out)
    finish(raw, out, p, ir, a.room_tone_db, total)
    os.remove(raw)
    print(f"{a.preset} {p} → {out} ({duration(out):.2f}s, шумовая подложка {a.room_tone_db} dBFS, "
          f"рампы {a.fade_in * 1000:.0f}/{a.fade_out * 1000:.0f} мс)")


if __name__ == "__main__":
    main()