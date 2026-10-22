"""Shared pipeline utilities: HTTP without the system proxy, ffmpeg/ffprobe, narration.json format.

narration.json is the single source of narrator timings:
{
  "duration": 112,                      # video length, seconds
  "voice": {"provider": "gpt-audio", "voice": "ash", "style": "..."},
  "lines": [{"start": 0.5, "text": "..."}, ...]
}
A line's window = up to the next line's start minus the gap; the last one runs to the end of the video.
"""

from array import array
import json
import math
import os
import subprocess
import urllib.error
import urllib.request
import wave

GAP = 0.35        # gap before the next line, seconds
TAIL = 0.6        # margin at the end of the video after the last line, seconds
FADE_IN = 0.015   # silence → speech edge, seconds
FADE_OUT = 0.05   # speech → silence edge, seconds
# Рампы шины (voice_bus.py) — короче: хвост реверберации теперь общий, и рамповать каждый
# кусок по 15 мс незачем. audio_qc.py меряет край относительно участка ЗА рампой, поэтому
# значения должны быть оттуда же.
BUS_FADE_IN = 0.010
BUS_FADE_OUT = 0.030


def http(url, body=None, headers=None, timeout=300):
    """POST/GET bypassing HTTPS_PROXY: on the laptop it drops connections (Hub repo rule).

    Ошибку хаба показываем с его собственным пояснением: «Голос sohee не найден.
    Доступные голоса: …». Без него в логе стадии остаётся «HTTP Error 400: Bad
    Request», и человек гадает минуту, что именно он сломал. Тип исключения прежний
    (HTTPError) — по нему отсеиваются 429 и 5xx для повтора.
    """
    data = json.dumps(body).encode() if isinstance(body, (dict, list)) else body
    req = urllib.request.Request(url, data=data, headers=headers or {}, method="POST" if data is not None else "GET")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        return opener.open(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            raw = e.read().decode("utf-8", "replace")
            try:
                detail = json.loads(raw).get("detail") or raw
            except ValueError:
                detail = raw
        except Exception:
            pass
        detail = (detail or "").strip()[:400]
        if detail:
            raise urllib.error.HTTPError(e.url, e.code, f"{e.reason}: {detail}",
                                         e.headers, None) from e
        raise


def run(args):
    subprocess.run(args, check=True)


def ffmpeg(*args):
    run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", *args])


def duration(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path],
        capture_output=True, text=True, check=True,
    ).stdout
    return float(out)


def load_narration(path):
    with open(path) as f:
        n = json.load(f)
    lines = n["lines"]
    for i, ln in enumerate(lines):
        nxt = lines[i + 1]["start"] - GAP if i + 1 < len(lines) else n["duration"] - TAIL
        ln["window"] = round(nxt - ln["start"], 3)
    return n


def env_key(*names):
    for name in names:
        if os.environ.get(name):
            return os.environ[name]
    raise SystemExit(f"missing key in env: {' or '.join(names)}")


def clip_name(i):
    return f"line_{i:02d}.wav"


def scene_groups(n):
    """Реплики по сценам, в порядке следования. Без поля `scene` — одна группа.

    Живёт здесь, а не в nd_voice.py: тем же порядком собирают сцены и произношение
    (pronounce.py), иначе отчёт об ударениях считался бы по одним сценам, а озвучка
    шла бы по другим.
    """
    groups, seen = [], {}
    for i, ln in enumerate(n["lines"]):
        key = ln.get("scene")
        if key is None or key not in seen:
            seen[key] = len(groups)
            groups.append([])
        groups[seen[key]].append(i)
    return groups


def scene_name(i, prev=False):
    return f"scene_{i:02d}.prev.wav" if prev else f"scene_{i:02d}.wav"


# ---- аудио в памяти: stdlib, потому что в образе нет ни numpy, ни scipy ----

def wav_format(data):
    """(частота, каналы) из байтов WAV: RIFF/WAVE, нужен только блок fmt."""
    if data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise ValueError("not a RIFF/WAVE")
    pos = 12
    while pos + 8 <= len(data):
        cid, size = data[pos:pos + 4], int.from_bytes(data[pos + 4:pos + 8], "little")
        if cid == b"fmt ":
            return (int.from_bytes(data[pos + 12:pos + 14], "little"),
                    int.from_bytes(data[pos + 10:pos + 12], "little"))
        pos += 8 + size + (size & 1)
    raise ValueError("no fmt chunk")


def wav_sr(data):
    return wav_format(data)[0]


def wav_samples(data):
    """Сэмплы int16 из WAV-байтов (PCM 16 bit). Стерео склеивается средним."""
    sr, ch = wav_format(data)
    pos, raw = 12, None
    while pos + 8 <= len(data):
        cid, size = data[pos:pos + 4], int.from_bytes(data[pos + 4:pos + 8], "little")
        if cid == b"data":
            raw = data[pos + 8:pos + 8 + size]
            break
        pos += 8 + size + (size & 1)
    if raw is None:
        raise ValueError("no data chunk")
    s = array("h")
    s.frombytes(raw[:len(raw) - len(raw) % 2])
    if ch > 1:
        s = array("h", [sum(s[i:i + ch]) // ch for i in range(0, len(s) - ch + 1, ch)])
    return s, sr


def read_wav(path):
    """(сэмплы int16, частота, каналы). Каналы склеиваются средним."""
    w = wave.open(path)
    n, ch, sr = w.getnframes(), w.getnchannels(), w.getframerate()
    s = array("h", w.readframes(n))
    w.close()
    if ch > 1:
        s = array("h", [sum(s[i:i + ch]) // ch for i in range(0, len(s) - ch + 1, ch)])
    return s, sr, ch


def write_wav(path, samples, sr, ch=1):
    w = wave.open(path, "wb")
    w.setnchannels(ch)
    w.setsampwidth(2)
    w.setframerate(sr)
    w.writeframes(samples if isinstance(samples, array) else array("h", samples))
    w.close()


ENV_WIN = 0.005      # окно RMS для поиска пауз, секунды
ENV_REL = 0.02       # тишина — это уровень ниже 2% от пика за окно


def envelope(samples, sr, win=ENV_WIN):
    """RMS по окнам: список уровней. Один проход, без срезов на весь файл."""
    n = max(1, int(win * sr))
    out = []
    for i in range(0, len(samples) - n + 1, n):
        chunk = samples[i:i + n]
        out.append(math.sqrt(sum(x * x for x in chunk) / n) / 32768.0)
    if not out and samples:
        out = [abs(samples[0]) / 32768.0]
    return out


def silence_spans(env, sr, minlen=0.10, rel=ENV_REL):
    """Паузы как (начало, конец) в секундах: тихо относительно пика сигнала."""
    if not env:
        return []
    peak = max(env) or 1.0
    floor = peak * rel
    step = ENV_WIN
    out, i = [], 0
    while i < len(env):
        if env[i] <= floor:
            j = i
            while j < len(env) and env[j] <= floor:
                j += 1
            if (j - i) * step >= minlen:
                out.append((round(i * step, 3), round(j * step, 3)))
            i = j
        else:
            i += 1
    return out


def trim_silence(samples, sr, rel=ENV_REL):
    """Срезать тишину с обоих краёв, оставив один шаг окна."""
    env = envelope(samples, sr)
    if not env:
        return samples
    floor = (max(env) or 1.0) * rel
    keep = [i for i, v in enumerate(env) if v > floor]
    if not keep:
        return samples
    lo = int(keep[0] * ENV_WIN * sr)
    hi = min(len(samples), int((keep[-1] + 1) * ENV_WIN * sr))
    return samples[lo:hi]


def edge_fades(dur=None, fin=FADE_IN, fout=FADE_OUT, start=0.0):
    """Fade-in/fade-out filters that kill the click at a clip edge.

    Every TTS engine cuts its clips hard: the file stops mid-waveform and some open on a
    step. Measured on projects/latin-tutorial, the last 2 ms of a line sat 0.6 dB below
    the level next to it — a full-level step to silence, one sample wide, i.e. a click,
    loudest in the gap between lines. A ramp lands tens of dB lower. `audio_qc.py`
    measures it and fails the build.

    dur/start are in seconds from the head of the stream being filtered: on a line
    already padded to the video length, start is the line's own start.
    ffmpeg 6.1 rejects a negative `afade` st, so a tail without a known duration is
    faded on a reversed stream (the same trick as fit_narration.TRIM).
    """
    out = []
    if fin:
        out.append(f"afade=t=in:st={start:g}:d={fin:g}")
    if fout:
        out.append(f"afade=t=out:st={dur - fout:g}:d={fout:g}" if dur is not None
                   else f"areverse,afade=t=in:st=0:d={fout:g},areverse")
    return ",".join(out)


# ---- NeuralDeep API (api.neuraldeep.ru): everything the pipeline takes from the hub ----

def nd_base():
    return os.environ.get("ND_API_BASE", "https://api.neuraldeep.ru").rstrip("/")


def nd_auth():
    return {"Authorization": f"Bearer {env_key('ND_API_KEY')}"}


def _retry(call, attempts=4):
    """Retry hub 429 and 5xx with backoff: a one-off gateway 503 shouldn't kill the whole pipeline.
    The gateway does hand out whole minutes of 502 Bad Gateway on /v1/audio/speech — retry, don't
    assume the payload is wrong."""
    import time
    import urllib.error
    for i in range(attempts):
        try:
            return call()
        except urllib.error.HTTPError as e:
            if i == attempts - 1 or not (e.code == 429 or e.code >= 500):
                raise
            wait = float(e.headers.get("Retry-After") or 0) or 3 * (i + 1)
            print(f"  HTTP {e.code}, retrying in {wait:.0f} s")
            time.sleep(min(wait, 60))


def retry(call, attempts=4):
    """Public wrapper: the same backoff for callers that post audio or other binary bodies."""
    return _retry(call, attempts)


def nd_json(path, body=None, timeout=300):
    hdr = {**nd_auth(), "Content-Type": "application/json"}

    def call():
        with http(f"{nd_base()}{path}", body, hdr, timeout) as r:
            return json.load(r)
    return _retry(call)


def nd_chat(messages, model=None, timeout=600, **kw):
    """Hub chat. For JSON and short structured answers use -noreason aliases only:
    base qwen models always think and return empty content on short answers."""
    body = {"model": model or os.environ.get("ND_CHAT_MODEL", "qwen3.8-27b-noreason"),
            "messages": messages, **kw}
    return nd_json("/v1/chat/completions", body, timeout)["choices"][0]["message"]["content"] or ""


def multipart(fields, files):
    """fields: {name: str}; files: {name: (filename, bytes, mime)} → (body, content_type)."""
    import uuid
    b = uuid.uuid4().hex
    out = []
    for k, v in fields.items():
        out.append(f"--{b}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode())
    for k, (fn, data, mime) in files.items():
        out.append(f"--{b}\r\nContent-Disposition: form-data; name=\"{k}\"; filename=\"{fn}\"\r\n"
                   f"Content-Type: {mime}\r\n\r\n".encode() + data + b"\r\n")
    out.append(f"--{b}--\r\n".encode())
    return b"".join(out), f"multipart/form-data; boundary={b}"


def nd_multipart(path, fields, files, timeout=300):
    body, ctype = multipart(fields, files)

    def call():
        with http(f"{nd_base()}{path}", body, {**nd_auth(), "Content-Type": ctype}, timeout) as r:
            return json.load(r)
    return _retry(call)


def nd_speak(text, voice="ryan", language="English", style=None, speed=1.0,
             model=None, seed=7, tries=5):
    """Синтез речи хаба → (wav-байты, движок). Движок выбирается по языку: Russian → ESpeech
    (F5 + RUAccent, ударения и омографы по контексту), остальное → Qwen3-TTS."""
    import urllib.error
    if model is None:
        model = "espeech-tts" if language == "Russian" else "qwen3-tts"
    body = {"model": model, "input": text, "voice": voice, "language": language,
            "response_format": "wav", "speed": speed, "seed": seed}
    if style:
        body["instructions"] = style
    key = env_key("ND_API_KEY")
    base = os.environ.get("ND_API_BASE", "https://api.neuraldeep.ru")

    def call():
        with http(f"{base}/v1/audio/speech", body,
                  {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}) as r:
            data, engine = r.read(), r.headers.get("X-TTS-Engine") or r.headers.get("x-tts-engine") or "?"
        if data[:4] != b"RIFF":
            raise urllib.error.URLError(f"not audio: {data[:120]!r}")
        return data, engine

    return retry(call, tries)


def nd_transcribe(audio, model="whisper-1", filename="clip.wav", timeout=600):
    """Распознавание речи хаба.

    Отправляются ТОЛЬКО `file` и `model`. Любое другое поле формы — `response_format`,
    `language`, `timestamp_granularities` — даёт 400 «Invalid model name passed in
    model=whisper-1», хотя модель в списке есть: сообщение врёт, и проверка речи падает
    целиком. Ответ по умолчанию уже содержит segments[].words[] с таймингами слов,
    поэтому ничего запрашивать не нужно.

    Модели: whisper-1 — единственная с пригодными таймингами слов; gigaam-v3 таймингов
    не отдаёт вовсе (годна как второй судья текста); whisper-podlodka-turbo дробит слова
    на слоги.
    """
    res = nd_multipart("/v1/audio/transcriptions", {"model": model},
                       {"file": (filename, audio, "audio/wav")}, timeout=timeout)
    return {
        "text": res.get("text", ""),
        "words": [(w.get("word", "").strip(), w.get("start"), w.get("end"))
                  for s in res.get("segments", []) for w in (s.get("words") or [])],
        "duration": res.get("duration"),
    }


def extract_json(text):
    """The model sometimes wraps JSON in ```json ... ``` or puts a word before it."""
    s = text.strip()
    if s.startswith("```"):
        s = s.split("\n", 1)[1].rsplit("```", 1)[0]
    i = min([p for p in (s.find("{"), s.find("[")) if p >= 0], default=0)
    return json.loads(s[i:])
