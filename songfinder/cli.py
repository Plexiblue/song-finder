"""Kommandozeile:  python -m songfinder <YouTube-Link oder Datei> [Optionen]"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .export import to_csv, to_text
from .pipeline import MODES, Options, analyze


def keys_from_env() -> dict:
    return {k: os.environ.get(env, "") for k, env in {
        "audd_token": "AUDD_TOKEN", "acr_host": "ACR_HOST",
        "acr_key": "ACR_ACCESS_KEY", "acr_secret": "ACR_ACCESS_SECRET"}.items()}


def main() -> None:
    p = argparse.ArgumentParser(prog="songfinder", description="Findet alle Songs in einem YouTube-Video.")
    p.add_argument("quelle", nargs="?", help="YouTube-Link oder Pfad zu einer Video-/Audiodatei")
    p.add_argument("--modus", choices=list(MODES), default="normal",
                   help="schnell = 15 s Raster, normal = 9 s, gruendlich = 5 s")
    p.add_argument("--ki", choices=["auto", "an", "aus"], default="auto",
                   help="KI-Stimmentrennung (Demucs) verwenden")
    p.add_argument("--shazam", action="store_true",
                   help="Zusätzlich Shazam fragen (kostenlos, aber langsam; Shazam blockiert schnell)")
    p.add_argument("--json", type=Path, help="Ergebnis zusätzlich als JSON speichern")
    p.add_argument("--csv", type=Path, help="Ergebnis zusätzlich als CSV (Excel) speichern")
    p.add_argument("--web", action="store_true", help="Web-Oberfläche starten")
    a = p.parse_args()

    if a.web or not a.quelle:
        from .web import run
        run()
        return

    def progress(pct: int, msg: str) -> None:
        sys.stderr.write(f"\r[{pct:3d}%] {msg[:90]:<90}")
        sys.stderr.flush()

    try:
        result = analyze(a.quelle, Options(mode=a.modus, use_ai=a.ki, keys=keys_from_env(),
                                         use_shazam=a.shazam), progress)
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception as exc:
        from .errors import friendly_error

        sys.stderr.write(f"\n\nFehler: {friendly_error(exc)}\n")
        sys.exit(1)
    sys.stderr.write("\n\n")
    print(to_text(result))
    if a.json:
        a.json.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    if a.csv:
        a.csv.write_text(to_csv(result), encoding="utf-8")
