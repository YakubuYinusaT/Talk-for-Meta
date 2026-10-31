#!/usr/bin/env bash
# Gives the server a voice of its own (Piper, open source, runs on the CPU): it speaks when the hosted voice is out of credit or out of reach, so a caller
# never hears silence. The hosted voice stays the first choice.
#
#   bash setup_voice.sh install    install the package and one English voice, point the settings at it, restart, and speak a test sentence
#   bash setup_voice.sh check      speak a test sentence with it and show the time it took
#
# Run as root, with the app installed in /opt/sofa-health (src for the code, venv for its Python, data for its files). The voice is a download of about 60 MB.
set -euo pipefail

NEW=/opt/sofa-health
ENVFILE="$NEW/src/.env"
VOICE="${PIPER_VOICE:-en_GB-jenny_dioco-medium}"      # a clear British English voice; en_US-lessac-medium is the usual alternative
FALLBACK_VOICE=en_US-lessac-medium
DIR="$NEW/data/voices"

[[ $EUID -eq 0 ]] || { echo "Run this as root"; exit 1; }

set_env() { sed -i "/^$1=/d" "$ENVFILE"; echo "$1=$2" >> "$ENVFILE"; }

check() {
  (cd "$NEW/src" && sudo -u sofa "$NEW/venv/bin/python" -m scripts.voice_local_check)
}

install_voice() {
  [[ -d "$NEW/src" ]] || { echo "The build is not in $NEW: install the app there first."; exit 1; }
  echo "1/4 Installing the package"
  "$NEW/venv/bin/pip" install --quiet piper-tts
  echo "2/4 Downloading the voice $VOICE (about 60 MB)"
  install -d -o sofa -g sofa "$DIR"
  if ! sudo -u sofa "$NEW/venv/bin/python" -m piper.download_voices "$VOICE" --data-dir "$DIR"; then
    echo "   that voice was not available: taking $FALLBACK_VOICE"
    VOICE="$FALLBACK_VOICE"
    sudo -u sofa "$NEW/venv/bin/python" -m piper.download_voices "$VOICE" --data-dir "$DIR"
  fi
  echo "3/4 Pointing the settings at it and restarting"
  set_env PIPER_MODEL "$DIR/$VOICE.onnx"
  chown sofa:sofa "$ENVFILE"
  systemctl restart sofa-health
  for i in $(seq 1 30); do curl -fsS --max-time 5 http://127.0.0.1:8001/health >/dev/null 2>&1 && break; sleep 2; done
  echo "4/4 Speaking a test sentence"
  check
}

case "${1:-}" in
  install) install_voice ;;
  check) check ;;
  *) echo "usage: bash setup_voice.sh install|check"; exit 2 ;;
esac
