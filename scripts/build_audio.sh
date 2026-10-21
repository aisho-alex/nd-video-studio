#!/usr/bin/env bash
# Project audio from what's already generated: raw lines → fitting → voice bus → mixdown.
#   scripts/build_audio.sh projects/jev-vs-llm [preset] [extra voice_bus flags, e.g. --wet 0.25]
# Sources (paid, so they're kept in git): audio/raw/scene_NN.wav (одна озвучка на сцену,
# из них nd_voice.py нарезает line_NN.wav) and audio/music.mp3.
# No music.mp3: only the voice is mixed.
#
# Порядок важен: реплики укладываются в окна (темп общий на сцену), потом собираются шиной —
# одна реверберация, громкость и компрессор на весь ролик. Обработка каждого клипа отдельно
# роняла комнату и уровень на стыках, и это слышалось как щелчок.
#
# Рампы краёв реплик ставит voice_bus.py в момент укладки, поэтому у audio/fit края жёсткие
# по построению: их гейтить нельзя, гейтятся стыки в готовой дорожке.
set -euo pipefail
P="${1:?project path}"; PRESET="${2:-trailer}"; shift $(( $# >= 2 ? 2 : 1 ))
S="$(cd "$(dirname "$0")" && pwd)"
python3 "$S/fit_narration.py" "$P/narration.json" "$P/audio/raw" "$P/audio/fit"
python3 "$S/voice_bus.py"   "$P/narration.json" "$P/audio/fit" "$P/audio/fx" --preset "$PRESET" "$@"
python3 "$S/mix.py"         "$P/narration.json" "$P/audio/fx" "$P/audio/music.mp3" "$P/audio/mix.wav"
# промежуточное: рампы тут ещё не на месте, гейт был бы ложным
python3 "$S/audio_qc.py"    "$P/audio/fit" --report-only
python3 "$S/audio_qc.py"    "$P/audio/fx/voice.wav" --narration "$P/narration.json" --clips "$P/audio/fit" \
                            --report "$P/audio/fx/bus_qc.json"
python3 "$S/audio_qc.py"    "$P/audio/mix.wav" --narration "$P/narration.json" --clips "$P/audio/fit"