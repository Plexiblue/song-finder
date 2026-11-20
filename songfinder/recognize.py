"""Erkennungsdienste: Shazam (kostenlos) sowie optional AudD und ACRCloud (API-Schlüssel)."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import re
import time
from dataclasses import asdict, dataclass, field

import aiohttp


@dataclass
class Match:
    title: str
    artist: str
    provider: str
    album: str | None = None
    label: str | None = None
    released: str | None = None
    isrc: str | None = None
    url: str | None = None
    apple_url: str | None = None
    cover: str | None = None
    skew: float | None = None
    song_offset: float | None = None  # Position im Originalsong (Sekunden)
    extra: dict = field(default_factory=dict)

    @property
    def key(self) -> str:
        return normalize(self.title) + "|" + normalize(self.artist)

    def to_dict(self) -> dict:
        return asdict(self)


def normalize(text: str | None) -> str:
    text = (text or "").lower()
    text = re.sub(r"\((feat|ft|with)[^)]*\)|\[[^\]]*\]", "", text)
    text = re.sub(r"[^\w]+", " ", text)
    return " ".join(text.split())


class RateLimiter:
    def __init__(self, min_interval: float):
        self.min_interval = min_interval
        self._lock = asyncio.Lock()
        self._last = 0.0

    async def wait(self) -> None:
        async with self._lock:
            delay = self._last + self.min_interval - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            self._last = time.monotonic()


class Recognizer:
    name = "base"

    async def recognize(self, wav: bytes) -> Match | None:  # pragma: no cover
        raise NotImplementedError

    async def close(self) -> None:
        pass


# --------------------------------------------------------------------------- #
# Shazam (inoffizielle Schnittstelle über shazamio)
# --------------------------------------------------------------------------- #

class ShazamBlocked(RuntimeError):
    """Shazam lehnt trotz langer Pausen weiterhin ab (Rate-Limit für diese IP-Adresse)."""


class ShazamRecognizer(Recognizer):
    """Fragt Shazam strikt nacheinander und passt das Tempo an.

    Antwortet Shazam mit „zu viele Anfragen“ (HTTP 429), wird eine Pause eingelegt, die bei jeder
    weiteren Ablehnung länger wird, und das Grundtempo dauerhaft gedrosselt.
    """

    name = "Shazam"
    PAUSES = (15, 30, 60, 90, 120)  # Sekunden; danach gilt Shazam als blockiert

    def __init__(self, min_interval: float = 1.0, on_wait=None):
        from aiohttp_retry import ExponentialRetry
        from shazamio import Shazam
        from shazamio.client import HTTPClient

        # 429 behandeln wir selbst (mit echten Pausen); Serverfehler kurz wiederholen
        self._client = HTTPClient(
            retry_options=ExponentialRetry(attempts=3, start_timeout=1, max_timeout=8,
                                           statuses={500, 502, 503, 504})
        )
        self._shazam = Shazam(language="de-DE", endpoint_country="DE", http_client=self._client)
        self.base_interval = min_interval
        self._limiter = RateLimiter(min_interval)
        self._lock = asyncio.Lock()
        self.on_wait = on_wait
        self.rate_limited = 0

    async def recognize(self, wav: bytes) -> Match | None:
        from shazamio.exceptions import RateLimited

        async with self._lock:  # nie zwei Anfragen gleichzeitig
            for pause in (*self.PAUSES, None):
                await self._limiter.wait()
                try:
                    answer = await self._shazam.recognize(wav)
                except RateLimited:
                    self.rate_limited += 1
                    if pause is None:
                        raise ShazamBlocked(
                            "Shazam nimmt von deinem Internetanschluss gerade keine Anfragen mehr an.")
                    # dauerhaft langsamer werden
                    self._limiter.min_interval = min(8.0, self._limiter.min_interval * 1.7 + 0.5)
                    if self.on_wait:
                        self.on_wait(pause)
                    await asyncio.sleep(pause)
                    continue
                # Nach Erfolg ganz langsam wieder schneller werden
                self._limiter.min_interval = max(self.base_interval, self._limiter.min_interval * 0.97)
                return parse_shazam(answer)
        return None

    async def close(self) -> None:
        try:
            await self._client.close()
        except Exception:
            pass


def parse_shazam(answer: dict) -> Match | None:
    track = (answer or {}).get("track")
    matches = (answer or {}).get("matches") or []
    if not track or not matches:
        return None
    meta: dict[str, str] = {}
    for section in track.get("sections", []):
        if section.get("type") == "SONG":
            for item in section.get("metadata", []):
                if item.get("title") and item.get("text"):
                    meta[item["title"].lower()] = item["text"]
    apple = None
    for opt in (track.get("hub") or {}).get("options", []) or []:
        for act in opt.get("actions", []) or []:
            if (act.get("uri") or "").startswith("https://music.apple.com"):
                apple = act["uri"]
    first = matches[0]
    skew = max(abs(first.get("timeskew", 0) or 0), abs(first.get("frequencyskew", 0) or 0))
    return Match(
        title=track.get("title") or "?",
        artist=track.get("subtitle") or "?",
        provider="Shazam",
        album=meta.get("album"),
        label=meta.get("label") or meta.get("plattenfirma") or meta.get("label/vertrieb"),
        released=meta.get("released") or meta.get("veröffentlicht"),
        isrc=track.get("isrc"),
        url=track.get("url"),
        apple_url=apple,
        cover=(track.get("images") or {}).get("coverart"),
        skew=skew,
        song_offset=first.get("offset"),
        extra={"genre": (track.get("genres") or {}).get("primary")},
    )


# --------------------------------------------------------------------------- #
# AudD (kostenpflichtig, https://audd.io)
# --------------------------------------------------------------------------- #

# Bedeutung der AudD-Fehlercodes laut offiziellem SDK (pypi: audd, errors.py)
AUDD_CODE_HINTS = {
    900: "Token ungültig", 901: "Token fehlt oder ungültig", 903: "Token ungültig",
    902: "Limit erreicht (Kontingent oder Limit pro Datei)",
    904: "Diese Funktion ist für deinen Token nicht freigeschaltet",
    905: "Diese Funktion ist für deinen Token nicht freigeschaltet",
    300: "Audiodatei nicht lesbar", 400: "Audiodatei nicht lesbar", 500: "Audiodatei nicht lesbar",
    19: "Zugriff gesperrt", 31337: "Zugriff gesperrt",
}


class AudDError(RuntimeError):
    """Fehler von AudD – mit Code und AudDs eigener Meldung (nicht übersetzt/verfälscht)."""

    def __init__(self, code: int | None, message: str):
        self.code = code
        self.server_message = message
        hint = AUDD_CODE_HINTS.get(code or 0)
        text = f"AudD meldet: {message}" + (f" (Code {code}: {hint})" if hint else f" (Code {code})" if code else "")
        super().__init__(text)

    @property
    def is_auth(self) -> bool:
        return self.code in (900, 901, 903)


def audd_raise_for_error(body) -> None:
    if not isinstance(body, dict):
        raise AudDError(None, "unerwartete Antwort")
    if body.get("status") != "error":
        return
    err = body.get("error")
    if isinstance(err, dict):
        code = err.get("error_code")
        try:
            code = int(code)
        except (TypeError, ValueError):
            code = None
        if code == 51 and body.get("result") is not None:  # nur Hinweis auf veralteten Parameter
            return
        raise AudDError(code, str(err.get("error_message") or "unbekannter Fehler"))
    raise AudDError(None, str(err or "unbekannter Fehler"))


AUDD_API_URL = "https://api.audd.io/"


class AuddRecognizer(Recognizer):
    name = "AudD"

    def __init__(self, token: str, min_interval: float = 0.3):
        self.token = token
        self._session: aiohttp.ClientSession | None = None
        self._limiter = RateLimiter(min_interval)

    async def recognize(self, wav: bytes) -> Match | None:
        await self._limiter.wait()
        self._session = self._session or aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60))
        form = aiohttp.FormData()
        form.add_field("api_token", self.token)
        form.add_field("return", "apple_music,spotify")
        form.add_field("file", wav, filename="clip.wav", content_type="audio/wav")
        async with self._session.post(AUDD_API_URL, data=form) as resp:
            data = await resp.json(content_type=None)
        audd_raise_for_error(data)
        r = data.get("result")
        if not r:
            return None
        return Match(
            title=r.get("title") or "?", artist=r.get("artist") or "?", provider="AudD",
            album=r.get("album"), label=r.get("label"), released=r.get("release_date"),
            url=r.get("song_link"),
            apple_url=(r.get("apple_music") or {}).get("url"),
            isrc=(r.get("apple_music") or {}).get("isrc") or
                 ((r.get("spotify") or {}).get("external_ids") or {}).get("isrc"),
        )

    async def close(self) -> None:
        if self._session:
            await self._session.close()


# --------------------------------------------------------------------------- #
# ACRCloud (kostenpflichtig, großer Katalog inkl. vieler Production-Music-Libraries)
# --------------------------------------------------------------------------- #

class AcrCloudRecognizer(Recognizer):
    name = "ACRCloud"

    def __init__(self, host: str, access_key: str, access_secret: str, min_interval: float = 0.3):
        self.host = host.replace("https://", "").replace("http://", "").strip("/")
        self.access_key = access_key
        self.access_secret = access_secret
        self._session: aiohttp.ClientSession | None = None
        self._limiter = RateLimiter(min_interval)

    async def recognize(self, wav: bytes) -> Match | None:
        await self._limiter.wait()
        self._session = self._session or aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60))
        ts = str(int(time.time()))
        to_sign = "\n".join(["POST", "/v1/identify", self.access_key, "audio", "1", ts])
        sig = base64.b64encode(
            hmac.new(self.access_secret.encode(), to_sign.encode(), hashlib.sha1).digest()
        ).decode()
        form = aiohttp.FormData()
        form.add_field("sample", wav, filename="clip.wav", content_type="audio/wav")
        for k, v in {"access_key": self.access_key, "data_type": "audio",
                     "signature_version": "1", "signature": sig,
                     "sample_bytes": str(len(wav)), "timestamp": ts}.items():
            form.add_field(k, v)
        async with self._session.post(f"https://{self.host}/v1/identify", data=form) as resp:
            data = await resp.json(content_type=None)
        code = (data.get("status") or {}).get("code")
        if code == 1001:  # kein Treffer
            return None
        if code != 0:
            raise RuntimeError(f"ACRCloud: {(data.get('status') or {}).get('msg')}")
        music = ((data.get("metadata") or {}).get("music") or [None])[0]
        if not music:
            return None
        return Match(
            title=music.get("title") or "?",
            artist=", ".join(a.get("name", "") for a in music.get("artists", [])) or "?",
            provider="ACRCloud",
            album=(music.get("album") or {}).get("name"),
            label=music.get("label"),
            released=music.get("release_date"),
            isrc=(music.get("external_ids") or {}).get("isrc"),
            song_offset=(music.get("play_offset_ms") or 0) / 1000,
            extra={"score": music.get("score")},
        )

    async def close(self) -> None:
        if self._session:
            await self._session.close()


def build_recognizers(keys: dict | None, on_wait=None) -> list[Recognizer]:
    """Shazam ist immer dabei; AudD/ACRCloud nur, wenn Schlüssel vorhanden sind."""
    keys = keys or {}
    recs: list[Recognizer] = [ShazamRecognizer(on_wait=on_wait)]
    if keys.get("audd_token"):
        recs.append(AuddRecognizer(keys["audd_token"]))
    if keys.get("acr_host") and keys.get("acr_key") and keys.get("acr_secret"):
        recs.append(AcrCloudRecognizer(keys["acr_host"], keys["acr_key"], keys["acr_secret"]))
    return recs


# --------------------------------------------------------------------------- #
# AudD „Enterprise“: ganze Datei auf einmal (schnell, ohne Rate-Limit-Probleme)
# Schnittstelle wie im offiziellen SDK (pypi: audd): POST https://enterprise.audd.io/
#   Felder: api_token, file, accurate_offsets, (skip, every, limit, skip_first_seconds)
#   Antwort: {"status": "success", "result": [{"offset": "MM:SS",
#             "songs": [{"title", "artist", "album", "label", "isrc", "score",
#                        "song_link", "release_date", "start_offset", "end_offset"}]}]}
#   Abrechnung: 1 Anfrage je 12 s Audio.
# --------------------------------------------------------------------------- #

AUDD_ENTERPRISE_URL = "https://enterprise.audd.io/"



def _offset_seconds(offset) -> float | None:
    if offset is None:
        return None
    try:
        total = 0.0
        for part in str(offset).strip().split(":"):
            total = total * 60 + float(part)
        return total
    except ValueError:
        return None


async def audd_enterprise(token: str, audio_path, progress=None) -> list[tuple[float, float, Match]]:
    """Schickt die komplette Tonspur an AudD und gibt (start, ende, Match) für alle Treffer zurück."""
    form = aiohttp.FormData()
    form.add_field("api_token", token)
    form.add_field("accurate_offsets", "true")
    with open(audio_path, "rb") as fh:
        form.add_field("file", fh.read(), filename="audio.mp3", content_type="audio/mpeg")
    timeout = aiohttp.ClientTimeout(total=3600, connect=30)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(AUDD_ENTERPRISE_URL, data=form) as resp:
            body = await resp.json(content_type=None)
    audd_raise_for_error(body)
    out: list[tuple[float, float, Match]] = []
    for chunk in body.get("result") or []:
        if not isinstance(chunk, dict):
            continue
        base = _offset_seconds(chunk.get("offset"))
        if base is None:
            continue
        for s in chunk.get("songs") or []:
            if not isinstance(s, dict) or not s.get("title"):
                continue
            start = base + (s.get("start_offset") or 0) / 1000
            end = base + (s.get("end_offset") or 12000) / 1000
            if end <= start:
                end = start + 12
            out.append((start, end, Match(
                title=s.get("title") or "?", artist=s.get("artist") or "?", provider="AudD",
                album=s.get("album"), label=s.get("label"), released=s.get("release_date"),
                isrc=s.get("isrc"), url=s.get("song_link"), extra={"score": s.get("score")},
            )))
    return out


async def audd_check(token: str) -> dict:
    """Testet den Token an beiden AudD-Schnittstellen mit einem 12-s-Testton (kostet 2 Anfragen)."""
    import io

    import numpy as np
    import soundfile as sf

    t = np.arange(16000 * 12) / 16000
    buf = io.BytesIO()
    sf.write(buf, (np.sin(2 * np.pi * 440 * t) * 0.3).astype(np.float32), 16000, format="WAV")
    wav = buf.getvalue()
    out = {}
    timeout = aiohttp.ClientTimeout(total=60)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        for key, url in (("standard", AUDD_API_URL), ("enterprise", AUDD_ENTERPRISE_URL)):
            form = aiohttp.FormData()
            form.add_field("api_token", token)
            form.add_field("file", wav, filename="test.wav", content_type="audio/wav")
            try:
                async with session.post(url, data=form) as resp:
                    body = await resp.json(content_type=None)
                audd_raise_for_error(body)
                out[key] = {"ok": True}
            except AudDError as exc:
                out[key] = {"ok": False, "code": exc.code, "message": str(exc)}
            except Exception as exc:
                out[key] = {"ok": False, "code": None, "message": f"Keine Verbindung zu AudD: {exc}"}
    return out
