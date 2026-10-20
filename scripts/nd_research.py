#!/usr/bin/env python3
"""Research for a video: queries → excerpts with sources in sources.md.

The script is then written only from these excerpts, and the frame shows what is confirmed vs estimated.

Two backends for web search:

  * `--gateway URL` (or SEARCH_GATEWAY_URL) — the Morpheus search gateway.
    Web queries go as one batch to POST /jobs and are collected later. That is
    the right shape here: this script runs 2–5 queries per video and nobody is
    waiting on the answer, so it can take the deferred tariff (16× cheaper) and
    the gateway handles the fallback if it is unavailable. Telegram still goes
    to the hub, the gateway has no such provider.
  * default — the hub Search API /v1/search/web, as before.

  ND_API_KEY=sk-... python3 scripts/nd_research.py projects/x/sources.md "Jev TypeSafe AI architecture" "Jev pricing latency"
  ... --tg "Jev model"          # also search Telegram channels
  ... --gateway http://127.0.0.1:8110
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(__file__))
from ndv_common import nd_json  # noqa: E402


def gateway_post(gateway, path, payload):
    req = urllib.request.Request(
        f"{gateway.rstrip('/')}{path}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def gateway_get(gateway, path):
    req = urllib.request.Request(f"{gateway.rstrip('/')}{path}")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def gateway_research(gateway, queries, limit, wait_sec, poll_sec):
    """Batch web search through the gateway. Returns {query: [results]}."""
    job = gateway_post(gateway, "/jobs", {
        "queries": queries, "limit": limit, "use_deferred": True,
    })
    job_id = job["job_id"]
    deadline = time.time() + wait_sec
    while time.time() < deadline:
        time.sleep(poll_sec)
        job = gateway_get(gateway, f"/jobs/{job_id}")
        if job.get("status") == "done":
            return job.get("results") or {}
        if job.get("status") == "failed":
            break
    raise RuntimeError(f"задание {job_id} не завершилось за {wait_sec} c")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("out")
    ap.add_argument("queries", nargs="+")
    ap.add_argument("--limit", type=int, default=5)
    ap.add_argument("--tg", nargs="*", default=[], help="Telegram channel queries")
    ap.add_argument("--chars", type=int, default=1500, help="excerpt length per source, chars")
    ap.add_argument("--gateway", default=os.environ.get("SEARCH_GATEWAY_URL", ""),
                    help="Morpheus search gateway; empty = hub Search API")
    ap.add_argument("--wait", type=int, default=1800,
                    help="сколько ждать пакетное задание, c")
    ap.add_argument("--poll", type=int, default=10, help="интервал опроса задания, c")
    a = ap.parse_args()

    parts, seen = [], set()
    if a.gateway:
        try:
            batch = gateway_research(a.gateway, a.queries, a.limit, a.wait, a.poll)
        except (urllib.error.URLError, OSError, ValueError, RuntimeError) as exc:
            # Не молча теряем ресёрч: без источников сториборд всё равно соберётся,
            # но на выдуманных фактах. Падаем на хаб — он по умолчанию.
            print(f"гейтвей не ответил ({exc}); откат на hub Search API",
                  file=sys.stderr)
            batch = None
        if batch is not None:
            for q in a.queries:
                rows = batch.get(q) or []
                parts.append(f"## web: {q}\n")
                for r in rows:
                    if r["url"] in seen:
                        continue
                    seen.add(r["url"])
                    parts.append(
                        f"### {r.get('title', '').strip()}\n{r['url']} {r.get('date') or ''}\n\n"
                        f"{(r.get('snippet') or '').strip()[:a.chars]}\n")
                print(f"web «{q}»: {len(rows)}")
    if not a.gateway or not parts:
        for q in a.queries:
            res = nd_json("/v1/search/web", {"query": q, "limit": a.limit})
            parts.append(f"## web: {q}\n")
            for r in res.get("results", []):
                if r["url"] in seen:
                    continue
                seen.add(r["url"])
                parts.append(f"### {r.get('title', '').strip()}\n{r['url']} {r.get('date') or ''}\n\n"
                             f"{(r.get('content') or '').strip()[:a.chars]}\n")
            print(f"web «{q}»: {len(res.get('results', []))}")
    for q in a.tg:
        res = nd_json(f"/v1/search/tg?q={urllib.parse.quote(q)}&limit={a.limit}")
        parts.append(f"## tg: {q}\n")
        for r in res.get("results", []):
            parts.append(f"### {r.get('title', '').strip()}\n{r.get('url', '')} {r.get('date') or ''}\n\n"
                         f"{(r.get('content') or '').strip()[:a.chars]}\n")
        print(f"tg «{q}»: {len(res.get('results', []))}")
    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    with open(a.out, "w") as f:
        f.write("# Sources\n\n" + "\n".join(parts))
    print("→", a.out)


if __name__ == "__main__":
    main()
