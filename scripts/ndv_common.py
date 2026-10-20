"""Shared pipeline utilities: HTTP without the system proxy, ffmpeg/ffprobe, narration.json format.

narration.json is the single source of narrator timings:
{
  "duration": 112,                      # video length, seconds
  "voice": {"provider": "gpt-audio", "voice": "ash", "style": "..."},
  "lines": [{"start": 0.5, "text": "..."}, ...]
}
A line's window = up to the next line's start minus the gap; the last one runs to the end of the video.
"""

import json
import os
import subprocess
import urllib.request

GAP = 0.35        # gap before the next line, seconds
TAIL = 0.6        # margin at the end of the video after the last line, seconds
FADE_IN = 0.015   # silence → speech edge, seconds
FADE_OUT = 0.05   # speech → silence edge, seconds


def http(url, body=None, headers=None, timeout=300):
    """POST/GET bypassing HTTPS_PROXY: on the laptop it drops connections (Hub repo rule)."""
    data = json.dumps(body).encode() if isinstance(body, (dict, list)) else body
    req = urllib.request.Request(url, data=data, headers=headers or {}, method="POST" if data is not None else "GET")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    return opener.open(req, timeout=timeout)


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


def extract_json(text):
    """The model sometimes wraps JSON in ```json ... ``` or puts a word before it."""
    s = text.strip()
    if s.startswith("```"):
        s = s.split("\n", 1)[1].rsplit("```", 1)[0]
    i = min([p for p in (s.find("{"), s.find("[")) if p >= 0], default=0)
    return json.loads(s[i:])
