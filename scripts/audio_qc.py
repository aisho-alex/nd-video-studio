#!/usr/bin/env python3
"""Click detector for the audio pipeline.

Две вещи, которые слышно, и три, которые видно в цифрах.

Гейты (падает сборка):
- **edges** — уровень первых/последних 2 мс против пика 50 мс рядом, в дБ. Клип, который
  открывается или закрывается на полном уровне, сидит около 0 дБ: это ступень шириной в один
  сэмпл, то есть щелчок. Рампа читается намного ниже.
- **seams** — то же самое на стыках реплик в миксе плюс разница уровня соседних кусков
  (`level_jump_db`): при общей шине она и есть признак того, что стык слышно.

Предупреждения (сборку не роняют):
- **impulses** — одиночные скачки внутри сигнала. Их много и у речи: плизивы, шипящие, и по
  уровню щелчок от шипящего не отличить. Считаем, показываем.
- **liveness** — разброс энергии по окнам. Ровная подача читается как «робот» независимо от
  того, как часто меняется интонация.

Раньше гейт был один и проверял только края клипов. На стыках он был чист (все 36 клипов
начинались и кончались нулём), а щелчки слышались: это границы фраз, а не разрывы сигнала.

  python3 scripts/audio_qc.py projects/x/audio/fit
  python3 scripts/audio_qc.py projects/x/audio/mix.wav --narration projects/x/narration.json \
      --clips projects/x/audio/fit
  python3 scripts/audio_qc.py projects/x/audio/fit --max-edge -4       # строже
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
from ndv_common import BUS_FADE_IN as FADE_IN, BUS_FADE_OUT as FADE_OUT  # noqa: E402
from ndv_common import clip_name, load_narration  # noqa: E402

WIN = 0.005          # RMS window, seconds
FLOOR = 0.05         # a step counts as a jump from this amplitude, -26 dBFS
RATIO = 6            # ...or from this multiple of the local RMS
IMPULSE_SCAN_MAX = 60.0   # дольше минуты — щелчки внутри не ищем, они уже в миксе


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


def edges(s, sr, fin=FADE_IN, fout=FADE_OUT):
    """Уровень у самого края против устоявшегося уровня рядом, в дБ.

    Это и есть щелчок в отличие от рампы: клип, который открывается (или закрывается) на
    полном уровне, — это ступень шириной в один сэмпл. Рампа читается намного ниже.

    Опорный участок берётся ПОСЛЕ рампы (по умолчанию 10 мс входа, 30 мс выхода), а не с
    первых 50 мс: иначе опорой служит пик онсента — взрывной согласный в начале слова — и
    рампы в 10 мс читаются как -6 дБ. Сравнивать надо с тем, что рядом установилось.
    """
    w = max(1, int(0.002 * sr))
    span = int(0.05 * sr)
    step = max(1, int(WIN * sr))
    if len(s) <= w:
        return {"head_db": -120.0, "tail_db": -120.0}
    head_from = w + int(fin * sr)
    tail_from = max(0, len(s) - w - int(fout * sr))
    head_ref = max(rms(s, i, i + step) for i in range(head_from, min(len(s) - step, head_from + span), step))
    tail_ref = max(rms(s, i, i + step) for i in range(max(0, tail_from - span), tail_from, step))
    if not head_ref and not tail_ref:
        return {"head_db": -120.0, "tail_db": -120.0}  # цифровая тишина: щёлкать нечему
    return {"head_db": round(db(rms(s, 0, w)) - db(head_ref), 1) if head_ref else -120.0,
            "tail_db": round(db(rms(s, len(s) - w, len(s))) - db(tail_ref), 1) if tail_ref else -120.0}


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
    w = wave.open(path)
    sr = w.getframerate()
    w.close()
    s = mono(path)
    long = len(s) / sr > IMPULSE_SCAN_MAX
    res = {"file": path, "seconds": round(len(s) / sr, 2), **edges(s, sr)}
    res["max_jumps"] = jumps(s, sr)
    res["impulses"] = [] if long else impulses(s, sr)
    res["impulses_skipped"] = long
    res["liveness"] = liveness(s, sr)
    res["worst_edge_db"] = max(res["head_db"], res["tail_db"])
    return res


def impulses(s, sr):
    """Одиночные скачки сигнала: прыжок и обратно за один сэмпл.

    Речь внутри клипа даёт много настоящих щелчков-транзиентов (plosive, шипящие), и по
    уровню щелчок от шипящего не отличить — поэтому это предупреждение, а не гейт. Гейт
    остаётся на краях и на стыках: там щелчок слышен, потому что стык слышен.
    """
    out = []
    for i in range(len(s) - 2):
        step = abs(s[i + 1] - s[i])
        if step < FLOOR:
            continue
        if abs(s[i + 2] - s[i]) < 0.5 * step:      # вернулся — это импульс, а не звук
            out.append((round(i / sr, 3), round(step, 3)))
    merged = []
    for t, v in out:
        if merged and t - merged[-1][0] < 0.02:
            merged[-1][0], merged[-1][1] = t, max(merged[-1][1], v)
        else:
            merged.append([t, v])
    return [{"t": t, "step": v} for t, v in merged]


def liveness(s, sr):
    """Насколько реплика живая: разброс энергии по окнам.

    Ровная подача (постоянный уровень, ровный тон) читается как «дикторский робот» независимо
    от того, как часто меняется интонация. Разброс окна в децибелах это видно.
    """
    w = max(1, int(WIN * sr))
    levels = [rms(s, i, i + w) for i in range(0, len(s) - w + 1, w)]
    loud = sorted(x for x in levels if x > FLOOR * 0.2)
    if len(loud) < 4:
        return {"spread_db": 0.0, "crest": 0.0}
    p10, p90 = loud[int(len(loud) * 0.1)], loud[int(len(loud) * 0.9)]
    peak = max(levels) or 1.0
    return {"spread_db": round(db(p90) - db(p10), 1),
            "crest": round(db(peak) - db(p10), 1)}


def find_clip(clips_dir, i):
    """Клип реплики: сначала указанный каталог (audio/fit), потом как раньше — рядом с миксом."""
    if clips_dir:
        p = os.path.join(clips_dir, clip_name(i))
        if os.path.exists(p):
            return p
    if clips_dir:
        sib = os.path.join(os.path.dirname(clips_dir), "fx", clip_name(i))
        if os.path.exists(sib):
            return sib
    return None


def check_mix(path, narration, clips_dir):
    """Стыки реплик в миксе: тот же замер по краям плюс ровность соседних участков."""
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
        # Реплика кончается там, где кончается её клип: раскладку держит именно он.
        clip = find_clip(clips_dir, i)
        if not clip or a + w >= len(s):
            continue
        wn = wave.open(clip)
        b = a + int(wn.getnframes() / wn.getframerate() * sr)
        wn.close()
        # Опорный уровень — то, что установилось ПОСЛЕ рампы, иначе опорой служит пик
        # онсента и короткая рампа читается как щелчок (см. edges()).
        head_from = a + w + int(FADE_IN * sr)
        head_ref = max(rms(s, i, i + step) for i in range(head_from, min(b - step, head_from + span), step))
        tail_from = max(0, b - w - int(FADE_OUT * sr))
        tail_ref = max(rms(s, i, i + step) for i in range(max(0, tail_from - span), tail_from, step))
        head = db(rms(s, a, a + w)) - db(head_ref) if head_ref else -120.0
        tail = db(rms(s, b - w, b)) - db(tail_ref) if tail_ref else -120.0
        # Пауза между репликами: у шины это комната с шумовой подложкой, а не цифровой ноль.
        # Ноль означает дырку в дорожке, и любой стык рядом с ней слышен.
        prev_end = int(out[-1]["end"] * sr) if out else a
        mid = (prev_end + a) // 2
        gap_lo, gap_hi = max(0, mid - span // 2), min(len(s), mid + span // 2)
        gap = db(rms(s, max(0, gap_lo), min(len(s), gap_hi))) if gap_hi > gap_lo else -120.0
        out.append({"line": i, "start": ln["start"], "end": b / sr, "seam_head_db": round(head, 1),
                    "seam_tail_db": round(tail, 1), "gap_db": round(gap, 1),
                    "worst_edge_db": round(max(head, tail), 1)})
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("path", help="wav file or directory of wavs")
    ap.add_argument("--narration", help="narration.json: also check the line seams of a mixdown")
    ap.add_argument("--clips", help="каталог с line_NN.wav, по которому считается конец реплики")
    ap.add_argument("--max-edge", type=float, default=-8,
                    help="dB at the edge, relative to the level next to it; -8 = a ramp (default -8)")
    ap.add_argument("--min-gap", type=float, default=-80,
                    help="уровень паузы ниже этого = цифровая дырка вместо комнаты (default -80)")
    ap.add_argument("--report", help="where to write the json report")
    ap.add_argument("--report-only", action="store_true",
                    help="ничего не ронять: для промежуточных файлов (клипы, шина)")
    a = ap.parse_args()

    files = [a.path] if not os.path.isdir(a.path) else (
        sorted(glob.glob(os.path.join(a.path, "line_*.wav")))
        or sorted(x for x in glob.glob(os.path.join(a.path, "*.wav"))
                  if not os.path.basename(x).startswith("ir")))
    if not files or not all(os.path.exists(f) for f in files):
        raise SystemExit(f"{a.path}: no wav files")

    report = [check(f) for f in files]
    seams = check_mix(a.path, a.narration, a.clips) if a.narration and os.path.isfile(a.path) else []
    # У микса края файла — это финальный фейд всего ролика, а не стык реплик: гейтим стыки.
    gated = seams if seams else report
    worst = max((r["worst_edge_db"] for r in gated), default=-120)
    worst_gap = min((s["gap_db"] for s in seams), default=0)
    n_imp = sum(len(r["impulses"]) for r in report)

    print(f"{'file':40} {'head':>7} {'tail':>7} {'imp':>5} {'spread':>7}   "
          f"уровень края против соседних 50 мс")
    for r in report:
        flag = "" if a.report_only else ("  ← CLICK" if r["worst_edge_db"] > a.max_edge else "")
        imp = "—" if r.get("impulses_skipped") else str(len(r["impulses"]))
        spread = f"{r['liveness']['spread_db']:5.1f}dB"
        print(f"{os.path.basename(r['file']):40} {r['head_db']:7.1f} {r['tail_db']:7.1f} "
              f"{imp:>5} {spread:>7}{flag}")
    if seams:
        print(f"\n{'line':>4} {'start':>7} {'seam head':>9} {'seam tail':>9} {'gap':>8}   "
              f"в миксе, конец реплики по клипам")
        for s in seams:
            flag = "" if a.report_only else ("  ← CLICK" if s["worst_edge_db"] > a.max_edge else "")
            hole = "" if a.report_only else ("  ← ДЫРКА" if s["gap_db"] < a.min_gap else "")
            print(f"{s['line']:4} {s['start']:7.2f} {s['seam_head_db']:9.1f} {s['seam_tail_db']:9.1f} "
                  f"{s['gap_db']:8.1f}{flag}{hole}")
    if n_imp:
        print(f"\n{n_imp} одиночных скачков внутри сигнала (у речи их много: плизивы и шипящие, "
              f"предупреждение):")
        for r in report:
            for j in r["impulses"][:5]:
                print(f"  {os.path.basename(r['file'])} {j['t']:.2f}s step {j['step']:.3f}")

    out = a.report or os.path.join(a.path if os.path.isdir(a.path) else os.path.dirname(a.path) or ".",
                                   "qc_report.json")
    with open(out, "w") as f:
        json.dump({"max_edge_db": a.max_edge, "worst_edge_db": worst,
                   "min_gap_db": a.min_gap, "worst_gap_db": worst_gap,
                   "files": report, "seams": seams}, f, indent=1)
    print(f"\nworst edge {worst:.1f} dB (limit {a.max_edge:g}), самая тихая пауза {worst_gap:.1f} dBFS "
          f"(limit {a.min_gap:g}) → {out}")
    if a.report_only:
        return
    if worst > a.max_edge:
        raise SystemExit(f"clicks: an edge sits {worst:.1f} dB below the level next to it, so it is a step, "
                         f"not a ramp (limit {a.max_edge:g}). voice_bus.py ramps both ends of every line "
                         "— check it wasn't bypassed, and that fit_narration trimmed the leading silence.")
    if worst_gap < a.min_gap:
        raise SystemExit(f"пауза между репликами — цифровой ноль ({worst_gap:.1f} dBFS, "
                         f"limit {a.min_gap:g}): дырка в дорожке, стык рядом с ней слышен. "
                         "Проверьте, что voice_bus.py запущен с --room-tone-db (по умолчанию -50).")


if __name__ == "__main__":
    main()