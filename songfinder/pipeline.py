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
from .platforms import credit_matches, parse_description, platform_from_label, search_links
from .recognize import Match, Recognizer, ShazamBlocked, ShazamRecognizer, build_recognizers

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


def analyze(source: str, opts: Options | None = None, progress: ProgressFn | None = None) -> dict:
    opts = opts or Options()
    step = MODES.get(opts.mode, MODES["normal"])
    want_ai = opts.use_ai == "an" or (opts.use_ai == "auto" and audio.demucs_available())
    if opts.use_ai == "an" and not audio.demucs_available():
        raise RuntimeError("KI-Trennung ist nicht installiert. Bitte 'pip install -r requirements-ki.txt' ausführen.")

    st = Stages(progress, {"download": 12, "prepare": 3, "ai": 35 if want_ai else 0, "recognize": 50})
    t0 = time.time()
    workdir = Path(tempfile.mkdtemp(prefix="songfinder_"))
    try:
        # 1) Quelle holen
        local = Path(source.strip().strip('"'))
        if local.exists() and local.is_file():
            meta = {"id": None, "title": local.stem, "uploader": "", "duration": None,
                    "description": "", "webpage_url": str(local), "thumbnail": None,
                    "yt_track": None, "yt_artist": None}
            src_path = local
        else:
            st.report("download", 0, "Video-Infos werden geladen …")
            from .download import download

            meta, src_path = download(source.strip(), workdir,
                                      lambda f, m: st.report("download", f, m),
                                      cookies_browser=opts.cookies_browser)
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
        st.report("recognize", 0, f"{len(windows)} Ausschnitte geplant, starte Erkennung …")

        order = (["music"] if music else []) + ["mix"] + (["side"] if opts.use_side else [])

        # 3) Erkennen
        hits, stats = asyncio.run(_recognize_all(src, windows, order, opts.keys,
                                                 lambda f, m: st.report("recognize", f, m)))

        # 4) Zusammenführen
        credits = parse_description(meta.get("description", ""))
        songs = merge_hits(hits, step, credits)

        stats.update({
            "duration": src.duration,
            "duration_fmt": fmt_time(src.duration),
            "step": step,
            "mode": opts.mode,
            "ai_used": bool(music),
            "variants": order,
            "elapsed": round(time.time() - t0, 1),
        })
        meta_out = {k: v for k, v in meta.items() if k != "description"}
        return {"video": meta_out, "songs": songs, "credits": credits, "stats": stats}
    finally:
        if not opts.keep_files:
            shutil.rmtree(workdir, ignore_errors=True)


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
