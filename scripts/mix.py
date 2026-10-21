#!/usr/bin/env python3
"""Mixdown: the voice bus + ducked music → one wav.

Voice is assembled once by voice_bus.py (audio/fx/voice.wav) — one room, one loudness, one
compressor for the whole video — and already normalized there to −16 LUFS. Music sits around
−27 LUFS and ducks under the voice via a sidechain compressor. loudnorm resamples to 192 kHz
internally and eats the tail, so it's always followed by aresample and a final apad to length.

For a fallback (mixing audio/fit or audio/fx directly, without the bus) the per-line layout
below still works: clips are placed on the timeline with adelay and ramped at their own edges.

  python3 scripts/mix.py projects/x/narration.json projects/x/audio/fx music.mp3 projects/x/audio/mix.wav
  python3 scripts/mix.py ... none projects/x/audio/mix.wav      # no music, voice only
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from ndv_common import FADE_IN, FADE_OUT, clip_name, duration, edge_fades, ffmpeg, load_narration  # noqa: E402

BUS = "voice.wav"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("narration")
    ap.add_argument("voice_dir")
    ap.add_argument("music")
    ap.add_argument("out")
    ap.add_argument("--music-lufs", type=float, default=-27)
    ap.add_argument("--music-offset", type=float, default=0, help="track start offset, seconds")
    ap.add_argument("--duck-ratio", type=float, default=6)
    ap.add_argument("--fade-in", type=float, default=FADE_IN, help="line onset, seconds")
    ap.add_argument("--fade-out", type=float, default=FADE_OUT, help="line offset, seconds")
    a = ap.parse_args()

    n = load_narration(a.narration)
    T = n["duration"]
    no_music = a.music == "none" or not os.path.exists(a.music)
    bus = os.path.join(a.voice_dir, BUS)
    use_bus = os.path.exists(bus)
    if no_music:
        print(f"no music ({a.music}), mixing voice only")
    print("voice:", "bus" if use_bus else f"{len(n['lines'])} clips (no {BUS}, fallback layout)")

    if use_bus:
        # Голос уже разложен и обработан целиком: берём дорожку как есть.
        args = ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"] if no_music else ["-i", a.music]
        args += ["-i", bus]
        graph = [f"[1:a]aresample=48000,aformat=channel_layouts=stereo,apad=whole_dur={T}[vo]"]
    else:
        # без шины (смешиваем audio/fit напрямую): раскладка реплик по таймлайну
        args = ["-f", "lavfi", "-t", str(T), "-i", "anullsrc=r=48000:cl=stereo"] if no_music else ["-i", a.music]
        for i in range(len(n["lines"])):
            args += ["-i", os.path.join(a.voice_dir, clip_name(i))]
        graph = []
        for i, ln in enumerate(n["lines"]):
            ms = int(ln["start"] * 1000)
            clip = os.path.join(a.voice_dir, clip_name(i))
            end = ln["start"] + duration(clip)
            ramps = edge_fades(dur=end, fin=a.fade_in, fout=a.fade_out, start=ln["start"])
            graph.append(f"[{i + 1}:a]aresample=48000,aformat=channel_layouts=stereo,adelay={ms}|{ms},"
                         f"apad=whole_dur={T}{',' + ramps if ramps else ''}[v{i}]")
        graph.append("".join(f"[v{i}]" for i in range(len(n["lines"]))) +
                     f"amix=inputs={len(n['lines'])}:normalize=0:duration=longest,atrim=0:{T}[vo]")

    f = graph[:]
    f.append("[vo]asplit=2[voa][sc]")
    if no_music:
        f.append(f"[0:a]apad=whole_dur={T}[mu]")
    else:
        f.append(f"[0:a]loudnorm=I={a.music_lufs}:TP=-6:LRA=11,aresample=48000,atrim={a.music_offset}:{a.music_offset + T},"
                 f"asetpts=N/SR/TB,afade=t=in:st=0:d=1.5,afade=t=out:st={T - 4}:d=4,apad=whole_dur={T}[mu]")
    f.append(f"[mu][sc]sidechaincompress=threshold=0.03:ratio={a.duck_ratio}:attack=30:release=500[duck]")
    tail = ",".join(x for x in (f"alimiter=limit=0.9", "apad=whole_dur=%g" % T,
                                 edge_fades(dur=T, fin=0, fout=0.5), f"atrim=0:{T}") if x)
    f.append(f"[duck][voa]amix=inputs=2:normalize=0:duration=longest,{tail}[out]")
    ffmpeg(*args, "-filter_complex", ";".join(f), "-map", "[out]", "-ac", "2", "-ar", "48000", "-c:a", "pcm_s16le", a.out)
    print(f"{a.out}: {duration(a.out):.2f}s (expected {T}s)")


if __name__ == "__main__":
    main()
