#!/usr/bin/env python3
"""Fit narration to the timeline: every line must fit its window.

Trims edge silence. Tempo is applied to the whole SCENE, not to each line separately:
реплики вырезаны из одной записи, и если ужать только одну, соседние фразы внутри сцены
поедут с разной скоростью — сцена развалится на слух. Сцена целиком уезжает как один кусок,
и разница темпа внутри неё не слышна.

Going above --max-tempo is audible. Such scenes are flagged TOO LONG: shorten the text in
narration.json and re-voice.

start_silence is 0, not 0.05/0.1: it is the silence silenceremove KEEPS, so a positive
value left ~20 ms of digital silence at the head of every clip, and a fade applied to
silence fades nothing.

  python3 scripts/fit_narration.py projects/x/narration.json projects/x/audio/raw projects/x/audio/fit
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from ndv_common import clip_name, duration, ffmpeg, load_narration  # noqa: E402

TRIM = ("silenceremove=start_periods=1:start_threshold=-45dB:start_silence=0,areverse,"
        "silenceremove=start_periods=1:start_threshold=-45dB:start_silence=0,areverse")


def scenes_of(n):
    """Реплики по сценам, в порядке следования."""
    groups, seen = [], {}
    for i, ln in enumerate(n["lines"]):
        key = ln.get("scene")
        if key is None or key not in seen:
            seen[key] = len(groups)
            groups.append([])
        groups[seen[key]].append(i)
    return groups


def scene_tempo(n, groups, lengths, min_tempo, max_tempo):
    """Темп для каждой сцены: максимум по её репликам, общий на всю сцену.

    Реплики вырезаны из одной записи, поэтому ужимать их по-разному нельзя — внутри сцены
    поедут соседние фразы. Берём самый необходимый темп сцены и применяем ко всем её
    репликам: после этого каждая реплика гарантированно влезает в своё окно, а те, кому
    темп не нужен, просто освобождают часть паузы.
    """
    out = []
    for idx in groups:
        need = [lengths[i] / max(0.1, n["lines"][i]["window"]) for i in idx]
        out.append(min(max_tempo, max(min_tempo, max(need))))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("narration")
    ap.add_argument("raw_dir")
    ap.add_argument("out_dir")
    ap.add_argument("--max-tempo", type=float, default=1.12)
    ap.add_argument("--min-tempo", type=float, default=0.95,
                    help="ниже этого замедлять нельзя: сцена начинает звучать неестественно")
    ap.add_argument("--line-tempo", action="store_true",
                    help="старое поведение: ужимать каждую реплику отдельно")
    a = ap.parse_args()

    n = load_narration(a.narration)
    os.makedirs(a.out_dir, exist_ok=True)
    groups = scenes_of(n)
    lengths, raw_dur = {}, {}
    for i in range(len(n["lines"])):
        raw_dur[i] = duration(os.path.join(a.raw_dir, clip_name(i)))
        tmp = os.path.join(a.out_dir, f"trim_{i:02d}.wav")
        ffmpeg("-i", os.path.join(a.raw_dir, clip_name(i)), "-af", TRIM, "-ar", "48000", tmp)
        lengths[i] = duration(tmp)          # длина речи без тишины — по ней считаем темп
        os.remove(tmp)

    groups = scenes_of(n)
    # темп считаем по фактической длине речи, а не по «окну строки»: реплика, которая
    # длиннее промежутка до следующей, наезжает на неё, и стык превращается в наложение
    # двух фраз. Раньше темп ограничивался ускорением (>= 1.0) и наложение проходило молча.
    tempo = scene_tempo(n, groups, lengths, a.min_tempo, a.max_tempo) if not a.line_tempo else [
        min(max(a.min_tempo, a.max_tempo), lengths[i] / max(0.1, n["lines"][i]["window"]))
        for i in range(len(n["lines"]))]
    per_scene = {}
    if not a.line_tempo:
        for si, idx in enumerate(groups):
            for i in idx:
                per_scene[i] = tempo[si]

    report, bad = [], 0
    for i, ln in enumerate(n["lines"]):
        src, dst = os.path.join(a.raw_dir, clip_name(i)), os.path.join(a.out_dir, clip_name(i))
        t = per_scene.get(i, 1.0) if not a.line_tempo else tempo[i]
        # обрезка тишины и темп в одном проходе: тишину режем обязательно, без неё
        # начало реплики начинается на полном уровне и щёлкает
        ffmpeg("-i", src, "-af", f"{TRIM},atempo={t:.4f}", "-ar", "48000", dst)
        if t > a.max_tempo:
            status = "TOO LONG"
        elif t < a.min_tempo:
            status = "TOO SHORT"
        else:
            status = "ok"
        bad += status != "ok"
        report.append({"line": i, "scene": ln.get("scene"), "start": ln["start"],
                       "window": ln["window"], "raw": round(raw_dur[i], 2),
                       "speech": round(lengths[i], 2), "tempo": round(t, 3),
                       "final": round(duration(dst), 2), "status": status})
        print(f"[{i:2}] scene {ln.get('scene')}  raw {raw_dur[i]:5.2f}s  речь {lengths[i]:5.2f}s  "
              f"tempo ×{t:.3f}  {status}")
    with open(os.path.join(a.out_dir, "fit_report.json"), "w") as f:
        json.dump(report, f, indent=1)
    if bad:
        scenes = sorted({r["scene"] for r in report if r["status"] != "ok"})
        raise SystemExit(
            f"{bad} реплик не влезают в окно даже при темпе ×{a.min_tempo}–×{a.max_tempo} "
            f"(сцены {scenes}). TOO LONG — сократите текст; TOO SHORT — реплика короче паузы, "
            "проверьте тайминг: запустите стадию голоса заново, она пересчитает старты "
            "по фактической длине озвучки.")


if __name__ == "__main__":
    main()