#!/usr/bin/env python3
"""Что диктор произносит на самом деле: написанный текст → речевой текст.

Экранный текст и произносимый — разные вещи. На экране «LLM», в миксе «эл-эл-эм»;
на экране «rate limiting», в миксе «рейт лимитинг». `text` в narration.json остаётся
человеческим, движок получает то, что он в состоянии выговорить.

Два словаря в блоке `voice` в narration.json:

    "voice": {
      "voice": "uncle_fu",
      "style": "...",
      "pronounce": {"LLM": "эл-эл-эм", "rate limiting": "рейт лимитинг"},
      "stress_occurrences": [
        {"scene": 1, "line": "Большая языковая модель…", "word": 0, "form": "больш+ая"}
      ]
    }

`pronounce` — латиница и англицизмы, произносимые по-русски. Работает и проверено:
whisper на «эл-эл-эм» пишет обратно «LLM», поэтому субтитры остаются правильными.

`stress_occurrences` — ваши прочтения спорных мест, по вхождению, а не по слову.
Внутри только то, что предлагает словарь омографов: слово «Большая» стоит в сцене 1
в реплике «Большая языковая модель…», первое слово, читать как «больш+ая». Якорь —
сцена, текст реплики и номер слова: индекс реплики сдвигается при удалении и переносе,
а текст остаётся. Если текст реплики изменился, прочтение перестаёт применяться и
показывается как устаревшее — молча применять его к другому слову нельзя.

Ударения ставит не этот скрипт, а RUAccent — тот же акцентуатор, что работает внутри
ESpeech на стороне хаба. Выбор прочтения — тоже его: этот скрипт не оценивает ударения
и не спорит со словарём. Свои формы ударений здесь не выдумываются: «а+гент» вместо
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
from ndv_common import load_narration, scene_groups  # noqa: E402

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

def match_case(form, sample):
    """Форма из словаря записана строчными, а слово в тексте может с заглавной.

    «больш+ая» в начале предложения → «Больш+ая». Заглавной делается первая БУКВА, а не
    первый символ: у формы «+один» знак «+» стоит впереди, и «+Один» читается иначе,
    чем «+один».
    """
    if not sample[:1].isupper():
        return form
    out = list(form)
    for i, ch in enumerate(out):
        if ch.isalpha():
            out[i] = ch.upper()
            break
    return "".join(out)


def rebuild(text, tokens_out):
    """Собрать текст заново из токенов: пробелы и знаки препинания — как были."""
    parts, pos, k = [], 0, 0
    for m in MARKED.finditer(text):
        parts.append(text[pos:m.start()])
        parts.append(tokens_out[k])
        k += 1
        pos = m.end()
    parts.append(text[pos:])
    return "".join(parts)


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


def gloss_with_map(text, table):
    """Подставить латиницу и вернуть карту «слово в тексте → слова в речи».

    Нужна, чтобы ударение попало в то самое слово. «LLM» на экране произносится
    «эл-эл-эм» — это три слова вместо одного, и после подстановки нумерация слов в
    реплике едет. Карта: для каждого слова текста — индексы слов в произнесённом.
    """
    by_words = {tuple(k.lower().split()): v for k, v in table.items()}
    toks = list(WORD.finditer(text))
    out, pos, i = [], 0, 0
    mapping = []          # для каждого слова текста: индексы слов в речи
    spoken = 0            # сколько слов уже получилось в речи
    while i < len(toks):
        hit = False
        for n in range(min(MAX_WORDS, len(toks) - i), 0, -1):
            key = tuple(t.group(0).lower() for t in toks[i:i + n])
            tail = toks[i + n].start() if i + n < len(toks) else len(text)
            joined = n == 1 or not text[toks[i + n - 1].end():tail].strip()
            if key in by_words and joined:
                out.append(text[pos:toks[i].start()])
                out.append(by_words[key])
                pos = toks[i + n - 1].end()
                words = WORD.findall(by_words[key])
                # Первое слово замены забирает все новые слова, остальные — пусто:
                # латиница в омографы не попадает, а нумерация дальше остаётся верной.
                mapping.append(list(range(spoken, spoken + len(words))))
                for _ in range(n - 1):
                    mapping.append([])
                spoken += len(words)
                i += n
                hit = True
                break
        if not hit:
            mapping.append([spoken])
            spoken += 1
            i += 1
    out.append(text[pos:])
    return "".join(out), mapping


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
        # Ваши прочтения: (сцена, текст реплики) → {номер слова: форма}.
        self.overrides = {}
        self.omographs = {}
        self._acc = None
        self._used = set()
        self._lines_present = set()
        self._rejected = []
        self._orphan = []
        if stress and self.language == "ru":
            self._load(model_size)
        self._load_overrides(v)

    def _load_overrides(self, v):
        """Прочтения из narration.json — по вхождению.

        Слово как таковое не годится: «большая» встречается в ролике несколько раз, и
        прочтение может зависеть от окружения. Зато индекс реплики сдвигается при
        удалении и переносе, поэтому якорь — сцена, ТЕКСТ реплики и номер слова.
        """
        if v.get("stress"):
            self.warnings.append("voice.stress больше не действует: прочтение задаётся "
                                 "по вхождению — voice.stress_occurrences")
        for o in (v.get("stress_occurrences") or []):
            if not isinstance(o, dict):
                continue
            scene, line = o.get("scene"), o.get("line")
            word, form = o.get("word"), o.get("form")
            if (not isinstance(scene, int) or not isinstance(word, int)
                    or not isinstance(line, str) or not isinstance(form, str)):
                self.warnings.append("voice.stress_occurrences: запись без сцены, реплики, "
                                     "номера слова или формы — отброшена")
                continue
            self.overrides.setdefault((scene, line.strip()), {})[word] = form.strip()

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
        """Текст сцены одной строкой: одна озвучка — один вызов движка."""
        return self.scene_spoken(lines, stress=stress)[0]

    def scene_spoken(self, lines, line_ids=None, scene=None, stress=True):
        """Текст сцены для движка и список спорных мест.

        Возвращает (speech, occurrences). В occurrences — по одной записи на омограф:
        где он стоит в реплике, какое прочтение выбрано, какие ещё предлагает словарь
        и чьё это решение — библиотеки или ваше.

        Контекст тут важен: омограф снимается соседними словами, и «Большая языковая
        модель» и «Большая книга» читаются по-разному. Поэтому библиотека смотрит на
        сцену целиком, а не на реплику по отдельности.
        """
        line_ids = line_ids if line_ids is not None else list(range(len(lines)))
        # 1. Текст сцены: реплики склеены, ручной `speech` подставлен.
        parts, owner = [], []
        for k, ln in enumerate(lines):
            manual = (ln.get("speech") or "").strip()
            text_k = manual if manual else (ln.get("text") or "").strip()
            self._lines_present.add((self._scene_of(ln, scene), text_k))
            if not text_k:
                continue
            parts.append(text_k)
            for j in range(len(WORD.findall(text_k))):
                owner.append((k, j))
        joined = " ".join(parts)
        if not stress or self._acc is None:
            return (self._gloss(joined)[0], [])

        # 2. Глоссарий: латиница → произносимое. Слова при этом разъезжаются,
        #    поэтому запоминаем, какое произнесённое слово пришло из какого написанного.
        glossed, gmap = self._gloss(joined)
        # 3. Библиотека снимает омографы по контексту сцены.
        marked = self._acc.process_all(glossed)
        spans = list(MARKED.finditer(marked))
        if len(spans) != sum(len(x) for x in gmap):
            self.warnings.append(f"сцена {scene}: RUAccent изменил число слов — "
                                 "ударения не проставлены")
            return marked, []
        # 4. Какое произнесённое слово из какой реплики и под каким номером.
        where = {}
        for p, targets in enumerate(gmap):
            if p >= len(owner):
                continue
            for t in targets:
                where[t] = owner[p]

        out, occurrences = [], []
        seen = {}           # реплика → номера слов, которые в ней нашлись
        for g, sp in enumerate(spans):
            tok = sp.group(0)
            plain = tok.replace("+", "")
            key = plain.lower()
            at = where.get(g)
            ov = None
            if at is not None:
                sc = self._scene_of(lines[at[0]], scene)
                ov = self.overrides.get((sc, (lines[at[0]].get("text") or "").strip()), {}).get(at[1])
                seen.setdefault(at[0], set()).add(at[1])
            if key not in self.omographs:
                # Не омограф: «+» убираем, иначе мы отвечали бы за ударение каждого
                # слова ролика, а движок справляется с однозначными сам.
                out.append(plain)
                if ov:
                    # Слово не из списка омографов — трогать его нельзя: читать его
                    # иначе нечем, а форма из словаря для него не существует.
                    self._rejected.append({"scene": scene, "line": at[0], "word": at[1],
                                           "form": ov, "text": plain,
                                           "why": "слово не омограф — его нечего читать иначе"})
                continue
            form, source = tok, "library"
            if ov:
                form, source = self._apply(self._scene_of(lines[at[0]], scene), at[0],
                                          lines[at[0]], at[1], plain, tok, form)
            out.append(form)
            occurrences.append({
                "line": line_ids[at[0]] if at is not None else None,
                "word": at[1] if at is not None else None,
                "text": plain,
                "form": form,
                # Что выбрала библиотека. Когда форму меняли — это и прочтение,
                # и выбор словаря, и по одному прочтению нельзя было бы понять,
                # куда возвращать.
                "library": tok,
                "alternatives": [a for a in self.omographs.get(key, [])
                                 if a.lower() != form.lower()],
                "source": source,
            })
        # Прочтения, у которых нет такого слова в реплике: номер слова больше, чем
        # слов в строке, — их отмечаем отдельно, иначе они выглядят как «реплика
        # изменилась», а это другое.
        for k, line_text in ((k, (lines[k].get("text") or "").strip()) for k in seen):
            sc = self._scene_of(lines[k], scene)
            for word, form in self.overrides.get((sc, line_text), {}).items():
                if word not in seen[k]:
                    self._orphan.append({"scene": scene, "line": k, "word": word, "form": form})
        return rebuild(marked, out), occurrences

    def _gloss(self, text):
        """Подставить латиницу вместе с картой слов."""
        if self.language == "ru":
            return gloss_with_map(text, self.gloss)
        return text, [[i] for i in range(len(WORD.findall(text)))]

    def _scene_of(self, ln, fallback):
        """Номер сцены из самой реплики.

        Нумерация в отчёте идёт по порядку групп, а в нарративе у реплики стоит её
        собственная сцена. Пока это одно и то же число, разницы нет; если разойдётся
        — прочтение ищется по номеру реплики, а не по номеру группы.
        """
        v = ln.get("scene")
        return v if isinstance(v, int) else fallback

    def _apply(self, scene, line_no, ln, word, plain, tok, form):
        """Ваше прочтение вместо библиотечного — если оно есть и выдержало проверку.

        Проверок три: слово должно быть омографом (иначе его нечего читать иначе),
        форма должна быть одной из тех, что предлагает словарь, и форма должна быть
        такой, чтобы движок не исказил слово.
        """
        ov = self.overrides.get((scene, (ln.get("text") or "").strip()), {}).get(word)
        if not ov:
            return form, "library"
        legal = self.omographs.get(plain.lower(), [])
        why = None
        if not any(a.lower() == ov.lower() for a in legal):
            why = f"формы «{ov}» нет в словаре ({', '.join(legal) or '—'})"
        else:
            err = stress_error(plain, ov)
            if err:
                why = err
        if why:
            self.warnings.append(f"прочтение «{ov}» для «{plain}» отброшено — {why}")
            self._rejected.append({"scene": scene, "line": line_no, "word": word,
                                   "form": ov, "text": plain, "why": why})
            return form, "library"
        self._used.add((scene, (ln.get("text") or "").strip(), word))
        return match_case(ov, tok), "override"

    def unused_overrides(self):
        """Прочтения, чья реплика изменилась: якорь перестал совпадать.

        Якорь — текст реплики. Если человек переписал её, прочтение молча применять
        не к чему, и молча терять его тоже нельзя: показываем как устаревшее.
        """
        out = []
        for (scene, line), words_ in self.overrides.items():
            if (scene, line) in self._lines_present:
                continue
            for word, form in words_.items():
                out.append({"scene": scene, "line": line, "word": word, "form": form})
        return out

    def rejected_overrides(self):
        """Прочтения, которые не применились: слово не омограф или формы нет в словаре."""
        return list(self._rejected)

    def orphan_overrides(self):
        """Прочтения, у которых в реплике нет такого номера слова."""
        return list(self._orphan)

    def _mark_omographs(self, text):
        """Оставить `+` только там, где слово омограф; остальное вернуть как было.

        RUAccent отмечает ударение в каждом слове. Оставлять все отметки нельзя: движок,
        увидев `+`, перестаёт акцентировать сам, и мы отвечаем за каждое слово ролика.
        """
        if self._acc is None:
            return text
        marked = self._acc.process_all(text)
        out = []
        for m in MARKED.finditer(marked):
            tok = m.group(0)
            plain = tok.replace("+", "")
            out.append(tok if plain.lower() in self.omographs else plain)
        return rebuild(marked, out)

    # -- отчёт ---------------------------------------------------------------

    def changed(self, lines):
        """Реплики, у которых речевой текст отличается от написанного.

        Без ударений: расхождение из-за `+` — это не другой текст, а другое прочтение
        того же слова, и оно показывается отдельно, списком спорных мест.
        """
        out = []
        for i, ln in enumerate(lines):
            spoken = self.line(ln, stress=False)
            if spoken.strip() != (ln.get("text") or "").strip():
                out.append({"line": i, "text": ln.get("text", ""), "speech": spoken})
        return out

    def occurrences(self, lines, groups):
        """Спорные места по всем сценам: где стоит омограф и какое прочтение выбрано."""
        out = []
        for si, idx in enumerate(groups):
            _, occ = self.scene_spoken([lines[i] for i in idx], idx, si)
            out.extend(occ)
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
    groups = scene_groups(n)
    report = {"language": p.language,
              "changed": p.changed(n["lines"]),
              "stress": p.occurrences(n["lines"], groups),
              "stale": p.unused_overrides(),
              "rejected": p.rejected_overrides(),
              "orphan": p.orphan_overrides(),
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
        print(f"  спорных мест: {len(report['stress'])}")
        for x in report["stress"]:
            alt = ", ".join(x["alternatives"]) or "—"
            print(f"    строка {x['line']:2} слово {x['word']}: {x['form']} "
                  f"[{x['source']}] · другие: {alt}")
    for x in report["stale"]:
        print(f"  ⚠️  прочтение «{x['form']}» не применено: реплики «{x['line'][:40]}» "
              f"в сцене {x['scene']} больше нет")
    for x in report["rejected"]:
        print(f"  ⚠️  прочтение «{x['form']}» для «{x['text']}» отброшено — {x['why']}")
    for x in report["orphan"]:
        print(f"  ⚠️  прочтение «{x['form']}» отброшено: в реплике сцены {x['scene']} "
              f"нет слова №{x['word']}")
    for w in p.warnings:
        print(f"  ⚠️  {w}")
    print("→", out)



if __name__ == "__main__":
    main()