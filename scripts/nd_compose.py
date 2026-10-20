#!/usr/bin/env python3
"""Build index.html from narration.json + words.json + assets/art.json.

The composer is generic: it lays out every scene in the layout the storyboard asked for
(full / hero / split / data / quote), draws the picture if assets/art.json says one was
generated, and falls back to a text-only layout when it wasn't. Nothing here is topic-specific —
only overrides.json can override the body of a single scene.

  python3 scripts/nd_compose.py                    # cwd = the project dir
  python3 scripts/nd_compose.py --overrides overrides.json --brand "Основы латыни"

Timeline rule: voice first, compose after nd_retime.py, otherwise the animation drifts.
"""

import argparse
import glob
import html
import json
import os
import shutil

GAP = 0.35          # same gap ndv_common uses: a line is on screen until the next one starts
DIP_IN, DIP_OUT = 0.32, 0.24     # scene fade in/out — the cut between scenes
KEN = 0.09           # how much a full-bleed picture drifts while the scene is on screen
ACCENT_HL = "#ffd479"
SUB_IDLE = "#9aa2b6"
DEFAULT_PALETTE = ["#0b1020", "#7aa2ff", "#ffd479", "#eef1f8"]


def esc(s):
    return html.escape(str(s or ""))


def sec(t):
    return round(float(t), 3)


def luminance(hex_color):
    h = hex_color.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def saturation(hex_color):
    h = hex_color.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))
    return max(r, g, b) - min(r, g, b)


def mix_white(color, amount):
    h = color.lstrip("#")
    rgb = [int(h[i:i + 2], 16) for i in (0, 2, 4)]
    return "#" + "".join(f"{round(c + (255 - c) * amount):02X}" for c in rgb)


def contrast_ratio(fg, bg):
    def lin(c):
        c /= 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    def lum(color):
        h = color.lstrip("#")
        r, g, b = (lin(int(h[i:i + 2], 16)) for i in (0, 2, 4))
        return 0.2126 * r + 0.7152 * g + 0.0722 * b
    a, b = sorted((lum(fg), lum(bg)), reverse=True)
    return (a + 0.05) / (b + 0.05)


def ensure_readable(color, bg, min_ratio=4.5, fallback=None):
    """Type set in this colour must pass WCAG AA against the background.

    A storyboard palette can be tasteful and unreadable at the same time: a dark navy accent on a
    dark navy background measured 1.66:1 on a real run and the source tags and the "ИТОГ РАЗДЕЛА"
    labels disappeared. Keep the hue, lighten until it reads; if even white cannot, take the default.
    """
    for _ in range(10):
        if contrast_ratio(color, bg) >= min_ratio:
            return color
        lighter = mix_white(color, 0.22)
        if contrast_ratio(lighter, bg) <= contrast_ratio(color, bg):
            return fallback or DEFAULT_PALETTE[1]
        color = lighter
    return fallback or color


def normalize_palette(palette):
    """The composer needs [background, accent, highlight, text] whatever the storyboard returned.

    The order the model sent is trusted, and only a role that is obviously broken is corrected: a
    background the white headings cannot sit on, text too dark to read, an accent that vanished into
    the background. A model that lists five colours in its own order must not leave the type grey
    on grey.
    """
    cols = [c for c in (palette or []) if isinstance(c, str) and c.startswith("#")][:4]
    if len(cols) < 4:
        return list(DEFAULT_PALETTE)
    bg, accent, highlight, text = cols
    if luminance(bg) > 0.30:
        # this composition is a dark video: white headings, a dark plate, lights on top
        bg = DEFAULT_PALETTE[0]
        if luminance(text) < 0.4:
            text = DEFAULT_PALETTE[3]
    if luminance(text) < 0.55:
        text = max(cols, key=luminance)
    if abs(luminance(accent) - luminance(bg)) < 0.15 or saturation(accent) < 0.12:
        accent = DEFAULT_PALETTE[1]
    if abs(luminance(highlight) - luminance(bg)) < 0.12 or highlight == accent:
        highlight = DEFAULT_PALETTE[2]
    # the accents ARE type (source tags, section labels), so they have to be readable, not just pretty
    return [bg, ensure_readable(accent, bg, fallback=DEFAULT_PALETTE[1]),
            ensure_readable(highlight, bg, fallback=DEFAULT_PALETTE[2]), text]


def words_of(text):
    return text.replace("—", " — ").split()


def url_host(url):
    return url.split("/")[2].replace("www.", "") if "//" in url and "." in url.split("/")[2] else ""


# ---------------------------------------------------------------- facts

def facts_html(facts, lang):
    """On-screen facts with their certainty. An estimate is never shown as a plain number."""
    out = []
    est = "оценка" if lang == "ru" else "estimate"
    src = "источник" if lang == "ru" else "source"
    for f in facts or []:
        c = f.get("certainty", "source")
        host = url_host(f.get("url", ""))
        label = est if c == "estimate" else f"{src}: {host}" if host else src
        out.append(f'<li class="fact fact--{"est" if c == "estimate" else "src"}">'
                   f'<span class="f-text">{esc(f.get("text"))}</span>'
                   f'<span class="f-tag">{esc(label)}</span></li>')
    return "".join(out)


def keyidea_html(text, lang):
    if not text:
        return ""
    label = "Итог раздела" if lang == "ru" else "Key idea"
    return f'<div class="keyidea"><span class="kd-label">{esc(label)}</span>' \
           f'<span class="kd-text">{esc(text)}</span></div>'


def facts_block(facts, lang):
    body = facts_html(facts, lang)
    return f'<ul class="facts">{body}</ul>' if body else ""


# ---------------------------------------------------------------- scene layouts

def plate(pic, cls="", dim=0.0):
    """The picture layer behind / beside the text. No picture = a plain gradient plate."""
    style = f"--dim:{dim};" if dim else ""
    if pic:
        return (f'<div class="plate {cls}" style="{style}">'
                f'<img class="pic" src="{esc(pic)}" data-layout-ignore />'
                f'<div class="scrim"></div></div>')
    return f'<div class="plate plate--empty {cls}" style="{style}"></div>'


def scene_full(s, pic, lang, kicker, head=""):
    """Picture over the whole frame, text in the lower third on a scrim."""
    return (f'{plate(pic.get("bg"), "plate--full", 0.35)}'
            f'<div class="cam">{head}<div class="stack stack--bottom">'
            f'{kicker}'
            f'<h2 class="heading">{esc(s["heading"])}</h2>'
            f'{facts_block(s.get("facts"), lang)}'
            f'{keyidea_html(s.get("key_idea"), lang)}'
            '</div></div>')


def scene_hero(s, pic, lang, kicker, head=""):
    """Text on the left, a cut-out subject on the right, its own scene plate far behind."""
    cutout = pic.get("hero")
    col = (f'<div class="piccol"><img class="cutout" src="{esc(cutout)}" data-layout-ignore /></div>'
           if cutout else '<div class="piccol"></div>')
    return (f'{plate(pic.get("bg"), "plate--soft", 0.6)}'
            f'<div class="cam">{head}<div class="cols">'
            '<div class="textcol">'
            f'{kicker}<h2 class="heading">{esc(s["heading"])}</h2>'
            f'{facts_block(s.get("facts"), lang)}{keyidea_html(s.get("key_idea"), lang)}'
            f'</div>{col}</div></div>')


def scene_split(s, pic, lang, kicker, head=""):
    """A framed picture on the right, the facts as rows on the left."""
    bg = pic.get("bg")
    frame = (f'<div class="picframe"><img class="pic" src="{esc(bg)}" data-layout-ignore /></div>'
             if bg else '')
    solo = " cols--solo" if not bg else ""
    # В f-строку подставляем готовый кусок: обратные слэши внутри выражения f-строки
    # допустимы только начиная с Python 3.12, а образ ставит 3.11.
    piccol = f'<div class="piccol">{frame}</div>' if frame else ''
    return (f'<div class="cam">{head}<div class="cols cols--split{solo}">'
            '<div class="textcol">'
            f'{kicker}<h2 class="heading">{esc(s["heading"])}</h2>'
            f'{facts_block(s.get("facts"), lang)}{keyidea_html(s.get("key_idea"), lang)}'
            f'</div>{piccol}</div></div>')


def scene_data(s, pic, lang, kicker, head=""):
    """Numbers first: the facts become the picture, the illustration is only an accent."""
    ac = pic.get("accent")
    accent = (f'<img class="accent" src="{esc(ac)}" data-layout-ignore />' if ac else "")
    return (f'<div class="cam">{head}<div class="stack stack--center">'
            f'{kicker}<h2 class="heading">{esc(s["heading"])}</h2>'
            f'<ul class="rows">{facts_html(s.get("facts"), lang)}</ul>'
            f'<div class="rowwrap">{accent}{keyidea_html(s.get("key_idea"), lang)}</div>'
            '</div></div>')


def scene_quote(s, pic, lang, kicker, head=""):
    """One sentence, big, with the picture sunk far back behind it."""
    src = ""
    for f in s.get("facts") or []:
        if f.get("certainty") == "source" and f.get("url"):
            src = f'<div class="q-src">{esc(url_host(f["url"]))}</div>'
            break
    return (f'{plate(pic.get("bg"), "plate--deep", 0.72)}'
            '<div class="cam"><div class="stack stack--center">'
            f'<div class="quote">{esc(s.get("kicker") or s.get("key_idea"))}</div>'
            f'{src}<h2 class="heading heading--small">{esc(s["heading"])}</h2>'
            '</div></div>')


LAYOUTS = {"full": scene_full, "hero": scene_hero, "split": scene_split,
           "data": scene_data, "quote": scene_quote}


# ---------------------------------------------------------------- CSS

def build_css(pal, lang):
    return f"""
* {{ margin:0; padding:0; box-sizing:border-box; }}
html,body {{ width:1920px; height:1080px; overflow:hidden; background:#05070d; }}
body {{ font-family: Inter, "Segoe UI", ui-sans-serif, system-ui, sans-serif; }}
#root {{ position:relative; width:100%; height:100%; overflow:hidden; color:{pal[3]}; }}
.clip {{ position:absolute; inset:0; }}
.full-bleed {{ position:absolute; inset:0; }}
#bg {{ background:{pal[0]}; }}
.bg-glow {{ position:absolute; inset:-20%;
  background: radial-gradient(58% 52% at 20% 16%, rgba(122,162,255,.20), transparent 60%),
              radial-gradient(52% 48% at 84% 86%, rgba(255,212,121,.14), transparent 62%); }}
.bg-grid {{ position:absolute; inset:0; opacity:.08;
  background-image: repeating-linear-gradient(90deg, rgba(255,255,255,.6) 0 2px, transparent 2px 240px); }}
.bg-vig {{ position:absolute; inset:0;
  background: radial-gradient(120% 100% at 50% 45%, transparent 40%, rgba(0,0,0,.78) 100%); }}
.watermark {{ position:absolute; inset:0; display:flex; align-items:center; justify-content:center;
  font-family: Georgia, "Times New Roman", serif; font-size:260px; letter-spacing:.14em;
  color:rgba(255,255,255,.05); white-space:nowrap; }}
.route {{ position:absolute; top:52px; left:120px; right:120px; }}
.route-track {{ height:4px; background:rgba(255,255,255,.12); border-radius:2px; overflow:hidden; }}
#progress {{ height:100%; width:100%; transform-origin:left center; transform:scaleX(0);
  background:linear-gradient(90deg,{pal[1]},{pal[2]}); }}
.route-marks {{ display:flex; justify-content:space-between; margin-top:12px; }}
.mark {{ font-size:19px; letter-spacing:.3em; color:rgba(255,255,255,.32); }}
.badge {{ position:absolute; right:60px; bottom:44px; font-size:20px; letter-spacing:.16em;
  color:rgba(255,255,255,.36); }}
.brand {{ position:absolute; left:120px; bottom:46px; font-size:22px; letter-spacing:.04em;
  color:rgba(255,255,255,.44); }}

.scene {{ background:transparent; }}
.plate {{ position:absolute; inset:0; }}
.plate .pic {{ width:100%; height:100%; object-fit:cover; object-position:center; }}
.plate .scrim {{ position:absolute; inset:0;
  background: linear-gradient(180deg, rgba(4,6,12,.55) 0%, rgba(4,6,12,.15) 34%,
              rgba(4,6,12,.80) 78%, rgba(4,6,12,.95) 100%); opacity:var(--dim,1); }}
.plate--deep .scrim {{ background: linear-gradient(180deg, rgba(4,6,12,.86), rgba(4,6,12,.93)); }}
.plate--soft {{ opacity:.30; filter:saturate(.7); }}
.plate--empty {{ background: radial-gradient(70% 60% at 72% 30%, rgba(122,162,255,.16), transparent 70%); }}
.cam {{ position:absolute; inset:0; transform-origin:50% 44%; }}
.eyebrow {{ position:absolute; top:104px; left:140px; right:140px; z-index:2; }}
.stack {{ position:absolute; inset:0; display:flex; flex-direction:column; align-items:flex-start;
  gap:26px; padding:150px 140px 300px; }}
.stack--bottom {{ justify-content:flex-end; padding-bottom:260px; }}
.stack--center {{ justify-content:center; align-items:center; text-align:center;
  padding:150px 220px 300px; }}
.cols {{ position:absolute; inset:0; display:grid; grid-template-columns:1.05fr .95fr;
  align-items:center; gap:80px; padding:170px 140px 300px; }}
.cols--split {{ grid-template-columns:1fr 1fr; }}
.cols--solo {{ grid-template-columns:1fr; }}
.eyebrow {{ display:flex; align-items:center; gap:24px; z-index:2; }}
.tag {{ font-size:23px; letter-spacing:.34em; text-transform:uppercase; color:{pal[2]}; }}
.rule {{ width:200px; height:2px; background:linear-gradient(90deg,{pal[2]},transparent); }}
.heading {{ max-width:1420px; font-size:76px; line-height:1.08; font-weight:800;
  letter-spacing:-.02em; color:#fff; text-shadow:0 12px 44px rgba(0,0,0,.85), 0 2px 6px rgba(0,0,0,.9); }}
.heading--small {{ font-size:52px; opacity:.82; }}
.kicker {{ font-size:34px; line-height:1.3; color:rgba(255,255,255,.72); max-width:1100px;
  text-shadow:0 4px 20px rgba(0,0,0,.9); }}
.facts {{ list-style:none; display:flex; flex-direction:column; gap:12px; max-width:1180px; }}
.fact {{ display:flex; align-items:baseline; gap:20px; padding:16px 26px; border-radius:14px;
  background:rgba(9,14,28,.78); border:1px solid rgba(255,255,255,.10); }}
.fact--est {{ border-color:rgba(255,212,121,.42); }}
.f-text {{ flex:1; font-size:33px; line-height:1.24; color:{pal[3]}; }}
.f-tag {{ flex:none; align-self:center; margin-left:20px; font-size:19px; letter-spacing:.14em; text-transform:uppercase;
  color:{pal[1]}; white-space:nowrap; }}
.fact--est .f-tag {{ color:{pal[2]}; }}
.keyidea {{ display:flex; align-items:center; gap:22px; padding:20px 36px;
  border-left:6px solid {pal[1]}; border-radius:12px; background:rgba(9,14,28,.84); max-width:1560px; }}
.kd-label {{ font-size:22px; letter-spacing:.22em; text-transform:uppercase; color:{pal[1]};
  white-space:nowrap; }}
.kd-text {{ font-size:34px; line-height:1.26; color:#eef1f8; }}
.piccol {{ display:flex; align-items:center; justify-content:center; height:100%; }}
.cutwrap {{ transform:scale(1.28); transform-origin:50% 50%; }}   /* subject sits small in a 2048px square */
.cutout {{ width:100%; max-height:720px; object-fit:contain;
  filter:drop-shadow(0 40px 70px rgba(0,0,0,.65)); }}
.picframe {{ position:relative; width:100%; aspect-ratio:16/10; border-radius:26px; overflow:hidden;
  border:1px solid rgba(255,255,255,.12); box-shadow:0 30px 70px rgba(0,0,0,.6); }}
.picframe .pic {{ width:100%; height:100%; object-fit:cover; }}

.accent {{ max-height:220px; object-fit:contain; filter:drop-shadow(0 20px 40px rgba(0,0,0,.6)); }}
.rows {{ display:flex; flex-direction:column; gap:18px; align-items:center; }}
.rows .fact {{ min-width:900px; justify-content:space-between; }}
.rows .f-text {{ font-size:46px; font-weight:700; }}
.rowwrap {{ display:flex; align-items:center; gap:36px; }}
.rowwrap .keyidea {{ max-width:1100px; }}
.quote {{ max-width:1420px; font-family: Georgia, "Times New Roman", serif; font-size:66px;
  line-height:1.22; color:#fff; text-shadow:0 10px 40px rgba(0,0,0,.9); }}
.q-src {{ font-size:24px; letter-spacing:.2em; text-transform:uppercase; color:{pal[1]}; }}
.subtitle {{ background:transparent; }}
.sub-line {{ position:absolute; left:50%; bottom:120px; transform:translateX(-50%); width:1640px;
  text-align:center; font-size:43px; line-height:1.34; font-weight:600; color:{SUB_IDLE};
  text-shadow:0 6px 26px rgba(0,0,0,.95), 0 0 3px rgba(0,0,0,.95); }}
.w {{ color:{SUB_IDLE}; }}
"""


# ---------------------------------------------------------------- gsap

def find_gsap(project_dir):
    """assets/gsap.min.js: своя копия, копия соседнего проекта, node_modules или сам пайплайн.

    Последний источник — файл в корне этого репозитория: приложение раскладывает
    проекты по своим каталогам, где ни соседей с готовым роликом, ни node_modules
    нет, и композиция падала с «assets/gsap.min.js not found».
    """
    dest = os.path.join(project_dir, "assets", "gsap.min.js")
    if os.path.exists(dest):
        return dest
    own = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets", "gsap.min.js")
    sources = [own]
    sources += sorted(glob.glob(os.path.join(project_dir, "..", "*", "assets", "gsap.min.js")))
    sources += ["node_modules/gsap/dist/gsap.min.js",
                os.path.join(project_dir, "node_modules", "gsap", "dist", "gsap.min.js")]
    for cand in sources:
        if cand and os.path.exists(cand):
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            shutil.copyfile(cand, dest)
            return dest
    return None


def build_timeline(n, art, lang):
    """One paused GSAP timeline. Only transforms and opacity — never a layout property."""
    DUR, scenes, lines = n["duration"], n["scenes"], n["lines"]
    js = [f'tl.fromTo("#progress", {{scaleX:0}}, {{scaleX:1, duration:{DUR}, ease:"none"}}, 0);']
    by_scene = {}
    for i, ln in enumerate(lines):
        by_scene.setdefault(ln["scene"], []).append(i)
    pics = {}
    for it in art.get("items", []):
        if it.get("ok") and it.get("file"):
            pics.setdefault(it["scene"], {})[it.get("kind", "bg")] = it["file"]

    for si, s in enumerate(scenes):
        t0, t1 = s["start"], scenes[si + 1]["start"] if si + 1 < len(scenes) else DUR
        dur, sel = t1 - t0, f"#scene{si}"
        layout = (s.get("visual") or {}).get("layout", "full")
        pic = pics.get(si, {})
        has_facts, has_key = bool(s.get("facts")), bool(s.get("key_idea"))

        # the cut: dip to the dark plate, then the next scene rises
        js.append(f'tl.fromTo("{sel}", {{opacity:0}}, {{opacity:1, duration:{DIP_IN}, ease:"power2.out"}}, {sec(t0)});')
        js.append(f'tl.to("{sel}", {{opacity:0, duration:{DIP_OUT}, ease:"power2.in"}}, {sec(t1 - DIP_OUT)});')

        # the camera settles, the picture drifts (Ken Burns) — transform only
        js.append(f'tl.fromTo("{sel} .cam", {{scale:1.05, y:12}}, {{scale:1.0, y:0, '
                  f'duration:{round(dur, 2)}, ease:"sine.out", immediateRender:false}}, {sec(t0)});')
        if pic.get("bg") and layout == "full":
            js.append(f'tl.fromTo("{sel} .plate--full .pic", {{scale:1.03, x:0, transformOrigin:"50% 50%"}}, '
                      f'{{scale:{round(1.03 + KEN, 3)}, x:-54, duration:{round(dur, 2)}, ease:"none", '
                      f'immediateRender:false}}, {sec(t0)});')
        if pic.get("bg") and layout == "split":
            js.append(f'tl.fromTo("{sel} .picframe .pic", {{scale:1.06}}, '
                      f'{{scale:{round(1.06 + KEN, 3)}, duration:{round(dur, 2)}, ease:"none", '
                      f'immediateRender:false}}, {sec(t0)});')
        elif pic.get("hero") and layout == "hero":
            bob = round(dur / 3.2, 2)
            for k in range(3):
                dy = -14 if k % 2 == 0 else 10
                js.append(f'tl.to("{sel} .cutout", {{y:{dy}, scale:1.02, duration:{bob}, '
                          f'ease:"sine.inOut"}}, {sec(t0 + k * bob)});')
        elif pic.get("bg") and layout == "quote":
            js.append(f'tl.fromTo("{sel} .plate--deep .pic", {{scale:1.10}}, '
                      f'{{scale:{round(1.10 + KEN / 2, 3)}, duration:{round(dur, 2)}, ease:"none", '
                      f'immediateRender:false}}, {sec(t0)});')

        if layout == "quote":
            js.append(f'tl.fromTo("{sel} .quote", {{opacity:0, y:26}}, {{opacity:1, y:0, duration:.8, '
                      f'ease:"power3.out", immediateRender:false}}, {sec(t0 + 0.4)});')
            js.append(f'tl.fromTo("{sel} .heading--small", {{opacity:0, y:18}}, {{opacity:1, y:0, duration:.6, '
                      f'ease:"power2.out", immediateRender:false}}, {sec(t0 + 1.5)});')
            continue

        if layout == "data":
            # only tween what the scene actually has: an empty selector is a GSAP warning in the log
            if has_facts:
                js.append(f'tl.fromTo("{sel} .rows .fact", {{opacity:0, y:26}}, {{opacity:1, y:0, duration:.5, '
                          f'ease:"power3.out", immediateRender:false, stagger:.16}}, {sec(t0 + 0.9)});')
            if pic.get("accent"):
                js.append(f'tl.fromTo("{sel} .accent", {{opacity:0, scale:.9}}, {{opacity:1, scale:1, duration:.6, '
                          f'ease:"back.out(1.6)", immediateRender:false}}, {sec(t0 + 1.6)});')
            if has_key:
                js.append(f'tl.fromTo("{sel} .keyidea", {{opacity:0, y:20}}, {{opacity:1, y:0, duration:.5, '
                          f'ease:"power2.out", immediateRender:false}}, {sec(t0 + 2.2)});')
            continue

        js.append(f'tl.fromTo("{sel} .eyebrow", {{opacity:0, y:-16}}, {{opacity:1, y:0, duration:.5, '
                  f'ease:"power3.out", immediateRender:false}}, {sec(t0 + 0.15)});')
        js.append(f'tl.fromTo("{sel} .heading", {{opacity:0, y:34}}, {{opacity:1, y:0, duration:.7, '
                  f'ease:"power3.out", immediateRender:false}}, {sec(t0 + 0.35)});')
        if s.get("kicker") and layout != "hero" and layout != "split":
            js.append(f'tl.fromTo("{sel} .kicker", {{opacity:0, y:22}}, {{opacity:1, y:0, duration:.6, '
                      f'ease:"power3.out", immediateRender:false}}, {sec(t0 + 0.7)});')
        if (pic.get("hero") or pic.get("bg")) and layout in ("hero", "split"):
            js.append(f'tl.fromTo("{sel} .piccol", {{opacity:0, scale:.96, x:30}}, {{opacity:1, scale:1, x:0, '
                      f'duration:.85, ease:"power3.out", immediateRender:false}}, {sec(t0 + 0.5)});')
        if has_facts:
            js.append(f'tl.fromTo("{sel} .fact", {{opacity:0, y:20}}, {{opacity:1, y:0, duration:.45, '
                      f'ease:"power2.out", immediateRender:false, stagger:.14}}, {sec(t0 + 1.0)});')
        if has_key:
            last = lines[by_scene[si][-1]]["start"] if by_scene.get(si) else t0
            js.append(f'tl.fromTo("{sel} .keyidea", {{opacity:0, x:-24}}, {{opacity:1, x:0, duration:.6, '
                      f'ease:"power2.out", immediateRender:false}}, {sec(min(last + 0.7, t1 - 1.6))});')

    # subtitles, word by word
    for i, ln in enumerate(lines):
        toks = words_of(ln["text"])
        end = lines[i + 1]["start"] - GAP if i + 1 < len(lines) else DUR
        ws = n.get("_wordtimes", {}).get(str(i))
        if ws and len(ws) == len(toks):
            times = [(w["t0"], w["t1"]) for w in ws]
        else:
            t0 = ln["start"] + 0.25
            t1 = max(t0 + 0.6, (lines[i + 1]["start"] - 0.2) if i + 1 < len(lines)
                     else ln["start"] + len(ln["text"]) / (13.0 if lang == "ru" else 6.5))
            step = (t1 - t0) / max(1, len(toks))
            times = [(t0 + k * step, t0 + (k + 1) * step) for k in range(len(toks))]
        n["_subtitle_spans"][i] = " ".join(
            f'<span class="w" data-t0="{sec(a)}" data-t1="{sec(b)}">{esc(t)}</span>'
            for t, (a, b) in zip(toks, times))
        n["_wordtimes"][str(i)] = times
        for k, (a, b) in enumerate(times):
            js.append(f'tl.to("#sub{i} .w:nth-of-type({k+1})", {{color:"{ACCENT_HL}", duration:.05, '
                      f'ease:"none"}}, {sec(a)});')
            js.append(f'tl.to("#sub{i} .w:nth-of-type({k+1})", {{color:"{SUB_IDLE}", duration:.10, '
                      f'ease:"none"}}, {sec(b)});')
    return js


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--narration", default="narration.json")
    ap.add_argument("--words", default="words.json", help="word timings from nd_align.py")
    ap.add_argument("--art", default="assets/art.json", help="manifest from nd_images.py")
    ap.add_argument("--overrides", help='{"cards": {"2": "<html>"}, "brand": "..."}')
    ap.add_argument("--out", default="index.html")
    ap.add_argument("--lang", choices=["en", "ru"])
    ap.add_argument("--brand", help="bottom-left line")
    ap.add_argument("--badge", help="bottom-right line")
    ap.add_argument("--watermark", help="big faint word behind everything")
    ap.add_argument("--no-watermark", action="store_true")
    a = ap.parse_args()

    n = json.load(open(a.narration))
    lang = a.lang or n.get("language", "en")
    scenes, lines, DUR = n["scenes"], n["lines"], n["duration"]

    art = json.load(open(a.art)) if os.path.exists(a.art) else {"items": []}
    pics = {}
    for it in art.get("items", []):
        if it.get("ok") and it.get("file"):
            pics.setdefault(it["scene"], {})[it.get("kind", "bg")] = it["file"]
    ov = json.load(open(a.overrides)) if a.overrides else {}
    cards = {int(k): v for k, v in (ov.get("cards") or {}).items()}

    pal = normalize_palette((n.get("style") or {}).get("palette"))

    n["_wordtimes"], n["_subtitle_spans"] = {}, {}
    if os.path.exists(a.words):
        try:
            wj = json.load(open(a.words))
            wj = wj.get("lines", wj) if isinstance(wj, dict) else wj
            for i, item in enumerate(wj):
                n["_wordtimes"][str(i)] = item.get("words") or []
        except json.JSONDecodeError:
            print("words.json is not readable, word timings fall back to an estimate")

    # ---- scene sections
    scene_html = []
    for si, s in enumerate(scenes):
        t0 = s["start"]
        t1 = scenes[si + 1]["start"] if si + 1 < len(scenes) else DUR
        layout = (s.get("visual") or {}).get("layout", "full")
        layout = layout if layout in LAYOUTS else "full"
        pic = pics.get(si, {})
        has_facts, has_key = bool(s.get("facts")), bool(s.get("key_idea"))
        tag = {"hook": "Зацепка", "problem": "Проблема", "solution": "Идея", "how": "Как это работает",
               "proof": "Доказательства", "takeaway": "Итог"}.get(s.get("beat", ""), "")
        eyebrow = (f'<div class="eyebrow"><span class="tag">'
                   f'{esc((tag + " · ") if tag and lang == "ru" else "")}'
                   f'{esc(f"{tag} · " if tag and lang == "en" else "")}'
                   f'{esc(f"Раздел {si+1} из {len(scenes)}" if lang == "ru" else f"Scene {si+1} of {len(scenes)}")}'
                   f'</span><span class="rule"></span></div>')
        kicker = f'<div class="kicker">{esc(s.get("kicker"))}</div>' if s.get("kicker") else ""
        body = cards.get(si) or LAYOUTS[layout](s, pic, lang, kicker, eyebrow)
        scene_html.append(
            f'  <section id="scene{si}" class="clip scene" data-start="{sec(t0)}" '
            f'data-duration="{round(t1 - t0, 3)}" data-track-index="1">{body}</section>')

    # the timeline also fills the word timings, so build it before the subtitle sections
    js = build_timeline(n, art, lang)

    # ---- subtitles
    sub_html = []
    for i, ln in enumerate(lines):
        end = lines[i + 1]["start"] - GAP if i + 1 < len(lines) else DUR - 0.4
        sub_html.append(f'  <section id="sub{i}" class="clip subtitle" data-start="{sec(ln["start"])}" '
                        f'data-duration="{round(max(0.5, end - ln["start"]), 2)}" data-track-index="5">'
                        f'<p class="sub-line">{n["_subtitle_spans"][i]}</p></section>')

    marks = "".join(f'<span class="mark">{i+1:02d}</span>' for i in range(len(scenes)))
    wm = "" if a.no_watermark or not a.watermark else \
        f'<div class="watermark" data-layout-ignore>{esc(a.watermark)}</div>'
    brand = a.brand or ov.get("brand") or n.get("title", "")
    badge = a.badge or ov.get("badge") or "© ND"

    gsap = find_gsap(os.getcwd())
    if not gsap:
        raise SystemExit("assets/gsap.min.js not found: run `npx hyperframes init` in the project "
                         "or copy it from another project")

    doc = f'''<!doctype html>
<html lang="{lang}">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=1920, height=1080" />
    <title>{esc(n.get("title") or brand)}</title>
    <script src="assets/gsap.min.js"></script>
    <style>{build_css(pal, lang)}</style>
  </head>
  <body>
    <div id="root" data-composition-id="main" data-start="0" data-width="1920" data-height="1080" data-duration="{sec(DUR)}">
      <div id="bg" class="full-bleed">
        <div class="bg-glow"></div>
        <div class="bg-grid"></div>
        <div class="bg-vig"></div>
        {wm}
        <div class="route" data-layout-ignore>
          <div class="route-track"><div id="progress"></div></div>
          <div class="route-marks">{marks}</div>
        </div>
        <div class="badge" data-layout-ignore>{esc(badge)}</div>
        <div class="brand" data-layout-ignore>{esc(brand)}</div>
      </div>
{chr(10).join(scene_html)}
{chr(10).join(sub_html)}
      <audio id="soundtrack" src="audio/mix.wav" data-start="0" data-duration="{sec(DUR)}" data-track-index="10"></audio>
    </div>
    <script>
      const tl = gsap.timeline({{ paused: true }});
{chr(10).join('      ' + x for x in js)}
      window.__timelines["main"] = tl;
    </script>
  </body>
</html>
'''
    with open(a.out, "w") as f:
        f.write(doc)

    layouts = {}
    for s in scenes:
        lay = (s.get("visual") or {}).get("layout", "full")
        layouts[lay] = layouts.get(lay, 0) + 1
    print(f"{a.out}: {len(scenes)} scenes ({', '.join(f'{k}×{v}' for k, v in layouts.items())}), "
          f"{len(pics)}/{len(scenes)} scenes illustrated, {DUR:.0f} s")


if __name__ == "__main__":
    main()