#!/usr/bin/env python3
"""Frame images via the hub Image API (FLUX): prompts → assets/*.png.

Two modes:

  ad-hoc     nd_images.py assets "hero:isometric glowing chip" --remove-bg hero
  from plan  nd_images.py assets --from narration.json [--style-file art-style.txt] [--jobs 3]

The plan mode is what a video run uses: it reads narration.json, takes the scene's visual.prompt
from nd_storyboard.py, prefixes the ONE style sentence that fixes the look of the whole video and
picks the picture kind from the scene layout (full/quote → wide bg, hero → cut-out subject,
split → wide bg, data → small accent). It writes assets/art.json, the manifest nd_compose.py
reads: which picture each scene really has. A scene without a picture is not a failure — the
composer lays it out as text, so a refused quota or a failed task still finishes the video.

Cheap to repeat: results are cached by sha1(prompt + style + aspect), so a rerun of the same
plan draws nothing and burns no quota. --force ignores the cache.

  ND_API_KEY=sk-... python3 scripts/nd_images.py projects/x/assets --from projects/x/narration.json
"""

import argparse
import concurrent.futures
import hashlib
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(__file__))
from ndv_common import http, nd_auth, nd_base, nd_json, nd_multipart  # noqa: E402

# layout → (picture kind, aspect). "hero" is a cut-out subject, everything else stays rectangular.
PICTURES = {
    "full": ("bg", "16:9"),
    "quote": ("bg", "16:9"),
    "split": ("bg", "16:9"),
    "hero": ("hero", "1:1"),
    "data": ("accent", "1:1"),
}
NO_TEXT = "no text, no letters, no numbers, no captions, no watermark, no logo, no user interface"


def wait(uid, timeout=600):
    t0 = time.time()
    while time.time() - t0 < timeout:
        st = nd_json(f"/v1/images/tasks/{uid}")
        status = str(st.get("status", "")).lower()
        if status in ("done", "completed", "success", "succeeded", "finished"):
            return st
        if status in ("failed", "error", "cancelled"):
            raise SystemExit(f"task {uid} failed: {st}")
        time.sleep(3)
    raise SystemExit(f"task {uid} timed out after {timeout} s")


def fetch(uid, path):
    with http(f"{nd_base()}/v1/images/tasks/{uid}/result", headers=nd_auth()) as r, open(path, "wb") as f:
        f.write(r.read())


def generate(prompt, path, opts, translate=False):
    body = {"prompt": prompt, "translate": translate}
    if opts:
        body["options"] = opts
    sub = nd_json("/v1/images/generate", body)
    wait(sub["task_uid"])
    fetch(sub["task_uid"], path)
    return sub.get("prompt_used", prompt)


def postprocess(path, remove_bg=False, upscale=False):
    for flag, endpoint in ((remove_bg, "/v1/images/background/remove"), (upscale, "/v1/images/upscale")):
        if not flag:
            continue
        with open(path, "rb") as f:
            sub = nd_multipart(endpoint, {}, {"image": (os.path.basename(path), f.read(), "image/png")})
        wait(sub["task_uid"])
        fetch(sub["task_uid"], path)


def cache_key(prompt, opts):
    blob = prompt + "|" + json.dumps(opts or {}, sort_keys=True)
    return hashlib.sha1(blob.encode()).hexdigest()[:16]


def one_item(job, out_dir, translate):
    """Draw one picture. Returns a manifest entry; never raises — a failed picture is a warning."""
    name, prompt, opts, remove_bg, upscale, force = job
    path = os.path.join(out_dir, f"{name}.png")
    cdir = os.path.join(out_dir, ".cache")
    os.makedirs(cdir, exist_ok=True)
    cpath = os.path.join(cdir, f"{cache_key(prompt, opts)}.png")

    try:
        if not force and os.path.exists(cpath) and os.path.getsize(cpath) > 1024:
            with open(cpath, "rb") as f:
                data = f.read()
            with open(path, "wb") as f:
                f.write(data)
            return {"name": name, "file": f"assets/{name}.png", "ok": True, "cached": True}

        opts = dict(opts or {})
        # aspect_ratio is what the hub honours; width/height are accepted by some builds.
        if "aspect" in opts:
            opts["aspect_ratio"] = opts.pop("aspect")
        generate(prompt, path, opts, translate)
        postprocess(path, remove_bg=remove_bg, upscale=upscale)
        with open(path, "rb") as f, open(cpath, "wb") as c:
            c.write(f.read())
        return {"name": name, "file": f"assets/{name}.png", "ok": True, "cached": False}
    except SystemExit as e:
        return {"name": name, "file": None, "ok": False, "error": str(e)[:200]}
    except Exception as e:  # noqa: BLE001 — one bad prompt must not sink the whole batch
        return {"name": name, "file": None, "ok": False, "error": f"{type(e).__name__}: {e}"[:200]}


def plan_jobs(narration, out_dir, aspect, style, translate, force, only=None):
    """One job per scene from narration.json's scenes[].visual. only = scene indices to draw."""
    scenes = narration["scenes"]
    style_art = (narration.get("style") or {}).get("art", "").strip()
    jobs = []
    for si, sc in enumerate(scenes):
        if only and si not in only:
            continue
        vis = sc.get("visual") or {}
        prompt = (vis.get("prompt") or "").strip()
        if not prompt:
            print(f"scene {si}: no visual.prompt, it will be laid out as text")
            continue
        kind, default_aspect = PICTURES.get(vis.get("layout", "full"), ("bg", "16:9"))
        name = f"scene{si:02d}_{kind}"
        full_prompt = f"{prompt}. {style_art}, {NO_TEXT}" if style_art else f"{prompt}. {NO_TEXT}"
        jobs.append({"name": name, "prompt": full_prompt, "kind": kind,
                     "opts": {"aspect": aspect or default_aspect},
                     "remove_bg": kind == "hero", "scene": si})
    return jobs


def run_jobs(jobs, out_dir, translate, workers, force=False):
    """Draw them, at most `workers` at a time. Each operation = 1 unit of image quota."""
    out = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {pool.submit(one_item, (j["name"], j["prompt"], j["opts"], j["remove_bg"],
                                       False, force), out_dir, translate): j for j in jobs}
        for fut in concurrent.futures.as_completed(futs):
            j = futs[fut]
            res = fut.result()
            res.update(scene=j["scene"], kind=j["kind"], layout=j.get("layout"))
            state = "cache" if res.get("cached") else ("ok" if res["ok"] else "FAIL")
            print(f"{j['name']:<18} {state:<6} {j['prompt'][:60]}")
            if not res["ok"]:
                print(f"  → {res['error']}  (сцена {j['scene']} останется текстовой)")
            out.append(res)
    return sorted(out, key=lambda r: (r["scene"], r["kind"]))


def from_narration(a):
    narration = json.load(open(a.from_narration))
    style = a.style or ""
    if a.style_file:
        style = open(a.style_file).read().strip()
    if not style:
        style = (narration.get("style") or {}).get("art", "").strip()

    only = {int(x) for x in a.scenes.split(",") if x.strip()} if a.scenes else None
    jobs = plan_jobs(narration, a.out_dir, a.aspect, style, a.no_translate, a.force, only)
    for j in jobs:
        j["layout"] = narration["scenes"][j["scene"]]["visual"].get("layout", "full")
    os.makedirs(a.out_dir, exist_ok=True)
    print(f"{len(jobs)} pictures, style: {style[:70] or '(none)'}")

    results = run_jobs(jobs, a.out_dir, a.translate, a.jobs, a.force)

    # merge with what an earlier run already drew, so the manifest stays complete
    mpath = os.path.join(a.out_dir, "art.json")
    manifest = {}
    if os.path.exists(mpath):
        manifest = json.load(open(mpath))
    manifest.setdefault("style", style)
    manifest.setdefault("aspect", a.aspect or "16:9")
    by_name = {it["name"]: it for it in manifest.get("items", [])}
    for res in results:
        by_name[res["name"]] = {k: v for k, v in res.items() if k != "cached"}
    manifest["items"] = sorted(by_name.values(), key=lambda r: r["name"])
    with open(mpath, "w") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=1)

    ok = sum(1 for r in manifest["items"] if r["ok"])
    print(f"{ok}/{len(manifest['items'])} pictures ready → {mpath}")
    try:
        print("quota:", json.dumps(nd_json("/v1/images/quota"), ensure_ascii=False)[:200])
    except SystemExit as e:
        print(f"quota: {e}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("out_dir")
    ap.add_argument("items", nargs="*", help="name:prompt (ad-hoc mode)")
    ap.add_argument("--from", dest="from_narration", help="narration.json: one picture per scene")
    ap.add_argument("--aspect", help="16:9 for wide scenes, 1:1 for cut-outs; empty = per layout")
    ap.add_argument("--style", help="style sentence shared by every prompt (overrides narration)")
    ap.add_argument("--style-file", help="read the style sentence from a file")
    ap.add_argument("--scenes", help="only these scene indices, e.g. --scenes 0,2,5 (redraw a few)")
    ap.add_argument("--jobs", type=int, default=3, help="pictures in flight at once")
    ap.add_argument("--force", action="store_true", help="ignore the prompt cache")
    ap.add_argument("--remove-bg", nargs="*", default=[], help="ad-hoc: names to cut out")
    ap.add_argument("--upscale", nargs="*", default=[], help="ad-hoc: names to upscale ×4")
    ap.add_argument("--no-translate", action="store_true", help="ad-hoc: don't translate prompts to EN")
    ap.add_argument("--translate", action="store_true",
                    help="plan mode: let the hub translate prompts to EN (they are EN already)")
    ap.add_argument("--options", default="{}", help='ad-hoc: extra generation options JSON')
    a = ap.parse_args()
    os.makedirs(a.out_dir, exist_ok=True)

    if a.from_narration:
        from_narration(a)
        return

    for item in a.items:
        name, prompt = item.split(":", 1)
        path = os.path.join(a.out_dir, f"{name}.png")
        opts = json.loads(a.options)
        generate(prompt, path, opts, not a.no_translate)
        print(f"{name}: {path}")
        postprocess(path, name in a.remove_bg, name in a.upscale)
    q = nd_json("/v1/images/quota")
    print("quota:", json.dumps(q, ensure_ascii=False)[:200])


if __name__ == "__main__":
    main()