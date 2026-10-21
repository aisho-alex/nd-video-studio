#!/usr/bin/env python3
"""Что диктор произносит на самом деле: написанный текст → речевой текст.

Экранный текст и произносимый — разные вещи. На экране «LLM», в миксе «эл-эл-эм»;
на экране «rate limiting», в миксе «рейт лимитинг». `text` в narration.json остаётся
человеческим, движок получает то, что он в состоянии выговорить.

Два словаря в блоке `voice` в narration.json:

    "voice": {
      "voice": "uncle_fu",
      "style": "...",
      "pronounce": {"LLM": "эл-эл-эм", "rate limiting": "рейт лимитинг"}
    }

`pronounce` — латиница и англицизмы, произносимые по-русски. Работает и проверено:
whisper на «эл-эл-эм» пишет обратно «LLM», поэтому субтитры остаются правильными.

Ударения ставит не этот скрипт, а RUAccent — тот же акцентуатор, что работает внутри
ESpeech на стороне хаба. Свои формы ударений здесь не выдумываются: «а+гент» вместо
«аг+ент» — это ровно тот класс ошибок, который ломает реплику целиком.

Важное правило: `+` ставится ТОЛЬКО в словах, которые RUAccent считает омографами. По
документации хаба, как только `+` есть в тексте, авто-акцентизатор не вмешивается
(`docs`: «если `+` уже есть в тексте, авто-акцентизатор не вмешивается»), значит
отметив всё подряд мы взяли бы на себя риск за каждое слово ролика вместо десятка омографов.

    python3 scripts/pronounce.py projects/x/narration.json          # отчёт, без сети
    python3 scripts/pronounce.py projects/x/narration.json --json
"""

import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(__file__))
from ndv_common import load_narration  # noqa: E402

LANGUAGES = {"ru": "Russian", "en": "English"}

# Слова в тексте. Дефис и пробел разбивают: «LLM-агент» — это два слова, а «rate
# limiting» — ключ из двух. Склеивать их в один токен нельзя, иначе словарь не найдёт
# ни «LLM», ни «rate limiting».
WORD = re.compile(r"\w+", re.UNICODE)
# Так выглядит слово после RUAccent: буквы, возможно с «+» перед ударной гласной.
# Знак может стоять и перед словом («+Он»): без ведущего «+» в шаблоне он остаётся
# снаружи и не вырезается, когда слово не омограф.
MARKED = re.compile(r"\+?\w+(?:\+\w+)*", re.UNICODE)
MAX_WORDS = 4       # максимум слов в одном ключе глоссария
VOWELS = set("аеёиоуыэюяАЕЁИОУЫЭЮЯaeiouyAEIOUY")


def stress_error(key, form):
    """Почему форму с `+` нельзя отдавать движку, либо None.

    Три проверки, каждая — из измеренного поведения ESpeech, а не из догадки:
    `+` должен стоять перед ударной гласной (перед согласной движок читает слово как
    другое: «за+мок» → «заумок»), стоять один, и форма должна быть тем же словом.
    """
    if not form or not key:
        return "пустая форма"
    if form.count("+") == 0:
        return "в форме нет «+» — это не ручное ударение, а переписывание слова"
    if form.count("+") > 1:
        return "больше одного «+»"
    pos = form.index("+")
    if pos + 1 >= len(form):
        return "«+» в конце формы"
    if form[pos + 1] not in VOWELS:
        return f"«+» перед «{form[pos + 1]}», а не перед гласной: движок прочитает слово иначе"
    if form.replace("+", "").lower() != key.lower():
        return f"форма «{form}» — не то же слово, что «{key}»"
    return None

# Латиница внутри русской реплики. Измерено на живом API: ESpeech глотает буквы,
# «LLM» на выходе распознаётся как «ЭМ», а транслитерация «эл-эл-эм» — обратно как «LLM».
# Ключи строчными: сравнение регистронезависимое. В английском тексте не применяется —
# Qwen3-TTS читает «rate limiting» сам и лучше, чем по-русски.
GLOSS = {
    "llm": "эл-эл-эм",
    "llms": "эл-эл-эм-ы",
    "llm-based": "эл-эл-эм-бейз",
    "gpt": "джи-пи-ти",
    "ai": "эй-ай",
    "ml": "эм-эл",
    "nlp": "эн-эл-пи",
    "ocr": "о-си-ар",
    "api": "а-пи-ай",
    "apis": "а-пи-ай-ы",
    "url": "у-р-л",
    "io": "ай-о",
    "ui": "ю-ай",
    "ux": "ю-экс",
    "it": "ай-ти",
    "cpu": "си-пи-ю",
    "gpu": "джи-пи-ю",
    "ssd": "эс-эс-ди",
    "http": "аш-т-т-п",
    "https": "аш-т-т-п-с",
    "json": "джей-сон",
    "csv": "си-си-веи",
    "xml": "икс-эм-эл",
    "pdf": "пи-ди-эф",
    "sql": "си-кью-эл",
    "nosql": "но-си-кью-эл",
    "html": "эйч-ти-эм-эл",
    "css": "си-эс-эс",
    "jwt": "джей-вэй-ти",
    "sdk": "эс-ди-кей",
    "rag": "рэг",
    "rate limiting": "рейт лимитинг",
    "rate limit": "рейт лимит",
    "load balancing": "лоуд-балансинг",
    "fallback": "фолбэк",
}
# Ключи ищутся по целому слову, поэтому порядок не важен.


def latin_gloss(text, table):
    """Подставить латиницу по словам, не трогая разделители и пунктуацию.

    Словарь — «слово → произносимая форма», а не правило по буквам: токенизатор не должен
    решать, как читать аббревиатуру. Ключ может быть многословным («rate limiting»),
    приоритет у самого длинного совпадения.
    """
    by_words = {tuple(k.lower().split()): v for k, v in table.items()}
    toks = list(WORD.finditer(text))
    out, pos, i = [], 0, 0
    while i < len(toks):
        for n in range(min(MAX_WORDS, len(toks) - i), 0, -1):
            key = tuple(t.group(0).lower() for t in toks[i:i + n])
            # ключ из нескольких слов должен идти через один пробел; дефис — это другое слово
            tail = toks[i + n].start() if i + n < len(toks) else len(text)
            joined = n == 1 or not text[toks[i + n - 1].end():tail].strip()
            if key in by_words and joined:
                out.append(text[pos:toks[i].start()])
                out.append(by_words[key])
                pos = toks[i + n - 1].end()
                i += n
                break
        else:
            i += 1
    out.append(text[pos:])
    return "".join(out)


def gloss_spans(text, table):
    """Что во что подставлено: [(слова как написаны, слова как произнесены)].

    Нужно проверке речи: «rate limiting» на экране произносится «рейт лимитинг», и сверять
    надо с произнесённым — иначе правильное произнесение выглядит как потерянное слово.
    """
    by_words = {tuple(k.lower().split()): v for k, v in table.items()}
    toks = list(WORD.finditer(text))
    out, i = [], 0
    while i < len(toks):
        for n in range(min(MAX_WORDS, len(toks) - i), 0, -1):
            key = tuple(t.group(0).lower() for t in toks[i:i + n])
            tail = toks[i + n].start() if i + n < len(toks) else len(text)
            if key in by_words and (n == 1 or not text[toks[i + n - 1].end():tail].strip()):
                out.append((list(key), list(WORD.findall(by_words[key]))))
                i += n
                break
        else:
            i += 1
    return out


class Pronouncer:
    """Речевой текст проекта. `stress=False` не грузит ruaccent — только глоссарий."""

    def __init__(self, narration, stress=True, model_size="turbo2"):
        v = narration.get("voice") or {}
        self.language = narration.get("language", "en")
        self.gloss = dict(GLOSS)
        self.gloss.update(v.get("pronounce") or {})
        self.warnings = []
        # Явные формы ударений проекта — единственный рычаг там, где RUAccent ошибся:
        # на «Большая языковая модель» он даёт «Б+ольшая» (сравнительная степень), а нужно
        # «больш+ая». Словарь омографов тут не помогает: дело не в омонимии.
        self.stress = {}
        for k, form in (v.get("stress") or {}).items():
            err = stress_error(k, form)
            if err:
                self.warnings.append(f"voice.stress: «{k}» → «{form}» отброшено — {err}")
            else:
                self.stress[k.lower()] = form
        self.omographs = {}
        self._acc = None
        if stress and self.language == "ru":
            self._load(model_size)

    def _load(self, model_size):
        try:
            from ruaccent import RUAccent
        except ImportError:
            self.warnings.append("ruaccent не установлен: ударения не расставляются "
                                 "(см. Dockerfile: pip install ruaccent)")
            return
        acc = RUAccent()
        acc.load(omograph_model_size=model_size, use_dictionary=True)
        self._acc = acc
        self.omographs = getattr(acc, "omographs", {}) or {}

    # -- текст ---------------------------------------------------------------

    def variants(self, text):
        """Слово как написано → слова, как оно прозвучит (только подставленные места)."""
        return {w[0]: v for w, v in gloss_spans(text, self.gloss)}

    def line(self, ln, stress=None):
        """Речевой текст реплики. `speech` в narration.json — ручной побег."""
        manual = (ln.get("speech") or "").strip()
        if manual:
            return manual
        return self.text(ln.get("text") or "", stress=stress)

    def text(self, raw, stress=None):
        out = latin_gloss(raw, self.gloss) if self.language == "ru" else raw
        use = (self._acc is not None) if stress is None else (stress and self._acc is not None)
        return self._mark_omographs(out) if use else out

    def scene(self, lines, stress=True):
        """Текст сцены одной строкой: одна озвучка — один вызов движка.

        Контекст важен: «Большая языковая модель» RUAccent разбирает как больш+ая язык+овая,
        а не б+ольшая язык+овая, и без соседних слов омограф не снимается.
        """
        return self.text(" ".join(self.line(ln, stress=False) for ln in lines
                                  if (ln.get("text") or "").strip()), stress=stress)

    def _mark_omographs(self, text):
        """Оставить `+` только там, где слово омограф; остальное вернуть как было.

        RUAccent отмечает ударение в каждом слове. Оставлять все отметки нельзя: движок,
        увидев `+`, перестаёт акцентировать сам, и мы отвечаем за каждое слово ролика.
        Формы из voice.stress применяются после: это ручное решение поверх RUAccent.
        """
        marked = self._acc.process_all(text)
        omographs = self.omographs

        def repl(m):
            w = m.group(0)
            override = self.stress.get(w.replace("+", "").lower())
            if override:
                return override if w[:1].isupper() else override.lower()
            return w if w.replace("+", "").lower() in omographs else w.replace("+", "")

        return MARKED.sub(repl, marked)

    # -- отчёт ---------------------------------------------------------------

    def changed(self, lines):
        """Реплики, у которых речевой текст отличается от написанного."""
        out = []
        for i, ln in enumerate(lines):
            spoken = self.line(ln)
            if spoken.strip() != (ln.get("text") or "").strip():
                out.append({"line": i, "text": ln.get("text", ""), "speech": spoken})
        return out

    def stress_marks(self, lines):
        """Где стоит `+`: для глаза и для проверки результата."""
        out = []
        for i, ln in enumerate(lines):
            for w in MARKED.findall(self.line(ln)):
                if "+" in w:
                    out.append({"line": i, "form": w})
        return out


def main():
    ap = argparse.ArgumentParser(description="речевой текст narration.json")
    ap.add_argument("narration")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--out")
    ap.add_argument("--model-size", default="turbo2", help="модель омографов ruaccent")
    ap.add_argument("--no-stress", action="store_true", help="без ruaccent: только глоссарий")
    a = ap.parse_args()

    n = load_narration(a.narration)
    p = Pronouncer(n, stress=not a.no_stress, model_size=a.model_size)
    report = {"language": p.language,
              "changed": p.changed(n["lines"]),
              "stress": p.stress_marks(n["lines"]),
              "warnings": p.warnings}
    out = a.out or os.path.join(os.path.dirname(a.narration) or ".", "speech_report.json")
    with open(out, "w") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=1)

    if a.json:
        print(json.dumps(report, ensure_ascii=False, indent=1))
        return
    print(f"{p.language}: глоссарий {len(p.gloss)}, омографов в словаре {len(p.omographs)}")
    for x in report["changed"]:
        print(f"  [{x['line']:2}] {x['text']}\n       → {x['speech']}")
    if report["stress"]:
        print(f"  ударений поставлено: {len(report['stress'])}")
        for x in report["stress"]:
            print(f"    строка {x['line']:2}: {x['form']}")
    for w in p.warnings:
        print(f"  ⚠️  {w}")
    print("→", out)


if __name__ == "__main__":
    main()