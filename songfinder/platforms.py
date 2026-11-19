"""Lizenz-Plattformen erkennen (über Label, Links und Videobeschreibung)."""
from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import quote_plus

from .recognize import normalize


@dataclass(frozen=True)
class Platform:
    name: str
    domain: str
    labels: tuple[str, ...]  # Teilstrings im Label/Copyright (klein geschrieben)
    kind: str = "Stock-Musik"  # Stock-Musik | Kommerziell | Kostenlos


PLATFORMS: list[Platform] = [
    Platform("Epidemic Sound", "epidemicsound.com", ("epidemic sound", "epidemic music")),
    Platform("Artlist", "artlist.io", ("artlist",)),
    Platform("Musicbed", "musicbed.com", ("musicbed",)),
    Platform("Soundstripe", "soundstripe.com", ("soundstripe",)),
    Platform("Uppbeat", "uppbeat.io", ("uppbeat",)),
    Platform("Lickd", "lickd.co", ("lickd",), "Kommerziell"),
    Platform("PremiumBeat", "premiumbeat.com", ("premiumbeat",)),
    Platform("Envato Elements / AudioJungle", "elements.envato.com", ("envato", "audiojungle")),
    Platform("Motion Array", "motionarray.com", ("motion array", "motionarray")),
    Platform("Storyblocks", "storyblocks.com", ("storyblocks", "audioblocks")),
    Platform("Pond5", "pond5.com", ("pond5",)),
    Platform("Audio Network", "audionetwork.com", ("audio network",)),
    Platform("Musicvine", "musicvine.com", ("musicvine",)),
    Platform("Monstercat (Gold)", "monstercat.com", ("monstercat",), "Kommerziell"),
    Platform("StreamBeats", "streambeats.com", ("streambeats", "harris heller"), "Kostenlos"),
    Platform("NoCopyrightSounds", "ncs.io", ("nocopyrightsounds", "ncs release", "ncs "), "Kostenlos"),
    Platform("Chillhop", "chillhop.com", ("chillhop",), "Kostenlos"),
]

# Plattformen, auf denen wir bei jedem Song eine Suche anbieten
SEARCH_PLATFORMS = ["Epidemic Sound", "Artlist", "Lickd", "Uppbeat", "Soundstripe", "Musicbed"]

_BY_NAME = {p.name: p for p in PLATFORMS}


def platform_from_label(*texts: str | None) -> Platform | None:
    blob = " ".join(t for t in texts if t).lower()
    if not blob:
        return None
    for p in PLATFORMS:
        if any(lbl in blob for lbl in p.labels):
            return p
    return None


def search_links(title: str, artist: str) -> list[dict]:
    q = f"{title} {artist}".strip()
    links = []
    for name in SEARCH_PLATFORMS:
        p = _BY_NAME[name]
        links.append({
            "label": name,
            # Google-Seitensuche funktioniert zuverlässig, auch wenn Plattformen ihre
            # internen Such-URLs ändern.
            "url": f"https://www.google.com/search?q={quote_plus(f'site:{p.domain} {q}')}",
        })
    links.append({"label": "Spotify", "url": f"https://open.spotify.com/search/{quote_plus(q)}"})
    links.append({"label": "YouTube", "url": f"https://www.youtube.com/results?search_query={quote_plus(q)}"})
    return links


# --------------------------------------------------------------------------- #
# Videobeschreibung auswerten
# --------------------------------------------------------------------------- #

_URL_RE = re.compile(r"https?://[^\s<>\"')\]]+", re.I)
_TIMESTAMP_RE = re.compile(r"^\(?\d{1,2}:\d{2}")
_KEYWORDS = re.compile(
    r"(🎵|🎶|♪|♫|\bmusi[ck]\b|\bmusik\b|\bsongs?\b|\btracks?\b|\bsoundtrack\b|\bbgm\b|"
    r"\bbackground music\b|\bhintergrundmusik\b|\bprovided by\b|\bcourtesy of\b)", re.I
)


def parse_description(description: str) -> list[dict]:
    """Findet Musik-Angaben in der Videobeschreibung (Links und Credit-Zeilen)."""
    credits: list[dict] = []
    seen: set[str] = set()
    lines = [ln.strip() for ln in (description or "").splitlines()]
    in_block = False  # Zeilen unter einer Überschrift wie "🎵 Musik:" gehören dazu
    for i, line in enumerate(lines):
        if not line:
            in_block = False
            continue
        urls = _URL_RE.findall(line)
        url_platforms = [(u, p) for u in urls for p in PLATFORMS if p.domain.split(".")[0] in u.lower()]
        text_platform = platform_from_label(line)
        keyword = bool(_KEYWORDS.search(line))
        if keyword and not urls and len(normalize(line).split()) <= 4 and line.rstrip().endswith(":") or \
                re.fullmatch(r"[\W_]*(musi[ck]|songs?|tracks?|soundtrack|music used|verwendete musik)[\W_]*",
                             line, re.I):
            in_block = True
            continue
        is_credit = bool(url_platforms) or bool(text_platform) or keyword or in_block
        if not is_credit:
            continue
        if _TIMESTAMP_RE.match(line) and not (url_platforms or text_platform or keyword):
            continue  # Kapitelmarke, kein Song
        # Reine Werbezeilen wie "Hol dir 30 Tage Epidemic Sound gratis: <link>" erkennen
        promo = bool(re.search(r"(gratis|free trial|kostenlos testen|30 tage|30 days|"
                               r"affiliate|rabatt|discount|referral|sign up|anmelden)", line, re.I))
        text = _URL_RE.sub("", line).strip(" -–:|•")
        # Credits stehen oft in der Zeile *vor* dem Link
        if not text and i > 0 and lines[i - 1]:
            text = _URL_RE.sub("", lines[i - 1]).strip(" -–:|•")
        if credits and not _URL_RE.sub("", line).strip(" -–:|•") and credits[-1]["text"] == text \
                and not credits[-1]["urls"]:
            credits.pop()  # "Titel - Künstler" + Link in der nächsten Zeile → ein Eintrag
        key = normalize(text) + "|".join(urls)
        if key in seen:
            continue
        seen.add(key)
        platform = url_platforms[0][1] if url_platforms else text_platform
        credits.append({
            "text": text or line,
            "urls": urls,
            "platform": platform.name if platform else None,
            "promo": promo,
        })
    # Zeilen ohne Plattform/Link, die nur "Musik" enthalten (z.B. "Musik: siehe unten")
    # sind wenig hilfreich – nur behalten, wenn sie konkret aussehen.
    return [c for c in credits if c["platform"] or c["urls"] or len(normalize(c["text"]).split()) >= 3][:40]


def credit_matches(title: str, artist: str, credits: list[dict]) -> dict | None:
    """Prüft, ob ein erkannter Song in der Beschreibung genannt wird."""
    t = normalize(title)
    if len(t) < 3:
        return None
    for c in credits:
        blob = normalize(c["text"] + " " + " ".join(c["urls"]).replace("-", " "))
        if t in blob:
            return c
    return None
