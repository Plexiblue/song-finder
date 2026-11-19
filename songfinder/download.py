"""Video-Metadaten und Tonspur von YouTube (oder jeder anderen yt-dlp-Seite) holen."""
from __future__ import annotations

import shutil
from pathlib import Path
from typing import Callable

ProgressFn = Callable[[float, str], None]


def _js_runtimes() -> dict:
    """yt-dlp braucht seit Ende 2025 eine JavaScript-Laufzeit für YouTube.

    Wir bevorzugen das per pip installierte ``deno``-Paket, damit nichts von Hand
    installiert werden muss; ein systemweites deno/node wird ebenfalls genutzt.
    """
    runtimes: dict = {}
    from .paths import bundled_bin

    if bundled := bundled_bin("deno"):
        return {"deno": {"path": bundled}}
    try:
        from deno import find_deno_bin

        runtimes["deno"] = {"path": find_deno_bin()}
    except Exception:
        if shutil.which("deno"):
            runtimes["deno"] = {}
    if shutil.which("node"):
        runtimes["node"] = {}
    return runtimes or {"deno": {}}


def download(url: str, workdir: Path, progress: ProgressFn | None = None,
             cookies_browser: str | None = None) -> tuple[dict, Path]:
    """Lädt nur die Tonspur herunter. Gibt (Metadaten, Pfad zur Audiodatei) zurück."""
    from . import updater

    updater.wait()
    try:
        import yt_dlp
    except Exception:  # aktualisierte Version defekt → mitgelieferte nehmen
        updater.disable()
        import yt_dlp

    def hook(d: dict) -> None:
        if not progress or d.get("status") != "downloading":
            return
        total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
        if total:
            progress(d.get("downloaded_bytes", 0) / total, "Tonspur wird heruntergeladen …")

    opts = {
        "format": "bestaudio/best",
        "outtmpl": str(workdir / "source.%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "progress_hooks": [hook],
        "js_runtimes": _js_runtimes(),
        "retries": 5,
    }
    if cookies_browser:  # hilft bei "Sign in to confirm you're not a bot"
        opts["cookiesfrombrowser"] = (cookies_browser,)
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        if info.get("_type") == "playlist" and info.get("entries"):
            info = next(e for e in info["entries"] if e)
        path = Path(ydl.prepare_filename(info))

    if not path.exists():  # Endung kann vom Format abweichen
        candidates = sorted(workdir.glob("source.*"))
        if not candidates:
            raise RuntimeError("Download fehlgeschlagen – keine Audiodatei gefunden.")
        path = candidates[0]

    meta = {
        "id": info.get("id"),
        "title": info.get("title") or "",
        "uploader": info.get("uploader") or info.get("channel") or "",
        "duration": info.get("duration"),
        "description": info.get("description") or "",
        "webpage_url": info.get("webpage_url") or url,
        "thumbnail": info.get("thumbnail"),
        # YouTube liefert diese Felder, wenn das Video einen Musik-Abschnitt hat
        "yt_track": info.get("track"),
        "yt_artist": info.get("artist") or info.get("creator"),
    }
    return meta, path
