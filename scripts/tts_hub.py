#!/usr/bin/env python3
"""Voice narration.json via the hub TTS (api.neuraldeep.ru/v1/audio/speech).

Two engines behind one endpoint: `language=Russian` is voiced by ESpeech (F5 + RUAccent, correct
stresses and homograph disambiguation), everything else by Qwen3-TTS. The engine is chosen by the
language, so `model` is only sent when it differs from that default; the answer tells which one
worked in the `X-TTS-Engine` header. Voices: vivian, serena, ono_anna, sohoe (f), dylan, ryan,
aiden, uncle_fu (m). Style is free text in `instructions`, rate is `speed`.

  ND_API_KEY=sk-... python3 scripts/tts_hub.py projects/x/narration.json projects/x/audio/raw --voice ryan
  python3 scripts/tts_hub.py narration.json audio/raw --only 12 17   # re-voice two lines
"""

import argparse
import os
import re
import sys
import urllib.error

sys.path.insert(0, os.path.dirname(__file__))
from ndv_common import clip_name, env_key, http, load_narration, retry  # noqa: E402

CHAR_CAP = 5000        # hub limit on len(input) for one call
LATIN = re.compile(r"[A-Za-z]")
CYRILLIC = re.compile(r"[\u0400-\u04FF]")


def warn_unpronounceable(lines, language):
    """Ask before spending the quota: ESpeech mangles Latin acronyms inside Russian text.

    Heard on a real run: "в запрос к LLM" came back as "в запрос КМ", "RAG" as "Раджи". The fix is
    a rewrite ("к языковой модели"), not another attempt at the same text.
    """
    if language != "Russian":
        return []
    bad = [i for i, ln in enumerate(lines)
           if LATIN.search(ln["text"]) and CYRILLIC.search(ln["text"])]
    if bad:
        print(f"⚠️  lines {bad} mix Latin into Russian: ESpeech misreads them "
              "(LLM comes out as «КМ», RAG as «Раджи»).\n"
              "    Rewrite them the way they are said before voicing, or the words come out "
              "wrong and the run costs a re-voice.")
    return bad


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("narration")
    ap.add_argument("out_dir")
    ap.add_argument("--voice", default=None)
    ap.add_argument("--language", help="English, Russian, ...; default from narration.json")
    ap.add_argument("--model", help="espeech-tts or qwen3-tts; default follows the language")
    ap.add_argument("--speed", type=float, default=1.0)
    ap.add_argument("--only", type=int, nargs="*")
    ap.add_argument("--tries", type=int, default=5, help="attempts per line on 429/5xx")
    a = ap.parse_args()

    n = load_narration(a.narration)
    v = n.get("voice", {})
    voice = a.voice or v.get("voice", "ryan")
    language = a.language or {"en": "English", "ru": "Russian"}.get(n.get("language", "en"), "Auto")
    style = v.get("style", "Energetic tech trailer narrator. Punchy, confident, crisp emphasis.")
    model = a.model or v.get("model") or ("espeech-tts" if language == "Russian" else "qwen3-tts")
    key = env_key("ND_API_KEY")
    base = os.environ.get("ND_API_BASE", "https://api.neuraldeep.ru")
    os.makedirs(a.out_dir, exist_ok=True)
    print(f"{model} · voice {voice} · {language} · {style[:50]}")

    warn_unpronounceable(n["lines"], language)

    for i, ln in enumerate(n["lines"]):
        if a.only and i not in a.only:
            continue
        if len(ln["text"]) > CHAR_CAP:
            raise SystemExit(f"line {i} is {len(ln['text'])} chars, the hub caps one call at {CHAR_CAP}: "
                             "split the line in narration.json")
        body = {"model": model, "input": ln["text"], "voice": voice, "language": language,
                "instructions": style, "response_format": "wav", "speed": a.speed, "seed": 7}

        def call():
            with http(f"{base}/v1/audio/speech", body,
                      {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}) as r:
                data, engine = r.read(), r.headers.get("X-TTS-Engine", "?")
            if data[:4] != b"RIFF":
                raise urllib.error.URLError(f"not audio: {data[:120]!r}")
            return data, engine

        data, engine = retry(call, a.tries)
        with open(os.path.join(a.out_dir, clip_name(i)), "wb") as f:
            f.write(data)
        print(f"[{i}] {engine} {len(data)} bytes | {ln['text'][:60]}")


if __name__ == "__main__":
    main()