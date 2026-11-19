#!/bin/bash
cd "$(dirname "$0")"
PY=$(command -v python3 || command -v python)
if [ -z "$PY" ]; then
  echo "Python wurde nicht gefunden. Bitte Python 3.10+ installieren: https://www.python.org/downloads/"
  read -n 1 -s -r -p "Taste drücken zum Schließen"; exit 1
fi
if [ ! -x .venv/bin/python ]; then
  echo "Ersteinrichtung – das dauert beim ersten Mal 1–2 Minuten …"
  "$PY" -m venv .venv || exit 1
fi
echo "Prüfe Updates (yt-dlp wird aktuell gehalten) …"
.venv/bin/python -m pip install -q --disable-pip-version-check -U -r requirements.txt || {
  read -n 1 -s -r -p "Installation fehlgeschlagen – Taste drücken"; exit 1; }
.venv/bin/python -m songfinder
