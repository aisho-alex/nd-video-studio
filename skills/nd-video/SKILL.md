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
6. **Voice** → `audio/raw/line_NN.wav`:
   - `scripts/tts_gpt_audio.py`: OpenAI gpt-audio via OpenRouter, best delivery. Voice `ash`.
   - `scripts/tts_hub.py`: the hub TTS. Russian → `espeech-tts` (F5 + RUAccent: correct stresses,
     homograph disambiguation), everything else → Qwen3-TTS, 8 voices each. It retries the 429/5xx
     the gateway hands out for minutes at a time, warns about lines that mix Latin into Russian
     (ESpeech reads "LLM" as «КМ»), and prints the `X-TTS-Engine` that actually worked.
   - `scripts/tts_elevenlabs.py`: ElevenLabs `eleven_multilingual_v2`. The best Russian of the three;
     `voices` lists the account's voices, `speak` voices narration.json (neighbouring lines go as context).
7. **Music** → `audio/music.mp3`:
   - `scripts/music_lyria.py`: Lyria 3 Pro, $0.08 per track.
   - `scripts/music_acestep.py`: self-hosted ACE-Step 1.5 (MIT), see `docs/acestep.md`.
8. **Sound**: `scripts/build_audio.sh projects/<slug> trailer` = fit lines into windows
   (`fit_narration.py`) → voice processing (`voice_fx.py`) → mix with ducking (`mix.py`)
   → click check (`audio_qc.py`, fails the build). Every line is ramped in and out;
   the composition has a single `<audio id="soundtrack" src="audio/mix.wav">` for the full length.
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
| ACE-Step | `ACESTEP_API_KEY` (if set) | ssh tunnel to the box, the API listens on 127.0.0.1 |

Never print secrets or commit them (`.env` is in `.gitignore`).

## Voice: pitfalls

- gpt-audio answers like an assistant ("Understood, here is…") and reads tags such as `<line>`
  aloud. Fix: a "you are a TTS engine" system prompt, bare text in the user turn, then transcribe
  the result, compare it with the text and retry (`tts_gpt_audio.py` does this).
- OpenRouter audio output works only with `stream: true`; the format is `pcm16`, 24 kHz mono.
- A line longer than its window: `fit_narration.py` speeds it up to ×1.12, beyond that it is
  audible. `TOO LONG` = shorten the text and re-voice only that line: `--only N`.
- Every engine stops its clips mid-waveform and some open on a step. Before the ramps, the last
  2 ms of a line sat 0.6 dB below the level next to it — a full-level step to silence, one sample
  wide, i.e. a click, loudest in the gap between lines. `voice_fx.py` now ramps both ends
  (15 ms in, 50–200 ms out by preset) as the last step after `loudnorm`, and `mix.py` ramps them
  again at the placement so the seam is safe even when mixing `audio/fit` directly. Don't bypass
  either.
- `fit_narration.py` trims with `start_silence=0`: that value is the silence silenceremove *keeps*,
  so the old 0.05/0.1 left ~20 ms of digital silence at the head of every clip, and a fade-in
  applied to silence fades nothing.
- Processing presets (`voice_fx.py`): `natural` (no processing), `trailer` (final Jev mix: bass
  +5 at 110 Hz, presence +2.5 at 3 kHz, 4:1 compression, 1.4 s / 18% reverb), `deep` (pitch
  −2 semitones), `titan` (−4), `cathedral` (3.2 s / 45% reverb), `radio` (350–3400 Hz band).
  Small EQ tweaks after loudnorm are barely audible (the first A/B sounded "like clones"), so the
  presets are spread far apart. Fine-tune with flags: `--pitch -1 --bass 7 --wet 0.25 --room 2`.
  Compare by ear: `voice_fx.py fit ab --compare 0`.

- ElevenLabs free tier: pcm output and library voices (native Russian speakers) are paid only
  (403 / 402), so the script takes mp3 and the premade voices work (Brian `nPczCjzI2devNBz1zQrb`
  was picked for Russian). 10 000 characters a month; a 90 s video is ~1 300. Reachable from RU IPs.
- Hub TTS for Russian: `language` Russian goes to ESpeech, which answers 500 on words it doesn't
  know (terms, transliterated names); `Auto` goes to Qwen3-TTS, whose presets are not native
  Russian speakers and drift in timbre and emotion between lines.

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

`build_audio.sh` ends with `audio_qc.py` on the clips and on the mixdown, and fails the build on a
click. Run it by hand after touching the chain:

```bash
python3 scripts/audio_qc.py projects/<slug>/audio/fx                       # every clip
python3 scripts/audio_qc.py projects/<slug>/audio/mix.wav --narration projects/<slug>/narration.json
```

It prints the level of the first/last 2 ms against the peak of the 50 ms next to it. Near 0 dB means
a step, one sample wide — a click. A ramp reads far below: the built projects are at −12…−46 dB,
and anything above `--max-edge` (default −8) fails. Sample-level steps *inside* the audio are only
a warning: a plosive in a word is indistinguishable from a click by level alone.

If it fails, check that `fit_narration` trimmed the leading silence and that neither `voice_fx` nor
`mix.py` was run with `--fade-in 0 --fade-out 0`. A click in the middle of a word is not a seam
problem — re-voice that line with `tts_hub.py --only N`.

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
