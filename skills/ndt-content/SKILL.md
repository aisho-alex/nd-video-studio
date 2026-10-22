---
name: ndt-content
description: Content made entirely on NeuralDeep models (api.neuraldeep.ru), no external APIs: research, storyboard, generated illustrations, voice, speech verification, word-level subtitles, frame review, music on your own GPU. Triggers: "make a video on our models", "NDT only", "content through the hub", "without OpenRouter", "video from a brief", "illustrated video".
---

# ndt-content: from brief to MP4 on hub models

One key, `ND_API_KEY` (your own hub `sk-`), for everything. Frame building, sound and rendering
are the same as in the [`nd-video`](../nd-video/SKILL.md) skill: its "HyperFrames techniques",
"Voice: pitfalls" and "Music: pitfalls" sections apply here too. This skill changes the sources:
instead of OpenRouter (gpt-audio, Lyria) and Gemini, only the hub and your own GPU.

## What comes from where

| step | hub model | endpoint | script |
|---|---|---|---|
| research | Search API (web, tg) | `/v1/search/web`, `/v1/search/tg` | `nd_research.py` |
| storyboard | `qwen3.8-27b-noreason` (or `kimi-k2.6`, `gemma-4-31b-noreason`) | `/v1/chat/completions` | `nd_storyboard.py` |
| script | same, but only when you skip the storyboard | `/v1/chat/completions` | `nd_script.py` |
| voice | `qwen3-tts` (8 voices, style as text), Russian via ESpeech + RUAccent | `/v1/audio/speech` | `nd_voice.py` |
| timing | from the real voiceover length | — | `nd_retime.py`, `fit_narration.py` |
| speech check + words | `whisper-1` | `/v1/audio/transcriptions` | `nd_align.py` |
| images | FLUX, background removal, upscale | `/v1/images/*` | `nd_images.py` |
| composition | local, from narration + art manifest | — | `nd_compose.py` |
| frame review | `qwen3.6-35b-a3b-noreason` (vision) | `/v1/chat/completions` | `nd_review.py` |
| music | self-hosted ACE-Step 1.5 on your own GPU (the hub has no music model) | box REST | `music_acestep.py`, [docs/acestep.md](../../docs/acestep.md) |
| voice processing, mixing | local, ffmpeg | — | `voice_fx.py`, `mix.py` |

For JSON and short structured answers use only the `-noreason` aliases: base qwen models always
think and return an empty `content` on short answers.

Check that everything above is up before a run: `python3 scripts/nd_probe.py` pings every chat
model from `/v1/models` and walks TTS → whisper, FLUX → vision, quota and search
(`--no-images` saves image quota).

## Workflow

Run everything from the project folder, `S=../../scripts`, key in env (never print or commit it).
Default length is **2–4 minutes** (8–14 scenes), not 60 s: an explainer that only states facts is
a slideshow with a voice. A short clip is `--seconds 60` and nothing else changes.

```bash
mkdir -p projects/<slug> && cd projects/<slug>
$EDITOR brief.md                                   # topic, audience, tone, length

python3 $S/nd_research.py sources.md "<query 1>" "<query 2>" [--tg "<query>"]
#   Поиск идёт через гейтвей Morpheus ($SEARCH_GATEWAY_URL), одним пакетным
#   заданием: берёт отложенный тариф (в 16 раз дешевле) — спешить тут некуда,
#   результат всё равно ждёт до сториборда. Гейтвей недоступен или исчерпал
#   месячный бюджет — откат на хаб Search API автоматически, качество падает
#   (гугл вместо русского индекса), но ресёрч не теряется.
python3 $S/nd_storyboard.py brief.md storyboard.json --sources sources.md --lang ru --seconds 180
#   → arc (hook → problem → solution → how → proof → takeaway), scenes with lines, facts
#     (source or estimate) and a visual prompt + layout each; read them, fix facts by hand.
#     The script does three repair passes on its own: facts in the wrong language, a plan thinner
#     than the target (`--no-grow` to skip), a plan longer than it. The printed "~N s of narration"
#     is the estimate nd_script.py lays out with.
python3 $S/nd_script.py brief.md narration.json --from storyboard.json
#   → the same plan on a timeline (voice length); no second generation

python3 $S/tts_hub.py narration.json audio/raw --voice aiden   # ru: language comes from narration.json
#   ru → espeech-tts (ударения), en → qwen3-tts; prints the X-TTS-Engine that worked
python3 $S/nd_retime.py narration.json audio/raw --pause 0.45 --scene-pause 0.8
python3 $S/fit_narration.py narration.json audio/raw audio/fit
python3 $S/nd_align.py narration.json audio/fit words.json     # BAD → re-voice: tts_hub.py --only N

python3 $S/nd_images.py assets --from narration.json --jobs 3
#   → one picture per scene in ONE style + assets/art.json (what the composer will read).
#   A redraw of one scene later: nd_images.py assets --from narration.json --scenes 4 --force.
#   Budget it: the free tier allows 10 images a day, and a hero scene costs two (cut-out).

python3 $S/nd_compose.py --watermark "<WORD>" --brand "<title>"
#   → index.html: scene layout from the storyboard, pictures where art.json has them,
#     word-level subtitles from words.json, on-screen source/estimate marks
npx hyperframes check && npx hyperframes snapshot --at <t1>,<t2>,...
python3 $S/nd_review.py snapshots/contact-sheet-*.jpg --brief "horizontal 16:9 explainer"

bash $S/build_audio.sh . trailer                  # fx + music ducking → audio/mix.wav
npx hyperframes render --output renders/<slug>.mp4
```

`npx`, `curl`, `gh` run without the system proxy:
`env -u HTTPS_PROXY -u HTTP_PROXY -u https_proxy -u http_proxy -u ALL_PROXY <cmd>`.

`nd_script.py brief.md narration.json --sources sources.md --seconds N` still works and is the
short way in: the model plans and writes the scenes itself, but they carry no `visual` and no
`facts`, so the video is text-only. Use it for quick drafts, the storyboard for anything published.

## Illustrations

The picture is half of an explainer: without it the render is a narrated slideshow, which is what
the first cuts looked like. `nd_images.py --from narration.json` is the only step needed:

- it reads `scenes[].visual.prompt` from the storyboard, so **the art is planned with the script**,
  not after it;
- `scenes[].visual.layout` picks the picture kind: `full`/`quote`/`split` → wide 16:9 background,
  `hero` → a cut-out subject (`--remove-bg`) for the text column, `data` → a small accent;
- every prompt gets the **same style sentence** (the storyboard's `style.art`, or `--style` /
  `--style-file`) plus a no-text clause. One style for the whole video: six different looks read as
  six different clips stitched together;
- results are cached by `sha1(prompt + style + aspect)`, so a rerun of the same plan draws nothing.
  `--force` redraws. `--jobs 3` keeps three generations in flight;
- `assets/art.json` is the manifest: which picture each scene really has. **A refused quota or a
  failed task is not fatal** — `nd_compose.py` lays that scene out as text and the render finishes.

Pick a style that leaves room for type: dark low-contrast backgrounds, no busy detail in the lower
third. Ask for one concrete subject per picture, never a collage, never a mood board — a montage of
unrelated objects reads as decoration, not explanation.

## Layouts

`nd_compose.py` renders five layouts, chosen by the storyboard per scene:

| layout | picture | text |
|---|---|---|
| `full` | full-bleed, slow Ken Burns drift | heading, kicker, facts, key idea in the lower third on a scrim |
| `hero` | cut-out subject on the right, slow float | heading, facts, key idea on the left |
| `split` | framed picture on the right | facts with source marks on the left |
| `data` | small accent | the facts are the picture: big rows, centred |
| `quote` | background sunk to 25% | one sentence in a serif face + source |

Facts always show their certainty: `источник: arxiv.org` or `оценка`. The composer drops every
element a scene does not have (no key idea → no key-idea tween), so the GSAP console stays clean.

One scene may need a hand-written body — a table of declensions, a formula. Pass
`--overrides overrides.json` with `{"cards": {"3": "<html>"}}`: that scene's inner content replaces
the generated one, the timing and the subtitles stay. Keep overrides for real tables only, they
are the one thing the layout engine cannot derive from the narration.

## Order matters: storyboard, then voice, then pictures, then HTML

The plan fixes the beats, the lines and the picture before anything is expensive: a picture is one
unit of quota, a re-voiced line is one character of TTS, a re-render is minutes. Move a scene in the
storyboard, not in the HTML.

TTS pace can't be guessed from text: `ryan` speaks ~1.7 words/s while the script draft assumes
2.1. So the starts from `nd_script.py` are a draft; `nd_retime.py` sets the real ones from clip
lengths. Lay out the composition after retime, otherwise the animation drifts from the voice. If
the HTML is already laid out to fixed starts (like the Jev video), skip retime: there
`fit_narration.py` squeezes lines into their windows, and long ones get shortened in the text.

## Pitfalls caught on the demo

- **The hub gateway throws whole minutes of `502 Bad Gateway` on `/v1/audio/speech`** and then
  serves the same request fine. It is not the payload: all voices, all languages and the minimal
  documented curl failed at once, and everything worked minutes later. `tts_hub.py` retries 429/5xx
  with backoff — if a run dies with a 502, re-run it, don't start rewriting the text.
- **`model` in the TTS body**: `espeech-tts` for Russian (F5 + RUAccent), `qwen3-tts` for the rest.
  `tts_hub.py` picks it from the language; the answer names the engine in the `X-TTS-Engine` header,
  which the script prints. Quota: `GET /v1/audio/quota` (characters of input text), voices:
  `GET /v1/audio/voices`, cap 5000 characters per call.
- **`/v1/audio/transcriptions` принимает только `file` и `model`.** Любое другое поле формы
  (`response_format`, `language`, `timestamp_granularities`) даёт
  `400 Invalid model name passed in model=whisper-1`, хотя модель в списке есть: сообщение
  врёт. Ответ по умолчанию уже содержит `segments[].words[]`, поэтому ничего запрашивать
  не нужно. Тайминги слов даёт только `whisper-1`: `gigaam-v3` не отдаёт их вовсе,
  `whisper-podlodka-turbo` дробит слова на слоги.
- **ESpeech misreads Latin inside Russian text.** Caught by `nd_align.py` on a real run: "в запрос
  к LLM" → «в запрос КМ», "RAG" → «Раджи». Словарь `voice.pronounce` чинит это без правки текста:
  "LLM" → "эл-эл-эм", и whisper распознаёт обратно как "LLM", так что субтитры остаются
  правильными. Переписывание реплики тоже работает, но стоит каждой строке отдельной правки.
- **Похожесть текста — плохой критерий «слово не потерялось».** При `ratio` 0.86 строка
  «Так у LLM появляются лапки» проходила, а на слух было «Так у ЛАЭМ появляются лапки».
  `nd_align.py` сверяет пословно и требует, чтобы латинское слово текста выжило в
  распознавании (своим написанием или речевой формой).
- **Ударения ставит RUAccent, а не рукописный список.** «а+гент» — не «аг+ент», и это уже
  другое слово; «Большая языковая модель» — `больш+ая`, а не `б+ольшая». Знак `+` ставится
  **перед ударной гласной**: перед согласной движок читает слово иначе (`за+мок` → «заумок»).
  Метка ставится **во всех словах**: как только `+` есть в тексте, авто-акцентиатор хаба не
  вмешивается, и один отмеченный омограф оставляет весь ролик читать ударения наугад
  («вопрос» → «вОпрос»). Чьи именно формы уходят — флаг `--accent`: по умолчанию `dictionary`
  (только формы из словаря RUAccent, без нейросетевых догадок про бренды), `all` — всё, что
  вернул `process_all`, `omographs` — прежнее поведение, оставлено как откат. Формы проверяет
  `tts_probe.py --stress`.
- **Ударение нельзя проверить автоматически.** Whisper его не слышит; F0 умеет лишь показать,
  что знак встал на запрошенный слог. Эталон — словарь или ухо человека.
- **TTS swallows and mixes up words**, easy to miss by ear: "reuse" came out as "ray use".
  `nd_align.py` compares the transcript with the text and exits with code 2 and an `--only N` list.
  Fix it by rephrasing the line, not by rerunning the same text.
- **A one-off gateway 503** can hit any endpoint. `ndv_common._retry` retries 429 and 5xx
  with a pause (`Retry-After` if present).
- **Images are square 1312×1312** by default. `--aspect 16:9` asks for a wide frame (the composer
  also sets `object-fit: cover`, so a square that came back anyway is cropped, not stretched).
- **Image models invent text.** FLUX draws plausible-looking garbage letters if the prompt hints at
  a chart, a label or a sign. The prompts say `no text` explicitly; if a frame still shows fake
  lettering, redraw it with a plainer prompt (`--force` on that scene) or crop the picture in.
- **A picture is not an illustration if it doesn't match the sentence.** The prompt comes from the
  storyboard, so fix it there: `nd_review.py` flags a frame whose subject is not the scene's topic.
- **Vision review** reads the contact-sheet grid imprecisely (mixes up timestamps), but it does
  catch problems: small text, overlaps, empty frames. It is a second pair of eyes, not a
  replacement for looking at the frames yourself.
- **Quotas**: search (web costs more than tg), images (`/v1/images/quota`), TTS in characters,
  all per the key's plan. A quota refusal is a 429 with `Retry-After`.

## Music

The hub has no music model. Self-hosted ACE-Step 1.5 XL (MIT) runs on your own GPU box
(`<gpu-box>` is its ssh alias); setup and card limits are in [docs/acestep.md](../../docs/acestep.md).
To play by hand: `ssh -N -L 18001:127.0.0.1:8001 <gpu-box> &` and
`python3 scripts/music_studio.py` → http://127.0.0.1:8765 (presets, up to 2 variants, history;
the "to project" button puts the track into `projects/<slug>/audio/music.mp3` and keeps the
previous one next to it). Reference: XL turbo 4B + LM 1.7B on a single A4500 20 GB, two 60 s
variants in 11 s. While the box is down, build the video without music: `mix.py` with an empty
track or `build_audio.sh` without `audio/music.mp3`.

## Syncing to music

From [vibecoder-anthem](https://github.com/rocketmandrey/vibecoder-anthem) (the "ЖГИ ТОКЕНЫ"
clip): cuts, camera hits and karaoke land on the **track's real beats**, not on a BPM grid.

```bash
uv run --no-project --with librosa python3 $S/beat_map.py audio/music.mp3 beats.json --bpm 174
```

Paste `beats.json` into the page as `window.BEATS = {...}`, next to `tools/beat-sync.js`:
`beat.snap(t)` / `beat.nextBar(t)` move a rough time onto a beat or downbeat,
`beat.hits(from, to, min)` returns hits for shakes and flashes, `beat.warpFrom(anchors)` moves a
finished animation onto a new version of the track: `[new, old]` anchors on the same events,
linear in between. Word-level timings for narration or vocals come from `nd_align.py`.

- The tempo detector gets octave errors: the Jev track from Lyria was requested at 110 and came
  out as 73.8 (i.e. 147.6/2). The `--bpm` hint helps, but always check the number.
- ACE-Step holds the requested tempo: DnB at 174 gave 172.3 BPM, beat jitter ±5 ms, no drift.
- Verify sync with a contact sheet on the hits: `snapshot --at` at the times from `hits`.

## Honesty

- The script is written only from `sources.md`; estimates are marked in the text ("likely", "est.")
  and on screen: `nd_compose.py` puts a `source: <host>` or `estimate` tag on every fact.
- Illustrations are illustration, not evidence: never let a generated picture imply a measurement
  the sources do not contain. Charts of invented numbers are the one thing to delete on sight.
- In the post about the video, state exactly what was used. If even one step bypassed the hub
  (e.g. music from Lyria), don't write "made entirely on neuraldeep.ru models".

## Example

A 31 s demo about KV cache went through the whole pipeline on the hub (research → script →
`ryan` → retime → whisper check with one caught TTS error → image → review).

The illustrated pipeline (storyboard → art → `nd_compose.py`) was checked the same way: five
layouts, `hyperframes check` clean, `snapshot` looked at frame by frame.
