"""Pfade – funktioniert im Entwicklungsmodus und in der gepackten App (PyInstaller)."""
from __future__ import annotations

import os
import sys
from pathlib import Path

APP_NAME = "SongFinder"
FROZEN = bool(getattr(sys, "frozen", False))


def bundle_dir() -> Path:
    """Ordner mit den mitgelieferten Dateien der App."""
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))


def bundled_bin(name: str) -> str | None:
    """Mitgeliefertes Programm (ffmpeg, deno) aus dem 'bin'-Ordner der App."""
    exe = name + (".exe" if os.name == "nt" else "")
    path = bundle_dir() / "bin" / exe
    return str(path) if path.is_file() else None


def data_dir() -> Path:
    """Beschreibbarer Ordner für Updates und Protokolle."""
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    path = base / APP_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def log_file() -> Path:
    return data_dir() / "songfinder.log"
