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
             cookies_browser: str | None = None, on_music=None) -> tuple[dict, Path]:
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
        "format": "bestaudio[ext=m4a]/bestaudio/best",  # m4a spielt das App-Fenster direkt ab
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
        info = ydl.extract_info(url, download=False)
        if info.get("_type") == "playlist" and info.get("entries"):
            info = next(e for e in info["entries"] if e)
        # Zuerst YouTubes eigene Musik-Angaben – die sind sofort da
        yt_music: list[dict] = []
        if (info.get("extractor_key") or "").startswith("Youtube") and info.get("id"):
            try:
                from .youtube_music import fetch_music_section

                yt_music = fetch_music_section(ydl, info["id"])
            except Exception as exc:  # Seite geändert o. ä. – Audio-Analyse läuft trotzdem
                print(f"YouTube-Musikabschnitt nicht lesbar: {type(exc).__name__}: {exc}")
        if on_music:
            on_music(yt_music, info)
        info = ydl.process_ie_result(info, download=True)
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
        "yt_music": yt_music,
    }
    return meta, path


def video_info(url: str, cookies_browser: str | None = None, on_music=None) -> dict:
    """Nur Infos + YouTubes Musik-Abschnitt, ohne Download (dauert wenige Sekunden).

    Funktioniert auch, wenn YouTube den Download per „Bot-Prüfung“ sperrt: die Musik-Angaben
    stehen in der Videoseite selbst.
    """
    from . import updater

    updater.wait()
    import yt_dlp

    from .youtube_music import fetch_music_section

    opts = {
        "quiet": True, "no_warnings": True, "noplaylist": True,
        "skip_download": True, "ignore_no_formats_error": True,  # Bot-Sperre der Streams ignorieren
        "js_runtimes": _js_runtimes(), "retries": 3,
    }
    if cookies_browser:
        opts["cookiesfrombrowser"] = (cookies_browser,)
    with yt_dlp.YoutubeDL(opts) as ydl:
        info: dict = {}
        info_error = None
        try:
            info = ydl.extract_info(url, download=False, process=False) or {}
        except Exception as exc:
            info_error = exc
        vid = info.get("id") or video_id_from_url(url)
        if not vid:
            if info_error:
                raise info_error
            raise RuntimeError("Das ist kein gültiger YouTube-Link.")
        yt_music: list[dict] = []
        try:
            yt_music = fetch_music_section(ydl, vid)
        except Exception as exc:
            print(f"YouTube-Musikabschnitt nicht lesbar: {type(exc).__name__}: {exc}")
            if info_error:
                raise info_error
        info.setdefault("id", vid)
        if on_music:
            on_music(yt_music, info)
    return {
        "id": vid,
        "title": info.get("title") or "",
        "uploader": info.get("uploader") or info.get("channel") or "",
        "duration": info.get("duration"),
        "description": info.get("description") or "",
        "webpage_url": info.get("webpage_url") or f"https://www.youtube.com/watch?v={vid}",
        "thumbnail": info.get("thumbnail") or f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg",
        "yt_track": info.get("track"),
        "yt_artist": info.get("artist"),
        "yt_music": yt_music,
    }


def video_id_from_url(url: str) -> str | None:
    import re

    m = re.search(r"(?:v=|youtu\.be/|shorts/|embed/|live/)([A-Za-z0-9_-]{11})", url)
    return m.group(1) if m else None
