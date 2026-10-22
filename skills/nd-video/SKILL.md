---
name: nd-video
description: Illustrated explainer videos with HyperFrames (HTML → MP4) plus generated pictures, narrator, music and mixing on NeuralDeep infrastructure. Triggers: "make a video", "explainer", "explain visually", "add voiceover", "add illustrations", "add music", "re-render", "change the voice / reverb".
---

# nd-video: from idea to MP4

The pipeline was built on the LLM vs Jev video (112 s, [`media/jev-vs-llm.mp4`](../../media/jev-vs-llm.mp4)).
Everything that worked there is here. Projects (`projects/`) are local and not tracked in git.
The HyperFrames composition contract is in `vendor/hyperframes/skills/hyperframes-core/SKILL.md`;
read it before writing the first line of HTML.

## Workflow

1. **Brief and storyboard.** One idea per scene, on an arc: hook → problem → solution → how →
   proof → takeaway. Each scene gets a question-style heading, a kicker, 3–6 narrator lines, a "key
   idea", the facts behind them (each marked `source` or `estimate`) and **the picture that will be
   on screen while it is said**. A 60 s cut of a 3-minute plan is a narrated slideshow — the main
   feedback on the first cut. Without subtitles and a route map viewers don't understand what they
   are looking at either.
2. **Pictures**, one per scene, one style for the whole video (`scripts/nd_images.py --from
   narration.json`, see the `ndt-content` skill). Not decoration: the picture has to be the scene's
   subject, in the same medium and palette as every other scene.
3. **Composition** `projects/<slug>/index.html`: `npx hyperframes init <slug> --example blank`, or
   `python3 scripts/nd_compose.py` when the generic layouts fit the plan. Proven techniques are
   below, under "Techniques".
4. **Check**: `npx hyperframes check` and `npx hyperframes snapshot --at t1,t2,...`, then look at
   the contact sheet yourself. `check` flags overlaps where the camera intentionally goes under
   overlays (subtitles, magnifier); those are false positives, trust the frames.
5. **Narration text** in `projects/<slug>/narration.json` (`start` + `text`, windows are computed).
6. **Voice** → `audio/raw/line_NN.wav` (и `audio/raw/scene_NN.wav`, из чего они нарезаны):
   - `scripts/nd_voice.py`: **одна озвучка на сцену**, не на реплику. Текст сцены идёт в
     движок целиком, whisper-1 даёт тайминги слов, `difflib` раскладывает слова по репликам,
     граница берётся в паузе. Реплики вырезаются в тишине — стык не
     щёлкает по построению. Речевой текст готовит `pronounce.py`: латиница по словарю,
     ударения по RUAccent. Параметры вызова: `--voice` (голос хаба), `--speed` (темп речи,
     1.0 — как есть; больше — быстрее, ролик укладывается в хронометраж), `--accent`
     (разметка ударений, см. «Pronunciation and stress»).
   - **Паузу на стыке реплик ставим мы**, а не берём у движка: у SpeechKit она появлялась
     не всегда (в реальной сцене два предложения из четырёх склеились без тишины), и сцена
     без паузы переозвучивалась до трёх раз, а потом стадия падала. Метка `sil<[200]>` в
     тексте сцены (см. `tts_speechkit.with_pauses`) даёт гарантированную тишину на стыке:
     работает и внутри предложения, и в длинном тексте, и для английского, и не читается
     вслух. Стык без паузы — не поломка: рез по концу слова, стык отмечается в отчёте
     (`tight`), сцена в ролик попадает.
   - **Битая сцена убирает свои клипы.** Сцена, которая не нарезалась (пустой срез, две
     реплики нашли одну паузу), больше не оставляет на диске ни пустых wav, ни клипов от
     прошлого дубля: следующий шаг иначе спотыкается о них и падает с «N/A» от ffprobe
     вместо того, чтобы сказать, какую сцену переозвучить.
   - `scripts/tts_hub.py` — старый путь «реплика на вызов». Оставлен для отладки; продом
     больше не используется.
   - `scripts/tts_speechkit.py` — второй движок озвучки, Yandex SpeechKit (REST v3,
     ключ `YANDEX_API_KEY`). Включается `--tts-provider speechkit`. Две модели:
     `general` (обычные голоса: alena, alexander, anton, dasha, ermil, filipp, jane,
     julia, kirill, lera, masha, marina, omazh, zahar) и `livetts` (премиум: irina,
     sergey, sofia, vasily). Роль голоса — `--role` (casual, formal, sales, neutral,
     friendly; набор ролей у голоса свой). Модель и голос задаёт приложение:
     `--speechkit-model general|livetts`.
   - `scripts/tts_elevenlabs.py`: ElevenLabs `eleven_multilingual_v2`. The best Russian of the three;
     `voices` lists the account's voices, `speak` voices narration.json (neighbouring lines go as context).
7. **Music** → `audio/music.mp3`:
   - `scripts/music_lyria.py`: Lyria 3 Pro, $0.08 per track.
   - `scripts/music_acestep.py`: self-hosted ACE-Step 1.5 (MIT), see `docs/acestep.md`.
8. **Sound**: `scripts/build_audio.sh projects/<slug> trailer` = fit lines into windows
   (`fit_narration.py`, темп общий на сцену) → voice bus (`voice_bus.py`: раскладка реплик
   по таймлайну и **одна** реверберация/громкость/компрессор на весь ролик) → mix with ducking
   (`mix.py`) → click check (`audio_qc.py`, fails the build). The composition has a single
   `<audio id="soundtrack" src="audio/mix.wav">` for the full length.
9. **Render**: `npx hyperframes render --output renders/<name>.mp4`; 112 s renders in ~1.5 min.
10. **Compress for sharing**: `ffmpeg -i in.mp4 -c:v libx264 -preset slow -crf 30 -tune animation
   -pix_fmt yuv420p -c:a aac -b:a 96k -movflags +faststart out.mp4`. For the Jev video this took
   51 MB down to 8.7 MB with no visible loss, small text included.

Outbound commands (`gh`, `curl`, `npx`) run without the system proxy:
`env -u HTTPS_PROXY -u HTTP_PROXY -u https_proxy -u http_proxy -u ALL_PROXY <command>`.

## Keys and where to run

| what | key | where from |
|---|---|---|
| gpt-audio, Lyria | `OPENROUTER_API_KEY` | not from a Russian IP: OpenRouter returns 403. Use a server outside Russia or `OPENROUTER_API_BASE` pointing at your egress |
| hub TTS | `ND_API_KEY` (your own `sk-`) | anywhere |
| Yandex SpeechKit | `YANDEX_API_KEY` (ключ Яндекс Клауда) | anywhere; REST v3, без gRPC |
| ACE-Step | `ACESTEP_API_KEY` (if set) | ssh tunnel to the box, the API listens on 127.0.0.1 |

Never print secrets or commit them (`.env` is in `.gitignore`).

## Voice: pitfalls

- **У SpeechKit три особенности, на которых спотыкается наивный код** (`tts_speechkit.py`):
  - голос, роль и темп читаются **только** из `hints`, и каждый объект `hints` несёт
    **одно** поле. Поле на верхнем уровне запроса молча игнорируется, а голос с ролью
    в одном объекте дают роль для голоса по умолчанию;
  - один запрос — не больше 249 символов, дальше `unsafeMode: true` и сервис сам режет
    текст по предложениям (интонация перезапускается на границе предложения — это цена
    лимита, а не ошибка);
  - звук приходит потоком, и в заголовке размер данных 0xFFFFFFFF: верь только
    собственному заголовку, который пишешь сам.
- **У премиум-голосов (livetts) нет таймингов слов и нет темпа**: движок отдаёт звук без
  `wordTimings` и принимает только `speed = 1.0`. Реплики в этом случае режутся по
  распознаванию (whisper-1), а темп подбирает укладка в хронометраж.
- **Конец слова у whisper включает хвостовую паузу**: «вопросом,» — 6.05–7.14 при тишине
  6.62–7.14. То есть конец слова это конец его паузы, а не конец звука слова, и искать
  паузу надо по этому, а не по середине ближайшей.
- **Общий движок (general) говорит медленнее премиумного** — реплики выходят длиннее, и
  укладка может не вписать их в окна (`TOO LONG`).

- **Одна озвучка на сцену, а не на реплику** — главное правило звука. Тридцать шесть отдельных
  высказываний дают тридцать шесть перезапусков комнаты, громкости и интонации, и стык реплик
  слышен как щелчок. На уровне сэмплов там ничего нет: у всех клипов первый и последний отсчёт
  равны нулю, QC показывал −44 dB. Щёлкали границы фраз, а не сигнал.
- **Обработка голоса — общая на весь ролик** (`voice_bus.py`). Реверберация, loudnorm и
  компрессор по одному разу на дорожку: иначе хвост реверберации обрезается на каждой
  реплике, а каждая выравнивается по −16 LUFS отдельно.
- **Рампы — на укладке, а не на краях кусков.** Края `audio/fit` жёсткие по построению,
  `voice_bus.py` рампит их в момент раскладки (10 мс вход, 30 мс выход). Поэтому QC по краям
  клипов идёт с `--report-only`, а гейтятся стыки в готовой дорожке.
- **Гейт края меряется против участка ПОСЛЕ рампы** (`audio_qc.edges`, `BUS_FADE_IN`), а не
  против пика онсента: иначе опорой служит взрывной согласный в начале слова и рампа в 10 мс
  читается как −6 dB. Второй раз та же ошибка дала +9 dB: опорное окно считалось от начала
  файла, а не от стыка.
- **Темп общий на сцену** (`fit_narration.py`). Реплики вырезаны из одной записи, ужимать их
  по-разному нельзя. Темп сцены = максимум по её репликам, в окне ×0.95…×1.12. Раньше темп
  ограничивался только ускорением (>= 1.0), и реплика длиннее промежутка до следующей молча
  наезжала на неё — а наложение двух фраз слышно как щелчок. Теперь это `TOO SHORT`.
- **Тишина между репликами — не ноль, а комната.** Шумовая подложка −50 dBFS (`--room-tone-db`)
  плюс `aresample=dither_method=triangular` перед 16-битным выходом. Цифровой ноль на паузе
  делает любой стык заметным; QC гейтит паузу (`--min-gap`, по умолчанию −80 dBFS).
- gpt-audio answers like an assistant ("Understood, here is…") and reads tags such as `<line>`
  aloud. Fix: a "you are a TTS engine" system prompt, bare text in the user turn, then transcribe
  the result, compare it with the text and retry (`tts_gpt_audio.py` does this).
- OpenRouter audio output works only with `stream: true`; the format is `pcm16`, 24 kHz mono.
- A line longer than its window: `fit_narration.py` speeds the whole scene up to ×1.12, beyond
  that it is audible. `TOO LONG` = shorten the text and re-voice that scene: `--scenes N`.
- ElevenLabs free tier: pcm output and library voices (native Russian speakers) are paid only
  (403 / 402), so the script takes mp3 and the premade voices work (Brian `nPczCjzI2devNBz1zQrb`
  was picked for Russian). 10 000 characters a month; a 90 s video is ~1 300. Reachable from RU IPs.
- Hub TTS for Russian: `language` Russian goes to ESpeech, which answers 500 on words it doesn't
  know (terms, transliterated names); `Auto` goes to Qwen3-TTS, whose presets are not native
  Russian speakers and drift in timbre and emotion between lines.

## Pronunciation and stress

- **Латинский глоссарий** (`pronounce.py`, словарь в `voice.pronounce` + встроенный): ESpeech
  глотает буквы — «LLM» распознаётся как «ЭМ», «rate limiting» как «Red Limiting». Замена
  словарём работает: «эл-эл-эм» распознаётся обратно как «LLM», поэтому субтитры остаются
  правильными, а `text` в narration.json — человеческим.
- **Словарь подставляется по целым словам**, с учётом многословных ключей («rate limiting») и
  дефисов («LLM-агент» → «эл-эл-эм-агент»). Токенизатор не решает, как читать аббревиатуру.
- **Ударения ставит RUAccent**, не рукописный список: «Большая языковая модель» → `больш+ая
  язык+овая`, и омограф снимается по контексту. Ставим `+` **во всех словах**, а не только в
  омографах: по документации хаба, как только `+` есть в тексте, авто-акцентиатор не
  вмешивается, и десять отмеченных омографов оставляли двести с лишним слов читать как
  угодно — «вопрос» выходил «вОпрос». Среднего нет: либо меток нет вовсе, либо они на каждом
  слове.
- **Чьи метки уходят в движок — решает `--accent`** (`nd_voice.py`, `pronounce.py`):
  `dictionary` (по умолчанию) берёт только формы из словаря RUAccent — они получены
  lookup'ом, а слова вне словаря (бренды, транслитерации) их разметку получают от нейросети,
  и за такую догадку отвечать не стоит; `all` берёт всё, что вернул `process_all`;
  `omographs` — прежнее поведение, оставлено как откат. Каждая оставленная метка
  проходит `stress_error`, отброшенные видны в логе стадии.
- Знак `+` ставится **перед ударной гласной**. Перед согласной слово ломается: `за+мок`
  произносится «заумок». Даже перед гласной не всегда: `а+гент` — «айгент». Поэтому формы
  проверяются стендом (`tts_probe.py --stress`): синтез с `+` и без, распознавание обоих,
  не выжил — откат.
- Ударения **нельзя проверить автоматически**: whisper ударение не слышит, а F0 умеет лишь
  показать, что знак встал на запрошенный слог. Эталон — словарь или ухо человека.
- **У SpeechKit та же разметка.** `+` перед ударной гласной — соглашение и SpeechKit
  (TTS-разметка), и нашего ruaccent, поэтому размеченный текст уходит движку как есть.
  Проверено на реальных сценах: с метками распознавание даёт 1.00, «+» не слышно.
  SpeechKit умеет ещё `sil<[500]>` (пауза), но в пайплайне она не нужна: паузы между
  репликами ставит движок, а границы режутся по ним.

## Music: pitfalls

- Lyria rejects prompts that mention AI/video/product as `PROHIBITED_CONTENT` (no charge).
  Describe only the music: genre, BPM, instruments, mood, "No vocals".
- A Lyria track is ~170 s; `mix.py` trims it to the video length, 1.5 s fade-in, 4 s fade-out;
  `--music-offset` shifts the start.
- Loudness: voice −16 LUFS, music −27 LUFS with sidechain ducking under the voice.
- `loudnorm` resamples to 192 kHz and eats the tail: follow it with `aresample` and `apad` to length.
- `afir length` is a **fraction of the IR, 0..1**, in ffmpeg 6 (older docs say milliseconds). The
  value here is `1` — the whole room. Anyone copying a millisecond value from an old tutorial gets
  a truncated impulse response and no reverb.
- The music branch was never run with a real track: `audio/music.mp3` exists in no project, so
  `atrim` + ducking + the 4 s fade-out are still unverified. The fades are placed so the music
  cut can't click (the 1.5 s fade-in starts at the `atrim`), but check the result by ear once.

## Audio QC

`build_audio.sh` заканчивается `audio_qc.py` на миксе и роняет сборку на щелчке или на
цифровой паузе. Отдельно (с `--report-only`) идёт отчёт по клипам. Запустить руками после
правки цепочки:

```bash
python3 scripts/audio_qc.py projects/<slug>/audio/fit --report-only
python3 scripts/audio_qc.py projects/<slug>/audio/mix.wav --narration projects/<slug>/narration.json \
    --clips projects/<slug>/audio/fit
```

Гейты: край у порога `--max-edge` (−8 dB) и пауза ниже `--min-gap` (−80 dBFS). Отчёт, не
гейт: одиночные скачки внутри сигнала (`impulses` — у речи их много: плизивы и шипящие, и по
уровню щелчок от шипящего не отличить) и разброс энергии (`liveness.spread_db`: ровная подача
читается как «робот»).

Замер края — это уровень первых 2 мс против того, что установилось ПОСЛЕ рампы. Правило
неочевидное, но с пиком онсента опорой короткая рампа читается как щелчок.

## Choosing a voice

`scripts/tts_probe.py <narration> --voices --out /tmp/ab` синтезирует реплику всеми восемью
голосами, склеивает в один файл с паузами и печатает длительность, тон и размах громкости.
Слушать надо самому: тембр по цифрам не выбирается. `--stress` аудирует формы с `+`,
`--lines N` озвучивает отдельную реплику.

## HyperFrames techniques proven on the video

- **Camera over a "world"**: a single `#world` (e.g. 3400×1800) holding two architecture
  "crystals"; the camera is `gsap.to("#world", {x, y, scale})` via `camState(x, y, s, sx, sy)`,
  which puts a world point at a screen point. Wide shot ~0.43, block ~1.15, detail ~2.7. Read
  finer details through a **magnifier**: a round overlay with a separate full-size SVG.
- **Counters** without `onUpdate` (callbacks are muted on seek): `@property --n { syntax: "<integer>";
  inherits: true }` + `counter-reset: n var(--n)` in `::before`, tween `"--n"` with `snap`.
  `inherits` must be `true`, otherwise the pseudo-element sees 0.
- Repeated tweens on the same element: `fromTo(..., {immediateRender: false})`, otherwise a late
  `fromTo` resets the earlier state on frame zero.
- Don't tween `letterSpacing` or other layout properties (lint `gsap_non_transform_motion`),
  don't tween `visibility`/`autoAlpha` on `.clip`.
- Seeded randomness only (`mulberry32`), no `Date.now`/`Math.random`.
- Put a dark gradient and a backing plate behind the header, otherwise zoomed text lands on blocks.
- Copyright: a corner badge + a faint centered watermark (7% white), removed in the finale.

## Illustrated scenes

A generated picture must behave like a physical object in the shot, not like a wallpaper. What the
first cuts got wrong, and what `nd_compose.py` now does:

- **The picture moves.** A still full-bleed frame behind 10 seconds of narration reads as a mistake.
  Ken Burns on `transform` only: `scale 1.03 → 1.12` with an `x` drift across the scene,
  `ease: "none"`. Never tween `width`/`height`/`object-position` — that is a layout property and
  the lint rejects it.
- **The picture earns its place.** One concrete subject per scene, illustrating that scene's
  sentence. A picture that could sit in any other scene is decoration; drop it and let the layout
  fall back to text rather than ship it.
- **One style for the whole video.** The same style sentence goes into every prompt. Mixed styles
  read as clips from different videos stitched together, and that is worse than no illustration.
- **The lower third belongs to the text.** A scrim gradient (`rgba(4,6,12,.8)` at the bottom) or a
  dark picture, never a bright busy area under the heading. `--aspect 16:9` and
  `object-fit: cover` for the background; `object-fit: contain` for a cut-out subject with
  `drop-shadow`.
- **No text in generated pictures.** Image models invent plausible garbage letters on anything that
  looks like a chart or a sign. The prompt carries `no text`; all type is set in the composition.
- **A scene without a picture is not a broken scene.** When the quota refuses a generation the scene
  keeps its layout and reads as text (`assets/art.json` is the manifest). Check the count the
  composer prints: `5/5 scenes illustrated` is the target, not a requirement.
- **Eyebrows and captions live inside the camera.** `.clip` sections are stacked absolutely, so a
  static header outside the camera div is covered by the picture plate. Position it inside.
- **Only tween what exists.** A `.keyidea` tween in a scene without a key idea logs a GSAP warning
  and does nothing. Gate the tween on the data, not on the layout name.

## Factual honesty

When breaking down someone else's model, mark on screen what a source confirms and what is an
estimate ("likely", "est.", "illustrative"). In the Jev video MoE, ~10B active parameters and
expert specialization are marked this way. `nd_compose.py` does this automatically: every fact
carries a `source: <host>` or `estimate` tag next to the number.

An illustration is never evidence. A generated chart of invented numbers is the worst thing that
can happen in a factual video — delete it, don't caption it.
