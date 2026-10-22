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
- пауза на стыке реплик: у хаба движок ставит её сам (0.75–0.93 с), у SpeechKit — мы сами
  (метка `sil<[200]>` в тексте сцены), потому что движок оставляет её не всегда.

Граница берётся в паузе, и реплика вырезается в тишине: стык в тишине
не щёлкает по построению. Паузу на стыке ставим мы (см. tts_speechkit.with_pauses): движок оставляет
её не всегда, и раньше сцена без паузы переозвучивалась до трёх раз, а потом стадия
падала. Теперь стык без паузы — не поломка: режем по концу слова, отмечаем стык в
отчёте и идём дальше.


  ND_API_KEY=sk-... python3 scripts/nd_voice.py projects/x/narration.json projects/x/audio/raw
  python3 scripts/nd_voice.py narration.json audio/raw --only 12 --voice uncle_fu
  python3 scripts/nd_voice.py narration.json audio/raw --scenes 0 3

Движок озвучки выбирается флагом --tts-provider: `hub` (по умолчанию) или `speechkit`
(Yandex SpeechKit, ключ в YANDEX_API_KEY). У SpeechKit два движения: general
(отдаёт тайминги слов — реплики режутся без распознавания) и livetts (премиум-голоса,
таймингов не отдаёт, темп принимает только 1.0).
"""

import argparse
import difflib
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(__file__))
from ndv_common import (clip_name, envelope, load_narration, nd_json, nd_speak, nd_transcribe,  # noqa: E402
                        retry, scene_groups, scene_name, silence_spans, trim_silence,
                        wav_samples, write_wav)
from pronounce import ACCENTS, Pronouncer  # noqa: E402
from tts_speechkit import (GENERAL, LIVETTS, MODELS, SEP, SpeechKitError,  # noqa: E402
                           with_pauses)
from tts_speechkit import speak as speechkit_speak  # noqa: E402
from difflib import SequenceMatcher  # noqa: E402

LANGUAGES = {"ru": "Russian", "en": "English"}
WORD = re.compile(r"\w+", re.UNICODE)
PAUSE_MIN = 0.10        # короче — не пауза, а стык слогов внутри фразы
SNAP_WINDOW = 0.5       # на каком расстоянии от конца слова ещё ищем паузу, секунды
TAIL = 0.06             # насколько конец слова может не дотягивать до конца паузы
LEAD = 0.12            # насколько пауза может начаться позже конца слова, секунды


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
    """Граница реплики — в паузе. None, если паузы рядом нет.

    Выбор паузы — по тому, где она стоит относительно конца слова. Whisper отдаёт
    конец слова вместе с хвостовой паузой: «вопросом,» — 6.05–7.14 при тишине
    6.62–7.14, то есть конец слова это конец его паузы, а не конец звука слова.

    Поэтому сначала идёт своя хвостовая пауза слова (та, что заканчивается на его
    конце), потом пауза, начинающаяся сразу за словом, потом ближайшая рядом, и
    только потом «плотный» рез по концу слова. Ближайшая нужна, когда конец слова
    определён неверно: whisper дробит «чат-бот» на два слова, и тогда конец
    реплики приходится на первое слово следующей.
    """
    if t_end is None:
        return None
    # Хвостовая пауза слова: та, что заканчивается на его конце.
    tail = [s for s in spans if s[0] <= t_end + TAIL and s[1] >= t_end - TAIL]
    if tail:
        best = max(tail, key=lambda s: s[1])
        return (best[0] + best[1]) / 2
    # Пауза сразу за словом: следующее слово начинается после неё, так что рез в
    # этой тишине не откусит ни слова, ни следующей реплики.
    lead = [s for s in spans if t_end - TAIL <= s[0] <= t_end + LEAD]
    if lead:
        best = min(lead, key=lambda s: s[0])
        return (best[0] + best[1]) / 2
    # Ближайшая пауза рядом — запасной путь. Нужен, когда конец слова определён
    # неверно: whisper дробит «чат-бот» на два слова, и тогда конец реплики
    # приходится на первое слово следующей. Раньше этот путь был единственным.
    near = [s for s in spans if s[1] >= t_end - SNAP_WINDOW and s[0] <= t_end + SNAP_WINDOW]
    if near:
        best = min(near, key=lambda s: abs((s[0] + s[1]) / 2 - t_end))
        return (best[0] + best[1]) / 2
    return None



def cut_scene(samples, sr, lines_spoken, words):
    """Разрезать озвучку сцены на реплики.

    Возвращает (резы, что услышали в каждой, стыки без паузы).

    `last_word[i]` — индекс последнего слова реплики, то есть её КОНЕЦ. Начало реплики
    получается из конца предыдущей, поэтому услышанное собирается по границам, а не
    начиная с last_word.

    Стык без паузы — не поломка сцены. Паузу на стыке ставим мы (см. tts_speechkit),
    но если движок её всё-таки не оставил, режем по концу слова и отмечаем стык:
    ролик от этого не рассыпается, а сцена не выбрасывается и стадия не падает.

    Границы держим в двух видах: сырые — для сравнений, округлённые — для отчёта.
    Сравнивать округлённое с неокруглённым нельзя: две соседние границы, нашедшие одну
    паузу, округлялись в одно и то же число, срез между ними выходил нулевой, и в
    файл писался пустой wav. Падало это не здесь, а тремя шагами позже, на замере
    длины, где выглядело как «N/A».
    """
    spans = silence_spans(envelope(samples, sr), sr, minlen=PAUSE_MIN)
    last_word = assign_lines(words, lines_spoken)

    start = 0
    heard, raw_cuts, cuts, tight = [], [0.0], [0.0], []
    for i in range(len(lines_spoken)):
        end = last_word.get(i)
        heard.append(" ".join(w for w, _, _ in words[start:(end + 1) if end is not None else None]))
        start = (end + 1) if end is not None else start
        if i == len(lines_spoken) - 1:
            break
        t_end = end and words[end][2]
        at = snap(t_end or raw_cuts[-1], spans)
        if at is None or at <= raw_cuts[-1]:
            # Паузы на стыке нет: режем по концу слова и отмечаем стык. Раньше здесь
            # сцена помечалась битой и озвучивалась заново — движок паузу не оставлял
            # не потому, что дубль неудачный, а потому, что не оставлял никогда.
            at = max(t_end or 0.0, raw_cuts[-1] + 0.05)
            tight.append(i)
        raw_cuts.append(at)
        cuts.append(round(min(at, len(samples) / sr), 3))
    raw_cuts.append(len(samples) / sr)
    cuts.append(round(len(samples) / sr, 3))
    return cuts, heard, tight


def voice_scene(text, voice, language, style, tries, seed, speed=1.0,
                provider="hub", model=GENERAL, role=None):
    """Озвучить сцену целиком → (байты, движок, тайминги слов или None).

    Тайминги слов отдаёт только SpeechKit общего движка: он сам знает, где какое
    слово прозвучало, и резать реплики по ним точнее, чем по распознаванию. У
    премиум-движка (livetts) таймингов нет вовсе, и у хаба тоже: там нарезку
    делает whisper.
    """
    if provider == "speechkit":
        def call():
            try:
                data, words = speechkit_speak(text, voice, model, role, speed)
            except SpeechKitError as exc:
                # Отказ движка не временный: второй дубль упал бы так же, только дороже.
                raise SystemExit(f"SpeechKit: {exc}")
            return data, f"speechkit {model}", (words or None)
        return retry(call, tries)
    data, engine = retry(lambda: nd_speak(text, voice=voice, language=language, style=style,
                                          tries=1, seed=seed, speed=speed), tries)
    return data, engine, None


def check_voice(voice):
    """Спросить у хаба, какие голоса есть, и не начинать синтез с несуществующим.

    Без этой проверки неизвестный голос умирал в середине минутной стадии с
    «HTTP Error 400» и без пояснения. Список не пришёл — не мешаем: пусть решает движок.
    """
    try:
        data = nd_json("/v1/audio/voices")
    except Exception as exc:
        print(f"⚠️  список голосов хаба недоступен ({exc}) — начинаю синтез как есть")
        return
    voices = [v for v in (data.get("voices") or []) if isinstance(v, str)]
    if voices and voice not in voices:
        raise SystemExit(f"голос «{voice}» хаб не знает: {', '.join(voices)}")


def drop_clips(out_dir, idx):
    """Убрать клипы сцены, которая не получилась.

    Оставленные файлы вредны: пустой wav роняет укладку в окна, а клип от прошлого
    дубля — тихое расхождение между текстом и звуком. Лучше не оставить ничего, чем
    оставить неправду.
    """
    for li in idx:
        path = os.path.join(out_dir, clip_name(li))
        if os.path.exists(path):
            try:
                os.remove(path)
            except OSError:
                pass


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


def process_scene(data, spoken, words=None):
    """Нарезка озвучки сцены на реплики. Работает и для свежего синтеза, и для отложенного.

    words — тайминги слов, если их отдал движок. Нет — распознаём речь сами.

    (сэмплы, sr, резы, что услышали в каждой реплике, стыки без паузы)
    """
    samples, sr = wav_samples(data)
    if words is None:
        words = nd_transcribe(data, "whisper-1")["words"]
    cuts, heard, tight = cut_scene(samples, sr, spoken, words)
    return samples, sr, cuts, heard, tight


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
    ap.add_argument("--tts-provider", default="hub", choices=("hub", "speechkit"),
                    help="движок озвучки: хаб (по умолчанию) или SpeechKit Яндекса")
    ap.add_argument("--speechkit-model", default=GENERAL, choices=MODELS,
                    help="модель SpeechKit: general (обычные голоса) или livetts (премиум)")
    ap.add_argument("--role", default=None,
                    help="роль голоса SpeechKit (casual, formal, sales, neutral, friendly)")
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
    if a.tts_provider == "speechkit":
        # Темп у премиум-движка не принимается: показываем правду, а не просим
        # человека гадать, почему ролик не ускоряется.
        speed = a.speed if a.speechkit_model == GENERAL else 1.0
        print(f"speechkit {a.speechkit_model} · {voice}"
              f"{' · ' + a.role if a.role else ''} · {language} · "
              f"темп {speed:g} · {len(groups)} сцен(ы) по одному вызову")
        if speed != a.speed:
            print("⚠️  премиум-голос движок не ускоряет (там только 1.0) — "
                  "темп в хронометраж подберёт fit_narration")
    else:
        print(f"{a.model or ('espeech-tts' if language == 'Russian' else 'qwen3-tts')} · {voice} · {language} · "
              f"темп {a.speed:g} · {len(groups)} сцен(ы) по одному вызову")
        check_voice(voice)

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
        # Разделитель реплик — перевод строки: на стыке мы сами ставим паузу
        # (движок оставляет её не всегда, а резать сцену надо в тишине).
        text, occ = pron.scene_spoken([n["lines"][i] for i in idx], idx, si,
                                      sep=SEP if a.tts_provider == "speechkit" else " ")
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

        # Синтез один: паузу на стыке ставим мы, и переозвучка её не создаёт. Раньше
        # сцена без паузы переозвучивалась до трёх раз, а потом стадия падала — движок
        # паузу не оставлял не потому, что дубль неудачный, а потому, что не оставлял
        # никогда. Теперь стык без паузы — не поломка: режем по концу слова и идём дальше.
        words = None
        if kept:
            data = kept
        else:
            speech = with_pauses(text) if a.tts_provider == "speechkit" else text
            data, engine, words = voice_scene(speech, voice, language, styles.get(beat, style),
                                               a.tries, a.seed, a.speed,
                                               a.tts_provider, a.speechkit_model, a.role)
            keep_previous(a.out_dir, si)
        with open(os.path.join(a.out_dir, scene_name(si)), "wb") as f:
            f.write(data)
        samples, sr, cuts, heard, tight = process_scene(data, spoken, words)
        if tight:
            print(f"  сцена {si}: стык без паузы {tight} — режем по концу слова")

        if si in restore:
            # Восстановленный набор озвучен прежним текстом, а не тем, что сейчас в
            # нарративе. Отчёт должен описывать звук на диске — иначе «прочтение
            # размечено, но ещё не озвучено» погаснет, хотя в звуке его нет.
            prev_entry = previous.get(si) or {}
            text = prev_entry.get("speech", text)
            occ = prev_entry.get("stress", occ)
        entry = {"scene": si, "lines": idx, "engine": engine, "beat": beat,
                 "duration": round(len(samples) / sr, 2), "speech": text.replace(SEP, " "),
                 "restored": si in restore,
                 # есть ли что возвращать: интерфейс показывает кнопку отката
                 "prev": os.path.exists(os.path.join(a.out_dir, scene_name(si, prev=True))),
                 "stress": occ,
                 # стыки без паузы: рез по концу слова, шов может быть с щелчком
                 "tight": tight,
                 "clips": []}
        empty = []
        for k, li in enumerate(idx):
            a0, a1 = int(cuts[k] * sr), int(cuts[k + 1] * sr)
            clip = trim_silence(samples[a0:a1], sr) if a1 > a0 else samples[:0]
            path = os.path.join(a.out_dir, clip_name(li))
            if not len(clip):
                # Срез пустой: границы схлопнулись (две реплики нашли одну паузу).
                # Пустой wav писать нельзя — на замере длины он даёт «N/A», и стадия
                # падает тремя шагами позже, в другом месте и с другим текстом.
                empty.append(li)
                continue
            write_wav(path, clip, sr)
            # сверяем с экранным текстом, а не с речевым: «эл-эл-эм» и «LLM» — одно и то же
            sim = SequenceMatcher(None, tokens(n["lines"][li]["text"]),
                                  tokens(heard[k])).ratio()
            entry["clips"].append({"line": li, "cut": cuts[k], "seconds": round(len(clip) / sr, 2),
                                   "sim": round(sim, 3), "heard": heard[k]})
            flag = "OK " if sim >= 0.85 else "BAD"
            print(f"  сцена {si} строка {li:2} {flag} {sim:.2f} {len(clip) / sr:5.2f}s | {heard[k][:56]}")
        if empty:
            # Сцена битая: клипы этой сцены убираем, чтобы следующий шаг не спотыкался
            # о них (пустой wav, а старая запись другой сцены — тоже не та).
            drop_clips(a.out_dir, idx)
            failed.append(si)
            report.append({"scene": si, "lines": idx, "error": f"пустой срез у реплик {empty}",
                           "stress": occ})
            print(f"  сцена {si}: пустой срез у реплик {empty} — переозвучите сцену")
            continue
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
        raise SystemExit(f"сцены {failed}: пустой срез у реплик — переозвучите их.")


if __name__ == "__main__":
    main()