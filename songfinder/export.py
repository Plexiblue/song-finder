"""Ergebnis als CSV (für Excel) oder Text ausgeben."""
from __future__ import annotations

import csv
import io


def to_csv(result: dict) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")  # Semikolon: öffnet sich in deutschem Excel korrekt
    w.writerow(["Titel", "Künstler", "Plattform", "Label", "Zeit im Video", "Sicherheit",
                "In Beschreibung", "Album", "ISRC", "Shazam-Link"])
    for s in result["songs"]:
        w.writerow([s["title"], s["artist"], s["platform"] or "unbekannt", s["label"] or "",
                    ", ".join(seg["label"] for seg in s["segments"]), s["confidence"],
                    "ja" if s["in_description"] else "nein", s["album"] or "", s["isrc"] or "",
                    s["shazam_url"] or ""])
    return "﻿" + buf.getvalue()  # BOM, damit Excel Umlaute richtig anzeigt


def to_text(result: dict) -> str:
    v, st = result["video"], result["stats"]
    lines = [f"Video: {v.get('title')}  ({st['duration_fmt']})", ""]
    if not result["songs"]:
        lines.append("Keine Songs erkannt.")
    for i, s in enumerate(result["songs"], 1):
        times = ", ".join(seg["label"] for seg in s["segments"])
        lines.append(f"{i:2d}. {s['title']} – {s['artist']}")
        lines.append(f"    Plattform: {s['platform'] or 'unbekannt'}"
                     + (f"  (Label: {s['label']})" if s["label"] else ""))
        if s["segments"]:
            lines.append(f"    Zeit: {times}   Sicherheit: {s['confidence']} ({s['hits']} Treffer)")
        if s.get("youtube"):
            lines.append("    ✓ laut YouTube" + (f" (Lizenz: {s['license']})" if s.get("license") else ""))
        if s["direct_url"]:
            lines.append(f"    Link: {s['direct_url']}")
        elif s["shazam_url"]:
            lines.append(f"    Shazam: {s['shazam_url']}")
    music_credits = [c for c in result["credits"] if not c["promo"]]
    if music_credits:
        lines += ["", "Musik-Angaben aus der Videobeschreibung:"]
        for c in music_credits:
            lines.append(f"  • {c['text']}" + (f"  [{c['platform']}]" if c["platform"] else "")
                         + ("  " + " ".join(c["urls"]) if c["urls"] else ""))
    if st.get("audio_analyzed") is False:
        lines += ["", f"Quelle: YouTube-Musikangaben ({st.get('youtube_section', 0)} Songs), Ton nicht analysiert"]
    else:
        lines += ["", f"{st['windows']} Ausschnitte, {st['matched']} mit Treffer, "
                  f"{st['requests']} Anfragen, {st['errors']} Fehler, {st['elapsed']} s"]
    if st.get("warning"):
        lines.append("Hinweis: " + st["warning"])
    return "\n".join(lines)
