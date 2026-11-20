"""Liest YouTubes eigenen „Musik“-Abschnitt (Content-ID-Daten) aus der Videoseite.

Viele Song-Finder-Websites sind deshalb so schnell: YouTube kennt die Songs eines Videos bereits
über Content ID (auch Epidemic Sound registriert seine Musik dort) und zeigt sie in der
Beschreibung unter „Musik“ an – bis zu 10 Songs, mit Künstler, Album und Lizenzgeber.

YouTube hat das Format mehrfach geändert; deshalb werden alle bekannten Varianten gelesen:
  1. ``videoDescriptionMusicSectionRenderer`` → ``carouselLockups`` → ``infoRows`` (SONG/ARTIST/…)
  2. Karten mit ``videoAttributeViewModel`` (title = Song, subtitle = Künstler)
  3. Alte ``metadataRowRenderer``-Zeilen (Song/Artist/Album/License, Trennlinie zwischen Songs)
"""
from __future__ import annotations

import json
import re
from typing import Any

_KEYS = {
    "song": "title", "titel": "title", "track": "title",
    "artist": "artist", "künstler": "artist", "kuenstler": "artist", "interpret": "artist",
    "album": "album",
    "licenses": "license", "license": "license", "lizenzen": "license", "lizenz": "license",
    "licensed to youtube by": "license",
}


def text_of(node: Any) -> str:
    """Text aus den verschiedenen YouTube-Textformaten holen."""
    if node is None:
        return ""
    if isinstance(node, str):
        return node.strip()
    if isinstance(node, dict):
        if "simpleText" in node:
            return str(node["simpleText"]).strip()
        if "runs" in node:
            return "".join(r.get("text", "") for r in node["runs"] if isinstance(r, dict)).strip()
        if "content" in node:
            return text_of(node["content"])
        if "text" in node:
            return text_of(node["text"])
    return ""


def _walk(node: Any):
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v)


def _clean(song: dict) -> dict | None:
    title = song.get("title", "").strip()
    if not title:
        return None
    return {
        "title": title,
        "artist": song.get("artist", "").strip(),
        "album": song.get("album", "").strip() or None,
        "license": song.get("license", "").strip() or None,
    }


def _from_info_rows(lockup: dict) -> dict | None:
    song: dict[str, str] = {}
    for row in _walk(lockup.get("infoRows", [])):
        r = row.get("infoRowRenderer")
        if not r:
            continue
        key = _KEYS.get(text_of(r.get("title")).lower())
        value = text_of(r.get("defaultMetadata")) or text_of(r.get("expandedMetadata"))
        if key and value:
            song.setdefault(key, value)
    if "title" not in song:  # manchmal steht der Titel nur im Video-Kärtchen
        for n in _walk(lockup.get("videoLockup", {})):
            if "title" in n and (t := text_of(n["title"])):
                song["title"] = t
                break
    return _clean(song)


def extract_music(data: dict) -> list[dict]:
    songs: list[dict] = []

    # Variante 1
    for n in _walk(data):
        sec = n.get("videoDescriptionMusicSectionRenderer")
        if not sec:
            continue
        for lock in _walk(sec.get("carouselLockups", [])):
            if (c := lock.get("carouselLockupRenderer")) and (s := _from_info_rows(c)):
                songs.append(s)

    # Variante 2: Karten, die in einem Abschnitt mit Überschrift „Music/Musik“ liegen
    if not songs:
        for n in _walk(data):
            lst = n.get("horizontalCardListRenderer")
            if not lst:
                continue
            header = text_of(next((h.get("title") for h in _walk(lst.get("header", {}))
                                   if isinstance(h, dict) and "title" in h), None))
            if header.lower() not in ("music", "musik"):
                continue
            for card in _walk(lst.get("cards", [])):
                vm = card.get("videoAttributeViewModel")
                if vm and (s := _clean({"title": text_of(vm.get("title")),
                                        "artist": text_of(vm.get("subtitle")),
                                        "album": text_of(vm.get("secondarySubtitle"))})):
                    songs.append(s)

    # Variante 3: alte Metadatenzeilen
    if not songs:
        current: dict[str, str] = {}
        for n in _walk(data):
            rows = n.get("metadataRowContainerRenderer", {}).get("rows")
            if not rows:
                continue
            for row in rows:
                r = row.get("metadataRowRenderer")
                if not r:
                    continue
                key = _KEYS.get(text_of(r.get("title")).lower())
                value = text_of((r.get("contents") or [None])[0])
                if key == "title" and current.get("title"):
                    if s := _clean(current):
                        songs.append(s)
                    current = {}
                if key and value:
                    current[key] = value
                if r.get("hasDividerLine") and current.get("title"):
                    if s := _clean(current):
                        songs.append(s)
                    current = {}
        if current.get("title") and (s := _clean(current)):
            songs.append(s)

    # Doppelte entfernen
    seen, out = set(), []
    for s in songs:
        k = (s["title"].lower(), s["artist"].lower())
        if k not in seen:
            seen.add(k)
            out.append(s)
    return out


def parse_initial_data(html: str) -> dict | None:
    m = re.search(r"(?:var\s+ytInitialData|window\[\"ytInitialData\"\])\s*=\s*", html)
    if not m:
        return None
    try:
        data, _ = json.JSONDecoder().raw_decode(html, m.end())
        return data
    except ValueError:
        return None


def fetch_music_section(ydl, video_id: str) -> list[dict]:
    """Holt die Videoseite (über yt-dlp, mit dessen Cookies/Consent) und liest den Musik-Abschnitt."""
    from yt_dlp.networking import Request

    url = f"https://www.youtube.com/watch?v={video_id}&hl=en&gl=US&has_verified=1&bpctr=9999999999"
    html = ydl.urlopen(Request(url, headers={"Accept-Language": "en-US,en;q=0.9"})).read() \
        .decode("utf-8", "replace")
    data = parse_initial_data(html)
    return extract_music(data) if data else []
