#!/usr/bin/env python3
"""Click detector for the audio pipeline.

The TTS engines cut their clips hard — the file ends mid-waveform and the next line
starts on a step — which is a click at both ends of every line, loudest in the gap
between lines. Two measurements:

- **edges**, the level of the first/last 2 ms against the peak of the 50 ms next to it,
  in dB. A clip that opens or closes at full level sits near 0 dB: that is a step, one
  sample wide, and it clicks. Anything below --max-edge is ramped. This is the gate.
- **jumps**, isolated sample-to-sample steps against the local RMS. Reported as warnings
  only: a plosive in the middle of a word looks exactly like a click by level alone.

A file or a directory of line_NN.wav; with --narration a mixdown is also checked at every
line seam. Exit code 1 when an edge is above the threshold, so build_audio.sh stops.

  python3 scripts/audio_qc.py projects/x/audio/fx
  python3 scripts/audio_qc.py projects/x/audio/mix.wav --narration projects/x/narration.json
  python3 scripts/audio_qc.py projects/x/audio/fx --max-edge -4       # stricter
"""

import argparse
import glob
import json
import math
import os
import sys
import wave
from array import array
from operator import sub

sys.path.insert(0, os.path.dirname(__file__))
from ndv_common import clip_name, load_narration  # noqa: E402

WIN = 0.005          # RMS window, seconds
FLOOR = 0.05         # a step counts as a jump from this amplitude, -26 dBFS
RATIO = 6            # ...or from this multiple of the local RMS


def mono(path):
    """Mono float samples in [-1, 1]; stereo is averaged down."""
    w = wave.open(path)
    if w.getsampwidth() != 2:
        raise SystemExit(f"{path}: only 16-bit PCM is supported, got {w.getsampwidth() * 8}-bit")
    n, ch = w.getnframes(), w.getnchannels()
    s = array("h", w.readframes(n))
    w.close()
    if ch == 1:
        return [x / 32768 for x in s]
    return [(s[i] + s[i + 1]) / 65536 for i in range(0, len(s) - 1, ch)]


def db(v):
    return 20 * math.log10(max(1e-6, v))


def rms(s, a, b):
    return math.sqrt(sum(x * x for x in s[a:b]) / (b - a)) if b > a else 0.0


def edges(s, sr):
    """Level at the very first/last 2 ms against the peak of the 50 ms next to it, dB.

    This is what a click is, as opposed to a fade: a clip that opens (or closes) at
    full level is a step from/to silence, one sample wide. A ramp — even a 15 ms one —
    starts far below the level next to it and reads as a large negative number.
    """
    w = max(1, int(0.002 * sr))
    span = int(0.05 * sr)
    step = max(1, int(WIN * sr))
    if len(s) <= w:
        return {"head_db": -120.0, "tail_db": -120.0}
    peak = max(rms(s, i, i + step) for i in range(0, min(span, len(s) - w + 1), step))
    if peak < 1e-4:            # digital silence at the edge: nothing can click there
        return {"head_db": -120.0, "tail_db": -120.0}
    return {"head_db": round(db(rms(s, 0, w)) - db(peak), 1),
            "tail_db": round(db(rms(s, len(s) - w, len(s))) - db(peak), 1)}


def jumps(s, sr):
    """Isolated sample-to-sample steps on a quiet background; speech transients show up too."""
    w = max(1, int(WIN * sr))
    win = [(rms(s, i, i + w), max(map(abs, map(sub, s[i + 1:i + w], s[i:i + w - 1]))))
           for i in range(0, len(s) - w + 1, w)]
    out = []
    for i, (level, step) in enumerate(win):
        # a step only counts against the background around it: a click is loud in a quiet place
        near = max(level for level, _ in win[max(0, i - 2):i] + win[i + 1:i + 3]) if len(win) > 2 else 0.0
        if step < max(FLOOR, RATIO * near):
            continue
        t = i * w / sr
        if out and t - out[-1]["t"] < 0.05:
            out[-1]["step"] = max(out[-1]["step"], round(step, 3))
        else:
            out.append({"t": round(t, 3), "step": round(step, 3)})
    return out


def check(path):
    sr_w = wave.open(path)
    sr = sr_w.getframerate()
    sr_w.close()
    s = mono(path)
    res = {"file": path, **edges(s, sr)}
    res["max_jumps"] = jumps(s, sr)
    res["worst_edge_db"] = max(res["head_db"], res["tail_db"])
    return res


def check_mix(path, narration):
    """Line seams in a mixdown: the same edge measurement around every line start and end."""
    w_clip = wave.open(path)
    sr = w_clip.getframerate()
    w_clip.close()
    s = mono(path)
    lines = load_narration(narration)["lines"]
    w = max(1, int(0.002 * sr))
    step = max(1, int(WIN * sr))
    span = int(0.05 * sr)
    out = []
    for i, ln in enumerate(lines):
        a = int(ln["start"] * sr)
        # the line ends where its own clip ends: the mix holds the placement, the length is the clip's
        clip = os.path.join(os.path.dirname(os.path.dirname(path)), "audio", "fx", clip_name(i))
        if not os.path.exists(clip) or a + w >= len(s):
            continue
        wn = wave.open(clip)
        b = a + int(wn.getnframes() / wn.getframerate() * sr)
        wn.close()
        head_ref = max(rms(s, a + i2, a + i2 + step) for i2 in range(0, span, step))
        tail_ref = max(rms(s, b - i2 - step, b - i2) for i2 in range(span, step, -step)) if b - span > 0 else 0.0
        head = db(rms(s, a, a + w)) - db(head_ref)
        tail = db(rms(s, b - w, b)) - db(tail_ref)
        out.append({"line": i, "start": ln["start"], "seam_head_db": round(head, 1),
                    "seam_tail_db": round(tail, 1), "worst_edge_db": round(max(head, tail), 1)})
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("path", help="wav file or directory of wavs")
    ap.add_argument("--narration", help="narration.json: also check the line seams of a mixdown")
    ap.add_argument("--max-edge", type=float, default=-8,
                    help="dB at the edge, relative to the level next to it; -8 = a ramp (default -8)")
    ap.add_argument("--report", help="where to write the json report")
    a = ap.parse_args()

    # a stage dir also holds the reverb IR and A/B files; only the voice clips are gated
    files = [a.path] if not os.path.isdir(a.path) else (
        sorted(glob.glob(os.path.join(a.path, "line_*.wav")))
        or sorted(glob.glob(os.path.join(a.path, "*.wav"))))
    if not files or not all(os.path.exists(f) for f in files):
        raise SystemExit(f"{a.path}: no wav files")

    report = [check(f) for f in files]
    seams = check_mix(a.path, a.narration) if a.narration and os.path.isfile(a.path) else []
    worst = max((r["worst_edge_db"] for r in report + seams), default=-120)
    n_jump = sum(len(r["max_jumps"]) for r in report)

    print(f"{'file':40} {'head':>7} {'tail':>7}    dB of the 2 ms edge below the 50 ms next to it")
    for r in report:
        flag = "  ← CLICK" if r["worst_edge_db"] > a.max_edge else ""
        print(f"{os.path.basename(r['file']):40} {r['head_db']:7.1f} {r['tail_db']:7.1f}{flag}")
    if seams:
        print(f"\n{'line':>4} {'start':>7} {'seam head':>9} {'seam tail':>9}   in the mixdown, ends from audio/fx")
        for s in seams:
            flag = "  ← CLICK" if s["worst_edge_db"] > a.max_edge else ""
            print(f"{s['line']:4} {s['start']:7.2f} {s['seam_head_db']:9.1f} {s['seam_tail_db']:9.1f}{flag}")
    if n_jump:
        print(f"\n{n_jump} sample-level step(s) inside the audio (plosives look the same, warning only):")
        for r in report:
            for j in r["max_jumps"][:5]:
                print(f"  {os.path.basename(r['file'])} {j['t']:.2f}s step {j['step']:.3f}")

    out = a.report or os.path.join(a.path if os.path.isdir(a.path) else os.path.dirname(a.path) or ".",
                                   "qc_report.json")
    with open(out, "w") as f:
        json.dump({"max_edge_db": a.max_edge, "worst_edge_db": worst, "files": report, "seams": seams}, f, indent=1)
    print(f"\nworst edge {worst:.1f} dB (limit {a.max_edge:g}) → {out}")
    if worst > a.max_edge:
        raise SystemExit(f"clicks: an edge sits {worst:.1f} dB below the level next to it, so it is a step, "
                         f"not a ramp (limit {a.max_edge:g}). voice_fx and mix.py fade both ends of every line "
                         "— check neither was bypassed, and that fit_narration trimmed the leading silence.")


if __name__ == "__main__":
    main()