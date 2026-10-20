#!/usr/bin/env python3
"""Video script → narration.json with scenes and timings.

Two inputs:
  brief.md (+ sources.md)  the model plans and writes the scenes itself
  storyboard.json          the plan from nd_storyboard.py: lines, facts and visuals are already
                           written, this script only lays them on the timeline

Timings are computed here, not by the model: line start = previous end + pause, duration from
speech rate (EN ~2.1 words/s, RU ~12 chars/s for the hub TTS). This is a draft: after voicing,
nd_retime.py sets starts from the real line lengths.

  ND_API_KEY=sk-... python3 scripts/nd_script.py brief.md projects/x/narration.json \
      --sources projects/x/sources.md --lang en --seconds 180
  python3 scripts/nd_script.py brief.md projects/x/narration.json --from projects/x/storyboard.json
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from ndv_common import extract_json, nd_chat  # noqa: E402

SYSTEM = """You write narration for short explainer videos rendered from HTML.
Rules:
- One idea per scene. Each scene: a heading phrased as a question, 2-4 narrator lines, one short key idea.
- Narrator lines are short spoken sentences, max ~22 words, no lists, no markdown, no emoji.
- Use ONLY facts from the sources given. If something is an estimate, say so in the line ("likely", "estimated").
- Total speaking time must fit the target duration.
Return ONLY JSON: {"title": str, "scenes": [{"heading": str, "key_idea": str, "lines": [str, ...]}]}"""


def speak_seconds(text, lang):
    return len(text.split()) / 2.1 if lang == "en" else len(text) / 12.0


def from_storyboard(sb, seconds, pause=0.6, scene_pause=1.2):
    """narration.json straight from the plan: the lines are already written, nothing to generate.

    Timings come from the speech rate here too — they are a draft until nd_retime.py measures the
    real voice. The plan's per-scene `seconds` is the budget, not the contract.
    """
    lang = sb.get("language", "en")
    lines, scenes = [], []
    t = 0.5
    for si, sc in enumerate(sb["scenes"]):
        if si:
            t += scene_pause
        scenes.append({"start": round(t, 3), "beat": sc.get("beat", ""), "heading": sc["heading"],
                       "kicker": sc.get("kicker", ""), "key_idea": sc.get("key_idea", ""),
                       "facts": sc.get("facts") or [],
                       "visual": sc.get("visual") or {"layout": "full"}})
        for text in sc["lines"]:
            lines.append({"start": round(t, 3), "text": text, "scene": si})
            t += speak_seconds(text, lang) + pause
    duration = round(t + 2.0, 1) if lines else 5.0
    return {"title": sb.get("title", ""), "duration": duration, "language": lang,
            "style": sb.get("style") or {},
            "voice": {"provider": "nd-tts", "voice": "ryan" if lang == "en" else "aiden",
                      "style": "Energetic tech explainer narrator. Confident, crisp emphasis."},
            "music": {"provider": "acestep",
                      "prompt": "Instrumental minimal electronic, 110 BPM, pulsing synth arpeggio, "
                                "soft sub bass, airy pads, no vocals"},
            "scenes": scenes, "lines": lines}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("brief")
    ap.add_argument("out")
    ap.add_argument("--sources")
    ap.add_argument("--from", dest="storyboard",
                    help="storyboard.json from nd_storyboard.py: skip generation, keep its plan")
    ap.add_argument("--lang", default="en", choices=["en", "ru"])
    ap.add_argument("--seconds", type=float, default=180)
    ap.add_argument("--model", help="default ND_CHAT_MODEL or qwen3.8-27b-noreason")
    ap.add_argument("--pause", type=float, default=0.6, help="pause between lines")
    ap.add_argument("--scene-pause", type=float, default=1.4, help="pause at scene change")
    a = ap.parse_args()

    if a.storyboard:
        out = from_storyboard(json.load(open(a.storyboard)), a.seconds, a.pause, a.scene_pause)
        with open(a.out, "w") as f:
            json.dump(out, f, ensure_ascii=False, indent=1)
        pics = sum(1 for s in out["scenes"] if (s.get("visual") or {}).get("prompt"))
        print(f"{len(out['scenes'])} scenes, {len(out['lines'])} lines, ~{out['duration']} s, "
              f"{pics} visual prompts → {a.out}")
        return

    brief = open(a.brief).read()
    src = open(a.sources).read()[:60000] if a.sources else "(no sources given: keep claims generic)"
    lang = "English" if a.lang == "en" else "Russian"
    user = (f"Language: {lang}. Target duration: {a.seconds:.0f} seconds of narration.\n\n"
            f"BRIEF:\n{brief}\n\nSOURCES:\n{src}")
    plan = extract_json(nd_chat([{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}],
                                model=a.model, temperature=0.4))

    t, lines, scenes = 0.5, [], []
    for si, sc in enumerate(plan["scenes"]):
        if si:
            t += a.scene_pause
        scenes.append({"start": round(t, 1), "heading": sc["heading"], "key_idea": sc.get("key_idea", "")})
        for text in sc["lines"]:
            lines.append({"start": round(t, 1), "text": text.strip(), "scene": si})
            t += speak_seconds(text, a.lang) + a.pause
    out = {"title": plan.get("title", ""), "duration": round(t + 2.5), "language": a.lang,
           "voice": {"provider": "nd-tts", "voice": "ryan" if a.lang == "en" else "aiden",
                     "style": "Energetic tech explainer narrator. Confident, crisp emphasis."},
           "music": {"provider": "acestep",
                     "prompt": "Instrumental minimal electronic, 110 BPM, pulsing synth arpeggio, soft sub bass, airy pads, no vocals"},
           "scenes": scenes, "lines": lines}
    with open(a.out, "w") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"{len(scenes)} scenes, {len(lines)} lines, ~{out['duration']} s → {a.out}")
    if out["duration"] > a.seconds * 1.15:
        print(f"⚠️ over target by {out['duration'] - a.seconds:.0f} s: shorten the brief or rerun")


if __name__ == "__main__":
    main()
