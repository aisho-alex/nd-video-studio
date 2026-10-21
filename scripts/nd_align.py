#!/usr/bin/env python3
"""Проверка озвучки и тайминги слов через whisper-1 хаба.

Для каждой line_NN.wav:
- распознаёт её и сверяет с текстом реплики: движок иногда глотает, переставляет или
  добавляет слова, и на слух это легко пропустить;
- пишет слова с таймингами (уже со сдвигом на начало реплики) в words.json. Композиция
  подсвечивает слово субтитров ровно тогда, когда оно произнесено.

Сверка пословная, а не «по доле похожего текста»: при ratio 0.86 строка «Так у LLM
появляются лапки» проходила, а на слух было «Так у ЛАЭМ появляются лапки». Теперь потерянное
слово — это потерянное слово, независимо от общей похожести. Отдельное правило для латиницы:
слово из текста считается услышанным либо самим собой, либо своей речевой формой из
voice.pronounce (LLM на экране → «эл-эл-эм» в миксе → whisper пишет обратно «LLM»).

  ND_API_KEY=sk-... python3 scripts/nd_align.py projects/x/narration.json projects/x/audio/fit projects/x/words.json
  python3 scripts/nd_align.py narration.json audio/fit words.json --max-lost 0 --min-sim 0.0
"""

import argparse
import difflib
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(__file__))
from ndv_common import clip_name, load_narration, nd_transcribe  # noqa: E402
from pronounce import Pronouncer  # noqa: E402

WORD = re.compile(r"\w+", re.UNICODE)
# Короткие служебные слова распознание теряет чаще всего, на произношение они не влияют.
SHORT = set("и в с к о об у а я на за из по не то что он мы вы они как но или если для при до".split())


def norm(s):
    """Токены для сравнения: без регистра и пунктуации, ё приравнена к е."""
    if isinstance(s, (list, tuple)):
        s = " ".join(s)
    s = re.sub(r"[^\w ]+", " ", s.lower().replace("ё", "е"))
    return WORD.findall(s)


def lost_tokens(text, spoken, heard, variants):
    """Слова текста, которых нет в распознавании.

    Слово засчитано услышанным, если в распознавании есть оно само или все слова его
    речевой формы: на экране «rate limiting», в миксе «рейт лимитинг», и whisper пишет
    «Рейт лимитинг» — это правильное произнесение, а не потерянное слово.
    """
    flat = "".join(norm(heard))
    lost = []
    for w in norm(text):
        if w in SHORT or len(w) < 3 or w in flat:
            continue
        alt = norm(variants.get(w, ""))
        if alt and all(piece in flat for piece in alt):
            continue
        lost.append(w)
    return lost


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("narration")
    ap.add_argument("audio_dir", help="обычно audio/fit (после укладки в окна)")
    ap.add_argument("out")
    ap.add_argument("--model", default="whisper-1",
                    help="модель хаба: тайминги слов даёт только whisper-1")
    ap.add_argument("--min-sim", type=float, default=0.85,
                    help="похожесть распознавания и текста, 0 = не проверять (по умолчанию 0.85)")
    ap.add_argument("--max-lost", type=int, default=0,
                    help="сколько значимых слов реплики можно потерять (по умолчанию ни одного)")
    ap.add_argument("--lines", type=int, nargs="*", help="проверить только эти строки")
    a = ap.parse_args()

    n = load_narration(a.narration)
    # проверке речи нужен только глоссарий латиницы: ударения уже стоят в самой записи сцены,
    # а ruaccent на каждую реплику грузить незачем
    pron = Pronouncer(n, stress=False)
    out, bad = [], []
    for i, ln in enumerate(n["lines"]):
        if a.lines and i not in a.lines:
            continue
        path = os.path.join(a.audio_dir, clip_name(i))
        with open(path, "rb") as f:
            res = nd_transcribe(f.read(), a.model, clip_name(i))
        heard = res["text"]
        lost = lost_tokens(ln.get("text", ""), pron.line(ln), heard, pron.variants(ln.get("text", "")))
        sim = difflib.SequenceMatcher(None, norm(ln["text"]), norm(heard)).ratio()
        words = [{"w": w, "t0": round(ln["start"] + t0, 3), "t1": round(ln["start"] + t1, 3)}
                 for w, t0, t1 in res["words"] if t0 is not None and t1 is not None]
        ok = sim >= a.min_sim and len(lost) <= a.max_lost
        out.append({"line": i, "text": ln["text"], "speech": pron.line(ln), "heard": heard.strip(),
                    "sim": round(sim, 3), "lost": lost, "words": words})
        if not ok:
            bad.append(i)
        flag = "OK " if ok else "BAD"
        detail = f" потеряно: {', '.join(lost)}" if lost else ""
        print(f"[{i:02d}] {flag} {sim:.2f}{detail} | {heard.strip()[:70]}")
    with open(a.out, "w") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print("→", a.out)
    if bad:
        print(f"⚠️ переозвучить: {' '.join(map(str, bad))}")
        sys.exit(2)


if __name__ == "__main__":
    main()