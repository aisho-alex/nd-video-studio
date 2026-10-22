#!/usr/bin/env python3
"""Синтез речи Yandex SpeechKit (REST v3) — второй движок озвучки, кроме хаба.

  POST https://tts.api.cloud.yandex.net/tts/v3/utteranceSynthesis
  Authorization: Api-Key <ключ>, тело — JSON.

Зависимостей нет: urllib и base64 из stdlib, в образе ничего доставлять не нужно.
Ключ берётся из YANDEX_API_KEY (в приложении — YANDEX_API_KEY в окружении стадии).

Что этот API делает не так, как ожидаешь (проверено запросами к ключу):

- Голос, роль и темп читаются ТОЛЬКО из `hints`, и каждый объект hints несёт ОДНО
  поле: `hints: [{"voice": "sofia"}, {"role": "casual"}]`. Поле того же имени на
  верхнем уровне запроса молча игнорируется, а голос с ролью в одном объекте
  дают роль для голоса по умолчанию — «Unknown role 'good' for 'marina' voice».
- Один запрос — не больше 249 символов. Сцена длиннее: `unsafeMode: true`, тогда
  сервис сам режет текст по предложениям и склеивает высказывания; интонация
  перезапускается на границе предложения, но это единственный способ уложиться
  в лимит одним вызовом.
- Две модели, и голос принадлежит ровно одной: `general` (обычные голоса, 22050 Гц)
  и `livetts` (премиум-голоса, 48000 Гц). София в `general` недоступна: 403
  «Feature permission denied model=general, voice=sofia».
- `general` отдаёт `wordTimings` — тайминги слов, по ним реплики режутся без
  распознавания. `livetts` таймингов не отдаёт вовсе, и темп (`speed`) принимает
  только 1.0: «parameter speed is not supported by selected model».
- Звук приходит потоком: первый кусок — WAV-заголовок, дальше чистый PCM. В
  заголовке размер данных 0xFFFFFFFF, верить ему нельзя: собираем звук сами и
  пишем свой заголовок.

  YANDEX_API_KEY=... python3 scripts/tts_speechkit.py --text "Проба" --voice sofia \
      --role casual --model livetts --out /tmp/probe.wav
"""

import argparse
import base64
import io
import json
import os
import sys
import urllib.error
import wave

sys.path.insert(0, os.path.dirname(__file__))
from ndv_common import env_key, http  # noqa: E402

URL = "https://tts.api.cloud.yandex.net/tts/v3/utteranceSynthesis"
GENERAL = "general"
LIVETTS = "livetts"
MODELS = (GENERAL, LIVETTS)
# Сколько символов можно отдать одним запросом без unsafeMode.
CHAR_LIMIT = 249
# Границы темпа, которые движок принимает (у general).
SPEED_MIN, SPEED_MAX = 0.5, 2.0


class SpeechKitError(Exception):
    """Понятная причина отказа движка.

    Отдельный тип: ошибка тут не временная (не 429 и не 5xx), и повторять её
    бессмысленно — второй дубль упал бы так же, только дороже.
    """


def _detail(exc):
    """Сообщение движка без служебных идентификаторов.

    Сервис пишет «Too long text, internal_id=…&request_id=…» — эти хвосты в логе
    стадии только мешают: человек видит «internal_id» и гадает, что это.
    """
    raw = str(getattr(exc, "reason", "") or exc)
    i = raw.find("{")
    if i >= 0:
        try:
            data = json.loads(raw[i:])
        except ValueError:
            data = None
        if isinstance(data, dict):
            err = data.get("error")
            if isinstance(err, dict) and err.get("message"):
                raw = str(err["message"])
    for tail in ("internal_id=", "request_id=", "client_request_id=", "client_id="):
        if tail in raw:
            raw = raw.split(tail)[0]
    return raw.strip()[:300]


def _explain(exc, voice, model, role):
    """Ошибка движка → текст, по которому видно, что делать."""
    msg = _detail(exc)
    low = msg.lower()
    if "too long text" in low:
        return (f"сцена длиннее {CHAR_LIMIT} символов, а движок режет по этому пределу — "
                "разбейте текст сцены в narration.json")
    if "speed is not supported" in low:
        return (f"голос «{voice}» ({model}) не принимает темп: у премиум-голосов движок "
                "держит только 1.0, темп подберёт укладка в хронометраж")
    if "role" in low and "not supported" in low:
        return f"у голоса «{voice}» нет роли «{role}»"
    if "feature permission denied" in low:
        return (f"голос «{voice}» недоступен в модели {model}: премиум-голоса живут "
                f"в модели {LIVETTS}, обычные — в {GENERAL}")
    return f"движок отказал: {msg}"


def _hints(voice, role, speed, model):
    """hints: список объектов, в каждом — ровно одно поле."""
    hints = [{"voice": voice}]
    if role:
        hints.append({"role": role})
    # Темп у livetts не принимается: лучше не отправлять и не получать 400,
    # чем ронять стадию на ровном месте.
    if model == GENERAL and speed and abs(speed - 1.0) > 1e-6:
        hints.append({"speed": speed})
    return hints


def _pcm_of(chunk, state):
    """PCM из куска потока. Первый кусок — WAV, дальше — чистые сэмплы.

    state — словарь, в котором держим частоту дискретизации: она нужна для
    заголовка, а в следующих кусках её уже нет.
    """
    if chunk[:4] == b"RIFF" and chunk[8:12] == b"WAVE":
        if len(chunk) < 44:
            raise SpeechKitError("движок прислал обрывок WAV-заголовка")
        state["sr"] = state["sr"] or int.from_bytes(chunk[24:28], "little")
        pos, raw = 12, b""
        while pos + 8 <= len(chunk):
            cid = chunk[pos:pos + 4]
            size = int.from_bytes(chunk[pos + 4:pos + 8], "little")
            if cid == b"data":
                raw = chunk[pos + 8:pos + 8 + size]
                break
            pos += 8 + size + (size & 1)
        if not raw:
            raw = chunk[44:]
        return raw
    return chunk


def speak(text, voice, model=GENERAL, role=None, speed=1.0, key=None, timeout=300):
    """Озвучить текст → (wav-байты, тайминги слов).

    Тайминги — список (слово, начало сек, конец сек) в масштабе всей записи.
    У general они приходят от движка; у livetts их нет, и тайминги будут пустыми:
    нарезку реплик тогда делает распознавание речи.
    """
    key = key or env_key("YANDEX_API_KEY")
    if model not in MODELS:
        raise SpeechKitError(f"модель движка «{model}» неизвестна: {', '.join(MODELS)}")
    if model == LIVETTS and speed and abs(speed - 1.0) > 1e-6:
        # Темп у премиум-голоса движок не принимает: молча оставляем 1.0, а не
        # роняем стадию. Темп подберёт укладка в хронометраж.
        speed = 1.0
    body = {"text": text, "hints": _hints(voice, role, speed, model), "unsafeMode": True}
    if model != GENERAL:
        body["model"] = model

    req_headers = {"Authorization": f"Api-Key {key}", "Content-Type": "application/json"}
    try:
        with http(URL, body, req_headers, timeout) as r:
            raw = r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        reason = _explain(exc, voice, model, role)
        if exc.code == 429 or exc.code >= 500:
            # Временная беда движка (лимит запросов, шаттл сервиса): повторять надо,
            # поэтому оставляем тип HTTPError — по нему вызывающий делает паузу.
            raise urllib.error.HTTPError(exc.url, exc.code, reason, exc.headers, None) from exc
        raise SpeechKitError(reason) from exc
    except urllib.error.URLError as exc:
        raise SpeechKitError(f"движок недоступен: {exc.reason}") from exc

    state = {"sr": 0}
    pcm, words = bytearray(), []
    for line in raw.splitlines():
        if not line.strip():
            continue
        try:
            result = (json.loads(line) or {}).get("result") or {}
        except ValueError:
            continue
        chunk = (result.get("audioChunk") or {}).get("data")
        if chunk:
            pcm += _pcm_of(base64.b64decode(chunk), state)
        for w in result.get("wordTimings") or []:
            try:
                start = int(w.get("startMs", 0)) / 1000.0
                length = int(w.get("lengthMs", 0)) / 1000.0
            except (TypeError, ValueError):
                continue
            # Метка ударения приходит прямо в слове («в+ы», «+он»): движок отдаёт
            # слово так, как оно лежит в размеченном тексте. Для нарезки метка не
            # нужна, а без её снятия слово распадается на два («в» и «ы»).
            words.append((str(w.get("word", "")).replace("+", "").strip(), start, start + length))
    if not pcm or not state["sr"]:
        raise SpeechKitError("движок не вернул звук")

    buf = io.BytesIO()
    w = wave.open(buf, "wb")
    w.setnchannels(1)
    w.setsampwidth(2)
    w.setframerate(state["sr"])
    w.writeframes(bytes(pcm[:len(pcm) - len(pcm) % 2]))
    w.close()
    return buf.getvalue(), words


def main():
    ap = argparse.ArgumentParser(description="проба синтеза SpeechKit")
    ap.add_argument("--text", required=True)
    ap.add_argument("--voice", default="sofia")
    ap.add_argument("--model", default=GENERAL, choices=MODELS)
    ap.add_argument("--role", default=None)
    ap.add_argument("--speed", type=float, default=1.0)
    ap.add_argument("--out", default=None, help="куда положить wav")
    a = ap.parse_args()
    try:
        data, words = speak(a.text, a.voice, a.model, a.role, a.speed)
    except SpeechKitError as exc:
        raise SystemExit(str(exc))
    if a.out:
        with open(a.out, "wb") as f:
            f.write(data)
    print(f"{a.model} · {a.voice}"
          f"{' · ' + a.role if a.role else ''} · {len(data)} байт · слов с таймингами: {len(words)}"
          f" → {a.out or 'в память'}")


if __name__ == "__main__":
    main()
