"""Gesamtablauf: Download → Aufbereitung → (KI-Trennung) → Erkennung → Zusammenführung."""
from __future__ import annotations

import asyncio
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import audio
from .platforms import (credit_matches, parse_description, platform_from_label, platform_rank,
                        search_links)
from .recognize import (Match, Recognizer, ShazamBlocked, ShazamRecognizer, audd_enterprise,
                        build_recognizers)

MODES = {  # Schrittweite zwischen zwei 12-s-Ausschnitten
    "schnell": 15.0,
    "normal": 10.0,
    "gruendlich": 5.0,
}


class RecognitionError(RuntimeError):
    """Die Erkennungsdienste (Shazam usw.) sind nicht erreichbar."""


@dataclass
class Options:
    mode: str = "normal"
    use_ai: str = "auto"  # "auto" | "an" | "aus"
    use_side: bool = True
    use_shazam: bool = False  # kostenlos, aber langsam und blockiert schnell → nur auf Wunsch
    keys: dict = field(default_factory=dict)
    keep_files: bool = False
    cookies_browser: str | None = None


@dataclass
class Hit:
    start: float
    end: float
    variant: str
    match: Match


ProgressFn = Callable[[int, str], None]


def fmt_time(seconds: float | None) -> str:
    if seconds is None:
        return "?"
    s = int(round(seconds))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


class Stages:
    """Übersetzt Fortschritt einzelner Schritte in einen Gesamtprozentwert."""

    def __init__(self, cb: ProgressFn | None, weights: dict[str, float]):
        self.cb = cb
        total = sum(weights.values())
        self.bounds: dict[str, tuple[float, float]] = {}
        acc = 0.0
        for name, w in weights.items():
            self.bounds[name] = (acc / total, (acc + w) / total)
            acc += w

    def report(self, stage: str, frac: float, msg: str) -> None:
        if not self.cb:
            return
        lo, hi = self.bounds[stage]
        self.cb(int(100 * (lo + (hi - lo) * max(0.0, min(1.0, frac)))), msg)


def analyze(source: str, opts: Options | None = None, progress: ProgressFn | None = None,
            on_partial: Callable[[dict], None] | None = None) -> dict:
    """Ablauf:
    1. YouTubes eigener Musik-Abschnitt (sofort, kostenlos)
    2. AudD-Scan der ganzen Datei, falls ein Token hinterlegt ist (schnell, mit Zeitstempeln)
    3. Shazam für die Stellen, die noch niemand erkannt hat
    """
    opts = opts or Options()
    step = MODES.get(opts.mode, MODES["normal"])
    audd_token = (opts.keys or {}).get("audd_token")
    need_audio = bool(audd_token or opts.use_shazam)
    want_ai = need_audio and (opts.use_ai == "an" or (opts.use_ai == "auto" and audio.demucs_available()))
    if need_audio and opts.use_ai == "an" and not audio.demucs_available():
        raise RuntimeError("KI-Trennung ist nicht installiert. Bitte 'pip install -r requirements-ki.txt' ausführen.")
    window_keys = {k: v for k, v in (opts.keys or {}).items() if k != "audd_token"}

    st = Stages(progress, {"download": 12, "prepare": 3, "ai": 35 if want_ai else 0,
                           "audd": 20 if audd_token else 0, "recognize": 50})
    t0 = time.time()
    workdir = Path(tempfile.mkdtemp(prefix="songfinder_"))

    def music_found(songs: list[dict], info: dict) -> None:
        if on_partial:
            on_partial({"video": {"id": info.get("id"), "title": info.get("title"),
                                  "uploader": info.get("uploader") or info.get("channel"),
                                  "thumbnail": info.get("thumbnail"),
                                  "duration_fmt": fmt_time(info.get("duration")),
                                  "webpage_url": info.get("webpage_url")},
                        "youtube_music": [enrich_youtube_song(x) for x in songs]})

    local = Path(source.strip().strip('"'))
    is_file = local.exists() and local.is_file()
    if not (audd_token or opts.use_shazam):
        if is_file:
            raise RuntimeError("Für Videodateien braucht es eine Audio-Erkennung: unter „Erweitert“ einen "
                               "AudD-Token eintragen oder Shazam einschalten.")
        return _youtube_only(source, opts, st, t0, music_found)

    try:
        # 1) Quelle holen (+ YouTube-Musikabschnitt)
        if is_file:
            meta = {"id": None, "title": local.stem, "uploader": "", "duration": None,
                    "description": "", "webpage_url": str(local), "thumbnail": None,
                    "yt_track": None, "yt_artist": None, "yt_music": []}
            src_path = local
        else:
            st.report("download", 0, "Video-Infos werden geladen …")
            from .download import download

            meta, src_path = download(source.strip(), workdir,
                                      lambda f, m: st.report("download", f, m),
                                      cookies_browser=opts.cookies_browser, on_music=music_found)
        st.report("download", 1, "Download fertig")

        # 2) Aufbereiten
        st.report("prepare", 0, "Audio wird aufbereitet …")
        stereo = audio.to_wav(src_path, workdir / "stereo16k.wav")
        st.report("prepare", 1, "Audio bereit")
        music = None
        if want_ai:
            st.report("ai", 0, "KI-Modell wird geladen (beim ersten Mal Download ~80 MB) …")
            music = audio.separate_music(src_path, workdir / "music16k.wav", workdir,
                                         lambda f, m: st.report("ai", f, m))
        src = audio.AudioSource(stereo, music)
        windows = audio.plan_windows(src.duration, step)

        # 3) AudD: ganze Datei in einem Rutsch
        hits: list[Hit] = []
        audd_info = None
        if audd_token:
            st.report("audd", 0.1, "Profi-Scan (AudD) läuft – die ganze Tonspur wird analysiert …")
            try:
                mp3 = audio.to_mp3(src_path, workdir / "upload.mp3")
                for start, end, m in asyncio.run(audd_enterprise(audd_token, mp3)):
                    hits.append(Hit(start, end, "mix", m))
                audd_info = f"{len(hits)} Treffer"
            except Exception as exc:
                audd_info = f"Fehler: {exc}"
                print(f"AudD-Scan fehlgeschlagen: {exc}")
            st.report("audd", 1, "Profi-Scan fertig")

        # 4) Shazam nur dort, wo noch nichts erkannt wurde
        covered = [(h.start, h.end) for h in hits]
        todo = [w for w in windows if not _is_covered(w, covered)] if opts.use_shazam else []
        order = (["music"] if music else []) + ["mix"] + (["side"] if opts.use_side else [])
        st.report("recognize", 0, f"{len(todo)} Stellen werden mit Shazam geprüft …"
                  if todo else "Alles erkannt")
        try:
            more, stats = asyncio.run(_recognize_all(src, todo, order, window_keys,
                                                     lambda f, m: st.report("recognize", f, m)))
        except RecognitionError as exc:
            if not (hits or meta.get("yt_music")):
                raise
            # YouTube/AudD haben schon geliefert → Ergebnis trotzdem zeigen
            more = []
            stats = {"silent": 0, "matched": 0, "requests": 0, "errors": 1, "providers": ["Shazam"],
                     "error_samples": [str(exc)[:300]], "rate_limited": 0,
                     "warning": "Shazam war nicht erreichbar – die Liste stammt nur aus "
                                + ("YouTube und AudD." if hits and meta.get("yt_music")
                                   else "dem AudD-Scan." if hits else "YouTubes Musik-Angaben (ohne Zeitstempel).")}
        hits = sorted(hits + more, key=lambda h: h.start)

        # 5) Zusammenführen
        credits = parse_description(meta.get("description", ""))
        songs = merge_hits(hits, max(step, 12.0), credits)
        songs = merge_youtube(songs, meta.get("yt_music") or [])

        stats.update({
            "windows": len(windows),
            "shazam_windows": len(todo),
            "duration": src.duration,
            "duration_fmt": fmt_time(src.duration),
            "step": step,
            "mode": opts.mode,
            "ai_used": bool(music),
            "variants": order,
            "audd": audd_info,
            "youtube_section": len(meta.get("yt_music") or []),
            "audio_analyzed": True,
            "elapsed": round(time.time() - t0, 1),
        })
        stats["providers"] = (["YouTube"] + (["AudD"] if audd_token else [])
                              + (["Shazam"] if opts.use_shazam else []))
        # Tonspur zum Anhören in der App behalten
        playback = None
        try:
            from .paths import data_dir

            if is_file and src_path.suffix.lower() in audio.PLAYABLE:
                playback = src_path
            else:
                playback = audio.keep_for_playback(src_path, data_dir() / "audio")
        except Exception as exc:
            print(f"Tonspur zum Anhören nicht gespeichert: {exc}")
        meta_out = {k: v for k, v in meta.items() if k != "description"}
        return {"video": meta_out, "songs": songs, "credits": credits, "stats": stats,
                "_playback": str(playback) if playback else None}
    finally:
        if not opts.keep_files:
            shutil.rmtree(workdir, ignore_errors=True)


def _youtube_only(source: str, opts: Options, st: "Stages", t0: float, music_found) -> dict:
    """Schnellmodus: nur YouTubes Musik-Angaben + Videobeschreibung, kein Download."""
    from .download import video_info

    st.report("download", 0.3, "YouTube-Musikangaben werden gelesen …")
    meta = video_info(source.strip(), cookies_browser=opts.cookies_browser, on_music=music_found)
    credits = parse_description(meta.get("description", ""))
    songs = merge_youtube([], meta.get("yt_music") or [])
    for s in songs:  # Beschreibung bestätigt? Liefert evtl. Plattform + Direktlink
        if c := credit_matches(s["title"], s["artist"], credits):
            s["in_description"] = True
            if not s["platform"] and c.get("platform"):
                s["platform"], s["platform_source"] = c["platform"], "Videobeschreibung"
            if s["platform"]:
                s["direct_url"] = next((u for u in c["urls"]
                                        if s["platform"].split()[0].lower() in u.lower()), None)
    songs = sort_songs(songs)
    stats = {
        "windows": 0, "shazam_windows": 0, "silent": 0, "matched": 0, "requests": 0, "errors": 0,
        "error_samples": [], "warning": None, "rate_limited": 0,
        "duration": meta.get("duration") or 0, "duration_fmt": fmt_time(meta.get("duration")),
        "step": 0, "mode": opts.mode, "ai_used": False, "variants": [], "audd": None,
        "youtube_section": len(meta.get("yt_music") or []), "providers": ["YouTube"],
        "audio_analyzed": False, "elapsed": round(time.time() - t0, 1),
    }
    meta_out = {k: v for k, v in meta.items() if k != "description"}
    return {"video": meta_out, "songs": songs, "credits": credits, "stats": stats}


def _is_covered(w: audio.Window, covered: list[tuple[float, float]]) -> bool:
    """Ein Ausschnitt gilt als erledigt, wenn ein Treffer ihn zu mindestens 60 % abdeckt."""
    need = (w.end - w.start) * 0.6
    for s, e in covered:
        if min(e, w.end) - max(s, w.start) >= need:
            return True
    return False


def enrich_youtube_song(y: dict) -> dict:
    platform = platform_from_label(y.get("license"), y.get("album"), y.get("artist"))
    return {**y, "platform": platform.name if platform else None,
            "links": search_links(y["title"], y.get("artist", ""))}


def merge_youtube(songs: list[dict], yt_music: list[dict]) -> list[dict]:
    """Ergänzt die Audio-Treffer um YouTubes eigene Angaben (bestätigt oder zusätzlich)."""
    from .recognize import normalize

    for y in yt_music:
        y = enrich_youtube_song(y)
        nt = normalize(y["title"])
        match = next((s for s in songs if normalize(s["title"]) == nt or
                      (len(nt) > 4 and (nt in normalize(s["title"]) or normalize(s["title"]) in nt))), None)
        if match:
            match["youtube"] = True
            match["confidence"] = "hoch"
            match["license"] = y.get("license")
            if not match.get("platform") and y["platform"]:
                match["platform"], match["platform_source"] = y["platform"], "YouTube"
        else:
            songs.append({
                "title": y["title"], "artist": y.get("artist") or "", "album": y.get("album"),
                "label": None, "released": None, "isrc": None,
                "platform": y["platform"], "platform_source": "YouTube" if y["platform"] else None,
                "direct_url": None, "confidence": "hoch", "hits": 0, "first_seen": 10**9,
                "total_seconds": 0, "segments": [], "song_offset": None, "variants": [],
                "providers": ["YouTube"], "shazam_url": None, "apple_url": None, "cover": None,
                "in_description": False, "links": y["links"], "youtube": True,
                "license": y.get("license"),
            })
    for s in songs:
        s.setdefault("youtube", False)
        s.setdefault("license", None)
    return sort_songs(songs)


def sort_songs(songs: list[dict]) -> list[dict]:
    """Epidemic Sound zuerst, dann Lickd, andere Libraries, Unbekannt; darin sichere vor unsicheren."""
    return sorted(songs, key=lambda s: (platform_rank(s.get("platform")),
                                        s.get("confidence") == "niedrig", s.get("first_seen", 0)))


async def _recognize_all(src: audio.AudioSource, windows: list[audio.Window], order: list[str],
                         keys: dict, report: Callable[[float, str], None]) -> tuple[list[Hit], dict]:
    done = 0
    hits: list[Hit] = []

    def songs_found() -> int:
        return len({h.match.key for h in hits})

    def on_wait(seconds: int) -> None:
        report(done / len(windows), f"Shazam bremst gerade – kurze Pause ({seconds} s), dann geht es weiter …")

    recognizers = build_recognizers(keys, on_wait=on_wait)
    shazam = next(r for r in recognizers if isinstance(r, ShazamRecognizer))
    paid = [r for r in recognizers if not isinstance(r, ShazamRecognizer)]
    stats = {"windows": len(windows), "silent": 0, "matched": 0, "requests": 0, "errors": 0,
             "providers": [r.name for r in recognizers], "error_samples": [], "warning": None}
    consecutive_errors = 0
    wins = {v: 0 for v in order}  # welche Variante funktioniert bei diesem Video?

    async def ask(rec: Recognizer, sig) -> Match | None:
        nonlocal consecutive_errors
        stats["requests"] += 1
        try:
            m = await rec.recognize(audio.wav_bytes(sig))
            consecutive_errors = 0
            return m
        except ShazamBlocked:
            raise
        except Exception as exc:  # Netzwerkfehler etc. – weitermachen
            stats["errors"] += 1
            consecutive_errors += 1
            if len(stats["error_samples"]) < 5:
                stats["error_samples"].append(f"{rec.name}: {type(exc).__name__}: {exc}"[:300])
            if consecutive_errors >= 8:
                raise RecognitionError(
                    "Zu viele Fehler in Folge bei der Erkennung – besteht eine Internetverbindung? "
                    f"Letzter Fehler: {stats['error_samples'][-1]}") from exc
            return None

    try:
        for w in windows:
            variants = await asyncio.to_thread(src.variants, w, order)
            if not variants:
                stats["silent"] += 1
            else:
                # Erfolgreichste Variante zuerst → weniger Anfragen
                ranked = sorted(variants.items(), key=lambda kv: -wins[kv[0]])
                found: tuple[str, Match] | None = None
                for vname, sig in ranked:
                    if m := await ask(shazam, sig):
                        found = (vname, m)
                        break
                if not found and paid:  # kostenpflichtige Dienste nur auf der besten Variante
                    vname, sig = ranked[0]
                    for rec in paid:
                        if m := await ask(rec, sig):
                            found = (vname, m)
                            break
                if found:
                    wins[found[0]] += 1
                    stats["matched"] += 1
                    hits.append(Hit(w.start, w.end, found[0], found[1]))
            done += 1
            report(done / len(windows),
                   f"Erkenne Musik … {done}/{len(windows)} Ausschnitte · {songs_found()} Songs gefunden")
    except ShazamBlocked:
        if not hits:
            raise RecognitionError(
                "Shazam nimmt von deinem Internetanschluss gerade keine Anfragen an (zu viele Anfragen). "
                "Letzter Fehler: Bitte 15–30 Minuten warten und dann mit „Schnell“ erneut versuchen.")
        stats["warning"] = (
            f"Shazam hat nach {fmt_time(windows[done].start)} gebremst – die Analyse wurde dort beendet. "
            "Die Liste ist deshalb evtl. unvollständig. In 15–30 Minuten erneut versuchen.")
    finally:
        for r in recognizers:
            await r.close()
    stats["rate_limited"] = shazam.rate_limited
    stats["analyzed_until"] = windows[min(done, len(windows) - 1)].end if windows else 0
    hits.sort(key=lambda h: h.start)
    return hits, stats


def merge_hits(hits: list[Hit], step: float, credits: list[dict]) -> list[dict]:
    groups: dict[str, list[Hit]] = {}
    for h in hits:
        groups.setdefault(h.match.key, []).append(h)

    seg_by_key: dict[str, list[list[float]]] = {}
    for key, hs in groups.items():
        hs.sort(key=lambda h: h.start)
        segments: list[list[float]] = []
        for h in hs:
            if segments and h.start - segments[-1][1] <= step + 2:
                segments[-1][1] = max(segments[-1][1], h.end)
            else:
                segments.append([h.start, h.end])
        seg_by_key[key] = segments
    _split_overlaps(seg_by_key)

    songs = []
    for key, hs in groups.items():
        segments = seg_by_key[key]
        # Bestes Metadaten-Exemplar: das mit Label, sonst das erste
        best = next((h.match for h in hs if h.match.label), hs[0].match)

        platform = platform_from_label(best.label, best.album, best.artist)
        credit = credit_matches(best.title, best.artist, credits)
        if not platform and credit and credit.get("platform"):
            platform_name = credit["platform"]
        else:
            platform_name = platform.name if platform else None

        n = len(hs)
        min_skew = min((h.match.skew for h in hs if h.match.skew is not None), default=None)
        if n >= 3 or credit:
            confidence = "hoch"
        elif n == 2 or (min_skew is not None and min_skew <= 0.002):
            confidence = "mittel"
        else:
            confidence = "niedrig"

        direct_url = None
        if credit:
            direct_url = next((u for u in credit["urls"]
                               if platform_name and platform_name.split()[0].lower() in u.lower()), None)

        songs.append({
            "title": best.title,
            "artist": best.artist,
            "album": best.album,
            "label": best.label,
            "released": best.released,
            "isrc": best.isrc,
            "platform": platform_name,
            "platform_source": ("Label" if platform else "Videobeschreibung") if platform_name else None,
            "direct_url": direct_url,
            "confidence": confidence,
            "hits": n,
            "first_seen": segments[0][0],
            "total_seconds": round(sum(e - s for s, e in segments)),
            "segments": [{"start": s, "end": e, "label": f"{fmt_time(s)}–{fmt_time(e)}"} for s, e in segments],
            "song_offset": hs[0].match.song_offset,
            "variants": sorted({h.variant for h in hs}),
            "providers": sorted({h.match.provider for h in hs}),
            "shazam_url": best.url,
            "apple_url": best.apple_url,
            "cover": best.cover,
            "in_description": bool(credit),
            "links": search_links(best.title, best.artist),
        })
    songs.sort(key=lambda s: s["first_seen"])
    return songs


def _split_overlaps(seg_by_key: dict[str, list[list[float]]]) -> None:
    """Ausschnitte sind 12 s lang, daher überlappen sich Songs an Übergängen.

    Läuft Song A bis 1:06 und Song B ab 1:03, liegt der Wechsel in der Mitte.
    Überlappt ein Song einen anderen komplett (z.B. kurzer Einspieler), bleibt alles stehen.
    """
    flat = sorted(((seg, key) for key, segs in seg_by_key.items() for seg in segs),
                  key=lambda x: x[0][0])
    for (a, ka), (b, kb) in zip(flat, flat[1:]):
        if ka != kb and a[0] < b[0] < a[1] < b[1]:
            mid = (b[0] + a[1]) / 2
            a[1] = b[0] = mid
