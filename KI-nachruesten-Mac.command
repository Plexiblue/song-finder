#!/bin/bash
cd "$(dirname "$0")"
[ -x .venv/bin/python ] || { echo "Bitte zuerst einmal Start-Mac.command ausführen."; exit 1; }
echo "Installiere KI-Stimmentrennung (Demucs + PyTorch, ca. 1–2 GB) …"
.venv/bin/python -m pip install --disable-pip-version-check -r requirements-ki.txt
echo; echo "Fertig. Song Finder neu starten, dann ist „KI-Stimmentrennung“ aktiv."
