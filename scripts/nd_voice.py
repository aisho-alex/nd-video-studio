#!/usr/bin/env python3
"""Одна озвучка на сцену, разбивка на реплики — по словам и паузам.

Почему так, а не по реплике: 36 отдельных вызовов движка дают 36 отдельных высказываний.
У каждого своя интонация, своя комната и своя громкость, и на стыке это слышно как щелчок,
хотя на уровне сэмплов там ничего нет. Сцена, сказанная целиком, — один кусок живой речи,
а паузы между предложениями ставит сам диктор.

Движок недетерминирован: три одинаковых вызова дают три разных наложения. Поэтому режем не
по первым сэмплам, а по двум независимым признакам сразу:

- whisper-1 отдаёт тайминги слов; `difflib` раскладывает их по репликам и говорит, где
  кончается последнее слово реплики;
- двигатель ставит между предложениями паузы по 0.75–0.93 с, а внутри реплики — 0.10–0.56 с,
  то есть граница реплики почти всегда совпадает с самой длинной паузой рядом.

Граница берётся по середине ближайшей паузы, и реплика вырезается в тишине: стык в тишине
не щёлкает по построению. Если паузы рядом нет — сцена переозвучивается один раз (движок
случайный, второй дубль обычно ставит паузу), и если не вышло, стадия падает с кодом 3.

  ND_API_KEY=sk-... python3 scripts/nd_voice.py projects/x/narration.json projects/x/audio/raw
  python3 scripts/nd_voice.py narration.json audio/raw --only 12 --voice uncle_fu
  python3 scripts/nd_voice.py narration.json audio/raw --scenes 0 3
"""

import argparse
import difflib
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(__file__))
from ndv_common import (clip_name, envelope, load_narration, nd_speak, nd_transcribe,  # noqa: E402
                        retry, scene_groups, scene_name, silence_spans, trim_silence,
                        wav_samples, write_wav)
from pronounce import ACCENTS, Pronouncer  # noqa: E402
from difflib import SequenceMatcher  # noqa: E402

LANGUAGES = {"ru": "Russian", "en": "English"}
WORD = re.compile(r"\w+", re.UNICODE)
PAUSE_MIN = 0.10        # короче — не пауза, а стык слогов внутри фразы
SNAP_WINDOW = 0.5       # на каком расстоянии от конца слов ещё ищем паузу, секунды


def tokens(s):
    return WORD.findall(s.lower().replace("ё", "е"))


MATCH = 0.5          # насколько распознанное слово должно совпадать с ожидаемым
LOOKAHEAD = 6        # на сколько слов вперёд ищем при совпадении


def assign_lines(words, lines_spoken):
    """Индекс последнего слова движка, попавшего в каждую реплику.

    Сопоставление по похожеству, а не по точному равенству: whisper отдаёт «LLM-агент»
    одним словом, а произносится «эл-эл-эм-агент» — четыре. При точном равенстве всё
    после этой точки съезжало на реплику, и стык попадал внутрь фразы.

    Основу слова берём целиком, с дефисами: если whisper вернул «эл-эл-эм-агент» одним
    токеном, обрезание по первому куску («эл») увело бы выравнивание на следующую реплику.
    """
    stems = [re.sub(r"[^\w-]", "", w.lower()) for w, _, _ in words]
    last, wi = {}, 0
    for i, spoken in enumerate(lines_spoken):
        for t in tokens(spoken):
            best, score = None, 0.0
            for k in range(wi, min(wi + LOOKAHEAD, len(stems))):
                s = SequenceMatcher(None, t, stems[k]).ratio() if stems[k] else 0.0
                if s > score:
                    best, score = k, s
            if best is not None and score >= MATCH:
                wi = best + 1
                if i not in last or best > last[i]:
                    last[i] = best
    return last


def snap(t_end, spans):
    """Граница реплики — середина ближайшей паузы. (граница, нашлась ли) — None, если нет."""
    near = [s for s in spans if s[1] >= t_end - SNAP_WINDOW and s[0] <= t_end + SNAP_WINDOW]
    if not near:
        return None
    best = min(near, key=lambda s: abs((s[0] + s[1]) / 2 - t_end))
    return (best[0] + best[1]) / 2


def cut_scene(samples, sr, lines_spoken, words):
    """Разрезать озвучку сцены на реплики. Возвращает (резы, что услышали в каждой, без паузы).

    `last_word[i]` — индекс последнего слова реплики, то есть её КОНЕЦ. Начало реплики
    получается из конца предыдущей, поэтому услышанное собирается по границам, а не
    начиная с last_word.
    """
    spans = silence_spans(envelope(samples, sr), sr, minlen=PAUSE_MIN)
    last_word = assign_lines(words, lines_spoken)

    start = 0
    heard, cuts, no_pause = [], [0.0], []
    for i in range(len(lines_spoken)):
        end = last_word.get(i)
        heard.append(" ".join(w for w, _, _ in words[start:(end + 1) if end is not None else None]))
        start = (end + 1) if end is not None else start
        if i == len(lines_spoken) - 1:
            break
        t_end = end and words[end][2]
        at = snap(t_end or cuts[-1], spans)
        if at is None or at <= cuts[-1]:
            at = max(t_end or 0.0, cuts[-1] + 0.05)
            no_pause.append(i)
        cuts.append(round(min(at, len(samples) / sr), 3))
    cuts.append(round(len(samples) / sr, 3))
    return cuts, heard, no_pause


def voice_scene(text, voice, language, style, tries, seed, speed=1.0):
    """Озвучить сцену целиком → (байты, движок)."""
    return retry(lambda: nd_speak(text, voice=voice, language=language, style=style,
                                  tries=1, seed=seed, speed=speed), tries)


def keep_previous(out_dir, si):
    """Отложить текущий набор сцены, прежде чем озвучить её заново.

    Движок случаен: тот же текст читается каждый раз чуть иначе. Если новый вариант не
    понравился, вернуть можно только прошлый — и вернуть нужно именно его, без повторного
    синтеза, иначе получится третье чтение, а не то, которое человек уже слышал. Хранится
    ровно один предыдущий вариант на сцену, история не растёт.
    """
    cur = os.path.join(out_dir, scene_name(si))
    if not os.path.exists(cur):
        return False
    with open(cur, "rb") as src, open(os.path.join(out_dir, scene_name(si, prev=True)), "wb") as dst:
        dst.write(src.read())
    return True


def process_scene(data, spoken):
    """Нарезка озвучки сцены на реплики. Работает и для свежего синтеза, и для отложенного.

    (сэмплы, sr, резы, что услышали в каждой реплике, реплики без паузы)
    """
    samples, sr = wav_samples(data)
    cuts, heard, no_pause = cut_scene(samples, sr, spoken, nd_transcribe(data, "whisper-1")["words"])
    return samples, sr, cuts, heard, no_pause


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("narration")
    ap.add_argument("out_dir")
    ap.add_argument("--voice", default=None)
    ap.add_argument("--language", help="Russian, English; по умолчанию из narration.json")
    ap.add_argument("--style", help="переопределить voice.style")
    ap.add_argument("--styles", help="json: смена бита → стиль (beat: \"...\")")
    ap.add_argument("--model", help="espeech-tts или qwen3-tts; по умолчанию по языку")
    ap.add_argument("--model-size", default="turbo2", help="модель омографов ruaccent")
    ap.add_argument("--accent", default="dictionary", choices=ACCENTS,
                    help="чьи ударения уходят в движок: из словаря ruaccent, все или "
                         "только омографы (прежнее поведение, оставлено как откат)")
    ap.add_argument("--only", type=int, nargs="*", help="переозвучить реплики (переозвучиваются их сцены)")
    ap.add_argument("--scenes", type=int, nargs="*", help="переозвучить эти сцены")
    ap.add_argument("--restore-scenes", type=int, nargs="*",
                    help="вернуть прошлый набор этих сцен (scene_NN.prev.wav) и перерезать его")
    ap.add_argument("--tries", type=int, default=5)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--speed", type=float, default=1.0,
                    help="темп речи: 1.0 — как есть, больше — быстрее (укладывает ролик в хронометраж)")
    a = ap.parse_args()

    n = load_narration(a.narration)
    v = n.get("voice", {})
    voice = a.voice or v.get("voice", "ryan")
    language = a.language or LANGUAGES.get(n.get("language", "en"), "Auto")
    style = a.style or v.get("style")
    styles = json.loads(a.styles) if a.styles else (v.get("styles") or {})
    pron = Pronouncer(n, stress=True, model_size=a.model_size, accent=a.accent)
    for w in pron.warnings:
        print(f"⚠️  {w}")

    groups = scene_groups(n)
    os.makedirs(a.out_dir, exist_ok=True)
    print(f"{a.model or ('espeech-tts' if language == 'Russian' else 'qwen3-tts')} · {voice} · {language} · "
          f"темп {a.speed:g} · {len(groups)} сцен(ы) по одному вызову")

    restore = set(a.restore_scenes or [])
    # Что озвучиваем. Важно: восстановление тоже задаёт выборку. Без этого «вернуть сцену 2»
    # прогоняло синтез по всем остальным сценам — дорого и неожиданно.
    selected = set(restore)
    if a.scenes:
        selected |= set(a.scenes)
    elif a.only:
        selected |= {si for si, idx in enumerate(groups) if any(i in a.only for i in idx)}
    if not selected:
        selected = set(range(len(groups)))

    report, failed = [], []
    # Отчёт прошлого прогона: при переозвучке одной сцены записи по остальным сценам
    # надо сохранить. Иначе «переозвучить сцену» стирал бы длительности и нарезку
    # ролика в интерфейсе, а сцены эти никто не переозвучивал.
    report_path = os.path.join(os.path.dirname(a.out_dir) or ".", "speech_report.json")
    previous = {}
    if os.path.exists(report_path):
        try:
            with open(report_path) as f:
                previous = {e.get("scene"): e for e in json.load(f)}
        except (ValueError, OSError):
            previous = {}
    for si, idx in enumerate(groups):
        # Текст сцены и список спорных мест считаем всегда, даже если сцену не
        # озвучиваем: интерфейс показывает, где стоит омограф и какое прочтение
        # выбрано, и это не зависит от того, синтезировали мы её сейчас или нет.
        text, occ = pron.scene_spoken([n["lines"][i] for i in idx], idx, si)
        spoken = [pron.line(n["lines"][i], stress=False) for i in idx]
        if si not in selected:
            kept = dict(previous.get(si) or {})
            # «skipped» значит «в этом прогоне не озвучивалось». У сохранённой записи
            # своя озвучка на месте, и интерфейс должен её показывать.
            if not kept:
                kept["skipped"] = True
            kept.update({"scene": si, "lines": idx, "stress": occ})
            report.append(kept)
            continue
        beats = {n["lines"][i].get("scene") for i in idx}
        beat = n["scenes"][min(beats)]["beat"] if beats and n.get("scenes") and min(beats) is not None else None

        # Восстановление: синтеза нет вообще, набор возвращается как есть и просто
        # перерезается. Синтезировать заново здесь нельзя — это было бы третье чтение,
        # а человек возвращает то, что уже слышал.
        kept = None
        if si in restore:
            prev = os.path.join(a.out_dir, scene_name(si, prev=True))
            if not os.path.exists(prev):
                raise SystemExit(f"сцена {si}: нет предыдущего набора ({os.path.basename(prev)})")
            with open(prev, "rb") as f:
                kept = f.read()
            engine = "prev"

        result = None
        # Три попытки: движок случаен, и сцена без паузы на стыке реплик — не
        # поломка, а неудачный дубль. Раньше двух не хватало, и одна сцена роняла
        # всю стадию.
        attempts = 1 if kept else 3
        for attempt in range(1, attempts + 1):
            if kept:
                data = kept
            else:
                data, engine = voice_scene(text, voice, language, styles.get(beat, style),
                                           a.tries, a.seed, a.speed)
                if attempt == 1:
                    keep_previous(a.out_dir, si)
            with open(os.path.join(a.out_dir, scene_name(si)), "wb") as f:
                f.write(data)
            samples, sr, cuts, heard, no_pause = process_scene(data, spoken)
            if not no_pause:
                result = (samples, sr, engine, cuts, heard)
                break
            print(f"  сцена {si}: нет паузы на стыке {no_pause}, переозвучка ({attempt}/{attempts})")
        if result is None:
            failed.append(si)
            report.append({"scene": si, "lines": idx, "error": "нет паузы на стыке реплик",
                           "stress": occ})
            continue

        samples, sr, engine, cuts, heard = result
        if si in restore:
            # Восстановленный набор озвучен прежним текстом, а не тем, что сейчас в
            # нарративе. Отчёт должен описывать звук на диске — иначе «прочтение
            # размечено, но ещё не озвучено» погаснет, хотя в звуке его нет.
            prev_entry = previous.get(si) or {}
            text = prev_entry.get("speech", text)
            occ = prev_entry.get("stress", occ)
        entry = {"scene": si, "lines": idx, "engine": engine, "beat": beat,
                 "duration": round(len(samples) / sr, 2), "speech": text,
                 "restored": si in restore,
                 # есть ли что возвращать: интерфейс показывает кнопку отката
                 "prev": os.path.exists(os.path.join(a.out_dir, scene_name(si, prev=True))),
                 "stress": occ,
                 "clips": []}
        for k, li in enumerate(idx):
            a0, a1 = int(cuts[k] * sr), int(cuts[k + 1] * sr)
            clip = trim_silence(samples[a0:a1], sr)
            path = os.path.join(a.out_dir, clip_name(li))
            write_wav(path, clip, sr)
            # сверяем с экранным текстом, а не с речевым: «эл-эл-эм» и «LLM» — одно и то же
            sim = SequenceMatcher(None, tokens(n["lines"][li]["text"]),
                                  tokens(heard[k])).ratio()
            entry["clips"].append({"line": li, "cut": cuts[k], "seconds": round(len(clip) / sr, 2),
                                   "sim": round(sim, 3), "heard": heard[k]})
            flag = "OK " if sim >= 0.85 else "BAD"
            print(f"  сцена {si} строка {li:2} {flag} {sim:.2f} {len(clip) / sr:5.2f}s | {heard[k][:56]}")
        report.append(entry)

    out = os.path.join(os.path.dirname(a.out_dir) or ".", "speech_report.json")
    with open(out, "w") as f:
        json.dump(report, f, ensure_ascii=False, indent=1)
    for x in pron.unused_overrides():
        print(f"⚠️  прочтение «{x['form']}» не применено: реплика в сцене {x['scene']} "
              f"изменилась — «{x['line'][:60]}»")
    # Разметка видна числами: метка в одном слове выключает авто-акцентиацию хаба на
    # всю сцену, и по логу должно быть видно, сколько слов ушло размеченными.
    m = pron.mark_stats()
    print(f"  ударения [{m['accent']}]: с меткой {m['marked']}, без метки {m['unmarked']}, "
          f"отброшено {m['rejected']}")
    for x in pron.bad_marks:
        print(f"⚠️  ударение «{x['form']}» у слова «{x['text']}» отброшено — {x['why']}")
    print("→", out)
    if failed:
        raise SystemExit(f"сцены {failed}: движок не оставил паузы на стыках реплик. "
                         f"Переозвучите их — движок случаен, обычно хватает дубля.")


if __name__ == "__main__":
    main()