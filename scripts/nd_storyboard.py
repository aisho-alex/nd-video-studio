#!/usr/bin/env python3
"""Brief (+ sources) → storyboard.json: the plan layer before narration.

A scene here is more than a heading: it carries its beat in the narrative arc, the
lines the narrator will say, the facts behind them (with a source or an estimate
mark) and the picture that will be on screen while it is said. nd_script.py turns
this into narration.json, nd_images.py draws visual.prompt, nd_compose.py lays it
out.

  ND_API_KEY=sk-... python3 scripts/nd_storyboard.py brief.md projects/x/storyboard.json \
      --sources projects/x/sources.md --lang ru --seconds 180
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from ndv_common import extract_json, nd_chat  # noqa: E402

BEATS = ["hook", "problem", "solution", "how", "proof", "takeaway"]
LAYOUTS = {
    "full": "wide atmospheric picture behind the whole scene, one strong idea in it, "
            "text sits in the lower third",
    "hero": "ONE large object or character filling most of the frame, centered, plain "
            "even background, nothing else in the picture: it gets cut out and shown next "
            "to the text, so it must not be small, off-centre or crowded",
    "split": "a scene illustrating the second half of the explanation, subject on the right",
    "data": "one symbolic object, large and centered, plain background: it is shown as a "
            "small accent next to big numbers",
    "quote": "an atmospheric background picture, low contrast, it stays behind a big quote",
}

SYSTEM_TMPL = """You are a director and screenwriter for short illustrated explainer videos (2-4 minutes,
16:9, 1920x1080) that are rendered from HTML with a narrator and subtitles.

You plan the video BEFORE anything is voiced. Return ONLY JSON.

ARC. The video follows the beats %s, in this order, one scene per beat by default:
  hook      cold open: the concrete problem or the promise, 5-8 seconds, no theory
  problem   why the obvious approach does not work / what actually breaks
  solution  the idea that resolves it
  how       the mechanism, step by step, the longest part, may be 2-3 scenes
  proof     numbers, examples, a concrete case from the sources
  takeaway  what the viewer should do or remember now, no new facts here
Use fewer beats only if the topic does not have them. Split a long beat into 2-3 scenes.

SCENES. Each scene:
  beat          one of %s
  heading       a question or a short claim, max 12 words
  kicker        one line of context shown under the heading, max 14 words (may be "")
  key_idea      one short sentence that closes the scene, max 14 words
  lines         3-6 narrator lines. Spoken sentences, max 25 words, no lists, no markdown,
                no emoji, no stage directions, no "in this video". Each line one idea.
  facts         1-3 short on-screen facts, WRITTEN IN THE NARRATION LANGUAGE (not English) with
                certainty: "source" when the given sources confirm it, "estimate" when it is a
                guess. Each fact: {"text": str, "url": str (exact url from the sources or ""),
                "certainty": "source"|"estimate"}. A fact is a number, a name or a quote that
                fits on one line — never a sentence that restates the heading.
  visual        {"layout": str, "prompt": str, "caption": str}
                layout is one of %s, picked by what the picture must DO on screen.
                Use "hero" at most twice in the whole video (it costs the most: cut-out plus
                background removal), "full" or "split" for the rest:
                - %s
  lines         4-7 of them for a 3-minute video: this is where the depth lives, concrete
                examples, numbers and consequences, not a second pass over the heading

LANGUAGE. The heading, the kicker, the key idea, the narrator lines AND facts[].text are all
written in %s. Only visual.prompt stays English. A fact left in English is a bug: the whole video
is Russian, so an English fact card reads as a mistake on screen.

HONESTY. Only facts present in SOURCES. Never invent numbers, names or quotes. Anything
you are not sure about is an "estimate" fact and the line says so ("likely", "about",
"estimated"). No sources given: keep the claims generic and true.

PICTURES. The prompt is a single image prompt in ENGLISH, 1-2 sentences, describing one
concrete scene that illustrates THIS scene's point (not a mood board, not an abstract
metaphor). Never ask for text, letters, numbers, captions, labels, a UI or a logo, and never
say "labeled", "written", "titled" or "sign" in the prompt — image models draw garbage letters
wherever text is implied. All text is typeset in the video itself. Describe the framing you need
for the layout. caption is a 3-8 word contact-sheet caption in the narration language.

SCHEDULE. The sum of ALL narrator lines must fill the target duration: roughly
`characters / 12` seconds in Russian, `words / 2.1` in English. Write the lines first and count
them against the budget — a plan of headings and thin lines is what makes a video feel thin.
Beats: hook and takeaway are short, how and proof are long.

Return ONLY JSON: {"title": str, "style": {"art": str, "palette": [str, ...]},
 "scenes": [{"beat": str, "heading": str, "kicker": str, "key_idea": str, "lines": [str, ...],
 "facts": [{"text": str, "url": str, "certainty": str}],
 "visual": {"layout": str, "prompt": str, "caption": str}}]}
"style.art is one sentence that fixes the illustration style for the WHOLE video
(medium, palette, lighting, no text), style.palette is 3-5 hex colours.
The video typesets white text over these pictures, so the style MUST ask for a dark or muted
background and low-contrast, soft lighting: "dark navy background", "moody", "low contrast".
Never "clean white background", "bright", "high key", "pastel". style.palette is EXACTLY 4 hex
colours in this role order: [background (darkest), accent, highlight, text (lightest)]."""

LAYOUT_HELP = "\n                ".join(f"- {k}: {v}" for k, v in LAYOUTS.items())
ARC_LINE = "→".join(BEATS)
BEATS_LINE = ", ".join(f'"{b}"' for b in BEATS)
LAYOUT_LINE = ", ".join(f'"{k}"' for k in LAYOUTS)


def system_prompt(lang):
    """The plan template, with the narration language baked in: facts on screen follow the voice."""
    return SYSTEM_TMPL % (ARC_LINE, BEATS_LINE, LAYOUT_LINE, LAYOUT_HELP,
                          "Russian" if lang == "ru" else "English")

STYLE_DEFAULT = {
    "art": ("cinematic 3D illustration, deep navy and teal background, warm amber key light, "
            "soft rim light, shallow depth of field, clean composition, no text, no letters"),
    "palette": ["#0b1020", "#7aa2ff", "#ffd479", "#eef1f8"],
}


def validate(sb):
    """Cheap structural check: the composer and the image step rely on these keys."""
    warn = []
    scenes = sb.get("scenes") or []
    if not scenes:
        raise SystemExit("storyboard has no scenes")
    for i, s in enumerate(scenes):
        if not s.get("lines"):
            warn.append(f"scene {i} has no lines")
        v = s.get("visual") or {}
        if v.get("layout") not in LAYOUTS:
            warn.append(f"scene {i}: bad layout {v.get('layout')!r}, allowed: {', '.join(LAYOUTS)}")
        if not v.get("prompt"):
            warn.append(f"scene {i}: no visual.prompt")
        for f in s.get("facts") or []:
            if f.get("certainty") not in ("source", "estimate"):
                warn.append(f"scene {i}: bad certainty {f.get('certainty')!r}")
    return warn


LINE_GAP, SCENE_GAP = 0.6, 1.2      # same defaults as nd_script.from_storyboard()


def estimate_seconds(scenes, lang):
    """How long the video will actually be: the speech rate nd_retime.py will later confirm,
    plus the pauses nd_script.py lays the same lines out with. Keep the two in step, or the
    storyboard promises 180 s and narration.json says 120."""
    def one(t):
        return len(t) / 12.0 if lang == "ru" else len(t.split()) / 2.1
    speech = sum(one(t) for sc in scenes for t in sc["lines"])
    return speech + LINE_GAP * sum(len(sc["lines"]) for sc in scenes) + SCENE_GAP * (len(scenes) - 1)


def _absorb(out, fixed, lang):
    """Take a revised plan: new lines and scenes, everything else the model didn't touch."""
    scenes = fixed.get("scenes") or []
    if not scenes:
        return False
    out["title"] = fixed.get("title") or out["title"]
    out["style"] = fixed.get("style") or out["style"]
    for i, sc in enumerate(scenes):
        if i >= len(out["scenes"]):
            break
        cur = out["scenes"][i]
        if sc.get("lines"):
            cur["lines"] = [(t or "").strip() for t in sc["lines"] if (t or "").strip()]
        for key in ("heading", "kicker", "key_idea", "beat"):
            if sc.get(key):
                cur[key] = sc[key]
        if sc.get("facts"):
            cur["facts"] = sc["facts"]
        if sc.get("visual"):
            cur["visual"] = {**cur["visual"], **{k: v for k, v in sc["visual"].items() if v}}
    # сцен могло стать больше: дописываем недостающие из ответа модели
    for i in range(len(out["scenes"]), len(scenes)):
        sc = scenes[i]
        v = sc.get("visual") or {}
        out["scenes"].append({
            "beat": sc.get("beat", "how"), "heading": (sc.get("heading") or "").strip(),
            "kicker": (sc.get("kicker") or "").strip(), "key_idea": (sc.get("key_idea") or "").strip(),
            "lines": [(t or "").strip() for t in (sc.get("lines") or []) if (t or "").strip()],
            "facts": [f for f in (sc.get("facts") or []) if f.get("text")],
            "visual": {"layout": v.get("layout", "full"), "prompt": (v.get("prompt") or "").strip(),
                       "caption": (v.get("caption") or "").strip()},
        })
    return True


CYRILLIC = set("аеёиоуыэюяАЕЁИОУЫЭЮЯ")


def wrong_language(scenes, lang):
    """Факты на экране на языке диктора. Русский ролик с английской карточкой — это баг, а не стилистика."""
    want_cyrillic = lang == "ru"
    bad = []
    for i, s in enumerate(scenes):
        for f in s.get("facts") or []:
            letters = [c for c in (f.get("text") or "") if c.isalpha()]
            if not letters:
                continue
            has_cyr = any(c in CYRILLIC for c in letters)
            if has_cyr != want_cyrillic:
                bad.append(i)
                break
    return bad


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("brief")
    ap.add_argument("out")
    ap.add_argument("--sources")
    ap.add_argument("--lang", default="en", choices=["en", "ru"])
    ap.add_argument("--seconds", type=float, default=180, help="target narration, 120-240 is the sweet spot")
    ap.add_argument("--model", help="default ND_CHAT_MODEL or qwen3.8-27b-noreason")
    ap.add_argument("--style", help="one sentence fixing the illustration style (overrides the model's)")
    ap.add_argument("--temperature", type=float, default=0.5)
    ap.add_argument("--no-grow", action="store_true",
                    help="don't add a pass that deepens a plan shorter than the target")
    a = ap.parse_args()

    brief = open(a.brief).read()
    src = open(a.sources).read()[:60000] if a.sources else "(no sources given: keep claims generic)"
    lang = "English" if a.lang == "en" else "Russian"
    user = (f"Language of the narration: {lang}. Target duration: {a.seconds:.0f} seconds.\n\n"
            f"BRIEF:\n{brief}\n\nSOURCES:\n{src}")
    plan = extract_json(nd_chat([{"role": "system", "content": system_prompt(a.lang)},
                                 {"role": "user", "content": user}],
                                model=a.model, temperature=a.temperature))

    style = plan.get("style") or STYLE_DEFAULT
    style.setdefault("palette", STYLE_DEFAULT["palette"])
    if not style.get("art"):
        style["art"] = STYLE_DEFAULT["art"]
    if a.style:
        style["art"] = a.style
    style["palette"] = style["palette"][:6]

    scenes = []
    for s in plan["scenes"]:
        v = s.get("visual") or {}
        scenes.append({
            "beat": s.get("beat", "how"),
            "heading": (s.get("heading") or "").strip(),
            "kicker": (s.get("kicker") or "").strip(),
            "key_idea": (s.get("key_idea") or "").strip(),
            "lines": [(t or "").strip() for t in (s.get("lines") or []) if (t or "").strip()],
            "facts": [f for f in (s.get("facts") or []) if f.get("text")],
            "visual": {"layout": v.get("layout", "full"),
                       "prompt": (v.get("prompt") or "").strip(),
                       "caption": (v.get("caption") or "").strip()},
        })
    out = {"title": plan.get("title", ""), "language": a.lang, "target_seconds": a.seconds,
           "style": style, "scenes": scenes}

    for w in validate(out):
        print(f"⚠️  {w}")

    # --language: a Russian video with an English fact card is a visible mistake. One pass to
    # rewrite only those strings — cheaper than redoing the whole plan.
    wrong = wrong_language(out["scenes"], a.lang)
    if wrong:
        print(f"⚠️  facts in the wrong language in scenes {wrong}")
        fix = extract_json(nd_chat([
            {"role": "system", "content": system_prompt(a.lang)},
            {"role": "user", "content": f"Translate these on-screen facts into "
             f"{'Russian' if a.lang == 'ru' else 'English'}. Keep the numbers, the names and the "
             'certainty. Return ONLY JSON {"scenes": [{"i": int, "facts": [{"text", "url", '
             '"certainty"}]}]} with one entry per given i.'},
            {"role": "assistant", "content": json.dumps(
                [{"i": i, "facts": out["scenes"][i]["facts"]} for i in wrong], ensure_ascii=False)},
        ], model=a.model, temperature=0.2))
        for item in fix.get("scenes") or []:
            i = int(item["i"])
            if 0 <= i < len(out["scenes"]) and item.get("facts"):
                out["scenes"][i]["facts"] = item["facts"]
        print("facts rewritten in the narration language")

    # --tighten: the plan overshot the duration. One more pass, cutting lines, before the
    # voice is paid for — a scene that loses a line costs nothing, a re-voiced one costs quota.
    # the model's per-scene "seconds" is its own bookkeeping and runs optimistic; the length that
    # matters is the narration actually written, so the trigger is the speech estimate
    budget = estimate_seconds(out["scenes"], a.lang)
    if budget < a.seconds * 0.8 and not a.no_grow:
        # a thin plan is the reason the first cuts felt thin: one pass to add the detail back
        print(f"narration is ~{budget:.0f} s of a {a.seconds:.0f} s target, growing it")
        grown = extract_json(nd_chat([
            {"role": "system", "content": system_prompt(a.lang)},
            {"role": "user", "content": f"Language: {lang}. The narration below runs {budget:.0f} s, "
             f"the target is {a.seconds:.0f} s. Add the missing depth, not filler: concrete examples "
             "from the sources, numbers, what breaks without this, what to do about it. Add lines "
             "to the 'how' and 'proof' scenes first and add a scene if a beat is too thin. Keep every "
             'heading, fact and visual as they are. Return ONLY the same JSON {"title", "style", '
             '"scenes"}.\nSOURCES:\n' + src[:40000]},
            {"role": "assistant", "content": json.dumps(out, ensure_ascii=False)},
        ], model=a.model, temperature=0.4))
        _absorb(out, grown, a.lang)
        budget = estimate_seconds(out["scenes"], a.lang)
        print(f"grown to ~{budget:.0f} s")
    if budget > a.seconds * 1.15:
        fixed = extract_json(nd_chat([
            {"role": "system", "content": system_prompt(a.lang)},
            {"role": "user", "content": f"Language: {lang}. The plan below runs {budget:.0f} s, the "
             f"target is {a.seconds:.0f} s. Cut it to fit: drop the weakest line of the longest "
             'scenes and shorten the rest. Keep every beat, every fact and every visual. '
             'Return ONLY the same JSON {"title", "style", "scenes"}.'},
            {"role": "assistant", "content": json.dumps(out, ensure_ascii=False)},
        ], model=a.model, temperature=0.3))
        if _absorb(out, fixed, a.lang):
            new_budget = estimate_seconds(out["scenes"], a.lang)
            print(f"tightened {budget:.0f} s → {new_budget:.0f} s")

    # --repair: a second pass fixes a broken layout or a missing picture prompt
    bad = [i for i, s in enumerate(out["scenes"]) if s["visual"]["layout"] not in LAYOUTS
           or not s["visual"]["prompt"]]
    if bad:
        fix = extract_json(nd_chat([
            {"role": "system", "content": system_prompt(a.lang)},
            {"role": "user", "content": lang + ". Rewrite ONLY the visual block of the scenes below. "
             f'Return ONLY JSON {{"scenes": [{{"i": int, "layout": one of {", ".join(LAYOUTS)}, '
             '"prompt": str, "caption": str}}]}} with one entry per given i. '
             "Same rules: one concrete picture in English, never any text in the image.\n"
             + json.dumps([{"i": i, "heading": out["scenes"][i]["heading"],
                            "lines": out["scenes"][i]["lines"],
                            "was": out["scenes"][i]["visual"]} for i in bad], ensure_ascii=False)},
        ], model=a.model, temperature=0.3))
        for item in fix.get("scenes") or []:
            i = int(item["i"])
            out["scenes"][i]["visual"] = {"layout": item.get("layout", "full"),
                                          "prompt": (item.get("prompt") or "").strip(),
                                          "caption": (item.get("caption") or "").strip()}
            print(f"repaired visual of scene {i}: {out['scenes'][i]['visual']['layout']}")

    with open(a.out, "w") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    words = sum(len(s["lines"]) for s in out["scenes"])
    print(f"{len(out['scenes'])} scenes, {words} lines, "
          f"~{estimate_seconds(out['scenes'], a.lang):.0f} s of narration → {a.out}")


if __name__ == "__main__":
    main()