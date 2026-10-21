#!/usr/bin/env python3
"""Стенд озвучки: послушать и сравнить, а не угадывать.

Три вещи, которые нельзя решить чтением кода:

  --voices   все восемь голосов на одной реплике, в один файл с паузами (AB-voices.wav)
             плюс замеры: длительность, тон (медиана и разброс), диапазон энергии.
             Размеренный голос читается как «живой», ровный — как «дикторский робот»,
             и это видно в числах раньше, чем на слух.
  --stress   аудит форм с `+`: синтезируем слово с формой и без, распознаём оба.
             RUAccent ставит ударение по контексту, а ручной знак отвечает за всё слово
             целиком: если форма ломает произношение, она откатывается. Измеренный пример:
             `за+мок` (знак перед согласной) вышел «заумок», `а+гент` — «айгент».
  --line     одна реплика в нескольких вариантах подачи (style/speed) — AB на слух.

  ND_API_KEY=sk-... python3 scripts/tts_probe.py projects/x/narration.json --voices
  python3 scripts/tts_probe.py narration.json --voices --out /tmp/ab
  python3 scripts/tts_probe.py narration.json --stress --voice aiden
"""

import argparse
import json
import math
import os
import re
import sys

sys.path.insert(0, os.path.dirname(__file__))
from ndv_common import (clip_name, duration, ffmpeg, load_narration, nd_speak,  # noqa: E402
                        nd_transcribe, wav_samples, write_wav)
from pronounce import MARKED, Pronouncer  # noqa: E402

LANGUAGES = {"ru": "Russian", "en": "English"}
VOICES = [("vivian", "ж"), ("serena", "ж"), ("ono_anna", "ж"), ("sohee", "ж"),
          ("dylan", "м"), ("ryan", "м"), ("aiden", "м"), ("uncle_fu", "м")]
GAP = 1.2               # пауза между вариантами в AB-файле, секунды


def f0_stats(samples, sr):
    """Медиана и разброс тона плюс размах энергии — грубо, но верно различает «живо» и «ровно»."""
    step, win = int(0.01 * sr), int(0.04 * sr)
    # полоса под обычный голос; шире — и автокорреляция цепляется за октаву
    lo, hi = int(sr / 320), int(sr / 80)
    f0, env = [], []
    for i in range(0, len(samples) - win, step):
        x = samples[i:i + win]
        env.append(math.sqrt(sum(v * v for v in x) / win) / 32768.0)
        if env[-1] < 0.02:
            continue
        span = min(hi, len(x) // 2) - lo
        if span <= 0:
            continue
        m = sum(x) / win
        y = [v - m for v in x]
        ac = [sum(y[k] * y[k + lag] for k in range(len(y) - lag)) for lag in range(lo, lo + span)]
        best = max(range(len(ac)), key=ac.__getitem__)   # индекс в ac, лаг = lo + best
        if ac[best] > 0.45 * ac[0]:        # слабые пики оставляем: это шум, а не голос
            f0.append(sr / (lo + best))
    if len(f0) < 4:
        return {"seconds": round(len(samples) / sr, 2)}
    loud = sorted(e for e in env if e > 0.004)
    p10, p90 = (loud[int(len(loud) * 0.1)], loud[int(len(loud) * 0.9)]) if loud else (0, 0)
    db = lambda v: round(20 * math.log10(max(v, 1e-9)), 1)
    sf = sorted(f0)                       # размах по краям зашумлён одиночными кадрами
    return {"seconds": round(len(samples) / sr, 2),
            "f0_median": round(sf[len(sf) // 2], 1),
            "f0_spread": round(sf[int(len(sf) * 0.9)] - sf[int(len(sf) * 0.1)], 1),
            "loudness_range_db": round(db(p90) - db(p10), 1) if p90 else 0}


def ab_file(parts, out, label):
    """Склеить варианты с паузами в один файл для прослушивания."""
    args = sum((["-i", p] for p in parts), [])
    graph = "".join(f"[{k}:a]apad=pad_dur={GAP},atrim=0:{duration(p) + GAP}[a{k}];" for k, p in enumerate(parts))
    graph += "".join(f"[a{k}]" for k in range(len(parts))) + f"concat=n={len(parts)}:v=0:a=1[o]"
    ffmpeg(*args, "-filter_complex", graph, "-map", "[o]", "-ac", "2", "-ar", "48000", out)
    print(f"A/B: {out} — порядок: {label}")


def probe_voices(a, n, pron):
    """Все восемь голосов на первой содержательной реплике."""
    text = pron.scene([n["lines"][0]]) or pron.text(n["lines"][0]["text"])
    parts, rows = [], []
    for voice, gender in VOICES:
        try:
            data, engine = nd_speak(text, voice=voice, language=LANGUAGES.get(n.get("language"), "Russian"),
                                    style=(n.get("voice") or {}).get("style"), tries=1)
        except Exception as exc:                      # хаб может отклонить конкретный голос
            print(f"  {voice}: недоступен ({exc})")
            continue
        path = os.path.join(a.out, f"voice_{voice}.wav")
        with open(path, "wb") as f:
            f.write(data)
        parts.append(path)
        st = f0_stats(wav_samples(data)[0], wav_samples(data)[1])
        rows.append({"voice": voice, "gender": gender, **st})
        print(f"  {voice:10} {gender}  {st.get('seconds', 0):5.2f}s  "
              f"тон {st.get('f0_median', 0):6} ± {st.get('f0_spread', 0):5}  "
              f"громкость {st.get('loudness_range_db', 0):4} дБ")
    if parts:
        ab_file(parts, os.path.join(a.out, "AB-voices.wav"),
                ", ".join(r["voice"] for r in rows))
    with open(os.path.join(a.out, "voices_report.json"), "w") as f:
        json.dump({"text": text, "voices": rows}, f, ensure_ascii=False, indent=1)


def probe_stress(a, n, pron, voice):
    """Формы с `+`: выжила ли форма при синтезе."""
    language = LANGUAGES.get(n.get("language"), "Russian")
    forms = {}
    for ln in n["lines"]:
        for w in MARKED.findall(pron.line(ln)):
            if "+" in w:
                forms.setdefault(w, w.replace("+", ""))
    if not forms:
        print("форм с «+» нет: в этом тексте нет омографов")
        return
    rows = []
    for form, plain in sorted(forms.items()):
        heard = []
        for text in (plain, form):
            data, _ = nd_speak(f"Слово: {text}.", voice=voice, language=language, tries=1)
            heard.append(nd_transcribe(data, "whisper-1", "probe.wav")["text"])
        ok = _same(heard[0], heard[1])
        rows.append({"form": form, "plain": plain, "heard_plain": heard[0], "heard_marked": heard[1],
                     "ok": ok})
        print(f"  {plain:14} → {form:16} {'OK ' if ok else 'ОТКАТ':6} "
              f"без: {heard[0][:34]!r} | с: {heard[1][:34]!r}")
    bad = [r for r in rows if not r["ok"]]
    with open(os.path.join(a.out, "stress_report.json"), "w") as f:
        json.dump({"voice": voice, "forms": rows}, f, ensure_ascii=False, indent=1)
    print(f"→ {os.path.join(a.out, 'stress_report.json')}: {len(rows) - len(bad)} из {len(rows)} форм выжили")
    if bad:
        print("  формы, которые ломают слово, в voice.stress не добавляем: " +
              ", ".join(r["form"] for r in bad))


def _same(a, b):
    """Распознавания считаются одинаковыми, если отличаются только пунктуацией и ё."""
    norm = lambda s: re.sub(r"[^\w]+", " ", s.lower().replace("ё", "е")).strip()
    return norm(a) == norm(b)


def probe_line(a, n, pron, voice):
    """Одна реплика: как она звучит целиком, для сравнения с прошлой сборкой."""
    for i in (a.lines or [0]):
        text = pron.line(n["lines"][i])
        data, engine = nd_speak(text, voice=voice, language=LANGUAGES.get(n.get("language"), "Russian"),
                                style=(n.get("voice") or {}).get("style"), tries=1)
        path = os.path.join(a.out, clip_name(i))
        with open(path, "wb") as f:
            f.write(data)
        st = f0_stats(wav_samples(data)[0], wav_samples(data)[1])
        print(f"  [{i:2}] {engine} {text[:50]}")
        print(f"       {st}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("narration")
    ap.add_argument("--voices", action="store_true", help="A/B всех голосов")
    ap.add_argument("--stress", action="store_true", help="аудит форм с «+»")
    ap.add_argument("--lines", type=int, nargs="*", help="озвучить отдельные реплики")
    ap.add_argument("--voice", default=None)
    ap.add_argument("--out", default=".")
    ap.add_argument("--model-size", default="turbo2")
    a = ap.parse_args()

    n = load_narration(a.narration)
    os.makedirs(a.out, exist_ok=True)
    pron = Pronouncer(n, stress=a.stress, model_size=a.model_size)
    for w in pron.warnings:
        print(f"⚠️  {w}")
    voice = a.voice or (n.get("voice") or {}).get("voice", "ryan")
    if a.voices:
        probe_voices(a, n, pron)
    if a.stress:
        probe_stress(a, n, pron, voice)
    if a.lines:
        probe_line(a, n, pron, voice)
    if not (a.voices or a.stress or a.lines):
        ap.error("что делать: --voices, --stress или --lines N")


if __name__ == "__main__":
    main()